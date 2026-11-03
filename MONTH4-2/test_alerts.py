"""Tests for alert_rules.py.  Run:  python test_alerts.py
Snapshots are built from the REAL metrics code (monitoring.Registry), not typed by hand, so the parser and the rules are
tested against exactly what the service prints."""
import io
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alert_rules as ar
import monitoring as mon
from testkit import Live, METRICS_KEY, API_KEY, free_port

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


def fresh():
    """A Monitor with no server; returns (monitor, take) where take(ts) renders and parses its metrics."""
    m = mon.Monitor({})
    m.runner = None
    m.registry._collectors.clear()           # no live process numbers: the tests set the gauges themselves

    def take(ts):
        return ar.Snapshot(ts, ar.parse_metrics(m.render()))
    return m, take


def ask(m, status="200", n=1, seconds=1.0):
    for _ in range(n):
        m.http_requests.inc(route="/ask", method="POST", status=status)
        m.http_duration.observe(seconds, route="/ask")


def names(alerts):
    return sorted(a.name for a in alerts)


@test
def parser_round_trips_the_real_output():
    m, take = fresh()
    ask(m, n=3)
    m.dep_up.set(1, name="redis")
    m.tokens.inc(5, kind="prompt")
    snap = take(0)
    assert snap.get("rag_http_requests_total", route="/ask", status="200") == 3
    assert snap.get("rag_dependency_up", name="redis") == 1
    assert snap.get("rag_http_request_duration_seconds_bucket", route="/ask", le="+Inf") == 3
    assert snap.get("nope") is None


@test
def parser_handles_escaped_labels_and_special_values():
    s = ar.parse_metrics('# HELP x y\nx{a="we\\"ird\\\\",b="2"} 3\ny +Inf\nz{q="1"} NaN\n\nbad line here\n')
    assert s[("x", (("a", 'we"ird\\'), ("b", "2")))] == 3
    assert s[("y", ())] == float("inf")


@test
def delta_counts_from_zero_after_a_restart():
    a = ar.Snapshot(0, {("c_total", ()): 100.0})
    b = ar.Snapshot(1, {("c_total", ()): 4.0})
    assert ar.delta(a, b, "c_total") == 4.0                       # went down => restarted
    assert ar.delta(None, b, "c_total") == 0.0
    c = ar.Snapshot(2, {("c_total", ()): 9.0})
    assert ar.delta(b, c, "c_total") == 5.0


@test
def quantile_matches_a_known_distribution():
    m, take = fresh()
    a = take(0)
    for v in [0.2] * 90 + [8.0] * 10:
        m.http_duration.observe(v, route="/ask")
    b = take(60)
    p50 = ar.quantile(a, b, "rag_http_request_duration_seconds", 0.5, route="/ask")
    p95 = ar.quantile(a, b, "rag_http_request_duration_seconds", 0.95, route="/ask")
    assert 0.1 <= p50 <= 0.25, p50
    assert 5 <= p95 <= 10, p95
    assert ar.quantile(a, a, "rag_http_request_duration_seconds", 0.95, route="/ask") is None      # nothing happened between


@test
def quiet_service_fires_nothing():
    m, take = fresh()
    m.dep_up.set(1, name="redis")
    m.dep_up.set(1, name="mongo")
    a = take(0)
    ask(m, n=20, seconds=2)
    b = take(60)
    assert ar.evaluate(a, b) == [] and ar.evaluate(None, a) == []


@test
def dependency_down_needs_no_history():
    m, take = fresh()
    m.dep_up.set(1, name="mongo")
    m.dep_up.set(0, name="redis")
    out = ar.evaluate(None, take(0))
    assert names(out) == ["dependency_down"] and "redis" in out[0].message and "mongo" not in out[0].message and out[0].severity == "critical"


@test
def server_errors_need_both_count_and_share():
    m, take = fresh()
    a = take(0)
    ask(m, n=100)
    ask(m, "502", n=1)                                # 1 of 101: below min_errors
    assert "server_errors" not in names(ar.evaluate(a, take(60)))
    ask(m, "502", n=1)                                # 2 of 102 = 2%: below the share
    assert "server_errors" not in names(ar.evaluate(a, take(61)))
    ask(m, "500", n=8)                                # 10 of 110 = 9%
    out = ar.evaluate(a, take(62))
    assert "server_errors" in names(out)


@test
def timeouts_busy_abandoned_auth():
    m, take = fresh()
    a = take(0)
    ask(m, "504")
    m.pipeline_abandoned.inc()
    ask(m, "503", n=3)
    for _ in range(10):
        m.http_requests.inc(route="/ask", method="POST", status="401")
    got = names(ar.evaluate(a, take(60)))
    assert got == ["abandoned_calls", "auth_failures", "busy", "timeouts"], got


@test
def saturation_must_hold_on_two_looks():
    m, take = fresh()
    m.slots.set(1)
    m.in_flight.set(1)
    m.waiting.set(2)
    a = take(0)
    assert "saturated" not in names(ar.evaluate(None, a))
    b = take(30)
    assert "saturated" in names(ar.evaluate(a, b))
    m.waiting.set(0)
    c = take(60)
    assert "saturated" not in names(ar.evaluate(b, c))


@test
def restart_is_noticed_by_the_start_time():
    m, take = fresh()
    a = take(0)
    m.start_time.set(m.started_wall + 500)
    assert "process_restarted" in names(ar.evaluate(a, take(60)))


