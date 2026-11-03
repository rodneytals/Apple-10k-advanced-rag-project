"""Tests for the Windows printing problem.  Run:  python test_windows_pipes.py

On Windows, a program whose output goes into a pipe (a notebook cell that captures it, a scheduled task) can only print plain
characters: the tick and cross marks used in these tools make Python stop with UnicodeEncodeError, often AFTER the real work was done.
These tests force the same setting here (PYTHONIOENCODING=cp1252:strict) and check that every tool still prints its marks."""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backup_tools as bt

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


WINDOWS_PIPE = dict(os.environ, PYTHONIOENCODING="cp1252:strict", PYTHONUTF8="0")
TOOLS = ["backup_tools", "check_launch", "manage_keys", "tunnel_tools", "notifiers", "watchdog", "make_frontdoor"]
TEST_FILES = ["test_frontdoor", "test_backup", "test_notifiers", "test_watchdog", "test_launch_files", "test_check_launch", "test_manage_keys",
              "test_tunnel_tools", "test_windows_pipes"]


def run(*args, cwd=None):
    r = subprocess.run([sys.executable, *args], capture_output=True, env=WINDOWS_PIPE, cwd=cwd, timeout=120)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def project():
    d = tempfile.mkdtemp(prefix="pipes_")
    open(os.path.join(d, ".env"), "w").write("RAG_API_KEYS=lc-key-9f8e7d6c5b4a3210fedcba98:me\nRAG_METRICS_KEY=metrics-key-1f2e3d4c5b6a7988\n"
                                             "NTFY_TOPIC_SECRET=rag-alerts-topic-0123456789abcdef\n")
    return d


def fake_backup(root):
    folder = os.path.join(root, "20260101-120000")
    os.makedirs(os.path.join(folder, "checkpoints"))
    p = os.path.join(folder, "checkpoints", "a.db")
    c = sqlite3.connect(p)
    c.execute("create table t(x)")
    c.executemany("insert into t values (?)", [(i,) for i in range(5)])
    c.commit()
    c.close()
    json.dump({"created_at": 1.0, "parts": {"checkpoints": {"files": {"a.db": {"kind": "sqlite", "integrity": "ok", "tables": {"t": 5},
               "size": os.path.getsize(p), "sha256": bt.sha256_file(p)}}}}}, open(os.path.join(folder, "MANIFEST.json"), "w"))
    return folder


@test
def the_test_setting_really_reproduces_the_windows_crash():
    # control: go around the protection (call main() directly instead of running the file) and the crash must happen
    d = project()
    try:
        rc, out, err = run("-c", f"import sys; sys.path.insert(0, {HERE!r}); import notifiers; sys.exit(notifiers.main(['--env-file', {os.path.join(d, '.env')!r}, '--status']))")
        assert rc != 0 and "UnicodeEncodeError" in err, "the control did not crash: this test would prove nothing"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def notifiers_prints_its_marks_into_a_windows_pipe():
    d = project()
    try:
        rc, out, err = run(os.path.join(HERE, "notifiers.py"), "--env-file", os.path.join(d, ".env"), "--status")
        assert rc == 0 and "✅ ntfy" in out and "Error" not in err, (out, err)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def check_launch_prints_its_marks_into_a_windows_pipe():
    d = project()
    try:
        rc, out, err = run(os.path.join(HERE, "check_launch.py"), "--no-docker", "--project", d, "--door", "http://127.0.0.1:1")
        assert rc == 1 and "❌ cannot reach the front door" in out and "NOT READY" in out and "Error" not in err, (out, err)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def manage_keys_prints_its_marks_into_a_windows_pipe():
    d = project()
    try:
        rc, out, err = run(os.path.join(HERE, "manage_keys.py"), "--env", os.path.join(d, ".env"), "add", "friend")
        assert rc == 0 and "✅ new key for friend" in out and "Error" not in err, (out, err)
        assert "lc-key-9f8e7d6c5b4a3210fedcba98" not in out
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def backup_tools_prints_its_marks_into_a_windows_pipe():
    d = tempfile.mkdtemp(prefix="pipes_")
    try:
        folder = fake_backup(d)
        rc, out, err = run(os.path.join(HERE, "backup_tools.py"), "verify", folder)
        assert rc == 0 and "✅" in out and "Error" not in err, (out, err)
        rc, out, err = run(os.path.join(HERE, "backup_tools.py"), "verify", os.path.join(d, "nothing-here"))
        assert rc == 1 and "❌" in out and "Error" not in err, (out, err)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def every_tool_asks_for_utf8_in_its_main_block_and_every_test_file_does_at_the_top():
    for name in TOOLS:
        src = open(os.path.join(HERE, name + ".py"), encoding="utf-8").read()
        m = re.search(r'^if __name__ == "__main__":\n((?:    .*\n)+)', src, re.M)
        assert m and m.group(1).lstrip().startswith("_utf8_stdio()"), f"{name}.py: _utf8_stdio() must be the first thing its main block does"
        assert "stream.reconfigure(encoding=\"utf-8\"" in src, name
    for name in TEST_FILES:
        head = open(os.path.join(HERE, name + ".py"), encoding="utf-8").read()[:1400]
        assert "_stream.reconfigure(encoding=\"utf-8\"" in head, f"{name}.py: no UTF-8 line at the top"


passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
