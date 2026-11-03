"""Tests for log_report.py.  Run:  python test_log_report.py"""
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import log_report as lr
from testkit import Live, API_KEY, API_KEY2

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


LOG = None


@test
def make_traffic():
    global LOG
    L = Live(RAG_REQUEST_TIMEOUT_SECONDS="1", RAG_MAX_CONCURRENT="2", RAG_SLOW_SECONDS="0.3")
    try:
        for i in range(5):
            assert L.ask(f"normal question {i}", session=f"s{i}").status_code == 200
        L.ask("WARN me")
        L.ask("BOOM one")
        L.ask("BOOM two")
        L.ask("x", key="bad-key-bad-key-bad-key")
        r = L.ask("SLOW 1.6", key=API_KEY2)                   # 504, then finishes late
        assert r.status_code == 504
        time.sleep(1.5)
        L.client.get("/healthz")
        L.metrics()
        time.sleep(0.3)
    finally:
        L.stop()
    raw = L.buf.getvalue()
    LOG = raw + "print() from inside the pipeline: not json\n" + raw.splitlines()[-1][:40] + "\n"          # a cut-off line is "other" too
    assert len(LOG.splitlines()) > 12


@test
def parse_separates_json_from_other_lines():
    ev, other = lr.parse(LOG)
    assert len(other) == 2 and any("not json" in x for x in other), other
    assert all("ts" in e and "level" in e for e in ev)


@test
def summary_numbers_are_right():
    ev, other = lr.parse(LOG)
    text = lr.summarize(ev, other)
    assert "ANSWERED QUESTIONS: 6 of 10" in text, text          # 5 normal + WARN answered; asks = 5+1+2+1(bad key)+1(slow)
    assert "2 x 502  RuntimeError at" in text, text
    assert "1 x timed out (504)" in text and "1 x wrong or missing key (401)" in text, text
    assert "finished AFTER the caller was told 504 (220 tokens still spent)" in text, text
    assert "answers that carried a warning: 1" in text
    assert "numeric/vector" in text
    assert "friend" in text and "rodney" in text and "TOKENS:" in text
    assert "STARTS AND STOPS" in text and "startup" in text and "shutdown" in text
    assert "OTHER LINES (not JSON): 2" in text
    assert "/healthz" not in text, "successful probes were never logged"
    print(text)


@test
def tokens_include_the_late_finish():
    ev, _ = lr.parse(LOG)
    late = [e for e in ev if e.get("event") == "pipeline_late_finish"]
    assert len(late) == 1 and late[0]["tokens_in"] == 200 and late[0]["tokens_out"] == 20
    total_in = sum(e.get("tokens_in", 0) for e in ev if e.get("event") in ("request", "pipeline_late_finish"))
    # 6 answered x 1500, 2 crashed x 10, the slow one: 100 on its 504 line + 200 on its late line = 300, counted once each
    assert total_in == 6 * 1500 + 2 * 10 + 300, total_in


@test
def trace_follows_one_request():
    ev, other = lr.parse(LOG)
    boom = [e for e in ev if e.get("event") == "request" and e.get("status") == 502][0]
    text = lr.trace(ev, other, boom["rid"])
    assert boom["rid"] in text and "error=RuntimeError" in text and "status=502" in text, text
    assert "nothing found" in lr.trace(ev, other, "does-not-exist-123")
    slow = [e for e in ev if e.get("event") == "pipeline_late_finish"][0]
    t2 = lr.trace(ev, other, slow["rid"])
    assert "request" in t2 and "pipeline_late_finish" in t2 and t2.index("status=504") < t2.index("outcome=ok"), t2


@test
def docker_prefix_utf16_and_bom_are_handled():
    ev0, _ = lr.parse(LOG)
    prefixed = "\n".join("rag-api  | " + l for l in LOG.splitlines())
    ev1, _ = lr.parse(prefixed)
    assert len(ev1) == len(ev0)
    ev2, _ = lr.parse(lr.decode(LOG.encode("utf-16")))
    assert len(ev2) == len(ev0)
    ev3, _ = lr.parse(lr.decode(b"\xef\xbb\xbf" + LOG.encode("utf-8")))
    assert len(ev3) == len(ev0)


@test
def since_keeps_only_recent_lines():
    ev, other = lr.parse(LOG)
    assert len(lr.filter_since(ev, 60)) == len(ev)
    assert 0 < len(lr.filter_since(ev, 0.01)) < len(ev)


@test
def percentile_is_nearest_rank():
    assert lr.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert lr.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert lr.percentile([], 50) is None and lr.percentile([7], 95) == 7


@test
def cli_reads_a_file_and_stdin():
    path = os.path.join(HERE, "work_test_log.txt")
    open(path, "wb").write(LOG.encode("utf-16"))
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "log_report.py"), path], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "ANSWERED QUESTIONS" in r.stdout, r.stderr
        r = subprocess.run([sys.executable, os.path.join(HERE, "log_report.py"), "-"], input=LOG.encode(), capture_output=True, timeout=60)
        assert r.returncode == 0 and b"ANSWERED QUESTIONS" in r.stdout
    finally:
        os.remove(path)


@test
def an_empty_log_does_not_crash():
    ev, other = lr.parse("")
    assert "0 lines" in lr.summarize(ev, other) and "PROBLEMS: none" in lr.summarize(ev, other)


if __name__ == "__main__":
    bad = 0
    for name, ok, tb in results:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
            print(tb)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)
