"""Tests for the Day 20 monitoring tools.  Run:  python test_monitoring.py
Real HTTP server (uvicorn in a thread) + real httpx calls; the pipeline is a fake that counts tokens the way yours does."""
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
import types
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import httpx
import uvicorn

import api_layer
import monitoring as mon
import serve
from usage_context import UsageProxy
from testkit import (API_KEY, API_KEY2, METRICS_KEY, SECRET, DB_PASSWORD, Usage, make_token_usage_class, make_pipeline,
                     free_port, Live, sample)

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


LIVE = None


def eventually(cond, message="condition never became true", seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(message)


# =================================================================== units ===
@test
def u01_request_id_pattern_matches_api_layer():
    assert mon.REQUEST_ID_PATTERN.pattern == api_layer.REQUEST_ID_PATTERN.pattern


@test
def u02_counter_gauge_histogram_text_is_valid_prometheus():
    from prometheus_client.parser import text_string_to_metric_families
    r = mon.Registry()
    c = r.counter("t_total", "a counter", ("route",))
    g = r.gauge("t_gauge", "a gauge", ("name",))
    h = r.histogram("t_seconds", "a histogram", ("route",), buckets=(0.1, 1, 10))
    c.inc(route="/ask")
    c.inc(2, route="/ask")
    g.set(1.5, name='we"ird\\')
    for v in (0.05, 0.5, 5, 50):
        h.observe(v, route="/ask")
    fams = {f.name: f for f in text_string_to_metric_families(r.render())}
    assert sample(r.render(), "t_total", route="/ask") == 3
    assert sample(r.render(), "t_seconds_count", route="/ask") == 4
    assert sample(r.render(), "t_seconds_bucket", route="/ask", le="1.0") == 2          # cumulative: 0.05 and 0.5
    assert sample(r.render(), "t_seconds_bucket", route="/ask", le="+Inf") == 4
    assert abs(sample(r.render(), "t_seconds_sum", route="/ask") - 55.55) < 1e-9
    assert "t_gauge" in fams


@test
def u03_counter_cannot_go_down():
    c = mon.Counter("x_total", "x")
    try:
        c.inc(-1)
    except ValueError:
        return
    raise AssertionError("negative increment accepted")


@test
def u04_cardinality_cap_collapses_new_labels_into_other():
    c = mon.Counter("cap_total", "x", ("route",), max_series=5)
    for i in range(50):
        c.inc(route=f"/p{i}")
    assert len(c._data) <= 6, len(c._data)                 # 5 real + the "other" bucket
    assert c.value(route="other") == 45, c.value(route="other")
    assert sum(c._data.values()) == 50


@test
def u05_clean_label():
    assert mon.clean_label("numeric") == "numeric"
    assert mon.clean_label(None) == "none"
    assert mon.clean_label("has \"quote\"") == "other"
    assert mon.clean_label("x" * 100) == "other"
    assert mon.clean_label("line\nbreak") == "other"


@test
def u06_unknown_label_name_is_an_error():
    c = mon.Counter("lbl_total", "x", ("a",))
    try:
        c.inc(b="1")
    except ValueError:
        return
    raise AssertionError("unknown label accepted")


@test
def u07_collect_secrets():
    env = {"RAG_API_KEYS": "abcdefghijklmnop:rodney,qrstuvwxyz123456:friend", "GROQ_API_KEY": "gsk_abcdefgh", "PORT": "8000",
           "MONGO_URI": "mongodb+srv://u:p@h/db", "REDIS_HOST": "redis", "FLAG_SECRET": "true", "SHORT_TOKEN": "abc",
           "RAG_HOST": "0.0.0.0", "NEO4J_URI": "neo4j+s://abc.databases.neo4j.io"}
    got = set(mon.collect_secrets(env))
    assert {"abcdefghijklmnop", "qrstuvwxyz123456", "gsk_abcdefgh", "mongodb+srv://u:p@h/db"} <= got, got
    assert "rodney" not in got and "redis" not in got and "8000" not in got and "abc" not in got
    assert "neo4j+s://abc.databases.neo4j.io" not in got          # a URL with no credentials in it is not a secret


@test
def u08_redactor_handles_json_escaped_secret():
    r = mon.Redactor(['pa"ss\\word-123456'])
    out = r.clean(json.dumps({"x": 'pa"ss\\word-123456'}))
    assert "word-123456" not in out and "[redacted]" in out, out


@test
def u09_json_formatter_no_traceback_text_and_redacts():
    buf = io.StringIO()
    mon.configure_logging({"GROQ_API_KEY": SECRET}, stream=buf)
    log = logging.getLogger("unit")
    log.warning("value is %s", SECRET)
    try:
        raise ValueError(f"bad thing {SECRET}")
    except ValueError:
        log.exception("it failed")
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert lines[0]["msg"] == "value is [redacted]", lines[0]
    assert lines[1]["exc_class"] == "ValueError" and "test_monitoring.py" in lines[1]["exc_at"], lines[1]
    assert "bad thing" not in buf.getvalue() and SECRET not in buf.getvalue()
    assert "Traceback" not in buf.getvalue()


@test
def u10_error_messages_are_opt_in_and_still_redacted():
    buf = io.StringIO()
    mon.configure_logging({"GROQ_API_KEY": SECRET, "RAG_LOG_ERROR_MESSAGES": "1"}, stream=buf)
    try:
        raise ValueError(f"bad thing {SECRET}")
    except ValueError:
        logging.getLogger("unit").exception("it failed")
    d = json.loads(buf.getvalue().splitlines()[0])
    assert "bad thing" in d["exc_message"] and SECRET not in buf.getvalue(), d


@test
def u11_text_format_option_works_and_hides_traceback():
    buf = io.StringIO()
    mon.configure_logging({"GROQ_API_KEY": SECRET, "RAG_LOG_FORMAT": "text"}, stream=buf)
    mon.log_event(logging.getLogger("rag.request"), logging.INFO, "request", rid="abc", status=200)
    try:
        raise ValueError(f"bad {SECRET}")
    except ValueError:
        logging.getLogger("unit").exception("failed")
    out = buf.getvalue()
    assert "request rid=abc status=200" in out and "ValueError" in out and "bad " not in out and SECRET not in out, out


@test
def u12_validate_metrics_key():
    for bad, keys in (("short", []), (API_KEY, [API_KEY])):
        try:
            mon.validate_metrics_key(bad, keys)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    mon.validate_metrics_key(METRICS_KEY, [API_KEY])


@test
def u13_context_executor_copies_context():
    import contextvars
    v = contextvars.ContextVar("v", default="none")
    ex = mon.ContextExecutor(max_workers=1)
    v.set("from-caller")
    assert ex.submit(v.get).result() == "from-caller"
    plain = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=1)
    assert plain.submit(v.get).result() == "none"            # the problem the class solves
    ex.shutdown()
    plain.shutdown()


