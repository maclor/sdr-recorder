import html
import json
import os
import posixpath
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings")).resolve()
PORT = int(os.environ.get("WEB_PORT", "8074"))

try:
    TIMEZONE = ZoneInfo(os.environ.get("TZ", "UTC"))
except Exception:
    TIMEZONE = None


def resolve_recording(rel_path):
    rel_path = rel_path.lstrip("/")
    target = (RECORDINGS_DIR / rel_path).resolve()
    if target != RECORDINGS_DIR and RECORDINGS_DIR not in target.parents:
        return None
    return target


_BITRATE_TABLES = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}


def _mp3_duration(path):
    try:
        with open(path, "rb") as f:
            data = f.read(1024)
        file_size = os.path.getsize(path)
    except OSError:
        return None

    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        offset = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])

    if offset + 4 > len(data):
        return None
    fh = data[offset:offset + 4]
    if len(fh) < 4 or fh[0] != 0xFF or (fh[1] & 0xE0) != 0xE0:
        return None

    version_bits = (fh[1] >> 3) & 0x03
    layer_bits = (fh[1] >> 1) & 0x03
    bitrate_index = (fh[2] >> 4) & 0x0F
    sample_index = (fh[2] >> 2) & 0x03
    padding = (fh[2] >> 1) & 0x01
    channel_mode = (fh[3] >> 6) & 0x03

    if version_bits == 3:
        version = 1
        sample_rates = [44100, 48000, 32000]
    elif version_bits == 2:
        version = 2
        sample_rates = [22050, 24000, 16000]
    elif version_bits == 0:
        version = 2
        sample_rates = [11025, 12000, 8000]
    else:
        return None

    if layer_bits == 1:
        layer = 3
    elif layer_bits == 2:
        layer = 2
    elif layer_bits == 3:
        layer = 1
    else:
        return None

    if sample_index >= len(sample_rates):
        return None
    sample_rate = sample_rates[sample_index]

    table = _BITRATE_TABLES.get((version, layer))
    if table is None or bitrate_index >= len(table):
        return None
    bitrate = table[bitrate_index] * 1000
    if bitrate == 0:
        return None

    if layer == 1:
        spf = 384
        frame_size = (12 * bitrate // sample_rate + padding) * 4
    else:
        spf = 1152 if (version == 1 or layer != 3) else 576
        frame_size = 144 * bitrate // sample_rate + padding

    frame_count = None
    side_info = (17 if version == 1 else 9) if channel_mode == 3 else (32 if version == 1 else 17)
    pos = offset + 4 + side_info
    if pos + 8 <= len(data) and data[pos:pos + 4] in (b"Info", b"Xing"):
        flags = int.from_bytes(data[pos + 4:pos + 8], "big")
        if flags & 0x01 and pos + 12 <= len(data):
            frame_count = int.from_bytes(data[pos + 8:pos + 12], "big")

    if frame_count is None:
        frame_count = max(1, (file_size - offset) // frame_size)

    return frame_count * spf / sample_rate


def _fmt_duration(seconds):
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return "%dm %02ds" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return "%dh %02dm %02ds" % (hours, minutes, sec)


def _transcription_meta(mp3):
    meta = mp3.with_suffix(".json")
    if meta.is_file():
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "error", "message": "cannot read status"}
    if mp3.with_suffix(".txt").is_file():
        return {"status": "transcribed"}
    return {"status": "pending"}


def list_recordings():
    recordings = []
    if not RECORDINGS_DIR.is_dir():
        return recordings
    for mp3 in sorted(RECORDINGS_DIR.rglob("*.mp3"), reverse=True):
        stat = mp3.stat()
        if TIMEZONE is not None:
            date_str = datetime.fromtimestamp(stat.st_mtime, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        duration = _mp3_duration(mp3)
        meta = _transcription_meta(mp3)
        recordings.append({
            "path": mp3.relative_to(RECORDINGS_DIR).as_posix(),
            "filename": mp3.name,
            "size": stat.st_size,
            "duration": duration,
            "duration_text": _fmt_duration(duration),
            "transcription_status": meta.get("status") or "pending",
            "transcription_message": meta.get("message") or "",
            "mtime": stat.st_mtime,
            "date": date_str,
        })
    return recordings


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sdr-recorder-web"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, message):
        body = ("<html><body><h1>%d</h1><p>%s</p></body></html>" % (code, html.escape(message))).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._serve_index()
        if path == "/api/recordings":
            return self._send_json({"recordings": list_recordings()})
        if path.startswith("/api/transcript/"):
            return self._serve_transcript(path[len("/api/transcript/"):])
        if path.startswith("/recordings/"):
            return self._serve_file(path[len("/recordings/"):])
        if path.startswith("/api/"):
            return self._send_error(404, "not found")
        return self._send_error(404, "not found")

    def _serve_transcript(self, rel_path):
        rel = urllib.parse.unquote(rel_path).lstrip("/")
        target = resolve_recording(rel)
        if target is None:
            return self._send_error(404, "not found")
        txt = target.with_suffix(".txt")
        if not txt.is_file():
            return self._send_json({"transcript": None}, code=404)
        try:
            text = txt.read_text(encoding="utf-8")
        except OSError:
            return self._send_error(500, "read error")
        return self._send_json({"transcript": text})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/recordings/delete-all":
            return self._delete_all()
        if path.startswith("/api/recordings/"):
            rel = urllib.parse.unquote(path[len("/api/recordings/"):])
            target = resolve_recording(rel)
            if target is None or not target.is_file():
                return self._send_error(404, "not found")
            try:
                target.unlink()
                self._remove_empty_dirs(target.parent)
            except OSError as e:
                return self._send_error(500, str(e))
            return self._send_json({"deleted": rel})
        return self._send_error(404, "not found")

    def _delete_all(self):
        deleted = 0
        for mp3 in list(RECORDINGS_DIR.rglob("*.mp3")):
            try:
                mp3.unlink()
                deleted += 1
            except OSError:
                pass
        for directory in sorted(
            [p for p in RECORDINGS_DIR.rglob("*") if p.is_dir()],
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        return self._send_json({"deleted": deleted})

    def _remove_empty_dirs(self, directory):
        directory = directory.resolve()
        while directory != RECORDINGS_DIR and RECORDINGS_DIR in directory.parents:
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent

    def _serve_index(self):
        body = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, rel_path):
        target = resolve_recording(rel_path)
        if target is None or not target.is_file():
            return self._send_error(404, "not found")
        size = target.stat().st_size
        range_header = self.headers.get("Range")

        if range_header:
            byte_range = self._parse_range(range_header, size)
            if byte_range is None:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            start, end = byte_range
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(target, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(target, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def _parse_range(self, header, size):
        unit, _, spec = header.partition("=")
        if unit.strip() != "bytes":
            return None
        spec = spec.strip()
        if "," in spec:
            return None
        if spec.startswith("-"):
            try:
                suffix = int(spec[1:])
            except ValueError:
                return None
            if suffix == 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
            return (start, end)
        if "-" in spec:
            start_s, _, end_s = spec.partition("-")
            try:
                start = int(start_s)
            except ValueError:
                return None
            if end_s == "":
                end = size - 1
            else:
                try:
                    end = int(end_s)
                except ValueError:
                    return None
            if start > end or start >= size:
                return None
            end = min(end, size - 1)
            return (start, end)
        return None


INDEX_HTML = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SDR Recorder</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1.5rem; color: #222; }
  h1 { font-size: 1.3rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }
  th { font-size: 0.85rem; color: #666; }
  audio { width: 100%; max-width: 320px; height: 34px; }
  button { padding: 6px 12px; cursor: pointer; border: 1px solid #c0392b; background: #fff; color: #c0392b; border-radius: 4px; }
  button:hover { background: #c0392b; color: #fff; }
  .empty { color: #888; margin-top: 1rem; }
  .size { color: #888; font-size: 0.85rem; }
  button.secondary { border-color: #666; color: #666; }
  button.secondary:hover { background: #666; color: #fff; }
  td.transcript-cell { background: #fafafa; }
  pre.transcript { margin: 0; white-space: pre-wrap; font-family: monospace; font-size: 0.9rem; color: #333; }
  td.status { font-size: 1rem; text-align: center; white-space: nowrap; }
  td.status .spinner { display: inline-block; animation: spin 1.2s linear infinite; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
</style>
</head>
<body>
<h1>SDR Recorder — nagrania</h1>
<div style="margin-bottom: 1rem;">
  <button id="refresh" style="border-color:#666;color:#666;">Odśwież</button>
  <button id="delete-all" style="border-color:#666;color:#666;">Usuń wszystkie</button>
</div>
<table id="table">
  <thead><tr><th>Data i godzina</th><th>Odsłuch</th><th>Długość</th><th>Transkrypcja</th><th></th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<div id="empty" class="empty" style="display:none">Brak nagrań.</div>

<script>
const transcriptCache = {};

const STATUS_ICONS = {
  pending:     { icon: "⏳", color: "#999", title: "oczekuje na transkrypcję" },
  processing:  { icon: "◌", color: "#2196f3", title: "transkrypcja w toku", spin: true },
  transcribed: { icon: "✓", color: "#2e7d32", title: "gotowe" },
  no_speech:   { icon: "—", color: "#bbb", title: "brak mowy" },
  skipped:     { icon: "⚠", color: "#f57c00", title: "pominięto" },
  error:       { icon: "⚠", color: "#c0392b", title: "błąd transkrypcji" },
};

function statusCell(r) {
  const td = document.createElement("td");
  td.className = "status";
  const s = STATUS_ICONS[r.transcription_status] || STATUS_ICONS.pending;
  td.style.color = s.color;
  td.title = r.transcription_message ? (s.title + ": " + r.transcription_message) : s.title;
  const span = document.createElement("span");
  if (s.spin) span.className = "spinner";
  span.textContent = s.icon;
  td.appendChild(span);
  return td;
}

async function toggleTranscript(path, tr) {
  const row = tr.nextElementSibling;
  if (row && row.classList.contains("transcript-row")) {
    row.remove();
    return;
  }
  let text = transcriptCache[path];
  if (text === undefined) {
    text = null;
    try {
      const res = await fetch("/api/transcript/" + path.split("/").map(encodeURIComponent).join("/"));
      if (res.ok) {
        const data = await res.json();
        text = data.transcript || "";
      }
    } catch (e) {
      text = null;
    }
    transcriptCache[path] = text;
  }
  const tr2 = document.createElement("tr");
  tr2.className = "transcript-row";
  const td = document.createElement("td");
  td.colSpan = 5;
  td.className = "transcript-cell";
  if (!text) {
    td.textContent = "Brak transkrypcji.";
  } else {
    const pre = document.createElement("pre");
    pre.className = "transcript";
    pre.textContent = text;
    td.appendChild(pre);
  }
  tr2.appendChild(td);
  tr.after(tr2);
}

async function load() {
  const res = await fetch("/api/recordings?_=" + Date.now());
  const data = await res.json();
  const rows = document.getElementById("rows");
  rows.innerHTML = "";
  const list = data.recordings || [];
  document.getElementById("empty").style.display = list.length ? "none" : "block";
  for (const r of list) {
    const tr = document.createElement("tr");
    const url = "/recordings/" + r.path.split("/").map(encodeURIComponent).join("/");
    const tdDate = document.createElement("td");
    tdDate.textContent = r.date;
    const tdAudio = document.createElement("td");
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = url;
    tdAudio.appendChild(audio);
    const tdSize = document.createElement("td");
    tdSize.className = "size";
    tdSize.textContent = r.duration_text || "—";
    const tdStatus = statusCell(r);
    const tdDel = document.createElement("td");
    const btnT = document.createElement("button");
    btnT.textContent = "Transkrypcja";
    btnT.className = "secondary";
    btnT.style.marginRight = "6px";
    btnT.onclick = function() { toggleTranscript(r.path, tr); };
    const btn = document.createElement("button");
    btn.textContent = "Usuń";
    btn.onclick = async function() {
      if (!confirm("Usunąć nagranie " + r.filename + "?")) return;
      const dres = await fetch("/api/recordings/" + r.path.split("/").map(encodeURIComponent).join("/"), {method: "POST"});
      if (dres.ok) load(); else alert("Błąd usuwania");
    };
    tdDel.appendChild(btnT);
    tdDel.appendChild(btn);
    tr.appendChild(tdDate);
    tr.appendChild(tdAudio);
    tr.appendChild(tdSize);
    tr.appendChild(tdStatus);
    tr.appendChild(tdDel);
    rows.appendChild(tr);
  }
}
load();
document.getElementById("refresh").onclick = function() { load(); };
document.getElementById("delete-all").onclick = async function() {
  if (!confirm("Usunąć wszystkie nagrania?")) return;
  const res = await fetch("/api/recordings/delete-all", {method: "POST"});
  if (res.ok) load(); else alert("Błąd usuwania");
};
</script>
</body>
</html>
"""


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("serving recordings from %s on port %d" % (RECORDINGS_DIR, PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
