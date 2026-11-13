"""Local web app: stdlib ThreadingHTTPServer bound to 127.0.0.1 only.

Hardening (any web page open in the same browser must not be able to start paid runs or read results):
  * binds 127.0.0.1 only
  * every request: Host must be exactly 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding)
  * every request with an Origin header: it must be this server's own origin (blocks cross-site POSTs)
  * every /api/ request (GET and POST, downloads included): header X-Review-Intel-Token must equal a
    per-process secrets.token_urlsafe(32) token, which is only injected into the served index.html
  * POST must be Content-Type: application/json (a cross-site HTML form cannot send that)

GET  /                      the page (web/index.html)
GET  /assets/<file>         static assets
GET  /template.csv          input template
GET  /api/health            which keys are set (booleans only, never values) + cap
POST /api/jobs              {"filename", "csv"} -> {"id"} | 400 {"error"}
GET  /api/jobs/<id>         live progress snapshot
GET  /api/jobs/<id>/csv     enriched CSV download
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
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
TOKEN = secrets.token_urlsafe(32)  # new every process start; never written to disk
TOKEN_HEADER = "X-Review-Intel-Token"
TOKEN_PLACEHOLDER = "__REVIEW_INTEL_TOKEN__"


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

    # ---------- request guards ----------

    def _guard(self, path: str, post: bool = False) -> bool:
        """True if the request may proceed; otherwise the error response has already been sent."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in allowed_hosts:
            self._json(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() != f"http://{host}":
            self._json(403, {"error": "forbidden origin"})
            return False
        if path.startswith("/api/"):
            sent = self.headers.get(TOKEN_HEADER) or ""
            if not hmac.compare_digest(sent.encode(), TOKEN.encode()):
                self._json(403, {"error": "missing or bad token"})
                return False
        if post:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._json(415, {"error": "Content-Type must be application/json"})
                return False
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self._guard(path):
            return
        if path in ("/", "/index.html"):
            page = (WEB / "index.html").read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, TOKEN)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                              {"Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                               "Content-Security-Policy": "frame-ancestors 'none'"})
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
        path = self.path.split("?", 1)[0]
        if not self._guard(path, post=True):
            return
        if path != "/api/jobs":
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
