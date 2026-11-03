"""
monitoring.py -- Day 20: see what the running service is doing, without ever logging what people asked.

serve.py switches this on for you. It adds four things around the API from Day 17, without editing api_layer.py:

  1. ONE JSON log line per request  (who, which route, status, how long, tokens, cost, which error class)
  2. a /metrics page of counters and timings, protected by its own key (RAG_METRICS_KEY)
  3. a log line + counter when a call is still running after the caller got a timeout (the thread cannot be killed)
  4. a secret filter: every secret value found in the environment is replaced by [redacted] in every log line

What is NEVER logged: question text, answer text, API keys, exception messages (they can hold connection strings),
raw session ids (a short hash is logged so you can still follow one conversation).

Standard library only, apart from the Starlette/FastAPI objects that api_layer already uses.
"""
import asyncio
import contextvars
import copy
import functools
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")      # must be the same pattern api_layer.py uses (a test checks)
PROBE_ROUTES = {"/healthz", "/readyz", "/metrics"}              # successful probes are counted but not logged line by line
KNOWN_METHODS = {"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"}
MIN_METRICS_KEY_LENGTH = 16
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIAL)", re.I)
URL_NAME = re.compile(r"(URI|URL|DSN)", re.I)
NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "pymongo", "neo4j", "asyncio", "filelock", "huggingface_hub")
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120)


# ============================================================ secret filter ==
def collect_secrets(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Secret-looking values in the environment (same idea as container_check.secret_values on Day 19)."""
    env = os.environ if env is None else env
    found = set()
    for name, value in env.items():
        if not value or len(value) < 8 or value.isdigit() or value.lower() in ("true", "false", "localhost"):
            continue
        if name == "RAG_API_KEYS":                      # "key1:rodney,key2:friend": the KEYS are secret, the names are not
            for pair in value.split(","):
                key = pair.split(":", 1)[0].strip()
                if len(key) >= 8:
                    found.add(key)
        elif SECRET_NAME.search(name) or (URL_NAME.search(name) and "@" in value):
            found.add(value)
    return sorted(found, key=len, reverse=True)           # longest first, so a long secret is not half-replaced by a short one


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        self.secrets = [s for s in secrets if s]

    def clean(self, text: str) -> str:
        for s in self.secrets:
            if s in text:
                text = text.replace(s, "[redacted]")
            escaped = json.dumps(s, ensure_ascii=False)[1:-1]       # the same secret as it looks inside a JSON string
            if escaped != s and escaped in text:
                text = text.replace(escaped, "[redacted]")
        return text


# ================================================================== logging ==
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_INFRA_FILES = {"monitoring.py", "serve.py", "api_layer.py"}          # our own wrappers: never the place to look
_STDLIB = os.path.normcase(os.path.dirname(os.__file__))


def _is_own_code(filename: str) -> bool:
    f = os.path.normcase(filename)
    return not ("site-packages" in f or "dist-packages" in f or f.startswith(_STDLIB) or os.path.basename(f) in _INFRA_FILES or f.startswith("<"))


def where(exc: BaseException) -> str:
    """Where it failed, as 'file.py:LINE:function'. The innermost frame (often inside a library) and, if different, the
    innermost frame of YOUR code (your pipeline), so the line to read is never hidden behind a library's internals.
    No message, no source code."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if not frames:
        return ""
    picked = [frames[-1]]
    mine = next((f for f in reversed(frames) if _is_own_code(f.filename)), None)
    if mine is not None and mine is not frames[-1]:
        picked.append(mine)
    return " < ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in picked)


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor, error_messages: bool = False):
        super().__init__()
        self.redactor, self.error_messages = redactor, error_messages

    def format(self, record: logging.LogRecord) -> str:
        d: Dict[str, Any] = {"ts": _iso(record.created), "level": record.levelname, "logger": record.name}
        event = getattr(record, "event", None)
        if event:
            d["event"] = event
            d.update(getattr(record, "fields", {}) or {})
        else:
            d["msg"] = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            d["exc_class"] = type(exc).__name__
            d["exc_at"] = where(exc)
            if self.error_messages:
                d["exc_message"] = str(exc)[:300]
        line = json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=str)
        return self.redactor.clean(line)


