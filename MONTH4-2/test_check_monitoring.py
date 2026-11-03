"""Tests for check_monitoring.py.  Run:  python test_check_monitoring.py
The checker is run against a live server, with a well-behaved pipeline and with pipelines that misbehave on purpose:
each failure it exists to catch must be CAUGHT."""
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_monitoring as cm
from testkit import Live, API_KEY, METRICS_KEY, SECRET, DB_PASSWORD, free_port

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


def run(L, secrets=(SECRET, DB_PASSWORD), **kw):
    kw.setdefault("log_wait_seconds", 3.0)
    return cm.check_monitoring(L.url, API_KEY, METRICS_KEY, secrets, get_logs=lambda: L.buf.getvalue(), question="What was net income?", **kw)


@test
def a_healthy_service_passes_everything():
    L = Live()
    try:
        rep = run(L)
        print(rep.summary())
        assert rep.ok and not rep.warned, rep.summary()
        assert "MONITORING WORKS" in rep.summary()
    finally:
        L.stop()


@test
def a_pipeline_that_logs_the_question_is_caught():
    L = Live()
    try:
        L.pipeline.state["leak"] = {"question"}
        rep = run(L)
        assert not rep.ok and any("the question text appears in the logs" in x for x in rep.failed), rep.summary()
    finally:
        L.stop()


@test
def a_pipeline_that_logs_a_secret_is_blanked_by_the_filter_so_it_passes():
    L = Live()
    try:
        L.pipeline.state["leak"] = {"secret"}
        rep = run(L)
        assert rep.ok, rep.summary()
        assert SECRET not in L.buf.getvalue() and "[redacted]" in L.buf.getvalue()
    finally:
        L.stop()


@test
def a_secret_the_filter_does_not_know_about_is_caught():
    L = Live(GROQ_API_KEY="")                                      # the filter was never told this value...
    try:
        L.pipeline.state["leak"] = {"secret"}                      # ...and the pipeline prints it
        rep = run(L)
        assert not rep.ok and any("secret #1" in x for x in rep.failed), rep.summary()
    finally:
        L.stop()


@test
def a_pipeline_that_logs_the_answer_is_caught():
    L = Live()
    try:
        L.pipeline.state["leak"] = {"answer"}
        rep = run(L)
        assert not rep.ok and any("answer" in x for x in rep.failed), rep.summary()
    finally:
        L.stop()


@test
def a_pipeline_that_logs_only_the_start_of_the_answer_is_caught():
    L = Live()
    try:
        L.pipeline.state["leak"] = {"answer_part"}
        rep = run(L)
        assert not rep.ok and any("start of the answer" in x for x in rep.failed), rep.summary()
    finally:
        L.stop()


@test
def missing_token_counts_is_a_warning_not_a_failure():
    L = Live()
    try:
        L.pipeline.state["no_tokens"] = True
        rep = run(L)
        assert rep.ok and any("no tokens were counted" in w for w in rep.warned), rep.summary()
    finally:
        L.stop()


@test
def no_metrics_key_is_explained():
    L = Live(RAG_METRICS_KEY="")
    try:
        rep = run(L)
        assert not rep.ok and "RAG_METRICS_KEY is not set" in rep.failed[0], rep.summary()
    finally:
        L.stop()


@test
def an_unreachable_service_is_explained():
    rep = cm.check_monitoring(f"http://127.0.0.1:{free_port()}", API_KEY, METRICS_KEY, get_logs=lambda: "")
    assert not rep.ok and "cannot reach" in rep.failed[0] and "docker compose ps" in rep.failed[0]


@test
def a_wrong_metrics_key_is_reported():
    L = Live()
    try:
        rep = cm.check_monitoring(L.url, API_KEY, "not-the-metrics-key-at-all", (), get_logs=lambda: L.buf.getvalue())
        assert not rep.ok and "opens with the metrics key" in rep.failed[0], rep.summary()
    finally:
        L.stop()


@test
def logs_that_never_arrive_are_reported():
    L = Live()
    try:
        rep = cm.check_monitoring(L.url, API_KEY, METRICS_KEY, (), get_logs=lambda: "", question="hi", log_wait_seconds=2)
        assert not rep.ok and any("no log line with request id" in x for x in rep.failed), rep.summary()
    finally:
        L.stop()


@test
def an_api_key_used_as_metrics_key_would_be_refused_by_the_server_and_by_the_checker():
    L = Live()
    try:
        rep = cm.check_monitoring(L.url, API_KEY, API_KEY, (), get_logs=lambda: L.buf.getvalue())
        assert not rep.ok, rep.summary()
    finally:
        L.stop()


@test
def keys_are_read_from_the_env_file():
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".env")
    open(p, "w").write(f"RAG_API_KEYS={API_KEY}:rodney,other-key-other-key-1234:friend\nRAG_METRICS_KEY={METRICS_KEY}\nGROQ_API_KEY={SECRET}\nRAG_HOST_PORT=9090\n")
    k = cm.keys_from_env(p)
    assert k["api_key"] == API_KEY and k["user"] == "rodney" and k["metrics_key"] == METRICS_KEY
    assert SECRET in k["secrets"] and k["base_url"] == "http://127.0.0.1:9090"


if __name__ == "__main__":
    bad = 0
    for name, ok, tb in results:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
            print(tb)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)
