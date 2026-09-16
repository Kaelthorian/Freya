"""Local same-origin HTTP adapter and replayable Server-Sent Events."""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .api import ApiError
from .security import sanitize

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_BODY = 256_000


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, application):
        super().__init__(address, Handler)
        self.application = application
        self.stopping = threading.Event()
        self.sse_slots = threading.BoundedSemaphore(32)

    def server_close(self):
        self.stopping.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentControlCenter/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Avoid logging URLs/query strings or other user content to stderr.
        pass

    def _headers(self, content_type):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def _json(self, status, data):
        payload = json.dumps(sanitize(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _guard(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "").lower()
        if host not in hosts:
            raise ApiError(403, "Local host not allowed.")
        origin = self.headers.get("Origin")
        if origin and origin.lower() != "http://" + host:
            raise ApiError(403, "Origin not allowed.")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ApiError(403, "Cross-site request blocked.")
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "Transfer-Encoding is not supported.")

    def _body(self):
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise ApiError(415, "Use Content-Type: application/json.")
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > MAX_BODY:
            raise ApiError(413, "Request is too large.")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("The body must be a JSON object.")
        return value

    def _handle(self):
        try:
            self._guard()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path).rstrip("/") or "/"
            query = {key: value[-1] for key, value in parse_qs(parsed.query).items()}
            if self.command == "GET":
                if path == "/api/events" or (path.startswith("/api/tasks/") and path.endswith("/events")):
                    self._events(path, query)
                    return
                if not path.startswith("/api/"):
                    self._static(path)
                    return
                body = {}
            else:
                body = self._body()
            status, value = self.server.application.dispatch(self.command, path, query, body)
            self._json(status, value)
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except KeyError:
            self._json(404, {"error": "Agent or task not found."})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except (ConnectionError, TimeoutError):
            pass
        except Exception:
            self._json(500, {"error": "Internal server error. Check the server status and try again."})

    do_GET = _handle
    do_POST = _handle
    do_PATCH = _handle
    do_DELETE = _handle

    def _static(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (FRONTEND / relative).resolve()
        if FRONTEND not in target.parents or target.suffix not in {".html", ".css", ".js", ".svg", ".ico"} or not target.is_file():
            raise ApiError(404, "File not found.")
        content = target.read_bytes()
        mime = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(target.suffix)
        mime = mime or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self._headers(mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _events(self, path, query):
        store = self.server.application.store
        parts = path.strip("/").split("/")
        task_id = parts[2] if len(parts) == 4 else None
        if path != "/api/events" and (len(parts) != 4 or parts[1] != "tasks"):
            raise ApiError(404, "Stream not found.")
        if task_id:
            store.get_task(task_id)
        after = int(self.headers.get("Last-Event-ID") or query.get("after", "0"))
        if after < 0:
            raise ValueError("Invalid cursor.")
        if not self.server.sse_slots.acquire(blocking=False):
            raise ApiError(503, "Too many event connections.")
        try:
            self.send_response(200)
            self._headers("text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 1500\n: connected\n\n")
            self.wfile.flush()
            deadline = time.monotonic() + 50
            heartbeat = time.monotonic()
            while not self.server.stopping.is_set() and time.monotonic() < deadline:
                events = store.list_events(task_id=task_id, after=after, limit=200)
                for event in events:
                    data = json.dumps(sanitize(event), ensure_ascii=False, allow_nan=False)
                    self.wfile.write(f"id: {event['id']}\nevent: update\ndata: {data}\n\n".encode("utf-8"))
                    after = event["id"]
                if events or time.monotonic() - heartbeat > 10:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                if len(events) < 200:
                    self.server.stopping.wait(0.4)
        except (ConnectionError, TimeoutError, OSError):
            pass
        finally:
            self.server.sse_slots.release()