@test
def u15_where_shows_the_library_frame_and_your_own_frame():
    lib = {}
    exec(compile("def boom():\n    raise ConnectionError('secret')\n", "/env/lib/site-packages/libx.py", "exec"), lib)
    mine = {"boom": lib["boom"]}
    exec(compile("def run_with_entity_memory():\n    boom()\n", "/app/rag_pipeline/pipeline.py", "exec"), mine)
    exec(compile("def wrapper():\n    run_with_entity_memory()\n", "/app/serve.py", "exec"), mine)
    mine["run_with_entity_memory"].__globals__.update(mine)
    try:
        mine["wrapper"]()
    except ConnectionError as e:
        w = mon.where(e)
    assert w == "libx.py:2:boom < pipeline.py:2:run_with_entity_memory", w


@test
def u14_where_has_no_message():
    try:
        raise KeyError("secret-thing")
    except KeyError as e:
        w = mon.where(e)
    assert "secret-thing" not in w and w.startswith("test_monitoring.py:"), w


# =============================================================== live HTTP ===
@test
def l00_start_server():
    global LIVE
    LIVE = Live()
    r = LIVE.client.get("/healthz")
    assert r.status_code == 200
    starts = LIVE.events("startup")
    assert starts and starts[0]["slots"] == 3 and starts[0]["metrics_enabled"] is True, LIVE.lines()[:5]
    assert starts[0]["watching_secrets"] >= 4, starts


@test
def l01_one_json_line_per_request_with_the_right_facts():
    L = LIVE
    q = "What was Apple's net income? MARKER-QUESTION-TEXT"
    r = L.ask(q, session="my-private-session-77")
    assert r.status_code == 200, r.text
    rid = r.headers["x-request-id"]
    ev = L.events("request", rid=rid)
    assert len(ev) == 1, ev
    e = ev[0]
    assert e["route"] == "/ask" and e["method"] == "POST" and e["status"] == 200, e
    assert e["user"] == "rodney" and len(e["session"]) == 10 and e["session"] != "my-private-session-77"
    assert e["tokens_in"] == 1500 and e["tokens_out"] == 75 and e["llm_calls"] == 2, e
    assert abs(e["cost_usd"] - (1500e-6 + 75 * 2e-6)) < 1e-9, e
    assert e["classification"] == "numeric" and e["retrieval_source"] == "vector" and e["reviewer_verdict"] == "pass", e
    assert e["duration_ms"] >= 0 and e["pipeline_ms"] >= 0 and "queue_ms" in e
    assert e["level"] == "INFO" and e["ts"].endswith("Z")
    assert r.json()["request_id"] == rid


