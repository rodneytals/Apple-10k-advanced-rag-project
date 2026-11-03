"""
log_report.py -- Day 20: read the JSON log lines and answer "what happened?" in plain words.

    python log_report.py --compose rag                 # ask Docker for the last 5000 lines of the service and summarise
    python log_report.py logs.txt                          # or a saved file
    python log_report.py --compose rag --rid 3f2a...   # everything that happened to ONE request
    python log_report.py --compose rag --since 60      # only the last 60 minutes

Lines that are not JSON (a print() inside your pipeline, a library banner) are counted and the first few shown.
Standard library only.
"""
import argparse
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


def decode(raw: bytes) -> str:
    """`docker compose logs > file` in Windows PowerShell 5 writes UTF-16; everything else is UTF-8."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8-sig", "replace")


def parse(text: str) -> Tuple[List[dict], List[str]]:
    events, other = [], []
    for line in text.splitlines():
        line = line.strip("\r\n ")
        if not line:
            continue
        start = line.find("{")                        # tolerate a "rag-api  | " prefix from docker compose
        if start >= 0 and line.endswith("}"):
            try:
                d = json.loads(line[start:])
                if isinstance(d, dict) and "ts" in d and "level" in d:
                    events.append(d)
                    continue
            except ValueError:
                pass
        other.append(line)
    return events, other


def _ts(e: dict) -> Optional[float]:
    try:
        return datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, ValueError):
        return None


def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.4999999)) - 1))
    return v[k]


def _ms(v: Optional[float]) -> str:
    return "-" if v is None else (f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms")


def filter_since(events: List[dict], minutes: float) -> List[dict]:
    stamps = [t for t in (_ts(e) for e in events) if t is not None]
    if not stamps:
        return events
    cutoff = max(stamps) - minutes * 60
    return [e for e in events if (_ts(e) or 0) >= cutoff]


def summarize(events: List[dict], other: List[str], top: int = 5) -> str:
    out: List[str] = []
    reqs = [e for e in events if e.get("event") == "request"]
    stamps = [t for t in (_ts(e) for e in events) if t is not None]
    span = ""
    if stamps:
        f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        span = f"{f(min(stamps))} to {f(max(stamps))} UTC"
    out.append(f"LOG REPORT   {span}")
    out.append(f"{len(events) + len(other)} lines: {len(events)} structured events, {len(other)} other lines")
    out.append("")

    by_route: Dict[str, Counter] = defaultdict(Counter)
    for e in reqs:
        by_route[e.get("route", "?")][e.get("status", 0)] += 1
    out.append(f"REQUESTS LOGGED: {len(reqs)}  (successful health/metrics checks are not logged)")
    for route, c in sorted(by_route.items(), key=lambda kv: -sum(kv[1].values())):
        out.append(f"  {route:<12} " + "   ".join(f"{s} x{n}" for s, n in sorted(c.items())))

    asks = [e for e in reqs if e.get("route") == "/ask"]
    ok = [e for e in asks if e.get("status") == 200]
    if ok:
        d = [e["duration_ms"] for e in ok if "duration_ms" in e]
        p = [e["pipeline_ms"] for e in ok if "pipeline_ms" in e]
        out.append("")
        out.append(f"ANSWERED QUESTIONS: {len(ok)} of {len(asks)}")
        out.append(f"  time to answer   typical {_ms(percentile(d, 50))}   slowest 5% {_ms(percentile(d, 95))}   worst {_ms(max(d) if d else None)}")
        if p:
            out.append(f"  inside pipeline  typical {_ms(percentile(p, 50))}   slowest 5% {_ms(percentile(p, 95))}")
        kinds = Counter((e.get("classification", "?"), e.get("retrieval_source", "?")) for e in ok)
        out.append("  kinds of question: " + ", ".join(f"{k[0]}/{k[1]} x{n}" for k, n in kinds.most_common(top)))
        warned = sum(1 for e in ok if e.get("warning"))
        if warned:
            out.append(f"  answers that carried a warning: {warned}")

    tin = sum(e.get("tokens_in", 0) for e in events if e.get("event") in ("request", "pipeline_late_finish"))
    tout = sum(e.get("tokens_out", 0) for e in events if e.get("event") in ("request", "pipeline_late_finish"))
    cost = sum(e.get("cost_usd", 0.0) for e in events if e.get("event") in ("request", "pipeline_late_finish"))
    if tin or tout:
        out.append("")
        out.append(f"TOKENS: {tin:,} in, {tout:,} out   estimated spend ${cost:.4f}")
        per_user: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0, 0])
        for e in events:
            if e.get("event") in ("request", "pipeline_late_finish") and e.get("user"):
                row = per_user[e["user"]]
                row[0] += 1 if e["event"] == "request" else 0
                row[1] += e.get("cost_usd", 0.0)
                row[2] += e.get("tokens_in", 0) + e.get("tokens_out", 0)
        for user, (n, c, t) in sorted(per_user.items(), key=lambda kv: -kv[1][1])[:top]:
            out.append(f"  {user:<16} {n} request(s)   {t:,} tokens   ${c:.4f}")

    problems: List[str] = []
    errs = [e for e in reqs if e.get("status", 0) >= 500 and e.get("status") not in (503, 504)]
    groups: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for e in errs:
        groups[(e.get("error", "?"), e.get("error_at", "?"))].append(e)
    for (cls, at), es in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:top]:
        problems.append(f"  {len(es)} x {es[0]['status']}  {cls} at {at}   e.g. request {es[0].get('rid')}")
    for status, label in ((504, "timed out (504)"), (503, "refused because every slot was busy (503)"), (429, "rate limited (429)"), (401, "wrong or missing key (401)")):
        n = sum(1 for e in reqs if e.get("status") == status)
        if n:
            problems.append(f"  {n} x {label}")
    late = [e for e in events if e.get("event") == "pipeline_late_finish"]
    if late:
        extra = sum(e.get("tokens_in", 0) + e.get("tokens_out", 0) for e in late)
        problems.append(f"  {len(late)} call(s) finished AFTER the caller was told 504 ({extra:,} tokens still spent); "
                        f"slowest took {_ms(max(e.get('pipeline_ms', 0) for e in late))}")
    slow = sorted((e for e in reqs if e.get("slow")), key=lambda e: -e.get("duration_ms", 0))
    if slow:
        problems.append(f"  {len(slow)} slow request(s); slowest: {slow[0].get('rid')} took {_ms(slow[0].get('duration_ms'))} "
                        f"({slow[0].get('classification', '?')}, user {slow[0].get('user', '-')})")
    closed = sum(1 for e in reqs if e.get("client_closed"))
    if closed:
        problems.append(f"  {closed} request(s) abandoned by the caller before an answer (499)")
    msgs = Counter((e.get("logger", "?"), (e.get("msg") or e.get("event") or "")[:90]) for e in events
                   if e.get("level") in ("WARNING", "ERROR", "CRITICAL") and e.get("event") not in ("request", "pipeline_late_finish"))
    for (lg, m), n in msgs.most_common(top):
        problems.append(f"  {n} x {lg}: {m}")
    out.append("")
    out.append("PROBLEMS" + ("" if problems else ": none"))
    out += problems

    life = [e for e in events if e.get("event") in ("startup", "shutdown")]
    if life:
        out.append("")
        out.append("STARTS AND STOPS")
        for e in life[-6:]:
            extra = f"slots={e.get('slots')}" if e["event"] == "startup" else f"in_flight={e.get('in_flight')} uptime={e.get('uptime_s')}s"
            out.append(f"  {e['ts']}  {e['event']:<9} {extra}")
    if other:
        out.append("")
        out.append(f"OTHER LINES (not JSON): {len(other)}; first few:")
        out += [f"  {x[:110]}" for x in other[:3]]
    return "\n".join(out)


def trace(events: List[dict], other: List[str], rid: str) -> str:
    mine = [e for e in events if e.get("rid") == rid]
    if not mine and not [x for x in other if rid in x]:
        return f"nothing found for request id {rid} in these lines"
    out = [f"EVERYTHING FOR REQUEST {rid}"]
    for e in sorted(mine, key=lambda e: e.get("ts", "")):
        fields = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "level", "logger", "event", "rid"))
        out.append(f"  {e['ts']}  {e['level']:<7} {e.get('event', e.get('msg', '')):<20} {fields}")
    out += [f"  (other line) {x[:150]}" for x in other if rid in x]
    return "\n".join(out)


def compose_logs(service: str, tail: int, project_dir: Optional[str] = None) -> str:
    r = subprocess.run(["docker", "compose", "logs", "--no-color", "--no-log-prefix", "--tail", str(tail), service],
                       cwd=project_dir, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("docker compose logs failed: " + r.stderr.decode("utf-8", "replace")[-300:])
    return decode(r.stdout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", nargs="?", help="a saved log file ('-' = read standard input)")
    ap.add_argument("--compose", metavar="SERVICE", help="read the log straight from Docker Compose, e.g. rag")
    ap.add_argument("--tail", type=int, default=5000)
    ap.add_argument("--rid", help="show everything for one request id")
    ap.add_argument("--since", type=float, help="only the last N minutes")
    ap.add_argument("--project-dir", default=None)
    a = ap.parse_args(argv)
    if a.compose:
        text = compose_logs(a.compose, a.tail, a.project_dir)
    elif a.file == "-":
        text = decode(sys.stdin.buffer.read())
    elif a.file:
        text = decode(open(a.file, "rb").read())
    else:
        ap.error("give a file or --compose SERVICE")
    events, other = parse(text)
    if a.since:
        events = filter_since(events, a.since)
    print(trace(events, other, a.rid) if a.rid else summarize(events, other))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
