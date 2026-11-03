"""Tests for usage_context.py, make_concurrency_safe.py, audit_globals.py and serve.py.
Run:  python3.11 test_concurrency.py"""
import contextvars, importlib, os, shutil, socket, sys, textwrap, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import httpx, uvicorn
import audit_globals as ag
import usage_context as uc
from make_concurrency_safe import PatchError, make_concurrency_safe
import serve

WORK = os.path.join(HERE, "work", "conc")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
sys.path.insert(0, WORK)
results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=4)))
    return fn


# A pipeline shaped like yours: a global tracker replaced per call, a decorated entry point with a docstring,
# an early-return path, and a graph whose middle step runs two nodes IN PARALLEL.
PIPELINE_SRC = r'''
import functools, operator, threading, time
from dataclasses import dataclass
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, END

@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_calls: int = 0

    def add(self, usage):
        self.prompt_tokens += usage["p"]
        self.completion_tokens += usage["c"]
        self.total_calls += 1

    def report(self):
        REPORTS.append((self.prompt_tokens, self.total_calls))

REPORTS = []
EVENT_LOG = []
usage_tracker = TokenUsage()

def tracked_llm_call(tokens):
    time.sleep(0.02)
    usage_tracker.add({"p": tokens, "c": 1})
    return "ok"

class S(TypedDict):
    m: int
    out: Annotated[list, operator.add]

def classify(state):
    tracked_llm_call(100 * state["m"]); return {"out": ["classify"]}
def retrieve_a(state):
    tracked_llm_call(10 * state["m"]); return {"out": ["a"]}
def retrieve_b(state):
    tracked_llm_call(20 * state["m"]); return {"out": ["b"]}
def generate(state):
    tracked_llm_call(1000 * state["m"]); return {"out": ["generate"]}

def build():
    g = StateGraph(S)
    for n, f in [("classify", classify), ("retrieve_a", retrieve_a), ("retrieve_b", retrieve_b), ("generate", generate)]:
        g.add_node(n, f)
    g.set_entry_point("classify")
    g.add_edge("classify", "retrieve_a"); g.add_edge("classify", "retrieve_b")   # parallel fan-out
    g.add_edge("retrieve_a", "generate"); g.add_edge("retrieve_b", "generate"); g.add_edge("generate", END)
    return g.compile()

multihop_graph = build()

import uuid

def trace(run_type="chain"):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            return fn(*a, **k)
        return wrapper
    return deco

@trace(run_type="chain")
def run_with_entity_memory(question: str, session_id: str, user_id: str) -> dict:
    """Run one question."""
    global usage_tracker
    usage_tracker = TokenUsage()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    print(f"\n{'='*60}\nSession: {session_id} | Question: {question}\n{'='*60}")
    EVENT_LOG.append(session_id)
    if question == "":
        return {"answer": "empty"}

    initial_state: dict = {
        "m": int(question), "out": [],
        # ... more fields ...
    }
    final = multihop_graph.invoke(initial_state, config=config)
    usage_tracker.report()
    print(f"\n✅ Answer: {final['out']}\n")
    return {"answer": f"calls={usage_tracker.total_calls} prompt={usage_tracker.prompt_tokens}"}

_hits = {}
def check_rate_limit(user_id):
    _hits[user_id] = _hits.get(user_id, 0) + 1
    return True, _hits[user_id]

class _R:
    def ping(self): return True
class _Admin:
    def command(self, c): return {"ok": 1}
class _M:
    admin = _Admin()
class _N:
    def verify_connectivity(self): return None
redis_client, mongo_client, neo4j_driver = _R(), _M(), _N()
'''

EXPECTED = {str(i): f"calls=4 prompt={i * 1130}" for i in range(1, 9)}   # 100+10+20+1000 per unit


def make_pkg(name, patched):
    d = os.path.join(WORK, name)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "__init__.py"), "w").write("")
    path = os.path.join(d, "pipeline.py")
    open(path, "w").write(PIPELINE_SRC)
    msg = make_concurrency_safe(path) if patched else None
    importlib.invalidate_caches()
    mod = importlib.import_module(name + ".pipeline")
    return mod, path, msg


