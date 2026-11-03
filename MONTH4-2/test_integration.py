"""End to end, the way you will use it: messy notebook -> freeze -> make_concurrency_safe -> audit -> serve -> HTTP.
Run:  python3.11 test_integration.py"""
import importlib, os, shutil, socket, subprocess, sys, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
FIX = os.path.join(HERE, "fixtures")
from fixture_cells import clean_cells
from run_cells import run_cells
import audit_globals as ag
from make_concurrency_safe import make_concurrency_safe
import freeze_kernel as fk
import httpx, uvicorn, serve

ENTRY = ["run_with_entity_memory", "multihop_graph", "check_rate_limit", "redis_client",
         "mongo_client", "neo4j_driver", "RAGState", "usage_tracker"]
WD = os.path.join(HERE, "work", "integ")
shutil.rmtree(WD, ignore_errors=True)
results = []

def test(fn):
    try:
        fn(); results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=4)))
    return fn

STATE = {}

@test
def step1_freeze_the_messy_notebook():
    cell = (f"sys.path.insert(0, {HERE!r})\nfrom freeze_kernel import freeze_from_kernel\n"
            f"report = freeze_from_kernel(get_ipython(), entry_points={ENTRY!r}, out_dir='rag_pipeline', graph_names=['multihop_graph'], env={{}})\n")
    outs, errs = run_cells(clean_cells() + [cell], WD, extra_sys_path=[FIX])
    assert "✅ freeze complete" in outs[-1], outs[-1]
    os.environ["PYTHONPATH"] = FIX
    sys.path.insert(0, WD); sys.path.insert(0, FIX)

@test
def step2_audit_reports_the_shared_state():
    findings = ag.audit(os.path.join(WD, "rag_pipeline", "pipeline.py"))
    kinds = {(f["kind"], f["name"]) for f in findings}
    assert ("REBINDS_GLOBAL", "usage_tracker") in kinds, kinds
    assert ("GROWS_FOREVER", "EVENT_LOG") in kinds, kinds
    print("   audit before:", sorted(kinds))

@test
def step3_patch_and_parity_still_holds():
    msg = make_concurrency_safe(os.path.join(WD, "rag_pipeline", "pipeline.py"))
    assert msg.startswith("patched") and "run_with_entity_memory" in msg, msg
    problems = fk.verify_in_fresh_process(os.path.join(WD, "rag_pipeline"))
    assert problems == [], problems
    after = {(f["kind"], f["name"]) for f in ag.audit(os.path.join(WD, "rag_pipeline", "pipeline.py"))}
    assert ("REBINDS_GLOBAL", "usage_tracker") not in after, after

@test
def step4_overlapping_calls_get_their_own_tracker():
    importlib.invalidate_caches()
    m = importlib.import_module("rag_pipeline.pipeline")
    STATE["m"] = m
    with ThreadPoolExecutor(8) as ex:
        outs = list(ex.map(lambda i: m.run_with_entity_memory(f"q{i}", f"s{i}", "u"), range(24)))
    assert all(o["calls"] == 2 for o in outs), [o["calls"] for o in outs]
    assert all("391" in o["answer"] for o in outs)
    assert m.usage_tracker.fallback_hits == 0 and m.usage_tracker.active is None

@test
def step5_serve_it_over_http():
    m = STATE["m"]
    app, cfg = serve.build_app({"RAG_API_KEYS": "dev-key-0123456789abcdef:rodney", "RAG_MAX_CONCURRENT": "3"}, pipeline=m)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    while not server.started: time.sleep(0.05)
    try:
        base = f"http://127.0.0.1:{port}"; H = {"X-API-Key": "dev-key-0123456789abcdef"}
        r = httpx.get(f"{base}/readyz"); assert r.status_code == 200 and r.json()["checks"] == {"redis": "ok", "mongo": "ok", "neo4j": "ok"}, r.text
        with ThreadPoolExecutor(6) as ex:
            rs = list(ex.map(lambda i: httpx.post(f"{base}/ask", json={"question": f"What was net income {i}?", "session_id": "d18"}, headers=H, timeout=30), range(6)))
        assert all(r.status_code == 200 and "391" in r.json()["answer"] for r in rs), [r.text[:100] for r in rs]
        assert m.usage_tracker.fallback_hits == 0
        assert httpx.post(f"{base}/ask", json={"question": "hi"}).status_code == 401
    finally:
        server.should_exit = True; t.join(timeout=5)

passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