@test
def slow_answers_cost_memory_disk():
    m, take = fresh()
    a = take(0)
    ask(m, n=10, seconds=45)
    m.cost.inc(0.5)
    m.rss.set(4 * 1024 ** 3)
    m.disk_free.set(500 * 1024 ** 2, path=".")
    out = ar.evaluate(a, take(60))
    got = names(out)
    assert got == ["cost_rate", "disk_low", "memory_high", "slow_answers"], got       # 0.5 USD in 60 s = 30 USD/hour
    assert [x for x in out if x.name == "disk_low"][0].severity == "critical"


@test
def thresholds_can_be_changed():
    m, take = fresh()
    a = take(0)
    m.cost.inc(0.01)
    b = take(60)
    assert "cost_rate" not in names(ar.evaluate(a, b))
    assert "cost_rate" in names(ar.evaluate(a, b, ar.Thresholds(cost_per_hour_usd=0.1)))


@test
def watcher_reports_changes_only():
    m, take = fresh()
    w = ar.Watcher(ar.Thresholds(window_seconds=120))
    m.dep_up.set(1, name="redis")
    assert w.feed(take(0)) == []
    m.dep_up.set(0, name="redis")
    ev = w.feed(take(30))
    assert [(e.kind, e.alert.name) for e in ev] == [("FIRING", "dependency_down")]
    assert w.feed(take(60)) == [] and w.feed(take(90)) == []                  # still down: silence
    m.dep_up.set(1, name="redis")
    ev = w.feed(take(120))
    assert [(e.kind, e.alert.name) for e in ev] == [("RESOLVED", "dependency_down")]
    assert w.feed(take(150)) == []
    assert "FIRING" in ar.format_event(ar.Event("FIRING", ar.Alert("x", "critical", "m"), 0))


@test
def delta_alerts_clear_when_they_fall_out_of_the_window():
    m, take = fresh()
    w = ar.Watcher(ar.Thresholds(window_seconds=100))
    assert w.feed(take(0)) == []
    ask(m, "504")
    assert [e.alert.name for e in w.feed(take(30))] == ["timeouts"]
    assert w.feed(take(60)) == []
    out = []
    for t in (90, 120, 150, 180, 210):
        out += w.feed(take(t))
    assert [(e.kind, e.alert.name) for e in out] == [("RESOLVED", "timeouts")], out


@test
def unreachable_fires_after_two_failures_and_resolves():
    w = ar.Watcher()
    assert w.feed(None, "URLError") == []
    ev = w.feed(None, "URLError")
    assert [(e.kind, e.alert.name) for e in ev] == [("FIRING", "api_unreachable")]
    assert w.feed(None, "URLError") == []
    m, take = fresh()
    ev = w.feed(take(100))
    assert [(e.kind, e.alert.name) for e in ev] == [("RESOLVED", "api_unreachable")]


# ------------------------------------------------------------ against a server
@test
def cli_once_against_a_live_server():
    L = Live()
    try:
        env = dict(os.environ, RAG_METRICS_KEY=METRICS_KEY)
        L.monitor.dep_cache_seconds = 0.0
        run = lambda *extra: subprocess.run([sys.executable, os.path.join(HERE, "alert_rules.py"), "--url", L.url, "--once", *extra],
                                            capture_output=True, text=True, env=env, timeout=60)
        r = run()
        assert r.returncode == 0 and "nothing is firing" in r.stdout, (r.returncode, r.stdout, r.stderr)
        L.pipeline.state["redis_down"] = True
        r = run()
        assert r.returncode == 1 and "dependency_down" in r.stdout and "redis" in r.stdout, (r.returncode, r.stdout, r.stderr)
        assert METRICS_KEY not in r.stdout + r.stderr
        L.pipeline.state["redis_down"] = False
        assert run().returncode == 0
        r = subprocess.run([sys.executable, os.path.join(HERE, "alert_rules.py"), "--url", f"http://127.0.0.1:{free_port()}", "--once"],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 2 and "cannot read" in r.stderr, (r.returncode, r.stdout, r.stderr)
        r = subprocess.run([sys.executable, os.path.join(HERE, "alert_rules.py"), "--url", L.url, "--once"], capture_output=True, text=True,
                           env=dict(os.environ, RAG_METRICS_KEY="wrong-key-wrong-key-wrong"), timeout=60)
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    finally:
        L.stop()


@test
def watch_loop_prints_firing_and_resolved_for_a_real_outage():
    L = Live()
    try:
        L.monitor.dep_cache_seconds = 0.0
        seen, stop = [], {"v": False}
        import threading
        t = threading.Thread(target=ar.watch, args=(L.url, METRICS_KEY, 0.3, ar.Thresholds(window_seconds=30)),
                             kwargs={"notify": seen.append, "stop": lambda: stop["v"]}, daemon=True)
        t.start()
        time.sleep(1.0)
        L.pipeline.state["redis_down"] = True
        deadline = time.time() + 8
        while time.time() < deadline and not any(e.kind == "FIRING" for e in seen):
            time.sleep(0.1)
        L.pipeline.state["redis_down"] = False
        deadline = time.time() + 8
        while time.time() < deadline and not any(e.kind == "RESOLVED" for e in seen):
            time.sleep(0.1)
        stop["v"] = True
        t.join(timeout=5)
        kinds = [(e.kind, e.alert.name) for e in seen]
        assert kinds == [("FIRING", "dependency_down"), ("RESOLVED", "dependency_down")], kinds
    finally:
        L.stop()


if __name__ == "__main__":
    bad = 0
    for name, ok, tb in results:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
            print(tb)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)
