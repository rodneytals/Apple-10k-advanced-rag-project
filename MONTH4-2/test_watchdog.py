"""Tests for watchdog.py.  Run:  python test_watchdog.py
Unit tests use a fake clock and fake probes (so six hours pass in a millisecond). The last tests run the REAL API, a real watchdog loop
and fake Telegram / ntfy / front door / uptime service on this computer."""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["WATCHDOG_ALIVE_FILE"] = os.path.join(tempfile.gettempdir(), f"wd_alive_{os.getpid()}")
import alert_rules as ar
import backup_tools as bt
import monitoring as mon
import notifiers as nt
import watchdog as wdg
from fakeservices import FakeDoor, FakeNtfy, FakePing, FakeTelegram
from testkit import METRICS_KEY, Live

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=6)))
    return fn


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Sink(nt.Notifier):
    """A 'phone': keeps what it was sent, and can be switched off."""
    name = "sink"

    def __init__(self):
        self.got, self.down = [], False

    def send(self, msg):
        if self.down:
            raise nt.NotifyError("sink: down", transient=False)
        self.got.append(msg)


def metrics_world():
    m = mon.Monitor({})
    m.runner = None
    m.registry._collectors.clear()
    m.dep_up.set(1, name="redis")
    m.dep_up.set(1, name="mongo")
    return m


def snapshot_of(m, ts=0.0):
    return ar.Snapshot(ts, ar.parse_metrics(m.render()))


class Rig:
    """A watchdog wired to fakes. rig.api = the metrics world; rig.status = what each URL answers; rig.pings = every URL probed."""

    def __init__(self, door=False, public=False, backup_dir="", heartbeat="", remind_hours=6.0, grace=600.0, **cfg_over):
        self.clock = Clock()
        self.m = metrics_world()
        self.api_down = False
        self.status = {}                               # url -> int or None (None = does not answer)
        self.pings = []
        self.sink = Sink()
        env = {"ALERT_LABEL": "rag-test", "ALERT_UTC_OFFSET_HOURS": "3"}
        self.cfg = wdg.Config(label="rag-test", api_url="http://api", door_url="http://door" if door else "", public_url="http://public.example" if public else "",
                              metrics_key="k" * 20, interval=30, grace=grace, backup_dir=backup_dir, remind_hours=remind_hours,
                              heartbeat_url=heartbeat, env=env)
        for k, v in cfg_over.items():
            setattr(self.cfg, k, v)
        self.logs = []
        self.disp = nt.Dispatcher([self.sink], retry_waits=(), sleep=lambda s: None, log=self.logs.append)
        self.wd = wdg.Watchdog(self.cfg, self.disp, now=self.clock, probe=self._probe, fetch=self._fetch)

    def _fetch(self, url, key):
        if self.api_down:
            raise ConnectionRefusedError("down")
        return snapshot_of(self.m, self.clock())

    def _probe(self, url, timeout=5.0):
        self.pings.append(url)
        if url in self.status:
            v = self.status[url]
            return (None, "ConnectionRefusedError") if v is None else (v, "")
        return (404, "") if any(url.endswith(p) for p in wdg.CLOSED_FROM_OUTSIDE) else (200, "")

    def step(self, advance=30):
        self.clock.advance(advance)
        return self.wd.step()

    def titles(self):
        return [m.title for m in self.sink.got]


@test
def w01_a_quiet_system_sends_nothing_but_still_shows_signs_of_life():
    r = Rig(door=True, heartbeat="http://hb/ping/uuid")
    for _ in range(5):
        assert r.step() == []
    assert r.sink.got == []
    assert "http://hb/ping/uuid" in r.pings and r.pings.count("http://hb/ping/uuid") == 5
    assert time.time() - os.path.getmtime(os.environ["WATCHDOG_ALIVE_FILE"]) < 60
    assert wdg.selfcheck(os.environ["WATCHDOG_ALIVE_FILE"]) == 0


@test
def w02_one_message_when_it_starts_one_when_it_clears_and_none_in_between():
    r = Rig()
    r.step()
    r.m.dep_up.set(0, name="redis")
    ev = r.step()
    assert [(e.kind, e.alert.name) for e in ev] == [("FIRING", "dependency_down")]
    for _ in range(10):
        r.step()
    assert r.titles() == ["CRITICAL dependency_down"], r.titles()
    msg = r.sink.got[0]
    assert msg.severity == "critical" and "redis is not answering" in msg.body and "rag-test" in msg.body and "UTC+3" in msg.body
    r.m.dep_up.set(1, name="redis")
    r.step()
    assert r.titles() == ["CRITICAL dependency_down", "RESOLVED dependency_down"]
    assert r.sink.got[1].severity == "resolved"
    r.step()
    assert len(r.sink.got) == 2


