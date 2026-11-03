"""Tests for check_launch.py.  Run:  python test_check_launch.py
The door tests start the REAL Caddy program in front of a stand-in API (they need `caddy` on this computer, or CADDY_BIN=...; otherwise SKIP)."""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backup_tools as bt
import check_launch as cl
import make_frontdoor as mf
from doorkit import KEY, LEAKY_CADDYFILE, Door, Skip, StubApi

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, "pass", ""))
    except Skip as e:
        results.append((fn.__name__, "skip", str(e)))
    except Exception:
        results.append((fn.__name__, "fail", traceback.format_exc(limit=6)))
    return fn


def texts(rep):
    return [("OK " if ok else "BAD " if ok is False else "WARN ") + t for ok, t in rep.lines]


def has(rep, status, needle):
    return any(ok is status and needle in t for ok, t in rep.lines)


METRICS_KEY = "metrics-key-1f2e3d4c5b6a7988"
GOOD_ENV = {"RAG_API_KEYS": f"{KEY}:me", "RAG_METRICS_KEY": METRICS_KEY, "NTFY_TOPIC_SECRET": "rag-alerts-topic-0123456789abcdef", "HEARTBEAT_URL_SECRET": "https://hc-ping.example/ping/abcdef-0123",
            "ALERT_UTC_OFFSET_HOURS": "3", "TELEGRAM_BOT_TOKEN": "123456789:AAH-fake_TOKEN_value-for-tests", "TELEGRAM_CHAT_ID": "424242"}


def make_backup(root, stamp="20260101-120000", rows=5, created=None, drill=None, env_inside=False, damage=False):
    folder = os.path.join(root, stamp)
    os.makedirs(os.path.join(folder, "checkpoints"))
    p = os.path.join(folder, "checkpoints", "a.db")
    c = sqlite3.connect(p)
    c.execute("create table t(x)")
    c.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    c.commit()
    c.close()
    manifest = {"created_at": 1.0, "parts": {"checkpoints": {"files": {"a.db": {"kind": "sqlite", "integrity": "ok", "tables": {"t": rows},
                "size": os.path.getsize(p), "sha256": bt.sha256_file(p)}}}}}
    json.dump(manifest, open(os.path.join(folder, "MANIFEST.json"), "w"))
    if env_inside:
        open(os.path.join(folder, ".env"), "w").write("X=1")
    if damage:
        with open(p, "ab") as f:
            f.write(b"damage")
    json.dump({"folder": stamp, "created_at": created or time.time() - 3600, "parts": ["checkpoints"], "verified": True}, open(os.path.join(root, "LATEST.json"), "w"))
    if drill is not None:
        json.dump(drill, open(os.path.join(root, "LAST_DRILL.json"), "w"))
    return folder


def good_drill(days_ago=3):
    return {"ok": True, "at": time.time() - days_ago * 86400, "folder": "20260101-120000"}


# ------------------------------------------------------------------- the door ----
@test
def d01_a_correct_door_passes_every_door_check_with_no_warning():
    d = Door()
    try:
        rep = cl.Report()
        assert cl.check_door(rep, d.url) is True
        assert rep.ok and not rep.warned, "\n".join(texts(rep))
        assert has(rep, True, "56 spellings") or has(rep, True, f"{len(mf.CLOSED_PATHS)} spellings"), texts(rep)
        assert has(rep, True, "413") and has(rep, True, "no key is refused")
        assert not any(h[1].startswith("/metrics") or h[1].startswith("//metrics") for h in d.api.hits), "closed paths reached the API"
    finally:
        d.stop()


@test
def d02_a_door_that_forwards_everything_is_caught_and_the_leaks_are_named():
    d = Door(caddyfile=LEAKY_CADDYFILE)
    try:
        rep = cl.Report()
        cl.check_door(rep, d.url)
        assert not rep.ok
        leak = [t for ok, t in rep.lines if ok is False and "closed route" in t]
        assert leak and "/metrics" in leak[0] and "make_frontdoor" in leak[0], texts(rep)
        assert has(rep, False, "GET /ask is closed") and has(rep, False, "30 KB question")
    finally:
        d.stop()