def hammer(mod, n=8):
    with ThreadPoolExecutor(n) as ex:
        futs = {str(i): ex.submit(mod.run_with_entity_memory, str(i), "s", "u") for i in range(1, n + 1)}
        return {k: f.result()["answer"] for k, f in futs.items()}


# ------------------------------------------------------------------ proxy ---
@test
def proxy_isolates_calls_and_counts_fallback_use():
    class T:
        n = 0
        def add(self, x): self.n += x
    proxy = uc.UsageProxy(T())
    a, b = T(), T()

    def work(tracker, amount):
        tok = proxy.begin_call(tracker)
        try:
            for _ in range(500):
                proxy.add(amount)
                time.sleep(0)
        finally:
            proxy.end_call(tok)
    ts = [threading.Thread(target=work, args=(a, 1)), threading.Thread(target=work, args=(b, 10))]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert (a.n, b.n) == (500, 5000), (a.n, b.n)
    assert proxy.fallback_hits == 0 and proxy.active is None
    proxy.add(7)                                        # no call in progress -> fallback, and it is counted
    assert proxy.fallback_hits == 1 and proxy._fallback.n == 7


@test
def proxy_forwards_attribute_reads_and_writes():
    class T:
        prompt_tokens = 0
        def report(self): return f"p={self.prompt_tokens}"
    proxy = uc.UsageProxy(T())
    mine = T()
    tok = proxy.begin_call(mine)
    proxy.prompt_tokens += 5
    assert mine.prompt_tokens == 5 and proxy.report() == "p=5" and proxy.fallback_hits == 0
    proxy.end_call(tok)
    assert proxy.active is None


@test
def proxy_add_is_exact_when_parallel_branches_share_one_tracker():
    class T:
        n = 0
        def add(self, x):
            v = self.n; time.sleep(0); self.n = v + x          # deliberately race-prone
    proxy, shared = uc.UsageProxy(T()), T()
    tok = proxy.begin_call(shared)
    ctxs = [contextvars.copy_context() for _ in range(8)]       # one copy per thread, all pointing at the SAME tracker
    ts = [threading.Thread(target=lambda c=c: c.run(lambda: [proxy.add(1) for _ in range(300)])) for c in ctxs]
    [t.start() for t in ts]; [t.join() for t in ts]
    proxy.end_call(tok)
    assert shared.n == 2400, shared.n


# ------------------------------------------------------- the real problem ---
@test
def unpatched_pipeline_gets_the_numbers_wrong_under_concurrency():
    mod, path, _ = make_pkg("pkg_before", patched=False)
    got = hammer(mod)
    wrong = {k: v for k, v in got.items() if v != EXPECTED[k]}
    assert wrong, "expected overlapping calls to corrupt each other's token counts"
    print(f"   unpatched, 8 overlapping calls: {len(wrong)} of 8 reported someone else's tokens, e.g. {list(wrong.items())[:2]}")


@test
def patched_pipeline_is_exact_even_with_parallel_branches():
    mod, path, msg = make_pkg("pkg_after", patched=True)
    assert msg.startswith("patched") and "run_with_entity_memory" in msg, msg
    for _ in range(3):
        got = hammer(mod)
        assert got == EXPECTED, {k: (v, EXPECTED[k]) for k, v in got.items() if v != EXPECTED[k]}
    assert mod.usage_tracker.fallback_hits == 0, f"{mod.usage_tracker.fallback_hits} calls used the shared fallback"
    assert mod.usage_tracker.active is None, "a finished call must not leave its tracker behind"
    assert mod.run_with_entity_memory("", "s", "u") == {"answer": "empty"}          # early-return path
    assert mod.usage_tracker.active is None, "early return must still release the tracker"


