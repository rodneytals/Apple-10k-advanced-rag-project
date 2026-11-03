"""Live-server tests: real uvicorn, real sockets, real threads. Run:  python test_api_live.py"""
import asyncio, socket, threading, time, traceback
import httpx, uvicorn
from api_layer import ApiConfig, create_app

KEY = "alice-key-0123456789"
H = {"X-API-Key": KEY}

# ---- fake pipeline: BLOCKING on purpose, like run_with_entity_memory
peak = {"now": 0, "max": 0}; lock = threading.Lock()
def fake_pipeline(question, session_id, user_id):
    with lock:
        peak["now"] += 1; peak["max"] = max(peak["max"], peak["now"])
    try:
        delay = {"slow": 2.0, "mid": 1.0, "quick": 0.0}.get(question, 1.0)
        time.sleep(delay)
        return {"answer": f"done:{question}"}
    finally:
        with lock: peak["now"] -= 1

class Server:
    def __init__(self, **cfg_kw):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        app = create_app(fake_pipeline, ApiConfig(api_keys={KEY: "alice"}, **cfg_kw))
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.t = threading.Thread(target=self.server.run, daemon=True)
    def __enter__(self):
        peak.update(now=0, max=0); self.t.start()
        for _ in range(100):
            if self.server.started: break
            time.sleep(0.05)
        assert self.server.started, "server did not start"
        self.base = f"http://127.0.0.1:{self.port}"; return self
    def __exit__(self, *a):
        self.server.should_exit = True; self.t.join(timeout=5)

async def ask(client, q, **kw):
    t0 = time.perf_counter()
    try:
        r = await client.post("/ask", json={"question": q}, headers=H, **kw)
        return r.status_code, round(time.perf_counter() - t0, 2), r
    except httpx.HTTPError as e:
        return type(e).__name__, round(time.perf_counter() - t0, 2), None

results = []
def test(fn):
    try: asyncio.run(fn()); results.append((fn.__name__, True, ""))
    except Exception: results.append((fn.__name__, False, traceback.format_exc(limit=3)))
    return fn

@test
async def bounded_concurrency_two_slots():
    with Server(max_concurrent=2, queue_wait_seconds=10, request_timeout_seconds=20) as s:
        async with httpx.AsyncClient(base_url=s.base, timeout=30) as c:
            t0 = time.perf_counter()
            out = await asyncio.gather(*[ask(c, "mid") for _ in range(4)])
            total = time.perf_counter() - t0
    assert [o[0] for o in out] == [200] * 4, [o[0] for o in out]
    assert peak["max"] == 2, f"never more than 2 at once expected, saw {peak['max']}"
    assert 1.9 < total < 3.0, f"two waves of 1s expected (~2s), got {total:.2f}s"
    print(f"   4 requests, 2 slots: peak={peak['max']} total={total:.2f}s")

@test
async def busy_gives_503_with_retry_after():
    with Server(max_concurrent=1, queue_wait_seconds=0.3, request_timeout_seconds=20) as s:
        async with httpx.AsyncClient(base_url=s.base, timeout=30) as c:
            out = await asyncio.gather(*[ask(c, "mid") for _ in range(3)])
    codes = sorted(o[0] for o in out)
    assert codes == [200, 503, 503], codes
    r503 = [o[2] for o in out if o[0] == 503][0]
    assert r503.headers["Retry-After"] == "5" and r503.json()["error"]["code"] == "busy"
    assert peak["max"] == 1
    print(f"   3 requests, 1 slot, 0.3s patience: {codes}")

@test
async def timeout_frees_caller_but_not_the_slot():
    with Server(max_concurrent=1, queue_wait_seconds=0.3, request_timeout_seconds=0.5) as s:
        async with httpx.AsyncClient(base_url=s.base, timeout=30) as c:
            code1, t1, r1 = await ask(c, "slow")                 # thread runs 2s, caller told at 0.5s
            assert code1 == 504 and 0.4 < t1 < 1.0, (code1, t1)
            assert r1.json()["error"]["code"] == "timeout"
            code2, t2, r2 = await ask(c, "quick")                # thread from request 1 STILL running
            assert code2 == 503, f"slot must still be held by the abandoned thread, got {code2}"
            st = (await c.get("/statusz", headers=H)).json()
            assert st["in_flight"] == 1, st
            await asyncio.sleep(1.8)                             # let the zombie thread finish (2s total)
            code3, t3, _ = await ask(c, "quick")
            assert code3 == 200, f"slot should be free again, got {code3}"
            st = (await c.get("/statusz", headers=H)).json()
            assert st["in_flight"] == 0, st
    print(f"   504 at {t1}s, immediate retry -> {code2}, after thread ends -> {code3}")