class TextFormatter(logging.Formatter):
    """RAG_LOG_FORMAT=text: the same facts as one readable line (for reading in a terminal without a JSON viewer)."""

    def __init__(self, redactor: Redactor, error_messages: bool = False):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.redactor, self.error_messages = redactor, error_messages

    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, "event", None):
            record = copy.copy(record)
            record.msg = record.event + " " + " ".join(f"{k}={v}" for k, v in (getattr(record, "fields", {}) or {}).items())
            record.args = ()
        return self.redactor.clean(super().format(record))

    def formatException(self, ei) -> str:                 # never print a traceback: it ends with the exception message
        return f"[{type(ei[1]).__name__} at {where(ei[1])}]"


def configure_logging(env: Optional[Dict[str, str]] = None, stream=None) -> Redactor:
    """JSON lines on stdout (what `docker compose logs` shows). Call once, before the app is built."""
    env = os.environ if env is None else env
    redactor = Redactor(collect_secrets(env))
    error_messages = env.get("RAG_LOG_ERROR_MESSAGES", "0") == "1"
    fmt = JsonFormatter if env.get("RAG_LOG_FORMAT", "json").lower() != "text" else TextFormatter
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(fmt(redactor, error_messages))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(env.get("RAG_LOG_LEVEL", "INFO").upper())
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("rag_api").setLevel(logging.WARNING)       # api_layer's own per-request text line: replaced by ours
    logging.getLogger("uvicorn.access").disabled = True          # same: our line carries the same facts plus more
    return redactor


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    logger.log(level, event, extra={"event": event, "fields": {k: v for k, v in fields.items() if v is not None}})


# ================================================================== metrics ==
def clean_label(value: Any, maxlen: int = 40) -> str:
    """Label values must come from a small fixed set. Anything odd becomes 'other'."""
    if value is None:
        return "none"
    s = str(value)
    return s if re.fullmatch(r"[A-Za-z0-9_.:/+\- ]{1,%d}" % maxlen, s) else "other"


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return str(v)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    r = repr(float(v))
    return r[:-2] if r.endswith(".0") else r


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: Tuple[str, ...], key: Tuple[str, ...], extra: Optional[Tuple[str, str]] = None) -> str:
    parts = [f'{n}="{_esc(v)}"' for n, v in zip(names, key)]
    if extra:
        parts.append(f'{extra[0]}="{extra[1]}"')
    return "{" + ",".join(parts) + "}" if parts else ""


class _Metric:
    kind = "untyped"

    def __init__(self, name: str, help_: str, labelnames: Tuple[str, ...] = (), max_series: int = 100):
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", name):
            raise ValueError(f"bad metric name {name!r}")
        self.name, self.help, self.labelnames, self.max_series = name, help_, tuple(labelnames), max_series
        self._lock = threading.Lock()
        self._data: Dict[Tuple[str, ...], Any] = {}

    def _key(self, labels: Dict[str, Any]) -> Tuple[str, ...]:
        unknown = set(labels) - set(self.labelnames)
        if unknown:
            raise ValueError(f"{self.name}: unknown label(s) {sorted(unknown)}")
        key = tuple(clean_label(labels.get(n, "")) if labels.get(n, "") != "" else "" for n in self.labelnames)
        if key not in self._data and len(self._data) >= self.max_series:     # cardinality cap: a flood of new label values cannot eat memory
            key = tuple("other" for _ in self.labelnames)
        return key

    def header(self) -> List[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]

    def value(self, **labels) -> float:
        with self._lock:
            return self._data.get(self._key(labels), 0)

    def total(self) -> float:
        with self._lock:
            return sum(v for v in self._data.values() if not isinstance(v, dict))


class Counter(_Metric):
    kind = "counter"

    def inc(self, amount: float = 1, **labels) -> None:
        if amount < 0:
            raise ValueError("a counter can only go up")
        with self._lock:
            k = self._key(labels)
            self._data[k] = self._data.get(k, 0) + amount

    def render(self) -> List[str]:
        with self._lock:
            items = sorted(self._data.items())
        if not items and not self.labelnames:
            items = [((), 0)]
        return self.header() + [f"{self.name}{_labels(self.labelnames, k)} {_fmt(v)}" for k, v in items]


