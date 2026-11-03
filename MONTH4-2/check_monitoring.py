"""
check_monitoring.py -- Day 20: ask one REAL question and prove that monitoring sees it and leaks nothing.

    python check_monitoring.py                 # reads .env; talks to http://127.0.0.1:8080; reads logs with `docker compose logs`

What it checks, in order:
  1. the service answers and /metrics is locked (no key, a wrong key and an API key are all refused)
  2. a question with a unique marker in it is answered, and the counters on /metrics moved
  3. the log has exactly the line for that request (same request id as the answer) with the right facts
  4. NOTHING private is in the logs or on /metrics: not the question, not the answer, not the session id,
     not any API key, not any secret from .env
  5. a request with a wrong key is logged, but the wrong key itself is not

It costs one real model call (a few thousand tokens). Exit code 0 = all good, 1 = something failed.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alert_rules  # noqa: E402
import log_report  # noqa: E402

DEFAULT_QUESTION = "What was Apple's total net sales for fiscal year 2024?"


@dataclass
class CheckReport:
    lines: List[Tuple[Optional[bool], str]] = field(default_factory=list)

    def add(self, ok: Optional[bool], text: str) -> None:
        self.lines.append((ok, text))

    @property
    def failed(self) -> List[str]:
        return [t for ok, t in self.lines if ok is False]

    @property
    def warned(self) -> List[str]:
        return [t for ok, t in self.lines if ok is None]

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        out = [("✅ " if ok else "❌ " if ok is False else "⚠️  ") + t for ok, t in self.lines]
        out.append("")
        if self.ok:
            out.append("✅ MONITORING WORKS" + (f"  ({len(self.warned)} warning(s) above)" if self.warned else ""))
        else:
            out.append(f"❌ {len(self.failed)} check(s) failed")
        return "\n".join(out)


def _http(method: str, url: str, headers: Optional[Dict[str, str]] = None, body: Optional[dict] = None, timeout: float = 180.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**(headers or {}), **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def _scan(label: str, text: str, needle: str, what: str, rep: CheckReport, problems: List[str]) -> None:
    if needle and needle in text:
        rows = [i + 1 for i, line in enumerate(text.splitlines()) if needle in line][:3]
        problems.append(f"{what} appears in the {label} (line(s) {', '.join(map(str, rows))})")


def check_monitoring(base_url: str, api_key: str, metrics_key: str, secrets: Tuple[str, ...] = (),
                     get_logs: Optional[Callable[[], str]] = None, question: str = DEFAULT_QUESTION,
                     log_wait_seconds: float = 15.0, project_dir: Optional[str] = None, service: str = "rag") -> CheckReport:
    rep = CheckReport()
    base = base_url.rstrip("/")
    get_logs = get_logs or (lambda: log_report.compose_logs(service, 3000, project_dir))

    # 1. alive, and /metrics locked
    try:
        st, _, _ = _http("GET", base + "/healthz", timeout=10)
    except Exception as e:
        rep.add(False, f"cannot reach {base}: {type(e).__name__}. Is the container up? (docker compose ps)")
        return rep
    rep.add(st == 200, f"service answers /healthz (HTTP {st})")
    st, _, _ = _http("GET", base + "/metrics", timeout=10)
    if st == 404:
        rep.add(False, "/metrics does not exist: RAG_METRICS_KEY is not set in .env (or the container was not rebuilt/restarted after you added it)")
        return rep
    rep.add(st == 401, f"/metrics refuses a request with no key (HTTP {st})")
    st, _, _ = _http("GET", base + "/metrics", {"X-Metrics-Key": "definitely-the-wrong-key-123"}, timeout=10)
    rep.add(st == 401, f"/metrics refuses a wrong key (HTTP {st})")
    st, _, _ = _http("GET", base + "/metrics", {"X-Metrics-Key": api_key}, timeout=10)
    rep.add(st == 401, f"/metrics refuses an API key (the two keys are separate) (HTTP {st})")
    st, _, text0 = _http("GET", base + "/metrics", {"X-Metrics-Key": metrics_key}, timeout=15)
    rep.add(st == 200, f"/metrics opens with the metrics key (HTTP {st})")
    if st != 200:
        return rep
    before = alert_rules.Snapshot(time.time(), alert_rules.parse_metrics(text0))
    for name in ("rag_http_requests_total", "rag_pipeline_slots", "rag_process_start_time_seconds"):
        if before.get(name) is None:
            rep.add(False, f"/metrics has no {name}")

    # 2. one real question with a marker
    marker = "MONCHECK-" + uuid.uuid4().hex[:10]
    session = "moncheck-" + uuid.uuid4().hex[:8]
    t0 = time.time()
    st, headers, body = _http("POST", base + "/ask", {"X-API-Key": api_key},
                              {"question": f"{question} ({marker})", "session_id": session})
    rep.add(st == 200, f"a real question was answered in {time.time() - t0:.1f}s (HTTP {st})")
    if st != 200:
        rep.add(False, "stopping: without an answer there is nothing to look for in the logs")
        return rep
    answer = json.loads(body)
    rid = answer.get("request_id") or headers.get("X-Request-ID") or headers.get("x-request-id")
    rep.add(bool(rid) and rid == (headers.get("X-Request-ID") or headers.get("x-request-id")), "the answer carries a request id that matches the X-Request-ID header")
    time.sleep(0.5)
    st, _, text1 = _http("GET", base + "/metrics", {"X-Metrics-Key": metrics_key}, timeout=15)
    after = alert_rules.Snapshot(time.time(), alert_rules.parse_metrics(text1))
    d = alert_rules.delta(before, after, "rag_http_requests_total", route="/ask", status="200")
    rep.add(d == 1, f"the /ask counter went up by exactly 1 (was {d:g})")
    tok = alert_rules.delta(before, after, "rag_llm_tokens_total")
    rep.add(True if tok > 0 else None, f"tokens counted for this question: {tok:,.0f}" if tok > 0 else
            "no tokens were counted: the monitor cannot see TokenUsage.add (is your usage class called TokenUsage?)")
    cost = alert_rules.delta(before, after, "rag_llm_cost_usd_total")
    rep.add(True if cost > 0 else None, f"estimated cost counted: ${cost:.5f}" if cost > 0 else "no cost counted (does TokenUsage have a cost_estimate?)")
    ans = alert_rules.delta(before, after, "rag_answers_total")
    rep.add(True if ans >= 1 else None, "the answer was counted by kind (classification / retrieval source)" if ans >= 1 else "rag_answers_total did not move")

    # 3. the log line for THIS request
    line, logs, deadline = None, "", time.time() + log_wait_seconds
    while time.time() < deadline:
        logs = get_logs()
        events, _ = log_report.parse(logs)
        found = [e for e in events if e.get("event") == "request" and e.get("rid") == rid]
        if found:
            line = found[0]
            break
        time.sleep(1.0)
    if line is None:
        rep.add(False, f"no log line with request id {rid} (is the container writing JSON logs? RAG_MONITORING must not be 0)")
        return rep
    rep.add(True, f"the log has one line for this request (id {rid[:8]}...)")
    rep.add(line.get("route") == "/ask" and line.get("status") == 200 and "duration_ms" in line,
            f"  it says: /ask, status {line.get('status')}, {line.get('duration_ms')} ms")
    rep.add("user" in line and "session" in line and line.get("session") != session,
            f"  it names the user ({line.get('user')}) and a hashed session ({line.get('session')}), not the raw session id")
    rep.add(True if "pipeline_ms" in line else None, f"  pipeline time {line.get('pipeline_ms')} ms" if "pipeline_ms" in line else "  no pipeline_ms in the line")
    rep.add(True if line.get("tokens_in") else None,
            f"  tokens in/out {line.get('tokens_in')}/{line.get('tokens_out')}, cost ${line.get('cost_usd')}" if line.get("tokens_in") else "  no token counts in the line")
    rep.add(True if line.get("classification") else None, f"  kind of question: {line.get('classification')} / {line.get('retrieval_source')}" if line.get("classification") else "  no classification in the line")

    # 5 (before 4, so its log line is part of the scan). a wrong key
    bad_key = "wrong-key-" + uuid.uuid4().hex
    st, bh, _ = _http("POST", base + "/ask", {"X-API-Key": bad_key}, {"question": "hi"})
    rep.add(st == 401, f"a request with a wrong key is refused (HTTP {st})")
    bad_rid = bh.get("X-Request-ID") or bh.get("x-request-id")

    # 4. nothing private anywhere
    time.sleep(1.0)
    logs = get_logs()
    st, _, metrics_now = _http("GET", base + "/metrics", {"X-Metrics-Key": metrics_key}, timeout=15)
    problems: List[str] = []
    for label, text in (("logs", logs), ("metrics page", metrics_now)):
        _scan(label, text, marker, "the question text", rep, problems)
        _scan(label, text, session, "the raw session id", rep, problems)
        _scan(label, text, bad_key, "the wrong API key that was sent", rep, problems)
        _scan(label, text, api_key, "an API key", rep, problems)
        _scan(label, text, metrics_key, "the metrics key", rep, problems)
        a = (answer.get("answer") or "").strip()
        if len(a) >= 30:
            _scan(label, text, a[:30], "the start of the answer text", rep, problems)
        if len(a) >= 90:
            _scan(label, text, a[45:75], "a piece from the middle of the answer text", rep, problems)
        for i, s in enumerate(secrets):
            _scan(label, text, s, f"secret #{i + 1} from .env", rep, problems)
    if problems:
        for p in problems:
            rep.add(False, "LEAK: " + p)
        rep.add(None, "  if it is the question or answer text: a print() or log call inside your pipeline writes it. Remove or shorten that line.")
    else:
        rep.add(True, f"nothing private in {len(logs.splitlines())} log lines or on /metrics (question, answer, session id, API keys and {len(secrets)} .env secret(s) all absent)")
    events, _ = log_report.parse(logs)
    bad_line = [e for e in events if e.get("event") == "request" and e.get("rid") == bad_rid]
    rep.add(bool(bad_line) and bad_line[0].get("status") == 401, "the refused request is in the log (status 401) without the key it sent")
    return rep


def keys_from_env(env_file: str = ".env") -> Dict[str, object]:
    from dotenv import dotenv_values
    vals = dotenv_values(env_file)
    pairs = [p.split(":", 1) for p in (vals.get("RAG_API_KEYS") or "").split(",") if ":" in p]
    secrets: List[str] = []
    try:
        from container_check import secret_values
        secrets = secret_values(env_file)
    except Exception:
        import monitoring
        secrets = monitoring.collect_secrets({k: v for k, v in vals.items() if v})
    port = vals.get("RAG_HOST_PORT") or os.environ.get("RAG_HOST_PORT") or "8080"
    return {"api_key": pairs[0][0].strip() if pairs else "", "user": pairs[0][1].strip() if pairs else "",
            "metrics_key": vals.get("RAG_METRICS_KEY") or "", "secrets": tuple(secrets), "base_url": f"http://127.0.0.1:{port}"}


def main(argv=None) -> int:
    env_file = (argv or sys.argv[1:] or [".env"])[0]
    k = keys_from_env(env_file)
    if not k["api_key"] or not k["metrics_key"]:
        print("❌ .env needs RAG_API_KEYS and RAG_METRICS_KEY")
        return 1
    rep = check_monitoring(k["base_url"], k["api_key"], k["metrics_key"], k["secrets"])
    print(rep.summary())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
