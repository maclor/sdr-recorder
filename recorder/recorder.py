import asyncio
import json
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sdr-recorder")

SAMPLE_RATE = int(os.environ.get("OUTPUT_RATE", "12000"))
SQUELCH_MARGIN = float(os.environ.get("SQUELCH_MARGIN", "10"))
RMS_MARGIN = float(os.environ.get("RMS_MARGIN", "20"))
PRE_ROLL_SECONDS = float(os.environ.get("PRE_ROLL_SECONDS", "2"))
END_SILENCE_SECONDS = float(os.environ.get("END_SILENCE_SECONDS", "3"))
MIN_DURATION_SECONDS = float(os.environ.get("MIN_DURATION_SECONDS", "1"))
MAX_RECORDING_SECONDS = float(os.environ.get("MAX_RECORDING_SECONDS", "600"))
NOISE_WINDOW_SECONDS = float(os.environ.get("NOISE_WINDOW_SECONDS", "600"))
MP3_BITRATE = int(os.environ.get("MP3_BITRATE", "32"))
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings"))
OPENWEBRX_WS_URL = os.environ.get("OPENWEBRX_WS_URL", "ws://192.168.68.67:8073/ws/")


def env_bool(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


# Recordings are first kept under a non-MP3 suffix.  This means that the web
# service and the transcriber cannot see a file while it is being denoised.
DENOISE_ENABLED = env_bool("DENOISE_ENABLED")
DENOISE_BACKEND = os.environ.get("DENOISE_BACKEND", "deepfilternet").strip().lower()
DENOISE_NR = float(os.environ.get("DENOISE_NR", "12"))
DENOISE_NOISE_FLOOR_DB = float(os.environ.get("DENOISE_NOISE_FLOOR_DB", "-45"))
DENOISE_SILENCE_DB = float(os.environ.get("DENOISE_SILENCE_DB", "-42"))
DENOISE_MIN_SIGNAL_SECONDS = float(os.environ.get("DENOISE_MIN_SIGNAL_SECONDS", "0.5"))
DEEPFILTER_BIN = os.environ.get("DEEPFILTER_BIN", "deep-filter")
DEEPFILTER_POST_FILTER = env_bool("DEEPFILTER_POST_FILTER")
DEEPFILTER_COMPENSATE_DELAY = env_bool("DEEPFILTER_COMPENSATE_DELAY")

INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66,
    73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449,
    494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272,
    2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]
SYNC = b"SYNC"

DBFS_FULLSCALE = 20.0 * math.log10(32768.0)


def parse_frequencies(raw):
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        freq = int(parts[0])
        mod = parts[1] if len(parts) > 1 else "nfm"
        out.append((freq, mod))
    return out


class AdpcmDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.index = 0
        self.predictor = 0

    def _decode(self, nibble):
        step = STEP_TABLE[self.index]
        diff = step >> 3
        if nibble & 1:
            diff += step >> 2
        if nibble & 2:
            diff += step >> 1
        if nibble & 4:
            diff += step
        if nibble & 8:
            diff = -diff
        self.predictor += diff
        if self.predictor > 32767:
            self.predictor = 32767
        elif self.predictor < -32768:
            self.predictor = -32768
        self.index += INDEX_TABLE[nibble]
        if self.index < 0:
            self.index = 0
        elif self.index > 88:
            self.index = 88
        return self.predictor

    def feed(self, data):
        self.buf += data
        out = bytearray()
        sum_sq = 0
        count = 0
        i = 0
        n = len(self.buf)
        while i < n:
            if i + 4 <= n and self.buf[i:i + 4] == SYNC:
                if i + 8 > n:
                    break
                self.index = int.from_bytes(self.buf[i + 4:i + 6], "little", signed=True)
                self.predictor = int.from_bytes(self.buf[i + 6:i + 8], "little", signed=True)
                self.index = max(0, min(88, self.index))
                i += 8
                continue
            if i + 1 > n:
                break
            b = self.buf[i]
            s0 = self._decode(b & 0x0F)
            s1 = self._decode((b >> 4) & 0x0F)
            out.append(s0 & 0xFF)
            out.append((s0 >> 8) & 0xFF)
            out.append(s1 & 0xFF)
            out.append((s1 >> 8) & 0xFF)
            sum_sq += s0 * s0 + s1 * s1
            count += 2
            i += 1
        del self.buf[:i]
        return out, sum_sq, count