@test
def d03_a_door_that_is_not_running_is_reported_with_the_next_step_and_stops_there():
    rep = cl.Report()
    from testkit import free_port
    assert cl.check_door(rep, f"http://127.0.0.1:{free_port()}") is False
    assert len(rep.lines) == 1 and has(rep, False, "cannot reach") and "docker compose logs proxy" in rep.lines[0][1]


@test
def d04_an_api_that_answers_without_a_key_is_called_out_loudly():
    d = Door(api=StubApi(auth=False))
    try:
        rep = cl.Report()
        cl.check_door(rep, d.url)
        assert has(rep, False, "ANSWERED a question without any key"), texts(rep)
    finally:
        d.stop()


@test
def d05_an_api_that_is_down_behind_a_working_door_is_not_called_a_door_problem():
    d = Door()
    d.api.stop()
    try:
        rep = cl.Report()
        cl.check_door(rep, d.url)
        assert has(rep, False, "/healthz") and has(rep, None, "not ready yet"), texts(rep)
        assert has(rep, True, "spellings"), "the closed routes are closed whether or not the API is up"
    finally:
        d.api = StubApi()
        d.stop()


@test
def d06_a_door_that_shows_what_software_it_is_is_noticed():
    text = mf.caddyfile_text(upstream="__API__", address=":__PORT__").replace("-Server", "X-Not-Removed 1")      # the Server line is gone: Caddy then adds "Server: Caddy"
    d = Door(caddyfile=text)
    try:
        rep = cl.Report()
        cl.check_door(rep, d.url)
        assert has(rep, False, "Server: Caddy"), texts(rep)
    finally:
        d.stop()


# ---------------------------------------------------------------- public address ----
@test
def p01_a_local_address_is_accepted_for_testing_with_a_warning():
    d = Door()
    try:
        rep = cl.Report()
        cl.check_public(rep, d.url)
        assert rep.ok, texts(rep)
        assert has(rep, None, "local one") and has(rep, True, f"{len(cl.PUBLIC_SAMPLE)} closed routes")
    finally:
        d.stop()


@test
def p02_closed_routes_open_from_the_internet_are_reported_as_a_failure():
    d = Door(caddyfile=LEAKY_CADDYFILE)
    try:
        rep = cl.Report()
        cl.check_public(rep, d.url)
        assert has(rep, False, "reachable from the internet") and "/metrics" in [t for ok, t in rep.lines if ok is False][0], texts(rep)
    finally:
        d.stop()


@test
def p03_a_403_from_cloudflare_in_front_counts_as_closed():
    d = Door(caddyfile=mf.caddyfile_text(upstream="__API__", address=":__PORT__").replace('respond "not found" 404', 'respond "forbidden" 403'))
    try:
        rep = cl.Report()
        cl.check_public(rep, d.url)
        assert has(rep, True, "closed from the internet"), texts(rep)
    finally:
        d.stop()


@test
def p04_plain_http_on_a_real_host_fails_and_an_unreachable_tunnel_gets_the_restart_hint():
    rep = cl.Report()
    cl.check_public(rep, "http://nothing-here.invalid")
    assert has(rep, False, "not https") and has(rep, False, "quick-tunnel"), texts(rep)


# ---------------------------------------------------------------------- backups ----
@test
def b01_a_fresh_verified_backup_with_a_recent_drill_passes():
    root = tempfile.mkdtemp()
    make_backup(root, drill=good_drill())
    rep = cl.Report()
    cl.check_backups(rep, root)
    assert rep.ok and not rep.warned, texts(rep)
    assert has(rep, True, "passes its own check") and has(rep, True, "no .env file") and has(rep, True, "restore was practised 3 day")


@test
def b02_each_backup_problem_is_a_separate_clear_failure():
    def run(**kw):
        root = tempfile.mkdtemp()
        drill = kw.pop("drill", good_drill())
        if kw.pop("nothing", False):
            rep = cl.Report()
            cl.check_backups(rep, root)
            return rep
        make_backup(root, drill=drill, **kw)
        rep = cl.Report()
        cl.check_backups(rep, root)
        return rep
    assert has(run(nothing=True), False, "no backup yet")
    assert has(run(created=time.time() - 100 * 3600, drill=good_drill()), False, "100.0 hours old")
    assert has(run(drill=None), False, "never practised")
    assert has(run(drill={"ok": False, "at": time.time()}), False, "FAILED")
    assert has(run(drill=good_drill(days_ago=60)), False, "60 day(s) ago")
    assert has(run(env_inside=True, drill=good_drill()), False, ".env file is inside")
    r = run(damage=True, drill=good_drill())
    assert has(r, False, "FAILS its check"), texts(r)


