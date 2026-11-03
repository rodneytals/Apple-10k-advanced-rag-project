"""
api_layer.py -- Day 17: the HTTP front door for the RAG pipeline.

This file knows NOTHING about LangGraph, Groq, Mongo or Neo4j.
It receives a plain function:   pipeline_fn(question, session_id, user_id) -> dict
and wraps it with: auth, validation, rate limiting, bounded concurrency,
timeouts, health/readiness checks, safe errors, and request IDs.

Needs Python 3.11+ (uses asyncio.timeout).
"""
import asyncio
import hmac
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("rag_api")

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MIN_KEY_LENGTH = 16


# ------------------------------------------------------------------ config --
@dataclass
class ApiConfig:
    api_keys: Dict[str, str]                 # api_key -> user_id (identity comes from HERE)
    max_concurrent: int = 1                  # pipeline calls running at once
    queue_wait_seconds: float = 5.0          # how long a request may wait for a free slot
    request_timeout_seconds: float = 60.0    # how long a caller waits for an answer
    rate_limit_retry_after: int = 60         # value sent in Retry-After on a 429
    max_body_bytes: int = 16 * 1024
    max_question_chars: int = 2000
    ready_cache_seconds: float = 5.0
    ready_check_timeout: float = 2.0
    enable_docs: bool = False


def load_keys_from_env(var: str = "RAG_API_KEYS") -> Dict[str, str]:
    """RAG_API_KEYS="longsecretkey1234:rodney,anotherlongkey5678:friend" """
    raw = os.getenv(var, "").strip()
    keys: Dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        if ":" not in pair:
            raise ValueError(f"{var}: each entry must look like key:user_id")
        key, user = pair.split(":", 1)
        if len(key) < MIN_KEY_LENGTH:
            raise ValueError(f"{var}: every key must be at least {MIN_KEY_LENGTH} characters")
        if not ID_PATTERN.match(user):
            raise ValueError(f"{var}: user_id '{user}' must match {ID_PATTERN.pattern}")
        keys[key] = user
    if not keys:
        # Fail CLOSED: an API with no keys configured must not start open.
        raise ValueError(f"{var} is empty -- refusing to start without any API keys")
    return keys


# ----------------------------------------------------------------- errors ---
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: Optional[dict] = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def error_response(request: Request, status: int, code: str, message: str,
                   headers: Optional[dict] = None) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}, "request_id": rid},
        headers=headers or {},
    )