@test
def patch_keeps_docstring_decorator_and_is_idempotent():
    mod = importlib.import_module("pkg_after.pipeline")
    path = os.path.join(WORK, "pkg_after", "pipeline.py")
    src = open(path).read()
    assert os.path.exists(path + ".before_concurrency_patch")
    assert os.path.exists(os.path.join(WORK, "pkg_after", "usage_context.py"))
    assert mod.run_with_entity_memory.__doc__ == "Run one question."
    assert hasattr(mod.run_with_entity_memory, "__wrapped__"), "decorator lost"
    assert "global usage_tracker" not in src and "from .usage_context import UsageProxy" in src
    assert "try:" in src and "end_call(_usage_token)" in src
    assert make_concurrency_safe(path) == "already patched: nothing to do"
    assert open(path).read() == src


@test
def patch_refuses_shapes_it_does_not_understand_and_changes_nothing():
    d = os.path.join(WORK, "weird")
    os.makedirs(d, exist_ok=True)
    cases = {
        "no_tracker": "def f():\n    return 1\n",
        "assigned_in_branch": textwrap.dedent('''
            class TokenUsage: pass
            usage_tracker = TokenUsage()
            def run(x):
                global usage_tracker
                if x:
                    usage_tracker = TokenUsage()
                return 1
        '''),
        "assigned_twice": textwrap.dedent('''
            class TokenUsage: pass
            usage_tracker = TokenUsage()
            def run(x):
                global usage_tracker
                usage_tracker = TokenUsage()
                usage_tracker = TokenUsage()
        '''),
    }
    for name, src in cases.items():
        path = os.path.join(d, name + ".py")
        open(path, "w").write(src)
        try:
            make_concurrency_safe(path)
            raise AssertionError(f"{name}: should have refused")
        except PatchError:
            pass
        assert open(path).read() == src, f"{name}: file was modified"
        assert not os.path.exists(path + ".before_concurrency_patch")


# ----------------------------------------------------------------- audit ----
@test
def audit_finds_shared_state_and_the_patch_removes_the_rebind():
    before = ag.audit(os.path.join(WORK, "pkg_before", "pipeline.py"))
    kinds = {(f["kind"], f["name"]) for f in before}
    assert ("REBINDS_GLOBAL", "usage_tracker") in kinds, kinds
    assert ("GROWS_FOREVER", "EVENT_LOG") in kinds and ("GROWS_FOREVER", "REPORTS") in kinds, kinds
    after = ag.audit(os.path.join(WORK, "pkg_after", "pipeline.py"))
    assert not any(f["kind"] == "REBINDS_GLOBAL" for f in after), after
    assert any(f["name"] == "EVENT_LOG" for f in after), "unbounded logs are still there; the patch does not touch them"


@test
def audit_ignores_local_names_and_bounded_structures():
    src = textwrap.dedent('''
        from collections import deque
        LOG = []
        RECENT = deque(maxlen=50)
        CACHE = {}
        def f():
            LOG = []                # a LOCAL list that happens to share the name
            LOG.append(1)
            RECENT.append(1)        # bounded by maxlen but still shared
            return LOG
        def g(k):
            CACHE[k] = 1
        def h():
            CACHE.clear()
    ''')
    f = {(x["kind"], x["name"]) for x in ag.audit_source(src)}
    assert ("GROWS_FOREVER", "LOG") not in f and ("SHARED_MUTABLE", "LOG") not in f, f
    assert ("SHARED_MUTABLE", "CACHE") in f, f
    assert ("GROWS_FOREVER", "RECENT") in f, "deque(maxlen=...) is still flagged: the audit cannot see maxlen"


# ----------------------------------------------------------------- serve ----
ENV = {"RAG_API_KEYS": "dev-key-0123456789abcdef:rodney", "RAG_MAX_CONCURRENT": "1"}