@test
def b03_a_broken_latest_file_and_a_latest_that_points_nowhere_are_both_reported():
    root = tempfile.mkdtemp()
    open(os.path.join(root, "LATEST.json"), "w").write("{not json")
    rep = cl.Report()
    cl.check_backups(rep, root)
    assert has(rep, False, "cannot be read")
    json.dump({"folder": "20990101-000000", "created_at": time.time()}, open(os.path.join(root, "LATEST.json"), "w"))
    rep = cl.Report()
    cl.check_backups(rep, root)
    assert has(rep, False, "not there")


# ----------------------------------------------------------------------- alerts ----
@test
def a01_alert_setup_is_judged_from_what_is_in_env():
    rep = cl.Report()
    cl.check_alerts(rep, {})
    assert has(rep, False, "no alert channel")
    rep = cl.Report()
    cl.check_alerts(rep, {"NTFY_TOPIC_SECRET": "rag-alerts-topic-0123456789abcdef"})
    assert rep.ok and has(rep, True, "ntfy") and has(rep, None, "dead-man") and has(rep, None, "ALERT_UTC_OFFSET_HOURS"), texts(rep)
    rep = cl.Report()
    cl.check_alerts(rep, GOOD_ENV)
    assert rep.ok and not rep.warned and has(rep, True, "telegram, ntfy"), texts(rep)
    rep = cl.Report()                                            # half set up = not set up
    cl.check_alerts(rep, {"TELEGRAM_BOT_TOKEN": "123456789:AAH-fake_TOKEN_value-for-tests"})
    assert has(rep, False, "no alert channel")


# ---------------------------------------------------------------------- secrets ----
@test
def s01_key_quality_rules():
    def run(**over):
        env = {**GOOD_ENV, **over}
        rep = cl.Report()
        cl.check_secrets(rep, tempfile.mkdtemp(), env)
        return rep
    assert run().ok and not run().warned
    assert has(run(RAG_API_KEYS="short:me"), False, "shorter than 16")
    assert has(run(RAG_API_KEYS="this-is-my-password-0123456789:me"), False, "example word")
    assert has(run(RAG_METRICS_KEY=KEY), False, "same as an API key")
    assert has(run(RAG_METRICS_KEY="tiny"), False, "too short")
    assert has(run(RAG_METRICS_KEY=""), False, "RAG_METRICS_KEY is not set")
    assert has(run(RAG_API_KEYS=""), False, "RAG_API_KEYS is not set")
    assert has(run(RAG_ENABLE_DOCS="1"), False, "RAG_ENABLE_DOCS")
    assert run(RAG_ENABLE_DOCS="0").ok
    assert has(run(RAG_API_KEYS=f"{KEY}:me,short:you"), False, "1 API key(s) shorter")


@test
def s02_git_must_ignore_env_and_backups_when_the_project_is_a_git_project():
    if not shutil.which("git"):
        raise Skip("no git on this computer")
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    rep = cl.Report()
    cl.check_secrets(rep, d, GOOD_ENV)
    assert has(rep, False, "git would commit your keys") and has(rep, False, "git would commit your conversations"), texts(rep)
    open(os.path.join(d, ".gitignore"), "w").write(".env\nbackups/\n")
    rep = cl.Report()
    cl.check_secrets(rep, d, GOOD_ENV)
    assert rep.ok and has(rep, True, "git ignores .env") and has(rep, True, "git ignores backups"), texts(rep)
    plain = tempfile.mkdtemp()                                  # not a git project: nothing to say
    rep = cl.Report()
    cl.check_secrets(rep, plain, GOOD_ENV)
    assert rep.ok and not any("git" in t for _, t in rep.lines)


# ------------------------------------------------------------------ containers ----
def row(service, state="running", health="healthy"):
    return {"Service": service, "State": state, "Health": health}