@test
def w03_several_alerts_in_one_round_are_one_message():
    r = Rig()
    r.step()
    r.m.dep_up.set(0, name="redis")
    r.m.dep_up.set(0, name="mongo")
    r.step()
    assert len(r.sink.got) == 1
    m = r.sink.got[0]
    assert m.title == "CRITICAL dependency_down" and "mongo, redis" in m.body        # both dependencies are named in the one alert
    r2 = Rig(door=True)
    r2.step()
    r2.m.dep_up.set(0, name="redis")
    r2.status["http://door/healthz"] = 502
    r2.step()
    r2.step()
    assert len(r2.sink.got) == 2                                                    # dependency_down at once; door_down needs two failed looks
    r3 = Rig(door=True)
    r3.step()
    r3.status["http://door/healthz"] = 502
    r3.m.dep_up.set(0, name="redis")
    r3.step()
    r3.step()
    r3.disp.send  # (quiet)
    assert [t for t in r3.titles()] == ["CRITICAL dependency_down", "CRITICAL door_down"]


@test
def w04_the_first_minutes_after_a_start_are_quiet_and_a_problem_that_lasts_is_announced_afterwards():
    r = Rig(door=True, grace=600)
    r.api_down = True
    r.status["http://door/healthz"] = 502
    for _ in range(10):                                   # 5 minutes of a stack that is still loading its models
        r.step()
    assert r.sink.got == [], r.titles()
    for _ in range(12):                                   # grace over, still down: now it matters
        r.step()
    names = sorted(m.title for m in r.sink.got)
    assert names == ["CRITICAL: 2 alerts (api_unreachable, door_down)"] or len(r.sink.got) == 1, names
    body = r.sink.got[0].body
    assert "api_unreachable" in body and "door_down" in body, body
    r.api_down = False
    r.status["http://door/healthz"] = 200
    r.step()
    assert r.sink.got[-1].title.startswith("RESOLVED"), r.titles()


@test
def w05_a_normal_start_up_never_bothers_you():
    r = Rig(door=True, grace=600)
    r.api_down = True
    r.status["http://door/healthz"] = 502
    for _ in range(6):
        r.step()
    r.api_down = False
    r.status["http://door/healthz"] = 200
    for _ in range(6):
        r.step()
    assert r.sink.got == [], r.titles()                                         # it came up inside the grace period: no FIRING, no RESOLVED


@test
def w06_after_it_has_been_healthy_an_outage_is_reported_at_once():
    r = Rig()
    r.step()
    r.api_down = True
    r.step()
    assert r.sink.got == []                                                      # one failed look is not an outage
    r.step()
    assert r.titles() == ["CRITICAL api_unreachable"], r.titles()
    assert "ConnectionRefusedError" in r.sink.got[0].body


@test
def w07_the_door_must_answer_and_the_wording_says_why():
    r = Rig(door=True)
    r.step()
    r.status["http://door/healthz"] = None
    r.step()
    assert r.sink.got == []
    r.step()
    assert "does not answer (ConnectionRefusedError)" in r.sink.got[-1].body
    r.status["http://door/healthz"] = 200
    r.step()
    assert r.titles()[-1] == "RESOLVED door_down"
    r.status["http://door/healthz"] = 530
    r.step()
    r.step()
    assert "tunnel to this computer is down" in r.sink.got[-1].body


@test
def w08_the_canary_notices_a_door_that_lets_internals_through():
    r = Rig(door=True)
    r.step()
    assert r.sink.got == []
    r.status["http://door/metrics"] = 200
    r.status["http://door/readyz"] = 503
    r.status["http://door/docs"] = 403                                           # blocked by something in front: fine
    r.step()
    assert r.titles() == ["CRITICAL door_exposes_internals"], r.titles()
    b = r.sink.got[0].body
    assert "/metrics (HTTP 200)" in b and "/readyz (HTTP 503)" in b and "/docs" not in b and "docker compose stop proxy" in b
    r.status.pop("http://door/metrics")
    r.status.pop("http://door/readyz")
    r.step()
    assert r.titles()[-1] == "RESOLVED door_exposes_internals"