class Gauge(_Metric):
    kind = "gauge"

    def set(self, value: float, **labels) -> None:
        with self._lock:
            self._data[self._key(labels)] = value

    def inc(self, amount: float = 1, **labels) -> None:
        with self._lock:
            k = self._key(labels)
            self._data[k] = self._data.get(k, 0) + amount

    def dec(self, amount: float = 1, **labels) -> None:
        self.inc(-amount, **labels)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def render(self) -> List[str]:
        with self._lock:
            items = sorted(self._data.items())
        if not items and not self.labelnames:
            items = [((), 0)]
        return self.header() + [f"{self.name}{_labels(self.labelnames, k)} {_fmt(v)}" for k, v in items]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name, help_, labelnames=(), buckets=DURATION_BUCKETS, max_series=50):
        super().__init__(name, help_, labelnames, max_series)
        self.buckets = tuple(sorted(buckets))

    def observe(self, value: float, **labels) -> None:
        with self._lock:
            k = self._key(labels)
            d = self._data.setdefault(k, {"b": [0] * len(self.buckets), "sum": 0.0, "count": 0})
            for i, edge in enumerate(self.buckets):
                if value <= edge:
                    d["b"][i] += 1
                    break
            d["sum"] += value
            d["count"] += 1

    def count(self, **labels) -> int:
        with self._lock:
            d = self._data.get(self._key(labels))
            return d["count"] if d else 0

    def render(self) -> List[str]:
        with self._lock:
            items = sorted((k, {"b": list(v["b"]), "sum": v["sum"], "count": v["count"]}) for k, v in self._data.items())
        lines = self.header()
        for k, d in items:
            running = 0
            for edge, n in zip(self.buckets, d["b"]):
                running += n
                lines.append(f"{self.name}_bucket{_labels(self.labelnames, k, ('le', repr(float(edge))))} {running}")
            lines.append(f"{self.name}_bucket{_labels(self.labelnames, k, ('le', '+Inf'))} {d['count']}")
            lines.append(f"{self.name}_sum{_labels(self.labelnames, k)} {_fmt(d['sum'])}")
            lines.append(f"{self.name}_count{_labels(self.labelnames, k)} {d['count']}")
        return lines


class Registry:
    def __init__(self):
        self._metrics: List[_Metric] = []
        self._collectors: List[Callable[[], None]] = []

    def _add(self, m):
        if any(x.name == m.name for x in self._metrics):
            raise ValueError(f"metric {m.name} registered twice")
        self._metrics.append(m)
        return m

    def counter(self, name, help_, labelnames=(), max_series=100) -> Counter:
        return self._add(Counter(name, help_, labelnames, max_series))

    def gauge(self, name, help_, labelnames=(), max_series=100) -> Gauge:
        return self._add(Gauge(name, help_, labelnames, max_series))

    def histogram(self, name, help_, labelnames=(), buckets=DURATION_BUCKETS, max_series=50) -> Histogram:
        return self._add(Histogram(name, help_, labelnames, buckets, max_series))

    def add_collector(self, fn: Callable[[], None]) -> None:
        self._collectors.append(fn)

    def render(self) -> str:
        for fn in self._collectors:                      # refresh "right now" gauges at scrape time
            try:
                fn()
            except Exception:
                logging.getLogger("rag.monitor").warning("a metrics collector failed", exc_info=True)
        lines: List[str] = []
        for m in self._metrics:
            lines += m.render()
        return "\n".join(lines) + "\n"


# ============================================================ per-request ====
@dataclass
class RequestRecord:
    rid: str
    method: str
    started: float                                       # time.perf_counter()
    started_wall: float
    route: str = "unmatched"
    status: int = 0
    user: str = "-"
    session: str = "-"
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    classification: Optional[str] = None
    retrieval_source: Optional[str] = None
    reviewer_verdict: Optional[str] = None
    has_warning: bool = False
    error_class: Optional[str] = None
    error_at: Optional[str] = None
    error_message: Optional[str] = None
    submitted: Optional[float] = None
    pipeline_started: Optional[float] = None
    pipeline_ended: Optional[float] = None
    pipeline_outcome: Optional[str] = None
    finished: Optional[float] = None
    reported: Tuple[int, int, float] = (0, 0, 0.0)       # tokens in/out and cost already printed on the request line
    logged: bool = False
    abandoned: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


CURRENT: "contextvars.ContextVar[Optional[RequestRecord]]" = contextvars.ContextVar("rag_request_record", default=None)