class Recorder:
    def __init__(self, url, frequency, mod):
        self.url = url
        self.frequency = frequency
        self.mod = mod
        self._running = True
        self._reconnect_delay = 1

        self.ws = None
        self.decoder = AdpcmDecoder()
        self.audio_compression = "adpcm"

        self.profiles = []
        self.center_freq = None
        self.samp_rate = None

        self._profiles_event = asyncio.Event()
        self._config_event = asyncio.Event()

        self.smeter_dbm = -200.0
        self.rms_dbm = -200.0
        self.noise_smeter_history = []
        self.noise_rms_history = []
        self.noise_floor_smeter = None
        self.noise_floor_rms = None
        self.last_active = 0.0
        self.last_message_time = 0.0

        self.pre_roll_max = int(PRE_ROLL_SECONDS * SAMPLE_RATE) * 2
        self.pre_roll = bytearray()

        self.recording = False
        self.lame = None
        self.rec_file = None
        self.rec_final_file = None
        self.rec_start = None
        self.postprocess_tasks = set()

    async def run(self):
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session ended with error")
            self._finalize_recording()
            delay = self._reconnect_delay
            logger.info("reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _session(self):
        self._reset()
        logger.info("connecting to %s", self.url)
        async with websockets.connect(
            self.url, ping_interval=None, ping_timeout=None, open_timeout=15, close_timeout=5
        ) as ws:
            self.ws = ws
            self._reconnect_delay = 1
            await ws.send("SERVER DE CLIENT client=sdr-recorder type=receiver")
            await ws.send(json.dumps({"type": "connectionproperties", "params": {"output_rate": SAMPLE_RATE}}))
            logger.info("handshake sent, waiting for server")

            ticker = asyncio.create_task(self._ticker())
            controller = asyncio.create_task(self._controller())
            try:
                async for message in ws:
                    if isinstance(message, str):
                        self._on_text(message)
                    elif isinstance(message, bytes):
                        self._on_binary(message)
            finally:
                ticker.cancel()
                controller.cancel()

    def _reset(self):
        self.ws = None
        self.decoder = AdpcmDecoder()
        self.audio_compression = "adpcm"
        self.profiles = []
        self.center_freq = None
        self.samp_rate = None
        self._profiles_event.clear()
        self._config_event.clear()
        self.smeter_dbm = -200.0
        self.rms_dbm = -200.0
        self.noise_smeter_history = []
        self.noise_rms_history = []
        self.noise_floor_smeter = None
        self.noise_floor_rms = None
        self.last_active = 0.0
        self.last_message_time = time.monotonic()
        self.pre_roll = bytearray()

    async def _controller(self):
        try:
            await self._profiles_event.wait()
            await self._ensure_covering_profile()
            await self._start_dsp()
            logger.info("receiver ready: freq=%d mod=%s", self.frequency, self.mod)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("setup failed")

    async def _ensure_covering_profile(self):
        if self._covers_target():
            return
        logger.info("current profile does not cover target frequency, scanning profiles")
        for profile in list(self.profiles):
            pid = profile.get("id")
            if not pid:
                continue
            logger.info("trying profile %s", pid)
            self._config_event.clear()
            await self.ws.send(json.dumps({"type": "selectprofile", "params": {"profile": pid}}))
            try:
                await asyncio.wait_for(self._config_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("no config update after selecting profile %s", pid)
                continue
            if self._covers_target():
                logger.info("profile %s covers target frequency", pid)
                return
        logger.warning("no profile found that covers target frequency %d", self.frequency)

    def _covers_target(self):
        if self.center_freq is None or self.samp_rate is None:
            return False
        half = self.samp_rate / 2
        margin = 10000
        return (self.center_freq - half + margin) <= self.frequency <= (self.center_freq + half - margin)

    async def _start_dsp(self):
        if self.center_freq is None:
            logger.warning("center frequency unknown, using offset 0")
            offset = 0
        else:
            offset = self.frequency - self.center_freq
        params = {
            "mod": self.mod,
            "offset_freq": int(offset),
            "squelch_level": -150,
            "low_cut": -5000,
            "high_cut": 5000,
        }
        logger.info("starting dsp: %s", params)
        await self.ws.send(json.dumps({"type": "dspcontrol", "params": params}))
        await self.ws.send(json.dumps({"type": "dspcontrol", "action": "start"}))

    def _on_text(self, text):
        self.last_message_time = time.monotonic()
        if text.startswith("CLIENT DE SERVER"):
            logger.info("server acknowledged: %s", text)
            return
        try:
            msg = json.loads(text)
        except ValueError:
            return
        t = msg.get("type")
        if t == "config":
            self._on_config(msg.get("value", {}))
        elif t == "profiles":
            self.profiles = msg.get("value", [])
            logger.info("received %d profiles", len(self.profiles))
            self._profiles_event.set()
        elif t == "smeter":
            self._on_smeter(msg.get("value", 0))
        elif t in ("sdr_error", "demodulator_error", "backoff"):
            logger.warning("%s: %s", t, msg.get("value", msg.get("reason", "")))

    def _on_config(self, value):
        if "center_freq" in value:
            self.center_freq = value["center_freq"]
        if "samp_rate" in value:
            self.samp_rate = value["samp_rate"]
        if "audio_compression" in value:
            self.audio_compression = value["audio_compression"]
            logger.info("audio compression: %s", self.audio_compression)
        self._config_event.set()

    def _on_smeter(self, value):
        v = max(float(value), 1e-12)
        self.smeter_dbm = 10.0 * math.log10(v)

    def _on_binary(self, data):
        self.last_message_time = time.monotonic()
        if not data:
            return
        tag = data[0]
        if tag != 2:
            return
        payload = data[1:]
        if self.audio_compression == "none":
            n = len(payload) // 2 * 2
            if n == 0:
                return
            pcm = bytes(payload[:n])
            sum_sq = 0
            count = n // 2
            for k in range(0, n, 2):
                v = int.from_bytes(payload[k:k + 2], "little", signed=True)
                sum_sq += v * v
        else:
            pcm, sum_sq, count = self.decoder.feed(payload)
            if count == 0:
                return
        self._process_audio(pcm, sum_sq, count)

    def _process_audio(self, pcm, sum_sq, count):
        self.pre_roll += pcm
        if len(self.pre_roll) > self.pre_roll_max:
            del self.pre_roll[:len(self.pre_roll) - self.pre_roll_max]

        if sum_sq > 0:
            self.rms_dbm = 10.0 * math.log10(sum_sq / count) - DBFS_FULLSCALE
        else:
            self.rms_dbm = -200.0

        if self.recording and self.lame is not None:
            try:
                self.lame.stdin.write(pcm)
            except (BrokenPipeError, OSError):
                logger.warning("lame pipe error; finalizing recording")
                self._finalize_recording()

    async def _ticker(self):
        while True:
            await asyncio.sleep(0.5)
            self._update_activity()
            if self.ws is not None and (time.monotonic() - self.last_message_time) > 60:
                logger.warning("no data received for 60s; forcing reconnect")
                try:
                    await self.ws.close()
                except Exception:
                    pass

    def _update_noise_floors(self):
        now = time.monotonic()
        cutoff = now - NOISE_WINDOW_SECONDS
        if self.smeter_dbm > -150.0:
            self.noise_smeter_history.append((now, self.smeter_dbm))
        if self.rms_dbm > -150.0:
            self.noise_rms_history.append((now, self.rms_dbm))
        while self.noise_smeter_history and self.noise_smeter_history[0][0] < cutoff:
            self.noise_smeter_history.pop(0)
        while self.noise_rms_history and self.noise_rms_history[0][0] < cutoff:
            self.noise_rms_history.pop(0)
        if self.noise_smeter_history:
            self.noise_floor_smeter = min(v for _, v in self.noise_smeter_history)
        if self.noise_rms_history:
            self.noise_floor_rms = min(v for _, v in self.noise_rms_history)

    def _is_active(self):
        if self.noise_floor_smeter is not None and self.smeter_dbm >= self.noise_floor_smeter + SQUELCH_MARGIN:
            return True
        if self.noise_floor_rms is not None and self.rms_dbm >= self.noise_floor_rms + RMS_MARGIN:
            return True
        return False

    def _update_activity(self):
        now = time.monotonic()
        self._update_noise_floors()
        if self.recording and self.rec_start is not None and (now - self.rec_start) >= MAX_RECORDING_SECONDS:
            logger.warning("recording exceeded max duration (%.0fs), finalizing", MAX_RECORDING_SECONDS)
            self._finalize_recording()
        if self._is_active():
            self.last_active = now
            if not self.recording:
                self._start_recording()
        elif self.recording and (now - self.last_active) >= END_SILENCE_SECONDS:
            self._finalize_recording()

    def _start_recording(self):
        now = datetime.now()
        day_dir = RECORDINGS_DIR / now.strftime("%Y-%m-%d")
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("cannot create recordings dir: %s", e)
            return
        stem = now.strftime("%Y-%m-%d_%H-%M-%S")
        final_path = day_dir / (stem + ".mp3")
        path = final_path.with_name(final_path.name + ".part")
        i = 1
        while final_path.exists() or path.exists():
            final_path = day_dir / ("%s_%d.mp3" % (stem, i))
            path = final_path.with_name(final_path.name + ".part")
            i += 1
        cmd = [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "-",
            "-ar", "32000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", str(MP3_BITRATE) + "k",
            "-f", "mp3", str(path),
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as e:
            logger.error("failed to start lame: %s", e)
            return
        self.lame = proc
        self.rec_file = path
        self.rec_final_file = final_path
        self.rec_start = time.monotonic()
        self.recording = True
        try:
            proc.stdin.write(bytes(self.pre_roll))
        except (BrokenPipeError, OSError):
            self._finalize_recording()
            return
        logger.info("recording started: %s", final_path.name)

    def _finalize_recording(self):
        proc = self.lame
        path = self.rec_file
        final_path = self.rec_final_file
        start = self.rec_start
        self.recording = False
        self.lame = None
        self.rec_file = None
        self.rec_final_file = None
        self.rec_start = None

        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

        if path is not None and final_path is not None and start is not None:
            dur = time.monotonic() - start
            if dur < MIN_DURATION_SECONDS:
                try:
                    path.unlink()
                    logger.info("discarded short recording (%.1fs): %s", dur, final_path.name)
                except OSError:
                    pass
            else:
                self._queue_postprocessing(path, final_path, dur)

    def _queue_postprocessing(self, path, final_path, duration):
        """Denoise in the background and publish only a useful recording."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # This path is useful for a direct/synchronous caller as well as
            # making shutdown robust if finalization happens outside run().
            self._postprocess_recording(path, final_path, duration)
            return

        task = loop.create_task(asyncio.to_thread(
            self._postprocess_recording, path, final_path, duration
        ))
        self.postprocess_tasks.add(task)
        task.add_done_callback(self.postprocess_tasks.discard)

    def _postprocess_recording(self, path, final_path, duration):
        """Run noise reduction, reject silence, then atomically publish MP3."""
        processed_path = final_path.with_name(final_path.name + ".processing")
        enhanced_audio = None
        try:
            enhanced_audio = self._enhance_audio(path, processed_path)

            if not self._has_signal_after_denoise(enhanced_audio):
                if enhanced_audio != processed_path:
                    enhanced_audio.unlink(missing_ok=True)
                processed_path.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                logger.info("discarded recording containing only noise (%.1fs): %s", duration, final_path.name)
                return

            if enhanced_audio != processed_path:
                self._encode_mp3(enhanced_audio, processed_path)

            # os.replace is atomic within the recordings directory.  A file
            # therefore becomes visible to both web and transcriber only after
            # denoising and signal detection have completed successfully.
            os.replace(processed_path, final_path)
            path.unlink(missing_ok=True)
            logger.info("published denoised recording (%.1fs): %s", duration, final_path)
        except Exception:
            # Keep the pending source for diagnostics/retry.  Its .part suffix
            # deliberately keeps it out of the public recording and scanning
            # glob patterns.
            processed_path.unlink(missing_ok=True)
            logger.exception("could not denoise recording; kept pending file: %s", path)
        finally:
            if enhanced_audio is not None and enhanced_audio != processed_path:
                enhanced_audio.unlink(missing_ok=True)

    def _enhance_audio(self, path, processed_path):
        """Return a denoised WAV/MP3 path which is still private to recorder."""
        if not DENOISE_ENABLED:
            # Still run the signal test when denoising is explicitly disabled;
            # this keeps the publication rule predictable.
            os.replace(path, processed_path)
            return processed_path

        if DENOISE_BACKEND == "deepfilternet":
            return self._enhance_with_deepfilternet(path)
        if DENOISE_BACKEND == "afftdn":
            self._run_command(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(path),
                    "-af", "afftdn=nr=%g:nf=%g:tn=1" % (
                        DENOISE_NR, DENOISE_NOISE_FLOOR_DB
                    ),
                    "-ar", "32000", "-ac", "1",
                    "-c:a", "libmp3lame", "-b:a", str(MP3_BITRATE) + "k",
                    "-f", "mp3", str(processed_path),
                ],
                "ffmpeg afftdn failed",
            )
            return processed_path
        raise RuntimeError("unknown DENOISE_BACKEND: %s" % DENOISE_BACKEND)

    def _enhance_with_deepfilternet(self, path):
        """Enhance one recording with the official CPU-only DeepFilterNet CLI.

        The standalone CLI works on 48 kHz WAV files and writes a WAV with the
        same basename.  A temporary directory keeps both files invisible to
        the web service and the transcriber.
        """
        work_dir = Path(tempfile.mkdtemp(prefix=".deepfilter-", dir=str(path.parent)))
        input_wav = work_dir / "input.wav"
        output_dir = work_dir / "out"
        output_dir.mkdir()
        try:
            self._run_command(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(path),
                    "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
                    str(input_wav),
                ],
                "could not convert recording to 48 kHz WAV",
            )
            cmd = [DEEPFILTER_BIN]
            if DEEPFILTER_POST_FILTER:
                cmd.append("--pf")
            if DEEPFILTER_COMPENSATE_DELAY:
                cmd.append("-D")
            cmd.extend(["-o", str(output_dir), str(input_wav)])
            self._run_command(cmd, "DeepFilterNet failed")
            enhanced_wav = output_dir / input_wav.name
            if not enhanced_wav.is_file():
                raise RuntimeError("DeepFilterNet produced no output WAV")
            # The directory must survive until signal detection and final MP3
            # encoding complete.  The caller removes it after this method's
            # returned path has been consumed by _postprocess_recording.
            kept_wav = path.with_name(path.name + ".enhanced.wav")
            os.replace(enhanced_wav, kept_wav)
            return kept_wav
        finally:
            try:
                for item in sorted(work_dir.rglob("*"), reverse=True):
                    if item.is_file() or item.is_symlink():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        item.rmdir()
                work_dir.rmdir()
            except OSError:
                logger.warning("could not clean DeepFilterNet temporary directory: %s", work_dir)

    @staticmethod
    def _run_command(cmd, message):
        """Run an audio command and expose a short useful error on failure."""
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise RuntimeError("%s: %s" % (message, detail[-1] if detail else "unknown error"))

    @staticmethod
    def _encode_mp3(source, destination):
        Recorder._run_command(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-ar", "32000", "-ac", "1",
                "-c:a", "libmp3lame", "-b:a", str(MP3_BITRATE) + "k",
                "-f", "mp3", str(destination),
            ],
            "could not encode denoised recording",
        )

    @staticmethod
    def _has_signal_after_denoise(path):
        """Return whether a non-silent segment remains in a processed file."""
        detect = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "info",
                "-i", str(path),
                "-af", "silencedetect=noise=%gdB:d=%g" % (
                    DENOISE_SILENCE_DB, DENOISE_MIN_SIGNAL_SECONDS
                ),
                "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if detect.returncode != 0:
            detail = (detect.stderr or "").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "silence detection failed")

        output = detect.stderr or ""
        starts = re.findall(r"silence_start:\s*([-+]?\d+(?:\.\d+)?)", output)
        ends = re.findall(r"silence_end:\s*([-+]?\d+(?:\.\d+)?)", output)
        if not starts:
            return True

        # A file which starts with silence and never reports its end is silent
        # from beginning to end.  Any silence_end means that audio rose above
        # the post-denoise threshold for long enough to count as a signal.
        try:
            starts_at_beginning = float(starts[0]) <= 0.05
        except ValueError:
            starts_at_beginning = False
        return not starts_at_beginning or bool(ends)

    async def wait_for_postprocessing(self):
        if self.postprocess_tasks:
            await asyncio.gather(*list(self.postprocess_tasks), return_exceptions=True)

    def stop(self):
        self._running = False
        self._finalize_recording()


async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    freqs = parse_frequencies(os.environ.get("FREQUENCIES", "149287500:nfm"))
    if not freqs:
        logger.error("no frequencies configured")
        return
    freq, mod = freqs[0]
    if len(freqs) > 1:
        logger.warning("multiple frequencies configured; recording only the first (%s)", freq)

    recorder = Recorder(OPENWEBRX_WS_URL, freq, mod)
    task = asyncio.create_task(recorder.run())
    await stop_event.wait()
    logger.info("shutting down")
    recorder.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await recorder.wait_for_postprocessing()


if __name__ == "__main__":
    asyncio.run(main())
