"""Local web app: stdlib ThreadingHTTPServer bound to 127.0.0.1 only.

GET  /                      the page (web/index.html)
GET  /assets/<file>         static assets
GET  /template.csv          input template
GET  /api/health            which keys are set (booleans only, never values) + cap
POST /api/jobs              {"filename", "csv"} -> {"id"} | 400 {"error"}
GET  /api/jobs/<id>         live progress snapshot
GET  /api/jobs/<id>/csv     enriched CSV download
"""
from __future__ import annotations

import json
import mimetypes
import socket
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import OUTPUT, WEB, Settings
from .pipeline import InputError, Job, template_csv
from .summarize import backend

JOBS: dict[str, Job] = {}
MAX_UPLOAD = 5 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "review-intel"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/assets/"):
            f = (WEB / "assets" / Path(path).name)
            if f.is_file():
                return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            return self._send(404, b"not found", "text/plain")
        if path == "/template.csv":
            return self._send(200, template_csv().encode(), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="review-intel-template.csv"'})
        if path == "/api/health":
            s = Settings()
            return self._json(200, {"treg_key": bool(s.treg_key), "anthropic_key": bool(s.anthropic_key),
                                    "llm_backend": backend(s.anthropic_key) or "none", "max_usd": s.max_usd,
                                    "workers": s.workers, "radius_mi": s.radius_mi, "model": s.model})
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job = JOBS.get(parts[2]) if len(parts) >= 3 else None
            if not job:
                return self._json(404, {"error": "unknown job"})
            if len(parts) == 4 and parts[3] == "csv":
                name = Path(job.filename).stem + "_enriched.csv"
                return self._send(200, job.csv_text().encode("utf-8"), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            return self._json(200, job.snapshot())
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/jobs":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            return self._json(413, {"error": "File is larger than 5 MB."})
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            text = str(data.get("csv") or "")
            fname = Path(str(data.get("filename") or "leads.csv")).name[:120]
        except ValueError:
            return self._json(400, {"error": "Bad request."})
        try:
            job = Job(text, fname)
        except InputError as e:
            return self._json(400, {"error": str(e)})
        if not job.s.treg_key:
            return self._json(400, {"error": "TREG_API_KEY is not set on this machine. Add it to .env and restart."})
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = job

        def _run():
            job.run()
            OUTPUT.mkdir(parents=True, exist_ok=True)
            (OUTPUT / (Path(fname).stem + "_enriched.csv")).write_text(job.csv_text(), encoding="utf-8")
            (OUTPUT / (Path(fname).stem + "_enriched_summary.json")).write_text(json.dumps(job.summary(), indent=2))

        threading.Thread(target=_run, daemon=True).start()
        return self._json(200, {"id": jid, "rows": len(job.rows)})


def _free_port(preferred: int) -> int:
    for p in ([preferred] if preferred else []) + list(range(8765, 8800)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def serve(port: int = 0, open_browser: bool = True) -> int:
    port = _free_port(port or Settings().port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"review-intel running at {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
