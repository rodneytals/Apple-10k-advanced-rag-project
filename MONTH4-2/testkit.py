"""testkit.py -- shared by the Day 20 tests (a fake pipeline, a real live server, a metrics reader). Not a tool you need."""
import io
import json
import logging
import socket
import sys
import os
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import httpx
import uvicorn

import monitoring as mon
import serve
from usage_context import UsageProxy

API_KEY = "test-api-key-0123456789abcd"
API_KEY2 = "second-api-key-0123456789ab"
METRICS_KEY = "test-metrics-key-9876543210"
SECRET = "gsk_live_TESTSECRET_abcdef123456"
DB_PASSWORD = "p4ssw0rd-Zz9-very-secret"


# ------------------------------------------------------------ a fake pipeline
class Usage:
    def __init__(self, p, c):
        self.prompt_tokens, self.completion_tokens = p, c


def make_token_usage_class():
    class TokenUsage:
        def __init__(self):
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.total_calls = 0

        def add(self, usage):
            self.prompt_tokens += usage.prompt_tokens
            self.completion_tokens += usage.completion_tokens
            self.total_calls += 1

        @property
        def cost_estimate(self):
            return self.prompt_tokens * 1e-6 + self.completion_tokens * 2e-6
    return TokenUsage


def make_pipeline(fail_redis=None):
    m = types.ModuleType("fake_pipeline")
    TokenUsage = make_token_usage_class()
    m.TokenUsage = TokenUsage
    m.usage_tracker = UsageProxy(TokenUsage())
    m.state = {"redis_down": False, "leak": set(), "no_tokens": False}

    class _Redis:
        def ping(self):
            if m.state["redis_down"]:
                raise ConnectionError(f"cannot reach redis://:{DB_PASSWORD}@redis:6379")
            return True

    class _Mongo:
        def command(self, c):
            return {"ok": 1}

    class _Neo:
        def verify_connectivity(self):
            return None

    m.redis_client, m.mongo_client, m.neo4j_driver = _Redis(), _Mongo(), _Neo()

    def run_with_entity_memory(question, session_id, user_id):
        tracker = TokenUsage()
        token = m.usage_tracker.begin_call(tracker)
        try:
            if question.startswith("BOOM"):
                m.usage_tracker.add(Usage(10, 1))
                raise RuntimeError(f"connection failed mongodb+srv://u:{DB_PASSWORD}@cluster.example.net")
            if question.startswith("SLOW"):
                m.usage_tracker.add(Usage(100, 10))
                time.sleep(float(question.split()[1]) if len(question.split()) > 1 else 2.0)
                m.usage_tracker.add(Usage(200, 20))                 # tokens spent AFTER the caller gave up
            elif question.startswith("MEDIUM"):
                time.sleep(0.4)
                m.usage_tracker.add(Usage(len(question), 7))
            elif not m.state["no_tokens"]:
                m.usage_tracker.add(Usage(1000, 50))
                m.usage_tracker.add(Usage(500, 25))
            log = logging.getLogger("fake.pipeline")
            if "question" in m.state["leak"]:
                log.info("received question: %s", question)
            if "secret" in m.state["leak"]:
                log.info("connecting with %s", SECRET)
            if "answer" in m.state["leak"]:
                log.info("answer was ANSWER-TEXT for %s", question)
            if "answer_part" in m.state["leak"]:
                log.info("answer began %s", f"ANSWER-TEXT for {question}"[:35])
            return {"answer": f"ANSWER-TEXT for {question}", "classification": "numeric", "retrieval_source": "vector",
                    "reviewer_verdict": "pass", "warning": "low confidence" if question.startswith("WARN") else None,
                    "internal_secret": SECRET}
        finally:
            m.usage_tracker.end_call(token)

    m.run_with_entity_memory = run_with_entity_memory
    m.check_rate_limit = lambda uid: (True, 1)
    return m


# ------------------------------------------------------------- a live server
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Live:
    def __init__(self, share=None, **env_over):
        self.env = {"RAG_API_KEYS": f"{API_KEY}:rodney,{API_KEY2}:friend", "RAG_MAX_CONCURRENT": "3",
                    "RAG_QUEUE_WAIT_SECONDS": "5", "RAG_REQUEST_TIMEOUT_SECONDS": "10", "RAG_METRICS_KEY": METRICS_KEY,
                    "GROQ_API_KEY": SECRET, "MONGO_URI": f"mongodb+srv://u:{DB_PASSWORD}@cluster.example.net/db",
                    "RAG_SLOW_SECONDS": "1.0"}
        self.env.update(env_over)
        self.buf = share or io.StringIO()
        self.redactor = mon.configure_logging(self.env, stream=self.buf)
        self.pipeline = make_pipeline()
        self.app, self.cfg = serve.build_app(self.env, self.pipeline)
        self.monitor = self.app.state.monitor
        self.monitor.dep_cache_seconds = 0.0
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_config=None, access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        assert self.server.started, "server did not start"
        self.url = f"http://127.0.0.1:{self.port}"
        self.client = httpx.Client(base_url=self.url, timeout=30)

    def ask(self, question, key=API_KEY, session="s1", **headers):
        return self.client.post("/ask", json={"question": question, "session_id": session}, headers={"X-API-Key": key, **headers})

    def metrics(self, key=METRICS_KEY):
        r = self.client.get("/metrics", headers={"X-Metrics-Key": key})
        assert r.status_code == 200, r.text
        return r.text

    def lines(self):
        out = []
        for ln in self.buf.getvalue().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                out.append({"_raw": ln})
        return out

    def events(self, event, **match):
        """Log lines are written just AFTER the response has gone out, so a test that knows the request id waits a moment."""
        def find():
            return [d for d in self.lines() if d.get("event") == event and all(d.get(k) == v for k, v in match.items())]
        found = find()
        if "rid" in match and event == "request":
            deadline = time.time() + 3
            while not found and time.time() < deadline:
                time.sleep(0.05)
                found = find()
        return found

    def stop(self):
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=15)


def sample(text, metric, **labels):
    """value of one sample from /metrics text, parsed by the REAL prometheus parser"""
    from prometheus_client.parser import text_string_to_metric_families
    total, found = 0.0, False
    for fam in text_string_to_metric_families(text):
        for s in fam.samples:
            if s.name == metric and all(s.labels.get(k) == v for k, v in labels.items()):
                total += s.value
                found = True
    return total if found else None