@test
def serve_refuses_extra_slots_on_an_unpatched_pipeline():
    mod = importlib.import_module("pkg_before.pipeline")
    app, cfg = serve.build_app(dict(ENV), pipeline=mod)               # 1 slot is fine
    assert cfg.max_concurrent == 1
    try:
        serve.build_app(dict(ENV, RAG_MAX_CONCURRENT="3"), pipeline=mod)
        raise AssertionError("should have refused")
    except SystemExit as e:
        assert "make_concurrency_safe" in str(e), e
    app, cfg = serve.build_app(dict(ENV, RAG_MAX_CONCURRENT="3"), pipeline=importlib.import_module("pkg_after.pipeline"))
    assert cfg.max_concurrent == 3


def _serve_in_thread(app):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        if server.started: break
        time.sleep(0.05)
    assert server.started
    return server, t, f"http://127.0.0.1:{port}"


def _http_hammer(base, n=6):
    H = {"X-API-Key": "dev-key-0123456789abcdef"}
    with ThreadPoolExecutor(n) as ex:
        futs = {str(i): ex.submit(lambda q: httpx.post(f"{base}/ask", json={"question": q}, headers=H, timeout=30), str(i))
                for i in range(1, n + 1)}
        return {k: f.result() for k, f in futs.items()}


@test
def end_to_end_three_slots_through_http_is_exact():
    mod = importlib.import_module("pkg_after.pipeline")
    app, cfg = serve.build_app(dict(ENV, RAG_MAX_CONCURRENT="3"), pipeline=mod)
    server, t, base = _serve_in_thread(app)
    try:
        r = httpx.get(f"{base}/readyz").json()
        assert r["checks"] == {"redis": "ok", "mongo": "ok", "neo4j": "ok"}, r
        t0 = time.time()
        got = _http_hammer(base, 6)
        dt = time.time() - t0
        assert all(v.status_code == 200 for v in got.values()), {k: v.status_code for k, v in got.items()}
        answers = {k: v.json()["answer"] for k, v in got.items()}
        assert answers == {k: EXPECTED[k] for k in answers}, answers
        assert mod.usage_tracker.fallback_hits == 0
        print(f"   6 simultaneous HTTP requests on 3 slots: all exact, {dt:.2f}s")
    finally:
        server.should_exit = True; t.join(timeout=5)


@test
def end_to_end_unpatched_with_forced_extra_slots_is_wrong__which_is_why_serve_refuses():
    from api_layer import ApiConfig, create_app
    mod = importlib.import_module("pkg_before.pipeline")
    cfg = ApiConfig(api_keys={"dev-key-0123456789abcdef": "rodney"}, max_concurrent=3, queue_wait_seconds=10, request_timeout_seconds=20)
    app = create_app(lambda q, s, u: mod.run_with_entity_memory(q, s, u), cfg)     # bypasses serve.py's interlock
    server, t, base = _serve_in_thread(app)
    try:
        got = _http_hammer(base, 6)
        answers = {k: v.json()["answer"] for k, v in got.items()}
        assert any(answers[k] != EXPECTED[k] for k in answers), "expected corrupted counts with 3 slots on the unpatched pipeline"
    finally:
        server.should_exit = True; t.join(timeout=5)


@test
def end_to_end_unpatched_with_one_slot_is_exact():
    mod = importlib.import_module("pkg_before.pipeline")
    app, cfg = serve.build_app(dict(ENV, RAG_QUEUE_WAIT_SECONDS="30"), pipeline=mod)
    server, t, base = _serve_in_thread(app)
    try:
        got = _http_hammer(base, 5)
        answers = {k: v.json()["answer"] for k, v in got.items()}
        assert answers == {k: EXPECTED[k] for k in answers}, answers
    finally:
        server.should_exit = True; t.join(timeout=5)


@test
def serve_fails_closed_without_keys_and_without_entry_point():
    mod = importlib.import_module("pkg_after.pipeline")
    try:
        serve.build_app({"RAG_API_KEYS": ""}, pipeline=mod)
        raise AssertionError("started with no API keys")
    except ValueError:
        pass
    class Empty: pass
    try:
        serve.build_app(dict(ENV), pipeline=Empty())
        raise AssertionError("accepted a module with no entry point")
    except SystemExit as e:
        assert "run_with_entity_memory" in str(e)


# ------------------------------------------------------------------------- #
passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