@test
def c01_containers_must_be_running_and_healthy():
    good = [row("redis"), row("rag"), row("proxy", health=""), row("watchdog")]
    rep = cl.Report()
    cl.check_containers(rep, ".", False, ps=lambda: good)
    assert rep.ok and not rep.warned, texts(rep)
    rep = cl.Report()
    cl.check_containers(rep, ".", False, ps=lambda: [row("redis"), row("rag"), row("proxy", health=""), row("watchdog", health="unhealthy")])
    assert has(rep, False, "watchdog: running, unhealthy") and "docker compose logs watchdog" in [t for ok, t in rep.lines if ok is False][0]
    rep = cl.Report()
    cl.check_containers(rep, ".", False, ps=lambda: [row("rag"), row("watchdog")])
    assert has(rep, False, "'proxy' is not in this project")
    rep = cl.Report()
    cl.check_containers(rep, ".", False, ps=lambda: [row("rag", state="exited", health=""), row("proxy", health=""), row("watchdog")])
    assert has(rep, False, "rag: exited")
    rep = cl.Report()
    cl.check_containers(rep, ".", True, ps=lambda: good + [row("tunnel", health="")])
    assert rep.ok and has(rep, None, "quick tunnel is running")
    rep = cl.Report()
    cl.check_containers(rep, ".", True, ps=lambda: (_ for _ in ()).throw(RuntimeError("daemon not running")))
    assert not rep.ok and has(rep, False, "could not ask Docker"), "Docker not answering must not be a pass"
    rep = cl.Report()
    cl.check_containers(rep, ".", False, ps=lambda: [row("rag"), row("proxy", health=""), row("watchdog")])
    assert has(rep, False, "'redis' is not in this project"), "a missing redis must fail, not be skipped"


@test
def c02_a_port_open_to_the_whole_network_is_a_failure():
    try:
        import yaml  # noqa: F401
    except ImportError:
        raise Skip("PyYAML is not installed")
    d = tempfile.mkdtemp()
    open(os.path.join(d, "docker-compose.yml"), "w").write('services:\n  rag:\n    ports:\n      - "8080:8000"\n  proxy:\n    ports:\n      - "127.0.0.1:8088:8080"\n')
    rep = cl.Report()
    cl.check_compose_ports(rep, d)
    assert has(rep, False, "rag: 8080:8000"), texts(rep)
    open(os.path.join(d, "docker-compose.yml"), "w").write('services:\n  rag:\n    ports:\n      - "127.0.0.1:8080:8000"\n')
    rep = cl.Report()
    cl.check_compose_ports(rep, d)
    assert rep.ok
    os.remove(os.path.join(d, "docker-compose.yml"))
    rep = cl.Report()
    cl.check_compose_ports(rep, d)
    assert has(rep, False, "docker-compose.yml is missing")


# ------------------------------------------------------------------ one question ----
@test
def q01_a_real_question_through_the_door_needs_an_answer_and_the_doors_request_id():
    d = Door()
    try:
        rep = cl.Report()
        cl.check_ask(rep, d.url, KEY, "q")
        assert rep.ok and has(rep, True, "answered through the door") and has(rep, True, "request id"), texts(rep)
        rep = cl.Report()
        cl.check_ask(rep, d.url, "a-wrong-key-0123456789abcdef", "q")
        assert has(rep, False, "HTTP 401"), texts(rep)
        rep = cl.Report()
        cl.check_ask(rep, d.url, "", "q")
        assert has(rep, False, "RAG_API_KEYS")
    finally:
        d.stop()


# ----------------------------------------------------------------- everything ----
def project(env=None, with_backup=True):
    d = tempfile.mkdtemp(prefix="d21p_")
    env = GOOD_ENV if env is None else env
    open(os.path.join(d, ".env"), "w").write("".join(f"{k}={v}\n" for k, v in env.items()))
    if with_backup:
        os.makedirs(os.path.join(d, "backups"))
        make_backup(os.path.join(d, "backups"), drill=good_drill())
    open(os.path.join(d, "docker-compose.yml"), "w").write('services:\n  proxy:\n    ports:\n      - "127.0.0.1:8088:8080"\n')
    json.dump({"at": time.time() - 86400, "ok_channels": ["ntfy"], "failed_channels": []}, open(os.path.join(d, "alert_test.json"), "w"))
    return d