@test
def l02_nothing_private_in_the_logs_or_metrics():
    L = LIVE
    text = L.buf.getvalue() + L.metrics()
    for forbidden in ("MARKER-QUESTION-TEXT", "ANSWER-TEXT", "my-private-session-77", SECRET, DB_PASSWORD, API_KEY, API_KEY2, METRICS_KEY):
        assert forbidden not in text, f"leaked: {forbidden[:12]}..."


@test
def l03_metrics_need_the_key_and_parse():
    L = LIVE
    assert L.client.get("/metrics").status_code == 401
    assert L.client.get("/metrics", headers={"X-Metrics-Key": "wrong-key-wrong-key-wrong"}).status_code == 401
    assert L.client.get("/metrics", headers={"X-API-Key": API_KEY}).status_code == 401            # an API key is not a metrics key
    r = L.client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_KEY}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4"), r.headers
    t = r.text
    assert sample(t, "rag_http_requests_total", route="/ask", status="200") >= 1
    assert sample(t, "rag_llm_tokens_total", kind="prompt") >= 1500
    assert sample(t, "rag_llm_tokens_total", kind="completion") >= 75
    assert sample(t, "rag_llm_cost_usd_total") > 0
    assert sample(t, "rag_answers_total", classification="numeric", retrieval_source="vector", reviewer_verdict="pass") >= 1
    assert sample(t, "rag_pipeline_slots") == 3
    assert sample(t, "rag_dependency_up", name="redis") == 1
    assert sample(t, "rag_process_start_time_seconds") > 1e9
    assert sample(t, "rag_build_info", version="dev") == 1
    assert sample(t, "rag_monitoring_errors_total") in (None, 0), "monitoring hit an error"


@test
def l04_metrics_calls_are_not_logged_line_by_line_but_failures_are():
    L = LIVE
    before = len(L.events("request", route="/metrics")) + len(L.events("request", route="/healthz"))
    for _ in range(5):
        L.client.get("/healthz")
        L.metrics()
    after = len(L.events("request", route="/metrics")) + len(L.events("request", route="/healthz"))
    assert after == before, "successful probes should not be logged"
    n401 = len(L.events("request", route="/metrics", status=401))
    L.client.get("/metrics")                                                   # a rejected scrape IS logged
    eventually(lambda: len(L.events("request", route="/metrics", status=401)) == n401 + 1)


@test
def l05_bad_api_key_is_logged_without_the_key():
    L = LIVE
    bad = "this-is-a-wrong-api-key-XYZ-123"
    r = L.ask("hello", key=bad)
    assert r.status_code == 401
    ev = L.events("request", rid=r.headers["x-request-id"])[0]
    assert ev["status"] == 401 and "user" not in ev and "pipeline_ms" not in ev, ev
    assert bad not in L.buf.getvalue()


@test
def l06_pipeline_crash_logs_class_and_place_not_message():
    L = LIVE
    r = L.ask("BOOM now")
    assert r.status_code == 502, r.text
    ev = L.events("request", rid=r.headers["x-request-id"])[0]
    assert ev["error"] == "RuntimeError" and "testkit.py" in ev["error_at"] and ev["level"] == "ERROR", ev
    assert ev["tokens_in"] == 10, ev                                           # the tokens spent before the crash are still counted
    assert DB_PASSWORD not in L.buf.getvalue() and "connection failed" not in L.buf.getvalue()
    t = L.metrics()
    assert sample(t, "rag_pipeline_calls_total", outcome="error") >= 1
    assert sample(t, "rag_http_requests_total", route="/ask", status="502") >= 1


@test
def l07_unknown_paths_do_not_create_unbounded_series():
    L = LIVE
    for i in range(300):
        L.client.get(f"/scan/{i}.php")
    t = L.metrics()
    routes = {s for s in re.findall(r'rag_http_requests_total\{route="([^"]+)"', t)}
    assert routes <= {"/ask", "/healthz", "/readyz", "/metrics", "/statusz", "unmatched", "other"}, routes
    assert sample(t, "rag_http_requests_total", route="unmatched", status="404") >= 300
    assert len(t) < 40000, len(t)


