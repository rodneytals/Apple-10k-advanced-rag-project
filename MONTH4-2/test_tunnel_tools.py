"""Tests for tunnel_tools.py.  Run:  python test_tunnel_tools.py"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tunnel_tools as tt

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


BOX = """2026-10-22T05:00:01Z INF Thank you for trying Cloudflare Tunnel.
2026-10-22T05:00:03Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-22T05:00:05Z INF +--------------------------------------------------------------------------------------------+
2026-10-22T05:00:05Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-10-22T05:00:05Z INF |  https://quiet-river-fox-demo.trycloudflare.com                                            |
2026-10-22T05:00:05Z INF +--------------------------------------------------------------------------------------------+
tunnel-1  | 2026-10-22T05:00:05Z INF Cannot determine default configuration path. See https://developers.cloudflare.com/ for help
"""


def env(text):
    p = os.path.join(tempfile.mkdtemp(), ".env")
    open(p, "wb").write(text.encode())
    return p


@test
def finds_the_address_in_cloudflareds_boxed_log_and_ignores_other_links():
    assert tt.find_tunnel_url(BOX) == "https://quiet-river-fox-demo.trycloudflare.com"
    assert tt.find_tunnel_url("see https://developers.cloudflare.com/ and https://trycloudflare.com/") is None
    assert tt.find_tunnel_url("") is None and tt.find_tunnel_url(None) is None


@test
def after_a_restart_the_newest_address_wins():
    log = BOX + "\n2026-10-22T09:00:05Z INF |  https://other-name-here-two.trycloudflare.com  |\n"
    assert tt.find_tunnel_url(log) == "https://other-name-here-two.trycloudflare.com"


@test
def set_env_value_adds_changes_and_leaves_the_rest_alone():
    p = env("A=1\nPUBLIC_URL=https://old-one.trycloudflare.com\nB=2\n")
    assert tt.set_env_value(p, "PUBLIC_URL", "https://new-one.trycloudflare.com") == "changed"
    assert open(p).read() == "A=1\nPUBLIC_URL=https://new-one.trycloudflare.com\nB=2\n"
    assert open(p + ".before_public_url").read().startswith("A=1\nPUBLIC_URL=https://old-one")
    assert tt.set_env_value(p, "PUBLIC_URL", "https://new-one.trycloudflare.com") == "unchanged"
    q = env("A=1\nB=2")                                           # no newline at the end: never glue onto the last line
    assert tt.set_env_value(q, "PUBLIC_URL", "https://x-y-z.trycloudflare.com") == "added"
    assert open(q).read() == "A=1\nB=2\nPUBLIC_URL=https://x-y-z.trycloudflare.com\n"
    r = env("A=1\n")
    tt.set_env_value(r, "PUBLIC_URL", "https://x-y-z.trycloudflare.com")
    assert open(r).read() == "A=1\nPUBLIC_URL=https://x-y-z.trycloudflare.com\n"


@test
def windows_line_endings_a_bom_and_similar_names_are_handled():
    p = env("A=1\r\nPUBLIC_URL_OLD=keep\r\nPUBLIC_URL=https://old-one.trycloudflare.com\r\n")
    tt.set_env_value(p, "PUBLIC_URL", "https://new-one.trycloudflare.com")
    assert open(p, "rb").read() == b"A=1\r\nPUBLIC_URL_OLD=keep\r\nPUBLIC_URL=https://new-one.trycloudflare.com\r\n"
    q = os.path.join(tempfile.mkdtemp(), ".env")
    open(q, "wb").write(b"\xef\xbb\xbfA=1\n")
    tt.set_env_value(q, "PUBLIC_URL", "https://x-y-z.trycloudflare.com")
    assert open(q, "rb").read() == b"A=1\nPUBLIC_URL=https://x-y-z.trycloudflare.com\n"


@test
def wait_for_url_polls_ignores_the_old_address_and_gives_up():
    state = {"n": 0, "t": 0.0}
    def logs():
        state["n"] += 1
        return "" if state["n"] < 3 else BOX
    got = tt.wait_for_url(logs, 30, sleep=lambda s: state.update(t=state["t"] + s), clock=lambda: state["t"])
    assert got == "https://quiet-river-fox-demo.trycloudflare.com" and state["n"] == 3
    state.update(t=0.0)
    assert tt.wait_for_url(lambda: BOX, 10, sleep=lambda s: state.update(t=state["t"] + s), clock=lambda: state["t"], since="https://quiet-river-fox-demo.trycloudflare.com/") is None


@test
def main_reports_a_missing_tunnel_without_touching_env():
    p = env("A=1\n")
    import subprocess
    real = subprocess.run
    subprocess.run = lambda *a, **k: type("R", (), {"stdout": "", "stderr": "no such service", "returncode": 1})()
    orig = tt.wait_for_url
    tt.wait_for_url = lambda get_logs, timeout=90, **k: orig(get_logs, 0, sleep=lambda s: None)
    try:
        assert tt.main([p]) == 1
    finally:
        subprocess.run, tt.wait_for_url = real, orig
    assert open(p).read() == "A=1\n"


@test
def cloudflares_own_addresses_in_error_lines_are_never_taken_for_a_tunnel():
    fail = ('2026-10-22T09:00:01Z ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: lookup api.trycloudflare.com: no such host\n'
            "2026-10-22T09:00:02Z INF see https://www.trycloudflare.com/ and https://dash.trycloudflare.com/\n")
    assert tt.find_tunnel_url(fail) is None
    assert tt.find_tunnel_url(fail + BOX) == "https://quiet-river-fox-demo.trycloudflare.com", "a real address after the errors is still found"
    assert tt.find_tunnel_url(BOX + fail) == "https://quiet-river-fox-demo.trycloudflare.com", "errors after a good address do not replace it"


@test
def a_failed_tunnel_prints_its_own_complaint_and_writes_nothing():
    import contextlib, io, subprocess
    fail = '2026-10-22T09:00:01Z ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: no such host\n'
    p = env("A=1\n")
    real = subprocess.run
    subprocess.run = lambda *a, **k: type("R", (), {"stdout": fail, "stderr": "", "returncode": 0})()
    orig = tt.wait_for_url
    tt.wait_for_url = lambda get_logs, timeout=90, **k: orig(get_logs, 0, sleep=lambda s: None)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = tt.main([p])
    finally:
        subprocess.run, tt.wait_for_url = real, orig
    assert rc == 1 and "failed to request quick Tunnel" in buf.getvalue() and open(p).read() == "A=1\n"


passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