@test
async def event_loop_stays_responsive_while_pipeline_blocks():
    with Server(max_concurrent=1, queue_wait_seconds=5, request_timeout_seconds=20) as s:
        async with httpx.AsyncClient(base_url=s.base, timeout=30) as c:
            slow = asyncio.create_task(ask(c, "slow"))
            await asyncio.sleep(0.3)
            times = []
            for _ in range(5):
                t0 = time.perf_counter(); r = await c.get("/healthz"); times.append(time.perf_counter() - t0)
                assert r.status_code == 200
            worst = max(times)
            await slow
    assert worst < 0.25, f"healthz stalled {worst:.2f}s while the pipeline was running"
    print(f"   healthz worst latency during a 2s blocking call: {worst*1000:.0f} ms")

@test
async def client_disconnect_does_not_leak_the_slot():
    with Server(max_concurrent=1, queue_wait_seconds=0.3, request_timeout_seconds=20) as s:
        async with httpx.AsyncClient(base_url=s.base, timeout=30) as c:
            code, t, _ = await ask(c, "mid", timeout=0.3)         # client gives up at 0.3s
            assert code == "ReadTimeout", code
            await asyncio.sleep(1.2)                              # pipeline (1s) finishes on its own
            code2, _, _ = await ask(c, "quick")
            assert code2 == 200, f"slot leaked after client disconnect: {code2}"
            st = (await c.get("/statusz", headers=H)).json()
            assert st == {"in_flight": 0, "waiting": 0, "max_concurrent": 1}, st

@test
async def waiting_counter_returns_to_zero_after_503s():
    with Server(max_concurrent=1, queue_wait_seconds=0.2, request_timeout_seconds=20) as s:
        async with httpx.AsyncClient(base_url=s.base, timeout=30) as c:
            out = await asyncio.gather(*[ask(c, "mid") for _ in range(6)])
            await asyncio.sleep(0.2)
            st = (await c.get("/statusz", headers=H)).json()
    assert st["waiting"] == 0 and st["in_flight"] == 0, st
    assert sorted(o[0] for o in out).count(503) == 5

# ---- demonstration: WHY max_concurrent defaults to 1 with the real pipeline ----
tracker = None
def clobbering_pipeline(question, session_id, user_id):
    global tracker
    tracker = {"tokens": 0}            # same pattern as: usage_tracker = TokenUsage()
    time.sleep(0.3)
    tracker["tokens"] += 100           # "this call used 100 tokens"
    time.sleep(0.2)
    return {"answer": str(tracker["tokens"])}

@test
async def global_tracker_is_unsafe_above_one_slot():
    async def run(slots):
        s0 = socket.socket(); s0.bind(("127.0.0.1", 0)); port = s0.getsockname()[1]; s0.close()
        app = create_app(clobbering_pipeline, ApiConfig(api_keys={KEY: "alice"}, max_concurrent=slots,
                                                         queue_wait_seconds=5, request_timeout_seconds=10))
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        th = threading.Thread(target=srv.run, daemon=True); th.start()
        while not srv.started: await asyncio.sleep(0.05)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as c:
            out = await asyncio.gather(ask(c, "a"), ask(c, "b"))
        srv.should_exit = True; th.join(timeout=5)
        return sorted(o[2].json()["answer"] for o in out)
    two = await run(2); one = await run(1)
    print(f"   2 slots -> each call reports tokens {two} (truth: 100 each)")
    print(f"   1 slot  -> each call reports tokens {one}")
    assert one == ["100", "100"]
    assert two != ["100", "100"], "expected the shared global to be clobbered with 2 slots"

passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