# ------------------------------------------------------------ the runner ----
class PipelineRunner:
    """
    Runs the (blocking, synchronous) pipeline in worker threads with:
      * a hard cap on how many run at once
      * a bounded wait for a free slot            -> 503 busy
      * a bounded wait for the answer             -> 504 timeout
    A Python thread cannot be killed. So when a call times out, the CALLER is
    released with a 504 but the SLOT stays taken until the thread really
    finishes. Otherwise timed-out work would pile up invisibly.
    """

    def __init__(self, fn: Callable[[str, str, str], dict], cfg: ApiConfig):
        self.fn = fn
        self.cfg = cfg
        self.executor = ThreadPoolExecutor(max_workers=cfg.max_concurrent,
                                           thread_name_prefix="pipeline")
        self._sem = asyncio.Semaphore(cfg.max_concurrent)
        self.in_flight = 0
        self.waiting = 0

    async def run(self, question: str, session_id: str, user_id: str) -> dict:
        self.waiting += 1
        try:
            async with asyncio.timeout(self.cfg.queue_wait_seconds):
                await self._sem.acquire()
        except TimeoutError:
            raise ApiError(503, "busy", "The server is busy. Please retry shortly.",
                           {"Retry-After": "5"})
        finally:
            self.waiting -= 1

        self.in_flight += 1
        loop = asyncio.get_running_loop()
        try:
            fut = loop.run_in_executor(self.executor, self.fn, question, session_id, user_id)
        except BaseException:
            self.in_flight -= 1
            self._sem.release()
            raise

        def _done(f: "asyncio.Future"):
            self.in_flight -= 1
            self._sem.release()                      # slot frees only when the thread ends
            if not f.cancelled() and f.exception() is not None:
                log.warning("pipeline thread ended with %s", type(f.exception()).__name__)

        fut.add_done_callback(_done)

        try:
            async with asyncio.timeout(self.cfg.request_timeout_seconds):
                return await asyncio.shield(fut)     # shield: timeout must not cancel the thread's future
        except TimeoutError:
            raise ApiError(504, "timeout", "The request took too long and was abandoned.")
        except ApiError:
            raise
        except Exception as e:                       # the pipeline itself blew up
            log.exception("pipeline error")
            raise ApiError(502, "pipeline_error",
                           f"The pipeline failed ({type(e).__name__}).")

    def shutdown(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


# ------------------------------------------------------------- schemas ------
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = Field(None, description="Optional; reuse it to continue a conversation")


class SourceReference(BaseModel):
    title: str = Field(..., max_length=300)
    page: Optional[str] = Field(None, max_length=40)


class AskResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    classification: Optional[str] = None
    retrieval_source: Optional[str] = None
    reviewer_verdict: Optional[str] = None
    warning: Optional[str] = None
    sources: list[SourceReference] = Field(default_factory=list)
    langsmith_run_id: Optional[str] = None
    elapsed_ms: int


def shape_result(state: dict) -> dict:
    """WHITELIST. The real final_state holds context, profile facts, history...
    none of that may leave the building. Only these keys ever do."""
    s = lambda k: (str(state[k]) if state.get(k) is not None else None)
    answer = s("answer") or ""
    # These are internal prompt-boundary labels, not useful answer content.
    for marker in ("[untrusted_retrieved_data]", "[/untrusted_retrieved_data]",
                   "<untrusted_retrieved_data>", "</untrusted_retrieved_data>"):
        answer = answer.replace(marker, "")
    safe_sources = []
    for item in state.get("sources") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        source = {"title": title.strip()[:300]}
        page = item.get("page")
        if page is not None:
            source["page"] = str(page)[:40]
        safe_sources.append(source)
    return {
        "answer": answer,
        "classification": s("classification"),
        "retrieval_source": s("retrieval_source"),
        "reviewer_verdict": s("reviewer_verdict"),
        "warning": s("warning"),
        "sources": safe_sources,
        "langsmith_run_id": s("langsmith_run_id"),
    }


# ------------------------------------------------------------- the app ------
def create_app(
    pipeline_fn: Callable[[str, str, str], dict],
    cfg: ApiConfig,
    rate_limit_fn: Optional[Callable[[str], Tuple[bool, int]]] = None,
    readiness_checks: Optional[Dict[str, Callable[[], Any]]] = None,
) -> FastAPI:
    runner = PipelineRunner(pipeline_fn, cfg)
    checks = readiness_checks or {}
    ready_cache: dict = {"ts": 0.0, "body": None, "ok": False}
    ready_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        runner.shutdown()

    app = FastAPI(
        title="Apple 10-K RAG API", version="1.0", lifespan=lifespan,
        docs_url="/docs" if cfg.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if cfg.enable_docs else None,
    )
    app.state.runner = runner

    # ---- request id + size cap + one log line per request
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex
        request.state.request_id = rid
        t0 = time.perf_counter()

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cfg.max_body_bytes:
            resp = error_response(request, 413, "body_too_large", "Request body too large.")
        else:
            try:
                resp = await call_next(request)
            except Exception:
                log.exception("unhandled error rid=%s", rid)
                resp = error_response(request, 500, "internal_error", "Internal server error.")
        resp.headers["X-Request-ID"] = rid
        log.info("rid=%s %s %s -> %s %dms user=%s", rid, request.method, request.url.path,
                 resp.status_code, (time.perf_counter() - t0) * 1000,
                 getattr(request.state, "user_id", "-"))
        return resp

    # ---- uniform error bodies
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(request, exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        problems = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "problem": e["msg"]}
                    for e in exc.errors()]
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(status_code=422, content={
            "error": {"code": "invalid_request", "message": "Request validation failed.",
                      "details": problems}, "request_id": rid})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return error_response(request, exc.status_code, "http_error", str(exc.detail))

    # ---- dependencies: auth first, then rate limit
    async def authenticate(request: Request,
                           x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> str:
        if not x_api_key:
            raise ApiError(401, "missing_api_key", "Send your key in the X-API-Key header.")
        match = None
        for key, user in cfg.api_keys.items():       # compare against ALL keys: no early exit timing leak
            if hmac.compare_digest(x_api_key.encode(), key.encode()):
                match = user
        if match is None:
            raise ApiError(401, "invalid_api_key", "Invalid API key.")
        request.state.user_id = match
        return match

    async def enforce_rate_limit(user_id: str = Depends(authenticate)) -> str:
        if rate_limit_fn is None:
            return user_id
        try:
            async with asyncio.timeout(2.0):
                allowed, count = await run_in_threadpool(rate_limit_fn, user_id)
        except Exception as e:                       # limiter trouble must not take the API down
            log.warning("rate limiter unavailable (%s) -- failing open", type(e).__name__)
            return user_id
        if not allowed:
            raise ApiError(429, "rate_limited", "Too many requests. Slow down.",
                           {"Retry-After": str(cfg.rate_limit_retry_after)})
        return user_id

    # ---- routes
    @app.post("/ask", response_model=AskResponse)
    async def ask(body: AskRequest, request: Request, user_id: str = Depends(enforce_rate_limit)):
        client_sid = body.session_id or uuid.uuid4().hex
        if not ID_PATTERN.match(client_sid):
            raise ApiError(422, "invalid_session_id",
                           "session_id may only contain letters, digits, '_' and '-' (max 64).")
        # Namespace by the AUTHENTICATED user, so nobody can read someone else's
        # conversation memory by guessing their session_id.
        internal_sid = f"{user_id}:{client_sid}"
        question = body.question.strip()
        if not question:
            raise ApiError(422, "invalid_request", "Question is empty.")

        t0 = time.perf_counter()
        state = await runner.run(question, internal_sid, user_id)
        shaped = shape_result(state if isinstance(state, dict) else {})
        if state and isinstance(state, dict) and state.get("rate_limited"):
            raise ApiError(429, "rate_limited", "Too many requests. Slow down.",
                           {"Retry-After": str(cfg.rate_limit_retry_after)})
        return AskResponse(request_id=request.state.request_id, session_id=client_sid,
                           elapsed_ms=int((time.perf_counter() - t0) * 1000), **shaped)

    @app.get("/healthz")
    async def healthz():
        """Liveness: 'is the process up and answering?' Touches no dependency."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        """Readiness: 'can it serve a real request right now?' Checks dependencies (cached)."""
        async with ready_lock:
            if time.monotonic() - ready_cache["ts"] > cfg.ready_cache_seconds or ready_cache["body"] is None:
                results, ok = {}, True
                for name, fn in checks.items():
                    try:
                        async with asyncio.timeout(cfg.ready_check_timeout):
                            await run_in_threadpool(fn)
                        results[name] = "ok"
                    except TimeoutError:
                        results[name], ok = "fail: timeout", False
                    except Exception as e:           # class name only: messages can hold connection strings
                        results[name], ok = f"fail: {type(e).__name__}", False
                ready_cache.update(ts=time.monotonic(), ok=ok,
                                   body={"status": "ready" if ok else "not_ready", "checks": results})
        return JSONResponse(status_code=200 if ready_cache["ok"] else 503, content=ready_cache["body"])

    @app.get("/statusz")
    async def statusz(user_id: str = Depends(authenticate)):
        """Authenticated: how loaded is the pipeline right now?"""
        return {"in_flight": runner.in_flight, "waiting": runner.waiting,
                "max_concurrent": cfg.max_concurrent}

    return app
