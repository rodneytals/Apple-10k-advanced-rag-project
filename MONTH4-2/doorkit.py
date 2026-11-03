"""Test helpers (Day 21, not used by the real tools): a stand-in API that behaves like the real one for keys and request ids,
and a runner that starts the REAL Caddy program in front of it."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import make_frontdoor as mf
from testkit import free_port

CADDY = os.environ.get("CADDY_BIN") or shutil.which("caddy")
KEY = "lc-key-9f8e7d6c5b4a3210fedcba98"


class Skip(Exception):
    pass


class StubApi:
    def __init__(self, auth=True):
        self.auth = auth
        self.hits = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n) if n else b""
                outer.hits.append((self.command, self.path))
                rid = self.headers.get("X-Request-ID", "none")
                path = self.path.split("?")[0]
                if self.command == "POST" and path == "/ask":
                    if outer.auth and self.headers.get("X-API-Key") != KEY:
                        status, body = 401, {"error": {"code": "unauthorized"}, "request_id": rid}
                    else:
                        status, body = 200, {"answer": "Apple's total net sales were $391.0 billion.", "request_id": rid, "session_id": "s"}
                else:
                    status, body = 200, {"stub": path}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Server", "stub-uvicorn/0.0")
                self.send_header("X-Request-ID", rid)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)

            do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _go

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass

        self.server = Quiet(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Door:
    """Real Caddy in front of a StubApi. `caddyfile` replaces the generated one (to test a door that is built wrongly)."""

    def __init__(self, api=None, caddyfile=None, **cfg):
        if not CADDY:
            raise Skip("no caddy program on this computer")
        self.api = api or StubApi()
        self.port = free_port()
        self.tmp = tempfile.mkdtemp(prefix="door_")
        text = caddyfile.replace("__API__", f"127.0.0.1:{self.api.port}").replace("__PORT__", str(self.port)) if caddyfile else \
            mf.caddyfile_text(**{"upstream": f"127.0.0.1:{self.api.port}", "address": f":{self.port}", **cfg})
        path = os.path.join(self.tmp, "Caddyfile")
        open(path, "w").write(text)
        env = dict(os.environ, XDG_DATA_HOME=self.tmp, XDG_CONFIG_HOME=self.tmp, HOME=self.tmp)
        self.proc = subprocess.Popen([CADDY, "run", "--config", path, "--adapter", "caddyfile"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines = []
        threading.Thread(target=lambda: [self.lines.append(l.rstrip()) for l in self.proc.stdout], daemon=True).start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.stop()
            raise AssertionError("caddy did not start:\n" + "\n".join(self.lines[-10:]))
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self.api.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


LEAKY_CADDYFILE = """{
	admin off
	auto_https off
}
:__PORT__ {
	reverse_proxy __API__
}
"""