@test
def l08_request_id_is_the_same_everywhere():
    L = LIVE
    r = L.ask("hi", **{"X-Request-ID": "caller-chosen-id-12345"})
    assert r.headers["x-request-id"] == "caller-chosen-id-12345" and r.json()["request_id"] == "caller-chosen-id-12345"
    assert L.events("request", rid="caller-chosen-id-12345")
    r = L.ask("hi", **{"X-Request-ID": "bad id with spaces!"})
    rid = r.headers["x-request-id"]
    assert rid != "bad id with spaces!" and r.json()["request_id"] == rid and L.events("request", rid=rid), rid
    r = L.client.get("/healthz")                                               # even a probe gets an id
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])


@test
def l09_overlapping_requests_each_keep_their_own_numbers():
    """The worker threads must see THEIR request's record (ContextExecutor), not another one's."""
    L = LIVE
    qs = {"MEDIUM aaaaaaaaaa": 17, "MEDIUM bbbbbbbbbbbbbbbbbbbbbb": 29, "MEDIUM c": 8}   # tokens_in = len(question)
    with ThreadPoolExecutor(3) as ex:
        futs = {q: ex.submit(L.ask, q, API_KEY if i % 2 else API_KEY2, f"sess{i}") for i, q in enumerate(qs)}
    for i, (q, n) in enumerate(qs.items()):
        r = futs[q].result()
        assert r.status_code == 200, r.text
        e = L.events("request", rid=r.headers["x-request-id"])[0]
        assert e["tokens_in"] == len(q) == n and e["tokens_out"] == 7 and e["llm_calls"] == 1, (q, e)
        assert e["user"] == ("rodney" if i % 2 else "friend"), e


@test
def l10_slow_call_gets_504_then_a_late_finish_line_with_real_totals():
    L = LIVE
    L.server.config  # (the server is already running with a 10 s timeout; use a second server for a short one)
    S = Live(share=L.buf, RAG_REQUEST_TIMEOUT_SECONDS="1", RAG_MAX_CONCURRENT="1", RAG_QUEUE_WAIT_SECONDS="1")
    try:
        r = S.ask("SLOW 2.5")
        assert r.status_code == 504, r.text
        rid = r.headers["x-request-id"]
        ev = S.events("request", rid=rid)[0]
        assert ev["status"] == 504 and ev["pipeline_still_running"] is True and ev["level"] == "WARNING", ev
        assert ev["tokens_in"] == 100, ev                                      # only the first add had happened by then
        # while it is still running: the slot is taken, so a second request is refused with 503 "busy"
        r2 = S.ask("hello again")
        assert r2.status_code == 503, r2.text
        assert S.events("request", rid=r2.headers["x-request-id"])[0]["level"] == "WARNING"
        t = S.metrics()
        assert sample(t, "rag_pipeline_in_flight") == 1 and sample(t, "rag_pipeline_abandoned_total") == 1, t
        deadline = time.time() + 10
        while time.time() < deadline and not S.events("pipeline_late_finish", rid=rid):
            time.sleep(0.1)
        late = S.events("pipeline_late_finish", rid=rid)
        assert len(late) == 1, S.lines()[-6:]
        late = late[0]
        assert late["outcome"] == "ok" and late["tokens_in"] == 200 and late["tokens_out"] == 20, late    # only the part after the 504 line
        assert late["after_response_ms"] > 0 and late["level"] == "WARNING"
        t = S.metrics()
        assert sample(t, "rag_pipeline_in_flight") == 0, "slot was never released"
        assert sample(t, "rag_pipeline_calls_total", outcome="ok") == 1
        assert sample(t, "rag_llm_tokens_total", kind="prompt") == 300, "tokens spent after the timeout must still be counted"
        assert sample(t, "rag_http_requests_total", route="/ask", status="503") == 1
    finally:
        S.stop()


@test
def l11_dependency_down_shows_in_metrics_and_recovers():
    L = LIVE
    L.pipeline.state["redis_down"] = True
    t = L.metrics()
    assert sample(t, "rag_dependency_up", name="redis") == 0 and sample(t, "rag_dependency_up", name="mongo") == 1, t
    assert sample(t, "rag_dependency_check_failures_total", name="redis") >= 1
    assert DB_PASSWORD not in t
    assert L.client.get("/readyz").status_code == 503
    eventually(lambda: L.events("request", route="/readyz", status=503), "a failing readiness probe must be logged")
    L.pipeline.state["redis_down"] = False
    assert sample(L.metrics(), "rag_dependency_up", name="redis") == 1


