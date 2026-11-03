"""Tests for make_frontdoor.py.  Run:  python test_frontdoor.py

The behaviour tests start the REAL Caddy program in front of a small stand-in API that writes down EVERY request it receives.
That is how "this path never reaches the API" is proven: by what the API actually received, not by what Caddy answered.
They need a `caddy` program on this computer (or CADDY_BIN=/path/to/caddy). Without one they are reported as SKIP; the door is
then checked live, through the container, by Cell 7 and by check_launch.py.
"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_frontdoor as mf
from testkit import free_port

CADDY = os.environ.get("CADDY_BIN") or shutil.which("caddy")
results = []


class Skip(Exception):
    pass


def test(fn):
    try:
        fn()
        results.append((fn.__name__, "pass", ""))
    except Skip as e:
        results.append((fn.__name__, "skip", str(e)))
    except Exception:
        results.append((fn.__name__, "fail", traceback.format_exc(limit=6)))
    return fn


# ---------------------------------------------------------------- helpers --
class StubApi:
    """Stands in for the API. Records every request exactly as it arrived (raw path, method, headers, how many body bytes)."""

    def __init__(self):
        self.hits = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                n = 0
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    try:
                        while True:
                            size = int(self.rfile.readline().strip() or b"0", 16)
                            if size == 0:
                                self.rfile.readline()
                                break
                            n += len(self.rfile.read(size))
                            self.rfile.readline()
                    except Exception:
                        pass
                else:
                    length = int(self.headers.get("Content-Length") or 0)
                    n = len(self.rfile.read(length)) if length else 0
                with outer.lock:
                    outer.hits.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": n})
                if unquote(self.path.split("?")[0]) == "/metrics":
                    body = b"rag_http_requests_total 1  # THE SECRET METRICS PAGE"
                else:
                    body = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Server", "stub-uvicorn/0.0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _serve

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):          # Caddy hanging up on the stand-in is normal here
                pass

        self.server = Quiet(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def seen_paths(self):
        with self.lock:
            return [h["path"] for h in self.hits]


class Door:
    def __init__(self, **cfg):
        if not CADDY:
            raise Skip("no caddy program on this computer")
        self.api = StubApi()
        self.port = free_port()
        self.tmp = tempfile.mkdtemp(prefix="door_")
        cfg.setdefault("upstream", f"127.0.0.1:{self.api.port}")
        cfg.setdefault("address", f":{self.port}")
        path = os.path.join(self.tmp, "Caddyfile")
        open(path, "w").write(mf.caddyfile_text(**cfg))
        env = dict(os.environ, XDG_DATA_HOME=self.tmp, XDG_CONFIG_HOME=self.tmp, HOME=self.tmp)
        self.proc = subprocess.Popen([CADDY, "run", "--config", path, "--adapter", "caddyfile"], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines = []
        threading.Thread(target=self._pump, daemon=True).start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise AssertionError("caddy did not start:\n" + "\n".join(self.lines[-10:]))

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())

    def access_log(self):
        out = []
        for l in list(self.lines):
            try:
                d = json.loads(l)
            except ValueError:
                continue
            if str(d.get("logger", "")).startswith("http.log.access"):
                out.append(d)
        return out

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.api.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def raw(port, target, method="GET", headers=None, body=b"", chunked=False, timeout=10, read=True):
    """Send exactly these bytes. Returns (status, headers, body). `target` is sent as it is: no tidying by a client library."""
    h = {"Host": "x"}
    h.update(headers or {})
    if chunked:
        h["Transfer-Encoding"] = "chunked"
    elif body or method in ("POST", "PUT"):
        h["Content-Length"] = str(len(body))
    head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "Connection: close\r\n\r\n"
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(head.encode("latin-1"))
        if chunked:
            for i in range(0, len(body), 4096):
                part = body[i:i + 4096]
                s.sendall(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            s.sendall(b"0\r\n\r\n")
        elif body:
            s.sendall(body)
        if not read:
            return None
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except (socket.timeout, ConnectionError):
                break
            if not chunk:
                break
            data += chunk
    except (BrokenPipeError, ConnectionResetError):
        data = data if "data" in locals() else b""
    finally:
        s.close()
    if not data:
        return 0, {}, b""
    head_b, _, rest = data.partition(b"\r\n\r\n")
    lines = head_b.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdrs = {}
    for l in lines[1:]:
        k, _, v = l.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, rest


def with_door(fn):
    """Run a test body with a fresh door; always stop it."""
    def run():
        d = Door(read_header="1s", read_body="2s")
        try:
            fn(d)
        finally:
            d.stop()
    run.__name__ = fn.__name__
    return run


# ============================================================ unit (no caddy) ==
@test
def u01_the_allow_list_is_exactly_two_things_and_has_no_wildcards():
    text = mf.caddyfile_text()
    assert mf.ALLOWED == (("POST", "/ask"), ("GET", "/healthz"))
    assert '== "/ask"' in text and '== "/healthz"' in text
    assert "path /" not in text and "*" not in text.replace("# ", "")      # no wildcard matchers anywhere
    assert text.count("handle ") == 3 and "respond \"not found\" 404" in text


@test
def u02_plain_address_turns_automatic_https_off_and_a_host_name_keeps_it():
    assert "auto_https off" in mf.caddyfile_text(address=":8080")
    assert "auto_https off" in mf.caddyfile_text(address="http://localhost:8080")
    assert "auto_https off" not in mf.caddyfile_text(address="api.example.com")


@test
def u03_no_request_header_can_reach_the_log_and_nothing_secret_is_in_the_file():
    text = mf.caddyfile_text()
    assert "request>headers delete" in text and "resp_headers delete" in text
    for bad in ("RAG_", "KEY=", "password", "Bearer ", "sk-", "gsk_"):
        assert bad not in text, bad


@test
def u04_closed_path_list_has_the_classic_tricks():
    for must in ("/metrics", "//metrics", "/%6Detrics", "/ask/../metrics", "/METRICS", "/readyz", "/docs", "/openapi.json", "/statusz"):
        assert must in mf.CLOSED_PATHS, must
    assert len(set(mf.CLOSED_PATHS)) == len(mf.CLOSED_PATHS)


@test
def u05_write_keeps_the_old_file():
    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "Caddyfile"), "w").write("old")
        p = mf.write_caddyfile(d)
        assert open(p + ".previous").read() == "old"
        assert "reverse_proxy rag:8000" in open(p).read()
        assert b"\r\n" not in open(p, "rb").read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# =================================================== behaviour (real Caddy) ==
@test
def d01_caddy_accepts_the_generated_file():
    if not CADDY:
        raise Skip("no caddy program on this computer")
    d = tempfile.mkdtemp()
    try:
        p = mf.write_caddyfile(d, upstream="127.0.0.1:1", address=":2")
        r = subprocess.run([CADDY, "validate", "--config", p, "--adapter", "caddyfile"], capture_output=True, text=True,
                           env=dict(os.environ, XDG_DATA_HOME=d, XDG_CONFIG_HOME=d, HOME=d))
        assert r.returncode == 0, r.stderr[-500:]
        assert "Valid configuration" in r.stderr + r.stdout
        for cmd in ([CADDY, "fmt", "--diff", p],):                      # the file is already in Caddy's own style
            out = subprocess.run(cmd, capture_output=True, text=True).stdout
            assert not [l for l in out.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")], out[:500]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
@with_door
def d02_the_two_allowed_requests_reach_the_api_and_get_one_door_request_id(d):
    s, h, b = raw(d.port, "/ask", "POST", {"Content-Type": "application/json", "X-API-Key": "k", "X-Request-ID": "client-supplied-id-1"}, b'{"question":"hi"}')
    assert s == 200 and b'"ok"' in b, (s, b)
    s2, h2, b2 = raw(d.port, "/healthz")
    assert s2 == 200, s2
    hits = d.api.hits
    assert [(x["method"], x["path"]) for x in hits] == [("POST", "/ask"), ("GET", "/healthz")], hits
    rid = hits[0]["headers"]["x-request-id"]
    assert rid != "client-supplied-id-1" and len(rid) == 36, rid                 # the DOOR decides the id, a client cannot choose it
    assert hits[0]["headers"]["x-api-key"] == "k"                                # the key is passed on to the API, which checks it
    assert hits[0]["body"] == len(b'{"question":"hi"}')


@test
@with_door
def d03_every_closed_spelling_gets_404_and_never_reaches_the_api(d):
    wrong = []
    for method in ("GET", "POST"):
        for p in mf.CLOSED_PATHS:
            body = b"{}" if method == "POST" else b""
            s, h, b = raw(d.port, p, method, {"X-Metrics-Key": "k", "X-API-Key": "k"}, body)
            if p in ("/healthz/",):                                              # (404 expected as well; listed so the loop is uniform)
                pass
            if s != 404 or b"SECRET" in b or b"rag_" in b:
                wrong.append((method, p, s))
    assert not wrong, wrong
    assert d.api.hits == [], d.api.seen_paths()
    assert len(mf.CLOSED_PATHS) * 2 >= 100


@test
@with_door
def d04_whatever_is_let_through_decodes_to_exactly_ask_or_healthz(d):
    # Spellings that DECODE to the allowed paths are fine to pass (the API decodes them the same way). Nothing else may.
    for p, method in (("/%61sk", "POST"), ("/%68ealthz", "GET"), ("/ask?x=1", "POST"), ("/healthz?x=1", "GET")):
        raw(d.port, p, method, {}, b"{}" if method == "POST" else b"")
    for h in d.api.hits:
        assert unquote(h["path"].split("?")[0]) in ("/ask", "/healthz"), h
    assert len(d.api.hits) == 4


@test
@with_door
def d05_other_methods_on_the_allowed_paths_are_404(d):
    for method, p in (("GET", "/ask"), ("PUT", "/ask"), ("DELETE", "/ask"), ("OPTIONS", "/ask"), ("PATCH", "/ask"),
                      ("POST", "/healthz"), ("PUT", "/healthz"), ("DELETE", "/healthz"), ("OPTIONS", "/healthz")):
        s, _, _ = raw(d.port, p, method, {}, b"{}" if method in ("POST", "PUT", "PATCH") else b"")
        assert s == 404, (method, p, s)
    assert d.api.hits == []


@test
@with_door
def d06_a_forged_forwarded_for_is_replaced_by_the_real_address(d):
    raw(d.port, "/healthz", "GET", {"X-Forwarded-For": "6.6.6.6", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"})
    h = d.api.hits[0]["headers"]
    assert "6.6.6.6" not in h.get("x-forwarded-for", ""), h
    assert h.get("x-forwarded-for", "").startswith("127.0.0.1"), h
    assert h.get("x-forwarded-host") != "evil.example", h


@test
@with_door
def d07_big_bodies_are_cut_at_the_door(d):
    body = b"x" * (1024 * 1024)
    s, _, _ = raw(d.port, "/ask", "POST", {}, body)
    assert s == 413, s
    s2, _, _ = raw(d.port, "/ask", "POST", {}, body, chunked=True)                # no declared size: the API's own check cannot see this one
    assert s2 in (413, 400, 0), s2
    assert all(h["body"] <= 21 * 1024 for h in d.api.hits), [h["body"] for h in d.api.hits]
    s3, _, _ = raw(d.port, "/ask", "POST", {}, b"y" * 15000)                      # a normal long question still passes
    assert s3 == 200, s3


@test
@with_door
def d08_response_headers_hide_the_server_and_forbid_caching(d):
    for target, method in (("/healthz", "GET"), ("/ask", "POST"), ("/nothing", "GET")):
        s, h, _ = raw(d.port, target, method, {}, b"{}" if method == "POST" else b"")
        assert "server" not in h, (target, h)
        assert h.get("x-content-type-options") == "nosniff", (target, h)
        assert h.get("cache-control") == "no-store", (target, h)


@test
@with_door
def d09_the_door_log_has_the_request_id_but_no_header_values(d):
    marks = {"X-API-Key": "MARK-API-KEY-12345", "Authorization": "Bearer MARK-BEARER-67890", "Cookie": "sid=MARK-COOKIE-abcde",
             "X-Metrics-Key": "MARK-METRICS-fghij"}
    raw(d.port, "/ask", "POST", marks, b"{}")
    raw(d.port, "/metrics", "GET", marks)
    deadline = time.time() + 5
    while time.time() < deadline and len(d.access_log()) < 2:
        time.sleep(0.1)
    log = d.access_log()
    assert len(log) == 2, d.lines[-5:]
    blob = json.dumps(log)
    for v in marks.values():
        assert v.split()[-1] not in blob, v
    assert '"headers"' not in blob
    ask = [l for l in log if l["request"]["uri"] == "/ask"][0]
    assert ask["status"] == 200 and ask["request"]["method"] == "POST"
    assert ask["request_id"] == d.api.hits[0]["headers"]["x-request-id"]        # the same id in the door log and in what the API received
    assert [l["status"] for l in log if l["request"]["uri"] == "/metrics"] == [404]


@test
@with_door
def d10_slow_clients_never_hold_the_api(d):
    # (a) a header that never finishes: the door closes the connection by itself
    s = socket.create_connection(("127.0.0.1", d.port), timeout=10)
    t0 = time.time()
    try:
        s.sendall(b"POST /ask HTTP/1.1\r\nHost: x\r\nContent-Le")
        s.settimeout(8)
        try:
            data = s.recv(100)
        except socket.timeout:
            data = b"STILL OPEN"
    finally:
        s.close()
    assert data != b"STILL OPEN" and time.time() - t0 < 6, (data, time.time() - t0)
    # (b) a body that stops half way: the API must never see the unfinished request. (Caddy gives up on it after read_body and
    # writes 499 in its own log; the idle TCP connection can linger until the client hangs up: a known limit, see the write-up.)
    s = socket.create_connection(("127.0.0.1", d.port), timeout=10)
    try:
        s.sendall(b"POST /ask HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nabc")
        time.sleep(3.5)
        assert d.api.hits == [], d.api.hits
        # (c) meanwhile the door still serves everybody else
        s2, _, _ = raw(d.port, "/healthz")
        assert s2 == 200
        log = [l for l in d.access_log() if l["request"]["uri"] == "/ask"]
        assert log and log[0]["status"] == 499, log
    finally:
        s.close()


@test
@with_door
def d11_when_the_api_is_down_the_answer_is_502_and_names_no_address(d):
    d.api.stop()
    s, h, b = raw(d.port, "/ask", "POST", {}, b"{}")
    assert s == 502, s
    assert b"127.0.0.1" not in b and str(d.api.port).encode() not in b, b
    assert "server" not in h and h.get("x-content-type-options") == "nosniff" and h.get("cache-control") == "no-store", h
    assert b == b"Bad Gateway", b
    s2, _, b2 = raw(d.port, "/healthz")
    assert s2 == 502
    d.api = StubApi()                      # so stop() has something to stop


@test
@with_door
def d12_head_on_healthz_works_for_uptime_services_that_use_it(d):
    s, h, b = raw(d.port, "/healthz", "HEAD")
    assert s == 200 and b == b"", (s, b)
    assert d.api.hits[-1]["method"] == "GET", d.api.hits


@test
@with_door
def d13_oversized_headers_are_refused(d):
    s, _, _ = raw(d.port, "/healthz", "GET", {"X-Junk": "j" * 40000})
    assert s in (431, 400, 0), s
    assert d.api.hits == []


@test
@with_door
def d14_fifty_parallel_requests_mixed_good_and_bad_all_get_the_right_answer(d):
    out, lock = [], threading.Lock()

    def one(i):
        if i % 3 == 0:
            r = raw(d.port, "/ask", "POST", {}, b"{}")
            exp = 200
        elif i % 3 == 1:
            r = raw(d.port, "/metrics", "GET")
            exp = 404
        else:
            r = raw(d.port, "/healthz")
            exp = 200
        with lock:
            out.append((r[0], exp))
    ts = [threading.Thread(target=one, args=(i,)) for i in range(50)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert len(out) == 50 and all(a == b for a, b in out), [o for o in out if o[0] != o[1]]
    assert all(unquote(p) in ("/ask", "/healthz") for p in d.api.seen_paths())


if __name__ == "__main__":
    bad = skipped = 0
    for name, status, tb in results:
        if status == "skip":
            skipped += 1
            print("SKIP", name, "-", tb)
            continue
        print(("PASS " if status == "pass" else "FAIL ") + name)
        if status == "fail":
            bad += 1
            print(tb)
    total = len(results) - skipped
    print(f"\n{total - bad}/{total} passed" + (f" ({skipped} skipped: no caddy program on this computer)" if skipped else ""))
    sys.exit(1 if bad else 0)