@test
def e01_the_whole_check_says_ready_when_everything_is_right_and_never_prints_a_secret():
    d = Door()
    p = project()
    try:
        rep = cl.check_launch(p, door=d.url, public="", docker=True, ps=lambda: [row("redis"), row("rag"), row("proxy", health=""), row("watchdog")], ask=True,
                              logs=lambda: '{"msg":"handled request","status":200}\n' * 3)
        out = rep.summary()
        assert rep.ok, out
        assert "1. THE FRONT DOOR" in out and "7. ONE REAL QUESTION" in out and "holds none of your keys" in out
        assert "READY TO LAUNCH" not in out and "NOTHING FAILED" in out and "NOT checked this time: the public address" in out, out
        for secret in (KEY, METRICS_KEY, GOOD_ENV["NTFY_TOPIC_SECRET"], GOOD_ENV["TELEGRAM_BOT_TOKEN"], GOOD_ENV["HEARTBEAT_URL_SECRET"]):
            assert secret not in out, "a secret was printed"
    finally:
        d.stop()


@test
def e02_the_whole_check_says_not_ready_and_lists_what_to_fix():
    d = Door(caddyfile=LEAKY_CADDYFILE)
    p = project(env={"RAG_API_KEYS": "short:me"}, with_backup=False)
    try:
        rep = cl.check_launch(p, door=d.url, public="", docker=False)
        out = rep.summary()
        assert not rep.ok and "NOT READY" in out
        for needle in ("closed route(s)", "no backup yet", "no alert channel", "shorter than 16", "RAG_METRICS_KEY"):
            assert needle in out, needle
    finally:
        d.stop()