@test
def w08b_a_single_503_or_405_is_not_a_leak_but_a_repeated_one_is_and_a_sure_one_is_at_once():
    r = Rig(door=True)
    r.step()
    r.status["http://door/metrics"] = 503                                        # an edge having a bad moment: ONE look is not a leak
    r.step()
    assert r.sink.got == [], r.titles()
    r.status.pop("http://door/metrics")
    r.step()
    r.status["http://door/metrics"] = 503
    r.step()
    assert r.sink.got == [], "503, gone, 503 again are not two in a row"
    r.step()
    assert r.titles() == ["CRITICAL door_exposes_internals"], r.titles()          # twice in a row
    r2 = Rig(door=True)
    r2.step()
    r2.status["http://door/metrics"] = 401                                       # only the API itself says 401: no waiting
    r2.step()
    assert r2.titles() == ["CRITICAL door_exposes_internals"], r2.titles()


@test
def w08c_a_stale_quick_tunnel_address_says_what_to_do():
    r = Rig(public=True)
    r.step()
    r.status["http://public.example/healthz"] = None
    for _ in range(12):
        r.step()
    body = r.sink.got[-1].body
    assert "public address does not answer" in body and "python tunnel_tools.py" in body and "--no-deps watchdog" in body, body
    assert "public" in r.titles()[-1].lower()


@test
def w09_the_public_address_is_checked_gently_and_only_when_set():
    r = Rig(public=True)
    for _ in range(40):
        r.step()
    healths = [u for u in r.pings if u == "http://public.example/healthz"]
    canaries = [u for u in r.pings if u == "http://public.example/metrics"]
    assert len(healths) == 10 and len(canaries) == 2, (len(healths), len(canaries))       # every 4th round / every 20th round
    r.status["http://public.example/healthz"] = 530
    for _ in range(8):
        r.step()
    assert any("public_down" in t for t in r.titles()), r.titles()
    assert "tunnel to this computer is down" in r.sink.got[-1].body
    r0 = Rig()
    r0.step()
    assert not [u for u in r0.pings if "public" in u]


def write_backup_files(d, age_hours=None, drill=None):
    if age_hours is not None:
        json.dump({"folder": "x", "created_at": time.time() - age_hours * 3600}, open(os.path.join(d, "LATEST.json"), "w"))
    if drill is not None:
        ok, days = drill
        json.dump({"ok": ok, "at": time.time() - days * 86400}, open(os.path.join(d, "LAST_DRILL.json"), "w"))