class ContextExecutor(ThreadPoolExecutor):
    """loop.run_in_executor does NOT carry ContextVars into the worker thread, so the request record would be invisible
    there. This executor copies the caller's context for every job (the same trick asyncio.to_thread uses)."""

    def __init__(self, *args, on_submit: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_submit = on_submit

    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        if self._on_submit is not None:
            try:
                self._on_submit()
            except Exception:
                pass
        return super().submit(ctx.run, fn, *args, **kwargs)


def _ensure_request_id(scope: dict) -> str:
    """api_layer accepts a caller's X-Request-ID if it looks safe, otherwise makes one. We decide first and put the
    result in the headers, so both layers use the SAME id."""
    headers = scope.get("headers") or []
    for k, v in headers:
        if k == b"x-request-id":
            incoming = v.decode("latin-1")
            if REQUEST_ID_PATTERN.match(incoming):
                return incoming
    rid = uuid.uuid4().hex
    scope["headers"] = [(k, v) for k, v in headers if k != b"x-request-id"] + [(b"x-request-id", rid.encode())]
    return rid


# =============================================================== the monitor =
class Monitor:
    def __init__(self, env: Optional[Dict[str, str]] = None, registry: Optional[Registry] = None):
        env = os.environ if env is None else env
        self.env = env
        self.redactor = Redactor(collect_secrets(env))
        self.slow_seconds = float(env.get("RAG_SLOW_SECONDS", "20"))
        self.log_probes = env.get("RAG_LOG_PROBES", "0") == "1"
        self.error_messages = env.get("RAG_LOG_ERROR_MESSAGES", "0") == "1"
        self.salt = env.get("RAG_LOG_SALT", "rag")
        self.watch_paths = [p.strip() for p in env.get("RAG_WATCH_PATHS", ".,checkpoints").split(",") if p.strip()]
        self.watch_files = [p.strip() for p in env.get("RAG_WATCH_FILES", "checkpoints/rag_checkpoints.db").split(",") if p.strip()]
        self.dep_cache_seconds = 10.0
        self.dep_timeout = 2.0
        self._dep_ts = -1e9
        self._dep_lock: Optional[asyncio.Lock] = None
        self.runner = None
        self.started_wall = time.time()
        self.req_log = logging.getLogger("rag.request")
        self.pipe_log = logging.getLogger("rag.pipeline")
        self.app_log = logging.getLogger("rag.app")

        r = self.registry = registry or Registry()
        self.http_requests = r.counter("rag_http_requests_total", "HTTP requests answered", ("route", "method", "status"), 200)
        self.http_duration = r.histogram("rag_http_request_duration_seconds", "Time to answer an HTTP request", ("route",))
        self.http_in_flight = r.gauge("rag_http_requests_in_flight", "HTTP requests being handled right now")
        self.queue_wait = r.histogram("rag_queue_wait_seconds", "Time from request start until the pipeline got a slot "
                                      "(auth, rate limit and waiting for a free slot)")
        self.pipeline_calls = r.counter("rag_pipeline_calls_total", "Pipeline calls that ended (also counts calls whose caller "
                                        "already got a timeout)", ("outcome",))
        self.pipeline_duration = r.histogram("rag_pipeline_duration_seconds", "How long pipeline calls really ran", ("outcome",))
        self.pipeline_abandoned = r.counter("rag_pipeline_abandoned_total", "Calls still running when the caller was answered "
                                            "(timeout or disconnect); the slot stays taken until they end")
        self.answers = r.counter("rag_answers_total", "Answers by kind of question and where the evidence came from",
                                 ("classification", "retrieval_source", "reviewer_verdict"), 60)
        self.answer_warnings = r.counter("rag_answer_warnings_total", "Answers that carried a warning")
        self.tokens = r.counter("rag_llm_tokens_total", "Tokens used by language-model calls", ("kind",))
        self.llm_calls = r.counter("rag_llm_calls_total", "Language-model calls")
        self.cost = r.counter("rag_llm_cost_usd_total", "Estimated spend (your TokenUsage.cost_estimate)")
        self.dep_up = r.gauge("rag_dependency_up", "1 = the dependency answered its check, 0 = it did not", ("name",))
        self.dep_failures = r.counter("rag_dependency_check_failures_total", "Failed dependency checks", ("name",))
        self.slots = r.gauge("rag_pipeline_slots", "Pipeline calls allowed at once")
        self.in_flight = r.gauge("rag_pipeline_in_flight", "Pipeline calls running right now (includes timed-out ones still running)")
        self.waiting = r.gauge("rag_pipeline_waiting", "Requests waiting for a free slot")
        self.monitor_errors = r.counter("rag_monitoring_errors_total", "Failures inside the monitoring code itself", ("where",))
        self.start_time = r.gauge("rag_process_start_time_seconds", "Unix time the process started")
        self.build_info = r.gauge("rag_build_info", "Constant 1; the labels say what is running", ("version", "python"))
        self.rss = r.gauge("rag_process_resident_memory_bytes", "Memory held by the process")
        self.fds = r.gauge("rag_process_open_fds", "Open file descriptors")
        self.threads = r.gauge("rag_process_threads", "Python threads alive")
        self.disk_free = r.gauge("rag_disk_free_bytes", "Free space where the path lives", ("path",))
        self.disk_total = r.gauge("rag_disk_total_bytes", "Size of the disk where the path lives", ("path",))
        self.file_bytes = r.gauge("rag_file_bytes", "Size of a watched file (the checkpoint database)", ("path",))
        self.bounded_items = r.gauge("rag_bounded_items", "Entries in a bounded container (see bounded.py)", ("name",))
        self.bounded_dropped = r.gauge("rag_bounded_dropped", "Oldest entries dropped so far to keep it bounded", ("name",))
        self.start_time.set(self.started_wall)
        self.build_info.set(1, version=env.get("RAG_VERSION", "dev"), python=f"{sys.version_info.major}.{sys.version_info.minor}")
        r.add_collector(self._collect_now)

    # ---------------------------------------------------------- helpers ----
    def hash_session(self, sid: str) -> str:
        return hashlib.sha256((self.salt + sid).encode()).hexdigest()[:10]

    def emit(self, logger: logging.Logger, level: int, event: str, **fields) -> None:
        log_event(logger, level, event, **fields)

    # ------------------------------------------------------ scrape-time -----
    def _collect_now(self) -> None:
        if self.runner is not None:
            self.slots.set(self.runner.cfg.max_concurrent)
            self.in_flight.set(self.runner.in_flight)
            self.waiting.set(self.runner.waiting)
        self.threads.set(threading.active_count())
        try:
            with open("/proc/self/statm") as f:
                self.rss.set(int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE"))
            self.fds.set(len(os.listdir("/proc/self/fd")))
        except Exception:
            pass                                          # not Linux: these two are simply absent
        for p in self.watch_paths:
            try:
                u = shutil.disk_usage(p)
                self.disk_free.set(u.free, path=p)
                self.disk_total.set(u.total, path=p)
            except Exception:
                pass
        for p in self.watch_files:
            try:
                self.file_bytes.set(os.path.getsize(p), path=p)
            except Exception:
                pass

    async def refresh_dependencies(self, checks: Dict[str, Callable[[], Any]]) -> None:
        """Run the same checks /readyz runs, at most every 10 s, so rag_dependency_up is fresh whenever someone scrapes."""
        if not checks:
            return
        from starlette.concurrency import run_in_threadpool
        if self._dep_lock is None:
            self._dep_lock = asyncio.Lock()
        async with self._dep_lock:
            if time.monotonic() - self._dep_ts < self.dep_cache_seconds:
                return

            async def one(name, fn):
                try:
                    async with asyncio.timeout(self.dep_timeout):
                        await run_in_threadpool(fn)
                    self.dep_up.set(1, name=name)
                except Exception:                          # includes TimeoutError; messages are never logged
                    self.dep_up.set(0, name=name)
                    self.dep_failures.inc(name=name)

            await asyncio.gather(*(one(n, f) for n, f in checks.items()))
            self._dep_ts = time.monotonic()

    def render(self) -> str:
        return self.registry.render()

    # ------------------------------------------------------ the request -----
    def finish_request(self, rec: RequestRecord, scope: dict, cancelled: bool, crashed: Optional[BaseException]) -> None:
        route = getattr(scope.get("route"), "path", None) or "unmatched"
        rec.route = route
        if not rec.status:
            rec.status = 499 if cancelled else 500
        if crashed is not None and rec.error_class is None:
            rec.error_class, rec.error_at = type(crashed).__name__, where(crashed)
        rec.finished = time.perf_counter()
        duration = rec.finished - rec.started
        method = rec.method if rec.method in KNOWN_METHODS else "OTHER"
        self.http_requests.inc(route=route, method=method, status=str(rec.status))
        self.http_duration.observe(duration, route=route)
        with rec.lock:
            rec.logged = True
            still_running = rec.pipeline_started is not None and rec.pipeline_ended is None
            rec.abandoned = still_running
            rec.reported = (rec.tokens_in, rec.tokens_out, rec.cost_usd)         # what this line says; a late line adds only the rest
            t_in, t_out, t_cost, t_calls = rec.tokens_in, rec.tokens_out, rec.cost_usd, rec.llm_calls
        if still_running:
            self.pipeline_abandoned.inc()
        if route in PROBE_ROUTES and rec.status < 400 and not self.log_probes:
            return
        slow = duration >= self.slow_seconds
        level = logging.INFO
        if rec.status in (429, 503, 504) or slow:
            level = logging.WARNING
        if rec.status >= 500 and rec.status not in (503, 504):
            level = logging.ERROR
        p_ms = None
        if rec.pipeline_started is not None:
            end = rec.pipeline_ended if rec.pipeline_ended is not None else rec.finished
            p_ms = round((end - rec.pipeline_started) * 1000)
        self.emit(self.req_log, level, "request",
                  rid=rec.rid, method=method, route=route, status=rec.status, duration_ms=round(duration * 1000),
                  user=rec.user if rec.user != "-" else None, session=rec.session if rec.session != "-" else None,
                  queue_ms=round((rec.submitted - rec.started) * 1000) if rec.submitted else None,
                  pipeline_ms=p_ms, pipeline_still_running=still_running or None,
                  llm_calls=t_calls or None, tokens_in=t_in or None, tokens_out=t_out or None,
                  cost_usd=round(t_cost, 6) if t_cost else None,
                  classification=rec.classification, retrieval_source=rec.retrieval_source,
                  reviewer_verdict=rec.reviewer_verdict, warning=rec.has_warning or None,
                  error=rec.error_class, error_at=rec.error_at, error_message=rec.error_message,
                  slow=slow or None, client_closed=(rec.status == 499) or None)

    # -------------------------------------------------- the pipeline call ----
    def on_submit(self) -> None:
        rec = CURRENT.get()
        if rec is not None:
            rec.submitted = time.perf_counter()
            self.queue_wait.observe(rec.submitted - rec.started)

    def wrap_pipeline(self, fn: Callable[[str, str, str], Any]) -> Callable[[str, str, str], Any]:
        mon = self

        @functools.wraps(fn)
        def monitored(question, session_id, user_id):
            rec = CURRENT.get()
            if rec is None:                                # called outside a request (a test, a script): nothing to record
                return fn(question, session_id, user_id)
            rec.user = clean_label(user_id, 64)
            rec.session = mon.hash_session(str(session_id))
            rec.pipeline_started = time.perf_counter()
            outcome, result = "ok", None
            try:
                result = fn(question, session_id, user_id)
                return result
            except BaseException as e:
                outcome = "error"
                rec.error_class, rec.error_at = type(e).__name__, where(e)
                if mon.error_messages:
                    rec.error_message = mon.redactor.clean(str(e))[:300]
                raise
            finally:
                try:
                    mon._pipeline_finished(rec, outcome, result)
                except Exception:
                    mon.monitor_errors.inc(where="pipeline_finished")
        return monitored

    def _pipeline_finished(self, rec: RequestRecord, outcome: str, result: Any) -> None:
        now = time.perf_counter()
        if outcome == "ok" and isinstance(result, dict):
            if result.get("rate_limited"):
                outcome = "rate_limited"
            rec.classification = clean_label(result.get("classification"))
            rec.retrieval_source = clean_label(result.get("retrieval_source"))
            rec.reviewer_verdict = clean_label(result.get("reviewer_verdict"))
            rec.has_warning = bool(result.get("warning"))
            self.answers.inc(classification=rec.classification, retrieval_source=rec.retrieval_source,
                             reviewer_verdict=rec.reviewer_verdict)
            if rec.has_warning:
                self.answer_warnings.inc()
        with rec.lock:
            rec.pipeline_ended, rec.pipeline_outcome = now, outcome
            late = rec.logged
            more_in, more_out = rec.tokens_in - rec.reported[0], rec.tokens_out - rec.reported[1]
            more_cost, more_calls = rec.cost_usd - rec.reported[2], rec.llm_calls
        self.pipeline_calls.inc(outcome=outcome)
        self.pipeline_duration.observe(now - (rec.pipeline_started or now), outcome=outcome)
        if late:                                           # the caller was already answered (timeout): say how it really ended
            after = (now - rec.finished) * 1000 if rec.finished else None
            self.emit(self.pipe_log, logging.WARNING, "pipeline_late_finish",
                      rid=rec.rid, outcome=outcome, pipeline_ms=round((now - rec.pipeline_started) * 1000),
                      after_response_ms=round(after) if after is not None else None,
                      user=rec.user if rec.user != "-" else None, session=rec.session if rec.session != "-" else None,
                      llm_calls=more_calls or None,
                      tokens_in=more_in or None, tokens_out=more_out or None,           # ONLY what was spent after the request line
                      cost_usd=round(more_cost, 6) if more_cost > 1e-12 else None,
                      error=rec.error_class, error_at=rec.error_at, error_message=rec.error_message)

    # ------------------------------------------------------- token usage ----
    def silence_prints(self, pipeline_module) -> Optional[str]:
        """Notebook code is full of print(f"Question: {q}") lines. In a container they land in the same log as everything
        else, with the question text in them. Defining `print` inside the pipeline module hides the module's own prints
        (functions look in their module before the built-ins). RAG_PIPELINE_PRINTS=1 turns them back on."""
        if self.env.get("RAG_PIPELINE_PRINTS", "0") == "1":
            return "print() output of the pipeline is ON (RAG_PIPELINE_PRINTS=1): it may contain questions and answers"
        if "print" in vars(pipeline_module):
            return None
        pipeline_module.print = _quiet_print
        n = count_prints(pipeline_module)
        return f"print() calls inside the pipeline are silenced ({n} found); set RAG_PIPELINE_PRINTS=1 to show them"

    def watch_bounded(self, pipeline_module) -> Optional[str]:
        """If make_bounded.py was run, show how full each bounded container is."""
        pkg = getattr(pipeline_module, "__package__", None)
        mod = sys.modules.get(f"{pkg}.bounded") if pkg else None
        if mod is None or not hasattr(mod, "stats"):
            return None

        def collect():
            for name, items, _limit, dropped in mod.stats():
                self.bounded_items.set(items, name=name)
                self.bounded_dropped.set(dropped, name=name)
        self.registry.add_collector(collect)
        return "bounded containers are shown in /metrics"

    def tap_usage(self, pipeline_module) -> str:
        """Count tokens by watching TokenUsage.add (your pipeline already calls it after every model call)."""
        cls = getattr(pipeline_module, "TokenUsage", None)
        if cls is None or not hasattr(cls, "add"):
            return "the pipeline has no TokenUsage class: tokens and cost will not appear in logs and metrics"
        original = cls.add
        if getattr(original, "_monitoring_owner", None) is self:
            return "token tap already installed"
        original = getattr(original, "_monitoring_original", original)      # a tap from an older Monitor: replace it, never stack
        mon = self

        @functools.wraps(original)
        def add(self_, usage, *a, **k):
            before = _snapshot(self_)
            out = original(self_, usage, *a, **k)
            try:
                mon._on_usage(before, _snapshot(self_), usage)
            except Exception:
                mon.monitor_errors.inc(where="usage_tap")
            return out

        add._monitoring_owner, add._monitoring_original = self, original
        cls.add = add
        return "token tap installed on TokenUsage.add"

    def _on_usage(self, before: dict, after: dict, usage: Any) -> None:
        dp = _delta(before.get("p"), after.get("p"), _usage_field(usage, "prompt_tokens"))
        dc = _delta(before.get("c"), after.get("c"), _usage_field(usage, "completion_tokens"))
        dcost = _delta(before.get("cost"), after.get("cost"), 0.0)
        self.tokens.inc(dp, kind="prompt")
        self.tokens.inc(dc, kind="completion")
        self.llm_calls.inc()
        if dcost > 0:
            self.cost.inc(dcost)
        rec = CURRENT.get()
        if rec is not None:
            with rec.lock:
                rec.tokens_in += int(dp)
                rec.tokens_out += int(dc)
                rec.llm_calls += 1
                rec.cost_usd += dcost


def _quiet_print(*args, **kwargs) -> None:
    return None


def count_prints(module) -> int:
    try:
        import ast
        import inspect
        path = inspect.getsourcefile(module)
        tree = ast.parse(open(path, encoding="utf-8").read())
        return sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print")
    except Exception:
        return 0


def _snapshot(tracker: Any) -> dict:
    out: Dict[str, Any] = {}
    for key, attr in (("p", "prompt_tokens"), ("c", "completion_tokens")):
        v = getattr(tracker, attr, None)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = v
    try:
        v = getattr(tracker, "cost_estimate", None)
        v = v() if callable(v) else v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out["cost"] = float(v)
    except Exception:
        pass
    return out


def _usage_field(usage: Any, name: str) -> float:
    v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _delta(before, after, fallback: float) -> float:
    if before is not None and after is not None:
        return max(0.0, float(after) - float(before))
    return max(0.0, fallback)


# ================================================================ middleware ==
class MonitoringMiddleware:
    """Outermost layer. Pure ASGI (not BaseHTTPMiddleware) so the ContextVar set here is seen by everything inside."""

    def __init__(self, app, mon: Monitor):
        self.app, self.mon = app, mon

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        mon = self.mon
        rid = _ensure_request_id(scope)
        rec = RequestRecord(rid=rid, method=scope.get("method", "-"), started=time.perf_counter(), started_wall=time.time())
        token = CURRENT.set(rec)
        mon.http_in_flight.inc()
        cancelled, crashed = False, None

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                rec.status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as e:
            crashed = e
            raise
        finally:
            try:
                CURRENT.reset(token)
            except ValueError:
                pass
            mon.http_in_flight.dec()
            try:
                mon.finish_request(rec, scope, cancelled, crashed)
            except Exception:                              # monitoring must never break serving
                mon.monitor_errors.inc(where="finish_request")


# ================================================================== wiring ====
def validate_metrics_key(key: str, api_keys: Iterable[str]) -> None:
    if len(key) < MIN_METRICS_KEY_LENGTH:
        raise ValueError(f"RAG_METRICS_KEY must be at least {MIN_METRICS_KEY_LENGTH} characters")
    if key in set(api_keys):
        raise ValueError("RAG_METRICS_KEY must be different from every key in RAG_API_KEYS "
                         "(the metrics key is given to monitoring tools, the API keys to users)")


def instrument(app, mon: Monitor, pipeline=None, checks: Optional[Dict[str, Callable[[], Any]]] = None,
               metrics_key: Optional[str] = None) -> List[str]:
    """Attach everything to an app made by api_layer.create_app. Returns plain-language notes about what was installed."""
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.responses import Response

    notes: List[str] = []
    runner = app.state.runner
    mon.runner = runner
    runner.fn = mon.wrap_pipeline(runner.fn)
    old = runner.executor
    runner.executor = ContextExecutor(max_workers=runner.cfg.max_concurrent, thread_name_prefix="pipeline", on_submit=mon.on_submit)
    old.shutdown(wait=False)
    notes.append("request records follow each call into its worker thread")
    if pipeline is not None:
        quiet_note = mon.silence_prints(pipeline)
        if quiet_note:
            notes.append(quiet_note)
        notes.append(mon.tap_usage(pipeline))
        bounded_note = mon.watch_bounded(pipeline)
        if bounded_note:
            notes.append(bounded_note)
    app.add_middleware(MonitoringMiddleware, mon=mon)

    if metrics_key:
        async def metrics(request: Request):
            supplied = request.headers.get("x-metrics-key", "")
            auth = request.headers.get("authorization", "")
            if not supplied and auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            if not hmac.compare_digest(supplied.encode(), metrics_key.encode()):
                return JSONResponse(status_code=401, content={"error": {"code": "invalid_metrics_key",
                                                                         "message": "Send the metrics key in X-Metrics-Key."}})
            await mon.refresh_dependencies(checks or {})
            return Response(mon.render(), media_type="text/plain; version=0.0.4; charset=utf-8", headers={"Cache-Control": "no-store"})

        app.add_api_route("/metrics", metrics, methods=["GET"], include_in_schema=False)
        notes.append("/metrics is on (needs the X-Metrics-Key header)")
    else:
        notes.append("/metrics is OFF: RAG_METRICS_KEY is not set")

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        cfg = runner.cfg
        mon.emit(mon.app_log, logging.INFO, "startup", slots=cfg.max_concurrent, queue_wait_s=cfg.queue_wait_seconds,
                 request_timeout_s=cfg.request_timeout_seconds, metrics_enabled=bool(metrics_key),
                 python=f"{sys.version_info.major}.{sys.version_info.minor}", version=mon.env.get("RAG_VERSION", "dev"),
                 watching_secrets=len(mon.redactor.secrets))
        async with original_lifespan(a) as state:
            yield state
        mon.emit(mon.app_log, logging.INFO, "shutdown", in_flight=runner.in_flight, waiting=runner.waiting,
                 uptime_s=round(time.time() - mon.started_wall))

    app.router.lifespan_context = lifespan
    notes.append(f"{len(mon.redactor.secrets)} secret value(s) from the environment are blanked out of every log line")
    return notes


def setup(app, cfg, pipeline=None, env: Optional[Dict[str, str]] = None, checks=None) -> Tuple[Monitor, List[str]]:
    """What serve.py calls. Reads RAG_METRICS_KEY and the other RAG_* monitoring settings from the environment."""
    env = os.environ if env is None else env
    mon = Monitor(env)
    key = env.get("RAG_METRICS_KEY", "").strip() or None
    if key:
        validate_metrics_key(key, cfg.api_keys.keys())
    notes = instrument(app, mon, pipeline=pipeline, checks=checks, metrics_key=key)
    return mon, notes