@test
def e03_command_line_exit_codes():
    d = Door()
    p = project()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_good = cl.main(["--project", p, "--door", d.url, "--no-docker", "--ask"])
        assert rc_good == 0, buf.getvalue()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_bad = cl.main(["--project", p, "--door", "http://127.0.0.1:1", "--no-docker"])
        assert rc_bad == 1 and "cannot reach" in buf.getvalue()
        shutil.rmtree(os.path.join(p, "backups"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_nobackup = cl.main(["--project", p, "--door", d.url, "--no-docker"])
        assert rc_nobackup == 1 and "no backup yet" in buf.getvalue()
    finally:
        d.stop()


@test
def e04_missing_env_file_is_a_failure_not_a_crash():
    d = Door()
    p = tempfile.mkdtemp()
    try:
        rep = cl.check_launch(p, door=d.url, docker=False)
        assert has(rep, False, ".env is missing"), texts(rep)
    finally:
        d.stop()


@test
def e05_the_door_address_comes_from_env_when_not_given():
    p = project(env={**GOOD_ENV, "RAG_DOOR_PORT": "1"})
    rep = cl.check_launch(p, public="", docker=False)
    assert has(rep, False, "127.0.0.1:1"), texts(rep)


@test
def r01_the_last_line_never_claims_more_than_was_checked():
    rep = cl.Report()
    rep.add(True, "x")
    assert "READY TO LAUNCH" in rep.summary()
    rep.skip("the containers and the door log")
    out = rep.summary()
    assert "READY TO LAUNCH" not in out and "NOTHING FAILED" in out and "the containers and the door log" in out
    rep.add(False, "y")
    assert "NOT READY" in rep.summary() and "READY TO LAUNCH" not in rep.summary()


@test
def r02_skipping_docker_or_the_question_or_the_public_address_is_said_out_loud():
    d = Door()
    p = project()
    try:
        out = cl.check_launch(p, door=d.url, public="", docker=False, ask=False).summary()
        for needle in ("the containers were NOT looked at", "the public address", "the containers and the door log", "a real question (add --ask)"):
            assert needle in out, needle
        assert "READY TO LAUNCH" not in out
    finally:
        d.stop()


@test
def r03_a_wrong_key_must_be_refused_by_the_api_not_by_a_dead_backend():
    d = Door()
    try:
        rep = cl.Report()
        cl.check_door(rep, d.url)
        assert has(rep, True, "wrong key is refused (HTTP 401)"), texts(rep)
        d.api.stop()                                         # now the API behind the door is gone: every answer is 502
        rep = cl.Report()
        cl.check_door(rep, d.url)
        assert not has(rep, True, "wrong key is refused") and has(rep, None, "wrong-key test could not run"), texts(rep)
    finally:
        d.api = StubApi()
        d.stop()
    d = Door(api=StubApi(auth=False))                        # an API that lets ANY key in: the wrong-key line must be a failure
    try:
        rep = cl.Report()
        cl.check_door(rep, d.url)
        assert has(rep, False, "wrong key got HTTP 200; expected 401"), texts(rep)
    finally:
        d.stop()


@test
def l01_a_key_in_the_door_log_is_a_failure_and_is_never_printed():
    rep = cl.Report()
    cl.check_proxy_log(rep, ".", [KEY, METRICS_KEY], logs=lambda: '{"status":200}\n{"request":{"headers":{"X-Api-Key":["' + KEY + '"]}}}\n')
    out = rep.summary()
    assert not rep.ok and has(rep, False, "1 of your secret(s) appear in the door's log") and KEY not in out
    rep = cl.Report()
    cl.check_proxy_log(rep, ".", [KEY, METRICS_KEY], logs=lambda: '{"status":200}\n' * 4)
    assert rep.ok and has(rep, True, "none of your keys")
    rep = cl.Report()
    cl.check_proxy_log(rep, ".", [KEY], logs=lambda: (_ for _ in ()).throw(RuntimeError("no such service: proxy")))
    assert rep.ok and has(rep, None, "could not read the door's log") and rep.skipped == ["the door's log"], "an unreadable log is NOT a pass: it is listed as not checked"
    assert "READY TO LAUNCH" not in rep.summary()
    rep = cl.Report()                                                  # an empty or tiny "secret" must not match everything
    cl.check_proxy_log(rep, ".", ["", "ab"], logs=lambda: "abab")
    assert rep.ok


@test
def l02_a_leak_found_in_the_door_log_makes_the_whole_check_fail():
    d = Door()
    p = project()
    try:
        rep = cl.check_launch(p, door=d.url, public="", docker=True, ps=lambda: [row("redis"), row("rag"), row("proxy", health=""), row("watchdog")], ask=True,
                              logs=lambda: "oops " + KEY)
        assert not rep.ok and "appear in the door's log" in rep.summary() and KEY not in rep.summary()
    finally:
        d.stop()


@test
def a03_the_record_of_the_last_test_alert_is_read_and_judged():
    d = tempfile.mkdtemp()
    now = 2_000_000_000.0
    rec = os.path.join(d, "alert_test.json")
    def judge():
        rep = cl.Report()
        cl.check_alerts(rep, GOOD_ENV, d, now)
        return rep
    rep = judge()
    assert has(rep, None, "no test alert on record") and rep.ok
    json.dump({"at": now - 2 * 86400, "ok_channels": ["ntfy", "telegram"], "failed_channels": []}, open(rec, "w"))
    rep = judge()
    assert has(rep, True, "last test alert (2 day(s) ago) was accepted by: ntfy, telegram"), texts(rep)
    json.dump({"at": now - 2 * 86400, "ok_channels": ["ntfy"], "failed_channels": ["telegram"]}, open(rec, "w"))
    rep = judge()
    assert not rep.ok and has(rep, False, "FAILED for: telegram"), texts(rep)
    json.dump({"at": now - 50 * 86400, "ok_channels": ["ntfy"], "failed_channels": []}, open(rec, "w"))
    assert has(judge(), None, "50 days ago")
    open(rec, "w").write("{broken")
    assert has(judge(), False, "cannot be read")
    shutil.rmtree(d, ignore_errors=True)


@test
def a02_the_dead_mans_switch_address_must_look_like_an_address():
    for bad in ("just-some-words", "ftp://x.example/abc", "https:///path", "https://localhost/abc"):
        rep = cl.Report()
        cl.check_alerts(rep, {**GOOD_ENV, "HEARTBEAT_URL_SECRET": bad})
        assert has(rep, False, "not a web address") and bad not in rep.summary(), bad
    rep = cl.Report()
    cl.check_alerts(rep, GOOD_ENV)
    assert rep.ok and has(rep, True, "dead-man's switch address is set")


# --------------------------------------------------------------------------- #
passed = sum(r[1] == "pass" for r in results)
skipped = [r for r in results if r[1] == "skip"]
for name, status, tb in results:
    print({"pass": "PASS ", "skip": "SKIP ", "fail": "FAIL "}[status] + name + (f"   ({tb})" if status == "skip" else ""))
    if status == "fail":
        print(tb)
print(f"\n{passed}/{len(results)} passed" + (f", {len(skipped)} skipped" if skipped else ""))
raise SystemExit(0 if all(r[1] != "fail" for r in results) else 1)