@test
def w10_backup_alerts():
    d = tempfile.mkdtemp()
    try:
        r = Rig(backup_dir=d)
        r.wd.started = time.time()
        r.clock.t = time.time()
        r.step()
        assert r.sink.got == []                                                    # no backup yet but the stack is young: be patient
        r.step(advance=7 * 3600)
        assert r.titles() == ["WARNING backup_missing"], r.titles()
        write_backup_files(d, age_hours=1, drill=(True, 1))
        r.clock.t = time.time()
        r.step(advance=0)
        assert r.titles()[-1] == "RESOLVED backup_missing"
        write_backup_files(d, age_hours=60)
        r.step(advance=0)
        assert r.titles()[-1] == "WARNING backup_stale" and "60 h old" in r.sink.got[-1].body
        write_backup_files(d, age_hours=2)
        r.step(advance=0)
        assert r.titles()[-1] == "RESOLVED backup_stale"
        open(os.path.join(d, "LATEST.json"), "w").write("{nope")
        r.step(advance=0)
        assert r.titles()[-1] == "WARNING backup_broken"
        # the same file read by the backup tool and by the watchdog gives the same answer
        write_backup_files(d, age_hours=5)
        t = time.time()
        a = bt.backup_status(d, now=t)
        b = wdg.read_backup_status(d, t)
        assert a[0] == b[0] == "ok" and abs(a[1] - b[1]) < 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def w10b_a_computer_that_restarts_every_day_still_hears_about_a_missing_backup():
    d = tempfile.mkdtemp()
    try:
        old = time.time() - 3 * 86400
        os.utime(d, (old, old))                                                  # the backups folder was made 3 days ago and is still empty
        r = Rig(backup_dir=d)
        r.clock.t = time.time()
        r.wd.started = r.clock.t                                                 # ... but the watchdog container started just now
        r.step()
        assert r.titles() == ["WARNING backup_missing"], r.titles()
        # a folder made a minute ago is still given its time
        d2 = tempfile.mkdtemp()
        r2 = Rig(backup_dir=d2)
        r2.clock.t = time.time()
        r2.wd.started = r2.clock.t
        r2.step()
        assert r2.sink.got == []
        shutil.rmtree(d2, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def w11b_the_never_practised_reminder_counts_from_the_oldest_backup_not_from_the_container_start():
    d = tempfile.mkdtemp()
    try:
        write_backup_files(d, age_hours=1)
        first = os.path.join(d, "20260101-120000")
        os.makedirs(first)
        old = time.time() - 3 * 86400
        os.utime(first, (old, old))                                              # the first backup is 3 days old and no restore was ever practised
        r = Rig(backup_dir=d)
        r.clock.t = time.time()
        r.wd.started = r.clock.t                                                 # the container just started
        r.step()
        assert r.titles() == ["WARNING drill_overdue"] and "never been practised" in r.sink.got[0].body, r.titles()
        # a backup made an hour ago, no restore yet: be patient for a day
        d3 = tempfile.mkdtemp()
        write_backup_files(d3, age_hours=1)
        os.makedirs(os.path.join(d3, "20260101-120000"))
        r3 = Rig(backup_dir=d3)
        r3.clock.t = time.time()
        r3.wd.started = r3.clock.t - 40 * 3600                                   # (even though the container has been up for 40 hours)
        r3.step()
        assert r3.sink.got == [], r3.titles()
        shutil.rmtree(d3, ignore_errors=True)
        # a restore practice that FAILED is announced at once
        write_backup_files(d, age_hours=1, drill=(False, 0.01))
        d4 = tempfile.mkdtemp()
        write_backup_files(d4, age_hours=1, drill=(False, 0.01))
        r4 = Rig(backup_dir=d4)
        r4.clock.t = time.time()
        r4.wd.started = r4.clock.t
        r4.step()
        assert r4.titles() == ["WARNING drill_overdue"] and "failed the last time" in r4.sink.got[0].body
        shutil.rmtree(d4, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def w11_a_restore_that_was_never_practised_is_pointed_out_after_the_first_day():
    d = tempfile.mkdtemp()
    try:
        write_backup_files(d, age_hours=1)
        os.makedirs(os.path.join(d, "20260101-120000"))                          # the first backup: made just now
        r = Rig(backup_dir=d)
        r.clock.t = time.time()
        r.wd.started = r.clock.t
        r.step()
        assert r.sink.got == []
        r.step(advance=25 * 3600)
        write_backup_files(d, age_hours=1)
        r.step(advance=0)
        assert r.titles() == ["WARNING drill_overdue"] and "never been practised" in r.sink.got[0].body, r.titles()
        write_backup_files(d, age_hours=1, drill=(False, 0.1))
        r.step(advance=0)
        assert r.titles() == ["WARNING drill_overdue"]                                # still firing: no second message
        write_backup_files(d, age_hours=1, drill=(True, 0.1))
        r.step(advance=0)
        assert r.titles()[-1] == "RESOLVED drill_overdue"
        write_backup_files(d, age_hours=1, drill=(True, 40))
        r.step(advance=0)
        assert r.titles()[-1] == "WARNING drill_overdue" and ("40 days ago" in r.sink.got[-1].body or "41 days ago" in r.sink.got[-1].body), r.sink.got[-1].body
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def w12_a_critical_problem_is_repeated_every_six_hours_a_warning_never():
    r = Rig(door=True)
    r.step()
    r.m.dep_up.set(0, name="redis")
    r.step()
    r.status["http://door/healthz"] = 502
    r.step()
    r.step()
    r.m.cache = None
    base = len(r.sink.got)
    r.step(advance=5 * 3600)
    assert len(r.sink.got) == base                                               # five hours: not yet
    r.step(advance=1.2 * 3600)
    assert r.titles()[-1].startswith("STILL FIRING") and "redis is not answering" in r.sink.got[-1].body
    n = len(r.sink.got)
    r.step(advance=3600)
    assert len(r.sink.got) == n
    r.step(advance=5.5 * 3600)
    assert r.titles()[-1].startswith("STILL FIRING") and len(r.sink.got) == n + 1
    # a warning
    w = Rig()
    w.step()
    for _ in range(3):
        w.m.http_requests.inc(route="/ask", method="POST", status="504")
        w.step()
    got = len(w.sink.got)
    assert got >= 1 and w.sink.got[0].severity == "warning"
    w.step(advance=30 * 3600)
    assert not any(t.startswith("STILL FIRING") for t in w.titles())


@test
def w13_when_nothing_can_be_delivered_nothing_is_lost_and_nothing_is_repeated():
    r = Rig()
    r.sink.down = True
    r.step()
    r.m.dep_up.set(0, name="redis")
    r.step()
    for _ in range(5):
        r.step()
    assert r.sink.got == [] and len(r.disp.pending) == 1
    r.sink.down = False
    r.step()
    assert r.titles() == ["CRITICAL dependency_down"] and "delivered late" in r.sink.got[0].body and not r.disp.pending


@test
def w14_a_failing_heartbeat_ping_is_logged_without_the_address_and_does_not_stop_anything():
    r = Rig(heartbeat="http://hb.example/ping/SECRET-UUID-1234")
    r.status["http://hb.example/ping/SECRET-UUID-1234"] = None
    import logging
    records = []
    h = logging.Handler()
    h.emit = records.append
    wdg.log.addHandler(h)
    try:
        r.step()
        r.m.dep_up.set(0, name="redis")
        r.step()
    finally:
        wdg.log.removeHandler(h)
    assert r.titles() == ["CRITICAL dependency_down"]
    text = " ".join(x.getMessage() for x in records)
    assert "heartbeat ping failed" in text and "ConnectionRefusedError" in text and "SECRET-UUID" not in text


@test
def w15_one_bad_round_does_not_end_the_watching():
    r = Rig()
    calls = {"n": 0}
    orig = r.wd.step

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return orig()
    r.wd.step = flaky
    r.wd.run(stop=lambda: calls["n"] >= 4, sleep=lambda s: r.clock.advance(30))
    assert calls["n"] == 4
    assert r.titles() == ["watchdog started"]


@test
def w16_config_comes_from_env_and_describe_shows_no_secret():
    env = {"RAG_METRICS_KEY": "METRICS-KEY-VALUE-123456", "HEARTBEAT_URL_SECRET": "https://hc-ping.com/UUID-SECRET-1", "WATCH_API_URL": "http://rag:8000/",
           "WATCH_DOOR_URL": "http://proxy:8080", "PUBLIC_URL": "https://abc.trycloudflare.com/", "WATCH_COST_PER_HOUR_USD": "0.25",
           "WATCH_DISK_FREE_GB": "5", "WATCH_INTERVAL": "10", "BACKUP_MAX_AGE_HOURS": "30", "ALERT_REMIND_HOURS": "bad"}
    c = wdg.Config.from_env(env)
    assert c.api_url == "http://rag:8000" and c.public_url == "https://abc.trycloudflare.com" and c.metrics_key == "METRICS-KEY-VALUE-123456"
    assert c.thresholds.cost_per_hour_usd == 0.25 and c.thresholds.disk_free_bytes == 5 * 1024 ** 3 and c.interval == 10 and c.backup_max_age_h == 30
    assert c.remind_hours == 6.0                                                 # junk falls back to the default
    text = "\n".join(c.describe())
    for secret in ("METRICS-KEY-VALUE", "UUID-SECRET", "abc.trycloudflare.com/"):
        assert secret not in text, secret
    assert "heartbeat: on" in text
    d = wdg.Config.from_env({})
    assert d.api_url == "http://rag:8000" and d.door_url == "" and d.interval == 30


@test
def w17_selfcheck_follows_the_alive_file():
    p = os.path.join(tempfile.gettempdir(), f"alive_{os.getpid()}_x")
    assert wdg.selfcheck(p) == 1
    open(p, "w").write("1")
    assert wdg.selfcheck(p) == 0
    os.utime(p, (time.time() - 600, time.time() - 600))
    assert wdg.selfcheck(p) == 1
    os.remove(p)


@test
def w18_the_whole_chain_with_the_real_api_and_fake_phones():
    live = Live()
    tg, nf, door, ping = FakeTelegram(), FakeNtfy(), FakeDoor(), FakePing()
    d = tempfile.mkdtemp()
    stop = {"v": False}
    try:
        write_backup_files(d, age_hours=1, drill=(True, 1))
        env = {"ALERT_LABEL": "rag-e2e"}
        cfg = wdg.Config(api_url=live.url, door_url=door.url, metrics_key=METRICS_KEY, interval=0.3, grace=0, backup_dir=d,
                         heartbeat_url=ping.url + "/ping/abc", env=env)
        disp = nt.Dispatcher([nt.TelegramNotifier(FakeTelegram.TOKEN, "424242", tg.url), nt.NtfyNotifier(nf.topic, nf.url)], retry_waits=(), sleep=lambda s: None, log=lambda s: None)
        wd = wdg.Watchdog(cfg, disp)
        t = threading.Thread(target=wd.run, args=(lambda: stop["v"], time.sleep), daemon=True)
        t.start()

        def wait(cond, secs=15):
            end = time.time() + secs
            while time.time() < end:
                if cond():
                    return True
                time.sleep(0.1)
            return False
        assert wait(lambda: len(tg.messages()) >= 1), "no start message"
        assert "watchdog started" in tg.messages()[0]["text"] and nf.requests
        assert wait(lambda: len(ping.pings()) >= 3), "no heartbeat pings"
        live.pipeline.state["redis_down"] = True
        assert wait(lambda: any("dependency_down" in m["text"] for m in tg.messages())), [m["text"] for m in tg.messages()]
        assert wait(lambda: any(r["headers"].get("title", "").endswith("dependency_down") for r in nf.requests)), "ntfy did not get it"
        live.pipeline.state["redis_down"] = False
        assert wait(lambda: any("RESOLVED" in m["text"] for m in tg.messages())), [m["text"] for m in tg.messages()]
        door.answers["/metrics"] = 200
        assert wait(lambda: any("door_exposes_internals" in m["text"] for m in tg.messages())), "canary did not fire"
        door.answers.pop("/metrics")
        door.answers["/healthz"] = 502
        assert wait(lambda: any("door_down" in m["text"] for m in tg.messages()))
    finally:
        stop["v"] = True
        live.stop()
        for f in (tg, nf, door, ping):
            f.stop()
        shutil.rmtree(d, ignore_errors=True)
    blob = json.dumps([m["text"] for m in tg.messages()]) + json.dumps([r["headers"] for r in nf.requests])
    assert METRICS_KEY not in blob


@test
def w19_command_line_config_once_and_selfcheck():
    live = Live()
    try:
        env = dict(os.environ, RAG_METRICS_KEY=METRICS_KEY, WATCH_API_URL=live.url, WATCHDOG_ALIVE_FILE=os.path.join(tempfile.gettempdir(), "no_such_alive_file"))
        py = [sys.executable, os.path.join(HERE, "watchdog.py")]
        r = subprocess.run(py + ["--config"], capture_output=True, text=True, env=env)
        assert r.returncode == 0 and "API:" in r.stdout and METRICS_KEY not in r.stdout + r.stderr, r.stdout
        r = subprocess.run(py + ["--once"], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "nothing is firing" in r.stdout, r.stdout + r.stderr
        live.pipeline.state["redis_down"] = True
        time.sleep(0.2)
        r = subprocess.run(py + ["--once"], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 1 and "FIRING dependency_down" in r.stdout, r.stdout + r.stderr
        os.path.exists(env["WATCHDOG_ALIVE_FILE"]) and os.remove(env["WATCHDOG_ALIVE_FILE"])
        assert subprocess.run(py + ["--selfcheck"], env=env).returncode == 1
        open(env["WATCHDOG_ALIVE_FILE"], "w").write("1")
        assert subprocess.run(py + ["--selfcheck"], env=env).returncode == 0
        os.remove(env["WATCHDOG_ALIVE_FILE"])
        env2 = dict(env)
        env2.pop("RAG_METRICS_KEY")
        assert subprocess.run(py + ["--once"], capture_output=True, text=True, env=env2).returncode == 2
    finally:
        live.stop()


@test
def w20_it_says_once_in_the_log_when_it_has_first_seen_the_api_healthy():
    import logging
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())
    h = H()
    wdg.log.addHandler(h)
    old_level = wdg.log.level
    wdg.log.setLevel(logging.INFO)
    try:
        r = Rig()
        r.api_down = True
        r.step()
        r.step()
        assert not [m for m in seen if "answers /metrics" in m], "must not claim it before it has happened"
        r.api_down = False
        r.step()
        r.step()
        r.step()
        assert len([m for m in seen if "answers /metrics" in m]) == 1, seen
    finally:
        wdg.log.removeHandler(h)
        wdg.log.setLevel(old_level)


if __name__ == "__main__":
    bad = 0
    for name, ok, tb in results:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
            print(tb)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)