@test
def l12_resource_gauges_exist_on_linux():
    t = LIVE.metrics()
    assert sample(t, "rag_process_threads") >= 2
    assert sample(t, "rag_disk_free_bytes", path=".") > 0
    if os.path.exists("/proc/self/statm"):
        assert sample(t, "rag_process_resident_memory_bytes") > 1e6 and sample(t, "rag_process_open_fds") > 3


@test
def l13_warning_answers_are_counted():
    L = LIVE
    assert L.ask("WARN me").status_code == 200
    eventually(lambda: [x for x in L.events("request") if x.get("warning") is True], "warning flag missing from the request line")
    assert sample(L.metrics(), "rag_answer_warnings_total") >= 1


@test
def l14_statusz_and_old_behaviour_unchanged():
    L = LIVE
    r = L.client.get("/statusz", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200 and set(r.json()) == {"in_flight", "waiting", "max_concurrent"}, r.json()
    assert L.client.get("/statusz").status_code == 401
    body = L.ask("plain question").json()
    assert set(body) == {"request_id", "session_id", "answer", "classification", "retrieval_source", "reviewer_verdict",
                         "warning", "langsmith_run_id", "elapsed_ms"}, body                                   # whitelist intact
    assert "internal_secret" not in body


@test
def l14b_bounded_containers_show_up_when_make_bounded_was_run():
    import bounded
    sys.modules["fakepkg.bounded"] = bounded
    p = make_pipeline()
    p.__package__ = "fakepkg"
    keep = bounded.BoundedList(maxlen=3, name="EVENT_LOG_TEST")
    keep.extend(range(10))
    app, _ = serve.build_app({"RAG_API_KEYS": f"{API_KEY}:rodney", "RAG_METRICS_KEY": METRICS_KEY}, p)
    text = app.state.monitor.render()
    assert sample(text, "rag_bounded_items", name="EVENT_LOG_TEST") == 3, text
    assert sample(text, "rag_bounded_dropped", name="EVENT_LOG_TEST") == 7
    del sys.modules["fakepkg.bounded"]


@test
def l15_monitoring_never_broke_itself():
    t = LIVE.metrics()
    assert sample(t, "rag_monitoring_errors_total") in (None, 0)
    assert not [d for d in LIVE.lines() if "_raw" in d], [d for d in LIVE.lines() if "_raw" in d][:3]       # every line is JSON


@test
def l16_shutdown_event_is_logged():
    L = LIVE
    L.stop()
    ev = L.events("shutdown")
    assert ev and ev[-1]["in_flight"] == 0 and "uptime_s" in ev[-1], L.lines()[-3:]


# ------------------------------------------------------------ configuration --
@test
def c01_no_metrics_key_means_no_metrics_page():
    L = Live(RAG_METRICS_KEY="")
    try:
        assert L.client.get("/metrics", headers={"X-Metrics-Key": METRICS_KEY}).status_code == 404
        assert L.ask("hi").status_code == 200
        eventually(lambda: L.events("request", route="/ask"), "logs must still work without a metrics key")
        assert L.events("startup")[0]["metrics_enabled"] is False
    finally:
        L.stop()


@test
def c02_bad_metrics_keys_stop_the_server_from_starting():
    for bad in ("short", API_KEY):
        env = {"RAG_API_KEYS": f"{API_KEY}:rodney", "RAG_METRICS_KEY": bad}
        try:
            serve.build_app(env, make_pipeline())
        except SystemExit as e:
            assert "RAG_METRICS_KEY" in str(e)
            continue
        raise AssertionError(f"server started with metrics key {bad!r}")


@test
def c03_monitoring_can_be_switched_off():
    env = {"RAG_API_KEYS": f"{API_KEY}:rodney", "RAG_MONITORING": "0", "RAG_METRICS_KEY": METRICS_KEY}
    app, cfg = serve.build_app(env, make_pipeline())
    assert app.state.monitor is None
    assert all(getattr(r, "path", "") != "/metrics" for r in app.routes)


@test
def c04_two_apps_in_one_process_do_not_share_counters():
    a, _ = serve.build_app({"RAG_API_KEYS": f"{API_KEY}:rodney", "RAG_METRICS_KEY": METRICS_KEY}, make_pipeline())
    b, _ = serve.build_app({"RAG_API_KEYS": f"{API_KEY}:rodney", "RAG_METRICS_KEY": METRICS_KEY}, make_pipeline())
    a.state.monitor.http_requests.inc(route="/ask", method="POST", status="200")
    assert b.state.monitor.http_requests.total() == 0


if __name__ == "__main__":
    bad = 0
    for name, ok, tb in results:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
            print(tb)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)
