"""
watchdog.py -- Day 21: the always-on watcher. It runs as its own container (service `watchdog`), starts with the stack, and sends
what it finds to your phone or inbox (notifiers.py).

Every 30 seconds it looks at:
  the API         /metrics, with all the Day 20 rules (dependencies, errors, timeouts, disk, memory, cost, restarts...)
  the front door  does /healthz answer through it?  and the canary: are /metrics, /readyz, /statusz, /docs still CLOSED to the outside?
  the public URL  (only if PUBLIC_URL is set) the same two questions, from the outside, through the tunnel
  the backups     is the newest backup recent?  was a restore practised lately?
and tells you only when something CHANGES (FIRING / RESOLVED), once, in one message per round. A critical problem that is still
there is repeated every few hours so it cannot be forgotten.

It also pings an uptime service (HEARTBEAT_URL_SECRET) every round. That is the answer to "who watches the watcher?": if the whole
computer is off, asleep or offline, the pings stop and the SERVICE (not this program) tells you.

    python watchdog.py                run (this is what the container does)
    python watchdog.py --once         one round, print what it found, send nothing
    python watchdog.py --selfcheck    exit 0 if the main loop ran in the last 3 minutes (the container health check)
    python watchdog.py --config       show what it is set up to watch (no secrets)

Standard library only (plus alert_rules.py and notifiers.py from this folder).
"""
import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alert_rules as ar            # noqa: E402
import notifiers as nt              # noqa: E402

log = logging.getLogger("watchdog")
ALIVE_FILE = os.environ.get("WATCHDOG_ALIVE_FILE", "/tmp/watchdog.alive")
CLOSED_FROM_OUTSIDE = ("/metrics", "/readyz", "/statusz", "/docs", "/openapi.json")
OPEN_LOOKING_STATUS = (200, 401, 405, 422, 503)       # answers only the API itself would give: the door let the request through
SURE_LEAK_STATUS = (200, 401, 422)                    # ... and these three nobody else would give: alert at once.  405 / 503 can also come from a
                                                      # server or an edge in front of the door having a bad moment: they must be seen twice in a row
GRACE_ALERTS = ("api_unreachable", "door_down", "public_down")


def _f(env, name, default):
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass
class Config:
    api_url: str = "http://rag:8000"
    door_url: str = ""
    public_url: str = ""
    metrics_key: str = ""
    interval: float = 30.0
    grace: float = 900.0
    backup_dir: str = "/backups"
    backup_max_age_h: float = 48.0
    drill_max_age_days: float = 35.0
    remind_hours: float = 6.0
    label: str = "rag-api"
    heartbeat_url: str = ""
    debug: bool = False
    thresholds: ar.Thresholds = field(default_factory=ar.Thresholds)
    env: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_env(env: Optional[Dict[str, str]] = None) -> "Config":
        env = dict(os.environ if env is None else env)
        th = ar.Thresholds()
        if env.get("WATCH_COST_PER_HOUR_USD"):
            th.cost_per_hour_usd = _f(env, "WATCH_COST_PER_HOUR_USD", th.cost_per_hour_usd)
        if env.get("WATCH_SLOW_P95_SECONDS"):
            th.slow_p95_seconds = _f(env, "WATCH_SLOW_P95_SECONDS", th.slow_p95_seconds)
        if env.get("WATCH_MEMORY_GB"):
            th.memory_bytes = _f(env, "WATCH_MEMORY_GB", 3) * 1024 ** 3
        if env.get("WATCH_DISK_FREE_GB"):
            th.disk_free_bytes = _f(env, "WATCH_DISK_FREE_GB", 2) * 1024 ** 3
        return Config(
            api_url=env.get("WATCH_API_URL", "http://rag:8000").rstrip("/"), door_url=env.get("WATCH_DOOR_URL", "").rstrip("/"),
            public_url=env.get("PUBLIC_URL", "").rstrip("/"), metrics_key=env.get("RAG_METRICS_KEY", ""),
            interval=max(1.0, _f(env, "WATCH_INTERVAL", 30)), grace=_f(env, "WATCH_STARTUP_GRACE", 900),
            backup_dir=env.get("BACKUP_DIR", "/backups"), backup_max_age_h=_f(env, "BACKUP_MAX_AGE_HOURS", 48),
            drill_max_age_days=_f(env, "DRILL_MAX_AGE_DAYS", 35), remind_hours=_f(env, "ALERT_REMIND_HOURS", 6),
            label=env.get("ALERT_LABEL", "rag-api"), heartbeat_url=env.get("HEARTBEAT_URL_SECRET", ""),
                      debug=env.get("WATCH_DEBUG", "") in ("1", "true", "yes"), thresholds=th, env=env)

    def describe(self) -> List[str]:
        return [f"label: {self.label}", f"API:   {self.api_url}", f"door:  {self.door_url or 'not watched'}",
                f"public: {'watched (the address is not printed)' if self.public_url else 'not watched'}", f"backups: {self.backup_dir} (newest must be under {self.backup_max_age_h:g} h old)",
                f"heartbeat: {'on' if self.heartbeat_url else 'off'}", f"every {self.interval:g}s; quiet for the first {self.grace:g}s after start"]


