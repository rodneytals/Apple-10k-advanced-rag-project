"""
alert_rules.py -- Day 20: turn the numbers on /metrics into "something needs a human" messages.

    python alert_rules.py                      # watch forever (Ctrl+C to stop); reads the key from .env
    python alert_rules.py --once               # one look, right now; exit code 0 = fine, 1 = something is firing, 2 = cannot reach it

Every 30 seconds it reads /metrics, compares with what it saw up to 5 minutes ago, and prints
    FIRING   [critical] dependency_down: redis is not answering
    RESOLVED            dependency_down
only when something CHANGES, so a long outage prints one line, not one per scrape.
Delivering those lines to your phone or e-mail is the next step (Day 21); `notify=` is the place it plugs in.

Standard library only.
"""
import argparse
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Callable, Deque, Dict, List, Optional, Tuple

Series = Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float]

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+-?\d+)?$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> Series:
    out: Series = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, labels, value = m.groups()
        try:
            v = float(value.replace("+Inf", "inf").replace("-Inf", "-inf"))
        except ValueError:
            continue
        pairs = tuple(sorted((k, val.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")) for k, val in _LABEL.findall(labels or "")))
        out[(name, pairs)] = v
    return out


@dataclass
class Snapshot:
    ts: float
    series: Series

    def _match(self, metric: str, labels: Dict[str, str]):
        for (n, pairs), v in self.series.items():
            if n == metric and all(dict(pairs).get(k) == val for k, val in labels.items()):
                yield dict(pairs), v

    def get(self, metric: str, **labels) -> Optional[float]:
        vals = [v for _, v in self._match(metric, labels)]
        return sum(vals) if vals else None

    def each(self, metric: str, **labels) -> List[Tuple[Dict[str, str], float]]:
        return list(self._match(metric, labels))


def delta(prev: Optional[Snapshot], cur: Snapshot, metric: str, **labels) -> float:
    """How much a counter went up between two looks. A counter that went DOWN means the service restarted: count from zero."""
    if prev is None:
        return 0.0
    total = 0.0
    before = {tuple(sorted(l.items())): v for l, v in prev._match(metric, labels)}
    for l, v in cur._match(metric, labels):
        b = before.get(tuple(sorted(l.items())), 0.0)
        total += v - b if v >= b else v
    return total


def quantile(prev: Optional[Snapshot], cur: Snapshot, metric: str, q: float, min_samples: int = 5, **labels) -> Optional[float]:
    """Estimate of a histogram quantile over the time between two looks (same method Prometheus uses)."""
    if prev is None:
        return None
    before = {tuple(sorted(l.items())): v for l, v in prev._match(metric + "_bucket", labels)}
    per_le: Dict[float, float] = {}
    for l, v in cur._match(metric + "_bucket", labels):
        key = tuple(sorted(l.items()))
        b = before.get(key, 0.0)
        d = v - b if v >= b else v
        le = float(l["le"].replace("+Inf", "inf"))
        per_le[le] = per_le.get(le, 0.0) + d
    if not per_le:
        return None
    edges = sorted(per_le)
    total = per_le[edges[-1]] if math.isinf(edges[-1]) else None
    if total is None or total < min_samples:
        return None
    rank = q * total
    lower, lower_count = 0.0, 0.0
    for le in edges:
        count = per_le[le]
        if count >= rank:
            if math.isinf(le):
                return lower                                  # slower than the last bucket: at least this
            if count == lower_count:
                return le
            return lower + (le - lower) * (rank - lower_count) / (count - lower_count)
        lower, lower_count = le, count
    return lower


# ---------------------------------------------------------------- the rules --
@dataclass
class Thresholds:
    window_seconds: float = 300.0          # how far back to compare
    error_rate: float = 0.05               # share of /ask answered with 500/502
    min_errors: int = 2                    # ...and at least this many
    timeouts: int = 1                      # 504s in the window
    busy: int = 3                          # 503s in the window
    auth_failures: int = 10                # 401s in the window
    slow_p95_seconds: float = 30.0         # p95 of /ask duration
    cost_per_hour_usd: float = 1.0
    memory_bytes: float = 3 * 1024 ** 3
    disk_free_bytes: float = 2 * 1024 ** 3


@dataclass
class Alert:
    name: str
    severity: str
    message: str


def _ask_counts(prev, cur, *statuses: str) -> float:
    return sum(delta(prev, cur, "rag_http_requests_total", route="/ask", status=s) for s in statuses)


def evaluate(prev: Optional[Snapshot], cur: Snapshot, th: Optional[Thresholds] = None) -> List[Alert]:
    """Everything that is wrong right now. `prev` is an older look (None on the very first one: only 'right now' rules can fire)."""
    th = th or Thresholds()
    out: List[Alert] = []

    down = [l["name"] for l, v in cur.each("rag_dependency_up") if v == 0]
    if down:
        out.append(Alert("dependency_down", "critical", f"{', '.join(sorted(down))} is not answering"))

    free = [(l.get("path", "?"), v) for l, v in cur.each("rag_disk_free_bytes") if v < th.disk_free_bytes]
    if free:
        out.append(Alert("disk_low", "critical", "; ".join(f"{p}: {v / 1024 ** 3:.1f} GB free" for p, v in free)))

    rss = cur.get("rag_process_resident_memory_bytes")
    if rss is not None and rss > th.memory_bytes:
        out.append(Alert("memory_high", "warning", f"the API process holds {rss / 1024 ** 3:.1f} GB (limit you set: {th.memory_bytes / 1024 ** 3:.1f} GB)"))

    slots, in_flight, waiting = cur.get("rag_pipeline_slots"), cur.get("rag_pipeline_in_flight"), cur.get("rag_pipeline_waiting")
    if prev is not None and slots and in_flight is not None and in_flight >= slots and (waiting or 0) >= 1:
        p_in, p_wait = prev.get("rag_pipeline_in_flight"), prev.get("rag_pipeline_waiting")
        if p_in is not None and p_in >= slots and (p_wait or 0) >= 1:
            out.append(Alert("saturated", "warning", f"all {int(slots)} slot(s) busy and requests are waiting, on two looks in a row"))

    if prev is None:
        return out

    start_before, start_now = prev.get("rag_process_start_time_seconds"), cur.get("rag_process_start_time_seconds")
    if start_before is not None and start_now is not None and start_now != start_before:
        out.append(Alert("process_restarted", "warning", "the API restarted during the window (crash, out-of-memory kill, or a deploy)"))

    asks = _ask_counts(prev, cur, "200", "401", "422", "429", "500", "502", "503", "504")
    errors = _ask_counts(prev, cur, "500", "502")
    if errors >= th.min_errors and asks and errors / asks >= th.error_rate:
        out.append(Alert("server_errors", "critical", f"{int(errors)} of {int(asks)} questions failed inside the pipeline (500/502)"))

    n = _ask_counts(prev, cur, "504")
    if n >= th.timeouts:
        out.append(Alert("timeouts", "warning", f"{int(n)} question(s) timed out (504)"))

    n = _ask_counts(prev, cur, "503")
    if n >= th.busy:
        out.append(Alert("busy", "warning", f"{int(n)} question(s) refused because every slot was busy (503)"))

    n = delta(prev, cur, "rag_pipeline_abandoned_total")
    if n >= 1:
        out.append(Alert("abandoned_calls", "warning", f"{int(n)} call(s) kept running after the caller was told it timed out"))

    n = delta(prev, cur, "rag_http_requests_total", status="401")
    if n >= th.auth_failures:
        out.append(Alert("auth_failures", "warning", f"{int(n)} requests with a wrong or missing key (someone guessing?)"))

    p95 = quantile(prev, cur, "rag_http_request_duration_seconds", 0.95, route="/ask")
    if p95 is not None and p95 > th.slow_p95_seconds:
        out.append(Alert("slow_answers", "warning", f"95% of questions were answered within {p95:.0f}s or faster; limit {th.slow_p95_seconds:.0f}s"))

    dt = cur.ts - prev.ts
    spent = delta(prev, cur, "rag_llm_cost_usd_total")
    if dt > 0 and spent > 0:
        rate = spent / dt * 3600
        if rate > th.cost_per_hour_usd:
            out.append(Alert("cost_rate", "warning", f"spending about ${rate:.2f}/hour (limit ${th.cost_per_hour_usd:.2f})"))

    if delta(prev, cur, "rag_monitoring_errors_total") >= 1:
        out.append(Alert("monitoring_errors", "warning", "the monitoring code itself hit an error (see the logs)"))
    return out


# ---------------------------------------------------------------- watching ---
@dataclass
class Event:
    kind: str                    # FIRING | RESOLVED
    alert: Alert
    ts: float


class Watcher:
    """Keeps the last few looks and reports only CHANGES in what is firing."""

    def __init__(self, thresholds: Optional[Thresholds] = None, unreachable_after: int = 2):
        self.th = thresholds or Thresholds()
        self.history: Deque[Snapshot] = deque()
        self.firing: Dict[str, Alert] = {}
        self.failures = 0
        self.unreachable_after = unreachable_after

    def _older(self, cur: Snapshot) -> Optional[Snapshot]:
        while self.history and cur.ts - self.history[0].ts > self.th.window_seconds * 1.5:
            self.history.popleft()
        for s in self.history:                       # the oldest look that is still inside the window
            if cur.ts - s.ts <= self.th.window_seconds:
                return s
        return self.history[-1] if self.history else None

    def feed(self, snapshot: Optional[Snapshot], error: Optional[str] = None) -> List[Event]:
        now = snapshot.ts if snapshot else time.time()
        if snapshot is None:
            self.failures += 1
            current = [Alert("api_unreachable", "critical", f"cannot read /metrics ({error})")] if self.failures >= self.unreachable_after else []
        else:
            self.failures = 0
            current = evaluate(self._older(snapshot), snapshot, self.th)
            self.history.append(snapshot)
        events: List[Event] = []
        names = {a.name for a in current}
        for a in current:
            if a.name not in self.firing:
                events.append(Event("FIRING", a, now))
            self.firing[a.name] = a
        for name in [n for n in self.firing if n not in names]:
            if name == "api_unreachable" and snapshot is None:
                continue
            events.append(Event("RESOLVED", self.firing.pop(name), now))
        return events


def fetch(url: str, key: str, timeout: float = 10.0) -> Snapshot:
    req = urllib.request.Request(url.rstrip("/") + "/metrics", headers={"X-Metrics-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return Snapshot(time.time(), parse_metrics(r.read().decode("utf-8", "replace")))


def format_event(e: Event) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.ts))
    if e.kind == "FIRING":
        return f"{stamp} FIRING   [{e.alert.severity}] {e.alert.name}: {e.alert.message}"
    return f"{stamp} RESOLVED          {e.alert.name}"


def read_key(env_file: str) -> Optional[str]:
    key = os.environ.get("RAG_METRICS_KEY")
    if key:
        return key
    try:
        from dotenv import dotenv_values
        return (dotenv_values(env_file) or {}).get("RAG_METRICS_KEY")
    except Exception:
        return None


def watch(url: str, key: str, interval: float = 30.0, thresholds: Optional[Thresholds] = None,
          notify: Callable[[Event], None] = lambda e: print(format_event(e), flush=True),
          stop: Callable[[], bool] = lambda: False) -> None:
    w = Watcher(thresholds)
    while not stop():
        try:
            events = w.feed(fetch(url, key))
        except Exception as e:                                   # urllib errors, timeouts, 401...
            events = w.feed(None, error=type(e).__name__ if not isinstance(e, urllib.error.HTTPError) else f"HTTP {e.code}")
        for ev in events:
            notify(ev)
        time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true", help="look once, print what is firing, exit 0/1/2")
    ap.add_argument("--wait", type=float, default=0.0, help="with --once: look twice, this many seconds apart (so rules that compare can fire)")
    for f in fields(Thresholds):
        ap.add_argument("--" + f.name.replace("_", "-"), type=float, default=None)
    a = ap.parse_args(argv)
    th = Thresholds(**{f.name: getattr(a, f.name) for f in fields(Thresholds) if getattr(a, f.name) is not None})
    key = read_key(a.env_file)
    if not key:
        print("RAG_METRICS_KEY is not set (environment or .env)", file=sys.stderr)
        return 2
    if a.once:
        try:
            first = fetch(a.url, key)
            second = first
            if a.wait > 0:
                time.sleep(a.wait)
                second = fetch(a.url, key)
        except Exception as e:
            print(f"cannot read {a.url}/metrics: {type(e).__name__}", file=sys.stderr)
            return 2
        alerts = evaluate(first if a.wait > 0 else None, second, th)
        for al in alerts:
            print(f"FIRING   [{al.severity}] {al.name}: {al.message}")
        print("OK: nothing is firing" if not alerts else f"{len(alerts)} alert(s) firing")
        return 1 if alerts else 0
    print(f"watching {a.url}/metrics every {a.interval:.0f}s (Ctrl+C to stop)", flush=True)
    try:
        watch(a.url, key, a.interval, th)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
