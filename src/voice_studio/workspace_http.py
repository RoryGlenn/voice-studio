"""Loopback-only HTTP routes for the audiobook workspace."""

import json
import mimetypes
import re
import secrets
import subprocess
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC = Path(__file__).resolve().parents[2] / "web" / "audiobook"


def make_handler(workspace, port):
    token = secrets.token_urlsafe(32)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_data(self, data, kind="application/json", status=200):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def json(self, value, status=200):
            self.send_data(json.dumps(value).encode(), status=status)

        def allowed(self):
            if self.headers.get("Host") not in allowed_hosts:
                self.json({"error": "Localhost access only"}, 403)
                return False
            return True

        def file(self, path, kind=None, download=False):
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if not match or not any(match.groups()):
                    self.send_error(416)
                    return
                a, b = match.groups()
                if a:
                    start, end = int(a), min(int(b), end) if b else end
                else:
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header(
                "Content-Type",
                kind
                or mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header(
                    "Content-Disposition", 'attachment; filename="audiobook-draft.m4b"'
                )
            self.end_headers()
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def do_GET(self):
            if not self.allowed():
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    self.send_data(
                        (STATIC / "index.html")
                        .read_text()
                        .replace("__TOKEN__", token)
                        .encode(),
                        "text/html; charset=utf-8",
                    )
                elif url.path in ("/app.js", "/style.css"):
                    self.file(STATIC / url.path[1:])
                elif url.path in ("/api/progress", "/api/workspace"):
                    self.json(workspace.snapshot())
                elif url.path == "/api/passages":
                    self.json(workspace.passages(int(query["chapter"][0])))
                elif url.path == "/api/log":
                    health = json.loads(
                        (workspace.job / "pipeline-status.json").read_text()
                    )
                    path = Path(health.get("log", "")).resolve()
                    if not path.is_relative_to(
                        (workspace.job / "worker-logs").resolve()
                    ):
                        raise ValueError("No current worker log")
                    with path.open("rb") as handle:
                        handle.seek(max(0, path.stat().st_size - 12000))
                        lines = (
                            handle.read().decode(errors="replace").splitlines()[-50:]
                        )
                    self.json({"lines": lines})
                elif url.path == "/audio":
                    self.file(workspace.safe_audio(query["id"][0]), "audio/wav")
                elif url.path == "/cover":
                    self.file(workspace.job / "cover.png", "image/png")
                elif url.path == "/download":
                    if not workspace.final_ready(workspace.snapshot().get("state")):
                        raise ValueError("Validated audiobook is not ready")
                    self.file(workspace.output / "audiobook.m4b", "audio/mp4", True)
                elif url.path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        self.wfile.write(
                            (
                                "data: " + json.dumps(workspace.snapshot()) + "\n\n"
                            ).encode()
                        )
                        self.wfile.flush()
                        time.sleep(2)
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, KeyError, OSError, StopIteration) as exc:
                self.json({"error": str(exc)}, 400)

        def do_POST(self):
            if not self.allowed():
                return
            origin = self.headers.get("Origin")
            if self.headers.get("X-Workspace-Token") != token or (
                origin and origin not in {"http://" + host for host in allowed_hosts}
            ):
                self.json(
                    {"error": "Refresh this local page before using controls"}, 403
                )
                return
            if self.path != "/api/action":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32000:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected an action object")
                self.json(workspace.request(payload), 202)
            except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                self.json({"error": str(exc)}, 409)

    return Handler