# ------------------------------------------------------------ probes --------
def http_probe(url: str, timeout: float = 5.0) -> Tuple[Optional[int], str]:
    """(status, '') or (None, error class). A 404 is an ANSWER, not an error."""
    req = urllib.request.Request(url, headers={"User-Agent": nt.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except urllib.error.URLError as e:
        return None, type(e.reason).__name__
    except (OSError, ValueError) as e:
        return None, type(e).__name__


def read_backup_status(backup_dir: str, now: float) -> Tuple[str, Optional[float]]:
    p = os.path.join(backup_dir, "LATEST.json")
    if not os.path.exists(p):
        return "missing", None
    try:
        return "ok", now - float(json.load(open(p, encoding="utf-8"))["created_at"])
    except Exception:
        return "broken", None


def read_drill_status(backup_dir: str, now: float) -> Tuple[str, Optional[float]]:
    p = os.path.join(backup_dir, "LAST_DRILL.json")
    if not os.path.exists(p):
        return "never", None
    try:
        d = json.load(open(p, encoding="utf-8"))
        return ("ok" if d.get("ok") else "failed"), now - float(d["at"])
    except Exception:
        return "never", None


def file_time(path: str) -> Optional[float]:
    """When a file or folder was last changed (None if it is not there)."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def oldest_backup_time(backup_dir: str, mtime: Callable[[str], Optional[float]] = file_time) -> Optional[float]:
    """When the oldest backup still on disk was made (backup folders are named 20261022-130000)."""
    try:
        names = [n for n in os.listdir(backup_dir) if re.fullmatch(r"\d{8}-\d{6}", n)]
    except OSError:
        return None
    times = [t for t in (mtime(os.path.join(backup_dir, n)) for n in names) if t is not None]
    return min(times) if times else None


class Tracker:
    """Remembers what is firing and reports CHANGES, like alert_rules.Watcher does for the API rules."""

    def __init__(self):
        self.firing: Dict[str, ar.Alert] = {}

    def update(self, current: List[ar.Alert], now: float) -> List[ar.Event]:
        out, names = [], {a.name for a in current}
        for a in current:
            if a.name not in self.firing:
                out.append(ar.Event("FIRING", a, now))
            self.firing[a.name] = a
        for n in [n for n in self.firing if n not in names]:
            out.append(ar.Event("RESOLVED", self.firing.pop(n), now))
        return out


# ------------------------------------------------------------ the watchdog --
class Watchdog:
    def __init__(self, cfg: Config, dispatcher: nt.Dispatcher, now: Callable[[], float] = time.time,
                 probe: Callable[[str], Tuple[Optional[int], str]] = http_probe,
                 fetch: Callable[[str, str], ar.Snapshot] = ar.fetch,
                 mtime: Callable[[str], Optional[float]] = file_time):
        self.cfg, self.disp, self.now, self.probe, self.fetch, self.mtime = cfg, dispatcher, now, probe, fetch, mtime
        self.started = now()
        self.api = ar.Watcher(cfg.thresholds, unreachable_after=2)
        self.extra = Tracker()
        self.seen_ok = False
        self.announced: Dict[str, float] = {}               # alert name -> when we told you (also when we last reminded you)
        self.severity: Dict[str, str] = {}
        self.fail_counts: Dict[str, int] = {}
        self._cache: Dict[str, List[ar.Alert]] = {}
        self._last_status: Dict[str, Optional[int]] = {}
        self.rounds = 0

    # --- the extra checks
    def _debounced(self, key: str, failing: bool, needed: int) -> bool:
        self.fail_counts[key] = (self.fail_counts.get(key, 0) + 1) if failing else 0
        return self.fail_counts[key] >= needed

    def _front_checks(self, base: str, name: str, needed: int, every: int, canary_every: int) -> List[ar.Alert]:
        """Health every `every` rounds, the closed-routes canary every `canary_every` rounds; in between the last answer stands."""
        who = "front door" if name == "door" else "public address"
        if self.rounds % every == 0 or f"{name}:health" not in self._cache:
            out: List[ar.Alert] = []
            status, err = self.probe(base + "/healthz")
            self._last_status[name] = status
            if self._debounced(name + "_down", status != 200, needed):
                if status is None:
                    why = f"does not answer ({err})"
                elif status in (502, 503, 504):
                    why = f"answers {status}: the API behind it is not answering"
                elif status in (530, 521, 522, 523, 1033):
                    why = f"answers {status}: the tunnel to this computer is down"
                else:
                    why = f"answers {status} instead of 200"
                hint = (" A free quick-tunnel address changes every time the tunnel restarts: run  python tunnel_tools.py  and then  "
                        "docker compose up -d --force-recreate --no-deps watchdog") if name == "public" else ""
                out.append(ar.Alert(f"{name}_down", "critical", f"the {who} {why}.{hint}" if hint else f"the {who} {why}"))
            self._cache[f"{name}:health"] = out
        if (self.rounds % canary_every == 0 or f"{name}:canary" not in self._cache) and self._last_status.get(name) is not None:
            hard, soft = [], []
            for path in CLOSED_FROM_OUTSIDE:
                st, _ = self.probe(base + path)
                if st in SURE_LEAK_STATUS:
                    hard.append(f"{path} (HTTP {st})")
                elif st in OPEN_LOOKING_STATUS:
                    soft.append(f"{path} (HTTP {st})")
            confirmed = self._debounced(name + "_soft_leak", bool(soft) and not hard, 2)
            leaks = hard + (soft if (hard or confirmed) else [])
            self._cache[f"{name}:canary"] = ([ar.Alert(f"{name}_exposes_internals", "critical",
                                                       f"the {who} lets these through: {', '.join(leaks)}. Take it down now: docker compose stop proxy")] if leaks else [])
        return self._cache.get(f"{name}:health", []) + self._cache.get(f"{name}:canary", [])

    def _backup_checks(self) -> List[ar.Alert]:
        c, t = self.cfg, self.now()
        out: List[ar.Alert] = []
        status, age = read_backup_status(c.backup_dir, t)
        if status == "missing":
            # How long has the backups folder existed without a backup in it?  (NOT how long this container has been up:
            # a computer that is restarted every day would never get to the answer.)
            since = self.mtime(c.backup_dir)
            if t - (since if since is not None else self.started) > 6 * 3600:
                out.append(ar.Alert("backup_missing", "warning", "no backup has been made yet (python backup_tools.py backup)"))
        elif status == "broken":
            out.append(ar.Alert("backup_broken", "warning", "backups/LATEST.json cannot be read"))
        elif age is not None and age > c.backup_max_age_h * 3600:
            out.append(ar.Alert("backup_stale", "warning", f"the newest backup is {age / 3600:.0f} h old (limit {c.backup_max_age_h:g} h)"))
        if status == "ok":
            d_status, d_age = read_drill_status(c.backup_dir, t)
            first = oldest_backup_time(c.backup_dir, self.mtime)
            waiting = d_status == "never" and (first is None or t - first <= 24 * 3600)       # a day to practise after the first backup
            if not waiting and (d_status != "ok" or (d_age is not None and d_age > c.drill_max_age_days * 86400)):
                if d_status == "never":
                    why = "has never been practised"
                elif d_status == "failed":
                    why = "failed the last time"
                else:
                    why = f"was last practised {d_age / 86400:.0f} days ago"
                out.append(ar.Alert("drill_overdue", "warning", f"restoring a backup {why} (python backup_tools.py drill)"))
        return out

    # --- one round
    def step(self) -> List[ar.Event]:
        t = self.now()
        c = self.cfg
        snapshot, error = None, ""
        try:
            snapshot = self.fetch(c.api_url, c.metrics_key)
            snapshot.ts = t
            if not self.seen_ok:
                log.info("the API answers /metrics: from now on a problem is announced at once")
            self.seen_ok = True
        except Exception as e:
            error = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__
        events = self.api.feed(snapshot, None if snapshot else error)
        current: List[ar.Alert] = []
        if c.door_url:
            current += self._front_checks(c.door_url, "door", needed=2, every=1, canary_every=1)
        if c.public_url:                                                  # over the internet: gently (every 2 minutes, canary every 10)
            current += self._front_checks(c.public_url, "public", needed=2, every=4, canary_every=20)
        if c.backup_dir:
            current += self._backup_checks()
        events += self.extra.update(current, t)
        if c.debug:                                                    # WATCH_DEBUG=1: one line per round (never a key or an address)
            log.info("round %d: api=%s door=%s public=%s seen_ok=%s firing=%s", self.rounds, error or "ok", self._last_status.get("door", "-"),
                     self._last_status.get("public", "-"), self.seen_ok, sorted({**self.api.firing, **self.extra.firing}))
        events = self._filter(events, t)
        self._deliver(events, t)
        self._remind(t)
        self.disp.flush()
        self._pulse(t)
        self.rounds += 1
        return events

    def _filter(self, events: List[ar.Event], t: float) -> List[ar.Event]:
        """Quiet while the stack is still starting (the first start loads models for minutes); RESOLVED only for what was announced."""
        out = []
        in_grace = (not self.seen_ok) and (t - self.started < self.cfg.grace)
        for e in events:
            if e.kind == "FIRING":
                if in_grace and e.alert.name in GRACE_ALERTS:
                    continue
                self.announced[e.alert.name] = t
                self.severity[e.alert.name] = e.alert.severity
                out.append(e)
            elif e.alert.name in self.announced:
                out.append(e)
            # a RESOLVED for something we never announced is dropped
        for name, alert in {**self.api.firing, **self.extra.firing}.items():           # began during the quiet period, still firing now
            if name not in self.announced and not (in_grace and name in GRACE_ALERTS):
                self.announced[name] = t
                self.severity[name] = alert.severity
                out.append(ar.Event("FIRING", alert, t))
        return out

    def _label(self, t):
        return f"{self.cfg.label} · {nt.stamp(self.cfg.env, t)}"

    def _deliver(self, events: List[ar.Event], t: float) -> None:
        firing = [e for e in events if e.kind == "FIRING"]
        resolved = [e for e in events if e.kind == "RESOLVED"]
        for e in firing:
            log.warning("FIRING %s: %s", e.alert.name, e.alert.message)
        for e in resolved:
            log.info("RESOLVED %s", e.alert.name)
            self.announced.pop(e.alert.name, None)
        if firing:
            worst = "critical" if any(e.alert.severity == "critical" for e in firing) else "warning"
            title = (f"{worst.upper()} {firing[0].alert.name}" if len(firing) == 1 else f"{worst.upper()}: {len(firing)} alerts ({', '.join(e.alert.name for e in firing)})")
            body = "\n".join(f"• [{e.alert.severity}] {e.alert.name}: {e.alert.message}" if len(firing) > 1 else e.alert.message for e in firing)
            self.disp.send(nt.Message(title, body + "\n\n" + self._label(t), worst))
        if resolved:
            names = ", ".join(e.alert.name for e in resolved)
            self.disp.send(nt.Message(f"RESOLVED {names}", "Back to normal.\n\n" + self._label(t), "resolved"))

    def _remind(self, t: float) -> None:
        wait = self.cfg.remind_hours * 3600
        if wait <= 0:
            return
        due = [n for n, sent in self.announced.items() if self.severity.get(n) == "critical" and t - sent >= wait]
        if not due:
            return
        alerts = {**{a.name: a for a in self.api.firing.values()}, **self.extra.firing}
        still = [alerts[n] for n in due if n in alerts]
        for n in due:
            self.announced[n] = t
        if still:
            self.disp.send(nt.Message("STILL FIRING: " + ", ".join(a.name for a in still),
                                      "\n".join(f"• {a.name}: {a.message}" for a in still) + "\n\n" + self._label(t), "critical"))

    def _pulse(self, t: float) -> None:
        try:
            with open(ALIVE_FILE, "w") as f:
                f.write(str(t))
        except OSError:
            pass
        if self.cfg.heartbeat_url:
            status, err = self.probe(self.cfg.heartbeat_url)
            if status is None or status >= 400:
                log.warning("heartbeat ping failed (%s)", err or f"HTTP {status}")

    def start_message(self) -> None:
        names = self.disp.channels()
        self.disp.send(nt.Message("watchdog started", f"Watching {self.cfg.label}. Delivery: {', '.join(names) if names else 'none set up (log only)'}.\n\n" + self._label(self.now()), "info"))

    def run(self, stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep) -> None:
        self.start_message()
        while not stop():
            try:
                self.step()
            except Exception as e:                                       # one bad round must never end the watching
                log.error("round failed (%s)", type(e).__name__)
            sleep(self.cfg.interval)


# ------------------------------------------------------------ entry points --
def selfcheck(path: str = ALIVE_FILE, max_age: float = 180.0) -> int:
    try:
        return 0 if time.time() - os.path.getmtime(path) < max_age else 1
    except OSError:
        return 1


def _setup_logging(env) -> None:
    try:
        import monitoring
        monitoring.configure_logging(env)
    except Exception:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true")
    g.add_argument("--selfcheck", action="store_true")
    g.add_argument("--config", action="store_true")
    a = ap.parse_args(argv)
    if a.selfcheck:
        return selfcheck()
    env = dict(os.environ)
    cfg = Config.from_env(env)
    if a.config:
        print("\n".join(cfg.describe() + ["delivery: " + (", ".join(n.name for n in nt.from_env(env)) or "none set up (log only)")]))
        return 0
    if not cfg.metrics_key:
        print("RAG_METRICS_KEY is not set: the watchdog cannot read /metrics", file=sys.stderr)
        return 2
    _setup_logging(env)
    notifiers = [nt.ConsoleNotifier()] if a.once else nt.from_env(env) + [nt.ConsoleNotifier(lambda s: log.warning(s))]
    if a.once:
        wd = Watchdog(cfg, nt.Dispatcher([nt.ConsoleNotifier()]))
        wd.step()
        time.sleep(1)
        events = wd.step()
        print("found:", [f"{e.kind} {e.alert.name}" for e in events] or "nothing is firing")
        return 1 if wd.api.firing or wd.extra.firing else 0
    wd = Watchdog(cfg, nt.Dispatcher(notifiers, log=lambda s: log.warning(s)))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    log.info("watchdog started: %s", "; ".join(cfg.describe()[1:4]))

    def sleeper(sec):
        end = time.time() + sec
        while time.time() < end and not stop["now"]:
            time.sleep(min(1.0, end - time.time()))
    wd.run(lambda: stop["now"], sleeper)
    log.info("watchdog stopped")
    return 0


def _utf8_stdio():
    """Windows hands a program a pipe that only knows cp1252, which cannot print the marks used here: ask for UTF-8 instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _utf8_stdio()
    raise SystemExit(main())
