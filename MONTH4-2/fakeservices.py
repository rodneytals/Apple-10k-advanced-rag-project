"""Stand-ins for the outside world, used by the Day 21 tests (never by the real tools): a fake Telegram, ntfy, uptime-ping
service and mail server, each on a free local port. They speak the same protocol as the real ones, record everything they receive,
and can be told to misbehave (refuse the login, answer 500, ask to slow down)."""
import base64
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional


class _Quiet(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class FakeHttp:
    """Base: runs a server whose behaviour is self.respond(method, path, headers, body) -> (status, headers, body_bytes)."""

    def __init__(self, host: str = "127.0.0.1"):
        self.requests: List[dict] = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                rec = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                with outer.lock:
                    outer.requests.append(rec)
                status, hdrs, payload = outer.respond(rec)
                self.send_response(status)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_HEAD = _go

        self.server = _Quiet((host, 0), H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def respond(self, rec) -> tuple:
        return 200, {}, b"ok"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeTelegram(FakeHttp):
    TOKEN = "123456789:AAH-fake_TOKEN_value-for-tests"

    def __init__(self):
        super().__init__()
        self.mode = "ok"                      # ok | unauthorized | rate | server
        self.rate_left = 1
        self.updates = [{"update_id": 1, "message": {"chat": {"id": 424242, "first_name": "Rodney"}, "text": "/start"}}]

    def messages(self):
        return [json.loads(r["body"]) for r in self.requests if r["path"].endswith("/sendMessage")]

    def respond(self, rec):
        j = {"Content-Type": "application/json"}
        if not rec["path"].startswith(f"/bot{self.TOKEN}/") or self.mode == "unauthorized":
            return 401, j, json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
        if self.mode == "server":
            return 502, j, b'{"ok":false}'
        if self.mode == "rate" and self.rate_left > 0:
            self.rate_left -= 1
            return 429, j, json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1", "parameters": {"retry_after": 1}}).encode()
        if rec["path"].endswith("/getUpdates"):
            return 200, j, json.dumps({"ok": True, "result": self.updates}).encode()
        body = json.loads(rec["body"] or b"{}")
        if str(body.get("chat_id")) != "424242":
            return 400, j, json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}).encode()
        return 200, j, json.dumps({"ok": True, "result": {"message_id": 7}}).encode()


class FakeNtfy(FakeHttp):
    def __init__(self, topic="rag-alerts-testtopic-0123456789abcdef", host: str = "127.0.0.1"):
        super().__init__(host)
        self.topic, self.mode, self.need_token = topic, "ok", None

    def respond(self, rec):
        if self.need_token and rec["headers"].get("authorization") != f"Bearer {self.need_token}":
            return 403, {}, b'{"error":"forbidden"}'
        if self.mode == "server":
            return 503, {}, b"{}"
        if rec["path"] != "/" + self.topic:
            return 404, {}, b"{}"
        return 200, {"Content-Type": "application/json"}, b'{"id":"abc"}'


class FakePing(FakeHttp):
    """Stands in for an uptime-ping service: records every GET."""

    def pings(self):
        return [r["path"] for r in self.requests]


class FakeDoor(FakeHttp):
    """Stands in for the front door in watchdog tests: you decide what each path answers."""

    def __init__(self):
        super().__init__()
        self.answers: Dict[str, int] = {"/healthz": 200}
        self.default = 404

    def respond(self, rec):
        path = rec["path"].split("?")[0]
        return self.answers.get(path, self.default), {}, b"x"


# --------------------------------------------------------------- SMTP -------
def make_self_signed(dirname: str) -> Optional[tuple]:
    """(certfile, keyfile) for 127.0.0.1 made with the openssl program, or None if this computer has none."""
    if not shutil.which("openssl"):
        return None
    cert, key = os.path.join(dirname, "cert.pem"), os.path.join(dirname, "key.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], capture_output=True)
    return (cert, key) if r.returncode == 0 else None


class FakeSmtp:
    """A very small SMTP server: EHLO, STARTTLS (optional), AUTH PLAIN/LOGIN, MAIL, RCPT, DATA, QUIT."""

    def __init__(self, user="alerts@example.test", password="app-password-1234", tls: Optional[tuple] = None, mode="plain"):
        self.user, self.password, self.mode, self.tls = user, password, mode, tls
        self.messages: List[dict] = []
        self.logins: List[bool] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.ctx = None
        if tls:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(*tls)
        self.client_ctx = None
        if tls:
            self.client_ctx = ssl.create_default_context(cafile=tls[0])
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            if self.mode == "ssl":
                c = self.ctx.wrap_socket(c, server_side=True)
            f = c.makefile("rwb", buffering=0)

            def send(line):
                f.write((line + "\r\n").encode())

            def readline():
                return f.readline().decode("latin-1").rstrip("\r\n")
            send("220 fake.test ESMTP")
            frm, rcpts, authed = "", [], False
            while True:
                line = readline()
                if not line:
                    return
                cmd = line.split(" ", 1)[0].upper()
                if cmd == "EHLO":
                    caps = ["250-fake.test", "250-AUTH PLAIN LOGIN"]
                    if self.mode == "starttls" and not isinstance(c, ssl.SSLSocket):
                        caps.append("250-STARTTLS")
                    caps.append("250 SIZE 100000")
                    for l in caps:
                        send(l)
                elif cmd == "STARTTLS":
                    send("220 go ahead")
                    c = self.ctx.wrap_socket(c, server_side=True)
                    f = c.makefile("rwb", buffering=0)
                elif cmd == "AUTH":
                    parts = line.split()
                    if parts[1].upper() == "PLAIN":
                        blob = parts[2] if len(parts) > 2 else None
                        if blob is None:
                            send("334 ")
                            blob = readline()
                        _, u, p = base64.b64decode(blob).decode().split("\x00")
                    else:
                        send("334 " + base64.b64encode(b"Username:").decode())
                        u = base64.b64decode(readline()).decode()
                        send("334 " + base64.b64encode(b"Password:").decode())
                        p = base64.b64decode(readline()).decode()
                    authed = (u == self.user and p == self.password)
                    self.logins.append(authed)
                    send("235 ok" if authed else "535 5.7.8 authentication failed")
                elif cmd == "MAIL":
                    frm = line.split(":", 1)[1].strip()
                    send("250 ok")
                elif cmd == "RCPT":
                    rcpts.append(line.split(":", 1)[1].strip())
                    send("250 ok")
                elif cmd == "DATA":
                    send("354 end with .")
                    data = []
                    while True:
                        l = readline()
                        if l == ".":
                            break
                        data.append(l[1:] if l.startswith("..") else l)
                    self.messages.append({"from": frm, "to": rcpts, "raw": "\r\n".join(data), "authed": authed})
                    send("250 queued")
                elif cmd == "QUIT":
                    send("221 bye")
                    return
                else:
                    send("250 ok")
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass

    def stop(self):
        self.sock.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
