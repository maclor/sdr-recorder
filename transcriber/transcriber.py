import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sdr-transcriber")


def env_number(name, default, cast):
    """Read a numeric setting, falling back to the default on a bad value.

    A typo in the compose file must not stop the container from starting.
    """
    raw = os.environ.get(name, str(default))
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a valid number, using %r", name, raw, default)
        return cast(str(default))


RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings"))
MIN_AGE_SECONDS = env_number("MIN_AGE_SECONDS", 300, int)
LANGUAGE = os.environ.get("LANGUAGE", "pl")
PRE_ROLL_SECONDS = env_number("PRE_ROLL_SECONDS", 2.0, float)
SLEEP_SECONDS = env_number("SLEEP_SECONDS", 60, int)
PAUSE_SECONDS = env_number("PAUSE_SECONDS", 5.0, float)
MAX_FILE_MB = env_number("MAX_FILE_MB", 20, int)
WHISPER_BIN = os.environ.get("WHISPER_BIN", "whisper-cli")
MODEL_PATH = os.environ.get("MODEL_PATH", "/models/ggml-base-q5_1.bin")
# 3, not 4: the J1800 has 4 logical cores but the OS and the recorder need
# them too. Transcribing with all of them starves everything else.
THREADS = env_number("THREADS", 3, int)
WHISPER_BEAM_SIZE = env_number("WHISPER_BEAM_SIZE", 2, int)
WHISPER_BEST_OF = env_number("WHISPER_BEST_OF", 2, int)
WHISPER_NO_SPEECH_THRESHOLD = env_number("WHISPER_NO_SPEECH_THRESHOLD", 0.6, float)
WHISPER_SUPPRESS_NON_SPEECH = os.environ.get("WHISPER_SUPPRESS_NON_SPEECH", "1").strip().lower() not in {
    "0", "false", "no", "off"
}

FINAL_STATUSES = {"transcribed", "no_speech", "skipped", "error"}

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
SEG_RE = re.compile(r"\[\s*(\d+):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*\d+:\d{2}:\d{2}[.,]\d{1,3}\s*\]\s*(.*)")

try:
    os.nice(19)
except Exception:
    logger.warning("unable to lower process priority")


def parse_recording_time(filename):
    m = TS_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def write_status(mp3, status, message=None):
    payload = {"status": status}
    if message:
        payload["message"] = message
    meta = mp3.with_suffix(".json")
    tmp = meta.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, meta)


def read_status(mp3):
    meta = mp3.with_suffix(".json")
    if not meta.is_file():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        return None


def transcribe_file(mp3):
    name = mp3.stem
    base_time = parse_recording_time(name)
    if base_time is None:
        logger.warning("cannot parse timestamp from %s, skipping", mp3.name)
        write_status(mp3, "error", "cannot parse timestamp")
        return False

    write_status(mp3, "processing")

    wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav = f.name
        conv = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if conv.returncode != 0:
            write_status(mp3, "error", "audio decode failed")
            return False

        whisper_cmd = [
            WHISPER_BIN,
            "-m", MODEL_PATH,
            "-l", LANGUAGE,
            "-t", str(THREADS),
            "-bs", str(WHISPER_BEAM_SIZE),
            "-bo", str(WHISPER_BEST_OF),
            "-nth", str(WHISPER_NO_SPEECH_THRESHOLD),
        ]
        if WHISPER_SUPPRESS_NON_SPEECH:
            whisper_cmd.append("-sns")
        whisper_cmd.extend(["-f", wav])
        proc = subprocess.run(
            whisper_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )

        lines = []
        for line in proc.stdout.splitlines():
            m = SEG_RE.match(line)
            if not m:
                continue
            start_sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            text = m.group(5).strip()
            if not text:
                continue
            t = base_time - timedelta(seconds=PRE_ROLL_SECONDS) + timedelta(seconds=start_sec)
            lines.append("[%s] %s" % (t.strftime("%H:%M:%S"), text))
    finally:
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass

    if not lines:
        logger.info("no speech detected in %s", mp3.name)
        write_status(mp3, "no_speech")
        return False

    txt = mp3.with_suffix(".txt")
    tmp = txt.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, txt)
    write_status(mp3, "transcribed")
    logger.info("transcribed %s -> %d lines", mp3.name, len(lines))
    return True


def process_batch():
    now = time.time()
    cutoff = now - MIN_AGE_SECONDS
    candidates = []
    max_bytes = MAX_FILE_MB * 1024 * 1024
    for mp3 in RECORDINGS_DIR.rglob("*.mp3"):
        if mp3.with_suffix(".txt").exists():
            continue
        if read_status(mp3) in FINAL_STATUSES:
            continue
        try:
            st = mp3.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            if st.st_size > max_bytes:
                logger.info("skipping oversized file %s (%.1f MB)", mp3.name, st.st_size / 1048576)
                write_status(mp3, "skipped", "file too large")
                continue
            candidates.append((st.st_mtime, mp3))

    candidates.sort(key=lambda x: x[0])
    if candidates:
        logger.info("%d file(s) awaiting transcription", len(candidates))
    for _, mp3 in candidates:
        try:
            transcribe_file(mp3)
        except Exception:
            logger.exception("transcription failed for %s", mp3.name)
            write_status(mp3, "error", "transcription failed")
        time.sleep(PAUSE_SECONDS)


def main():
    logger.info("using whisper binary %s with model %s", WHISPER_BIN, MODEL_PATH)
    logger.info(
        "whisper settings: threads=%d beam=%d best_of=%d no_speech=%.2f suppress_nst=%s",
        THREADS, WHISPER_BEAM_SIZE, WHISPER_BEST_OF,
        WHISPER_NO_SPEECH_THRESHOLD, WHISPER_SUPPRESS_NON_SPEECH,
    )
    while True:
        try:
            process_batch()
        except Exception:
            logger.exception("batch error")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
