"""Offline contract tests: no network, no real pipeline. Run:  python test_api_offline.py"""
import os, time, threading, traceback
from fastapi.testclient import TestClient
from api_layer import ApiConfig, create_app, load_keys_from_env

KEY_A = "alice-key-0123456789"
KEY_B = "bob---key-0123456789"
seen = []                                   # what the fake pipeline actually received

def fake_pipeline(question, session_id, user_id):
    seen.append({"q": question, "sid": session_id, "uid": user_id})
    if question == "BOOM":
        raise RuntimeError("mongodb+srv://user:SECRETPASSWORD@cluster0.example.net")
    if question == "LIMITED":
        return {"answer": "You've sent too many requests", "rate_limited": True}
    return {
        "answer": f"echo: {question}", "classification": "SIMPLE", "retrieval_source": "local",
        "reviewer_verdict": "PASS", "warning": None, "langsmith_run_id": "run-123",
        # things that must NEVER be returned to a caller:
        "context": "SECRET CONTEXT CHUNKS", "user_profile_facts": ["lives in Kampala"],
        "conversation_history": [{"role": "user", "content": "private"}],
    }

def make(**kw):
    cfg = ApiConfig(api_keys={KEY_A: "alice", KEY_B: "bob"}, **kw)
    return cfg

H = lambda k=KEY_A: {"X-API-Key": k}
results = []
def test(fn):
    try:
        fn(); results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=2)))
    return fn

def client(app):
    return TestClient(app, raise_server_exceptions=False)

@test
def auth_missing_and_invalid():
    with client(create_app(fake_pipeline, make())) as c:
        r = c.post("/ask", json={"question": "hi"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "missing_api_key", r.text
        r = c.post("/ask", json={"question": "hi"}, headers={"X-API-Key": "nope"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
        r = c.post("/ask", json={"question": "hi"}, headers={"X-API-Key": KEY_A[:-1]})
        assert r.status_code == 401                      # one char short is still wrong

@test
def auth_runs_before_validation():
    with client(create_app(fake_pipeline, make())) as c:
        r = c.post("/ask", json={"wrong": "shape"})
        assert r.status_code == 401, f"unauthenticated caller should not see validation errors: {r.status_code}"

@test
def happy_path_and_whitelist():
    seen.clear()
    with client(create_app(fake_pipeline, make())) as c:
        r = c.post("/ask", json={"question": "  What was net income?  "}, headers=H())
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["answer"] == "echo: What was net income?"         # stripped
        assert r.headers["X-Request-ID"] == b["request_id"]
        for forbidden in ("context", "user_profile_facts", "conversation_history"):
            assert forbidden not in b, f"leaked {forbidden}"
        assert "SECRET CONTEXT" not in r.text and "Kampala" not in r.text
        assert set(b) == {"request_id","session_id","answer","classification","retrieval_source",
                          "reviewer_verdict","warning","langsmith_run_id","elapsed_ms"}, set(b)

@test
def validation_cases():
    with client(create_app(fake_pipeline, make())) as c:
        cases = [({"question": ""}, "empty"), ({"question": "   "}, "whitespace"),
                 ({"question": "x" * 2001}, "too long"), ({}, "missing"),
                 ({"question": 123}, "wrong type"), ({"question": ["a"]}, "list")]
        for body, label in cases:
            r = c.post("/ask", json=body, headers=H())
            assert r.status_code == 422, (label, r.status_code, r.text)
            assert r.json()["error"]["code"] in ("invalid_request",), (label, r.text)
        r = c.post("/ask", content=b"{not json", headers={**H(), "Content-Type": "application/json"})
        assert r.status_code == 422, r.text
        r = c.post("/ask", json={"question": "x" * 2000}, headers=H())
        assert r.status_code == 200                                # exactly at the limit is fine

@test
def identity_comes_from_key_not_body():
    seen.clear()
    with client(create_app(fake_pipeline, make())) as c:
        c.post("/ask", json={"question": "hi", "user_id": "admin"}, headers=H())
        c.post("/ask", json={"question": "hi"}, headers=H(KEY_B))
    assert [s["uid"] for s in seen] == ["alice", "bob"], seen

@test
def sessions_are_namespaced_per_user():
    seen.clear()
    with client(create_app(fake_pipeline, make())) as c:
        a = c.post("/ask", json={"question": "hi", "session_id": "shared"}, headers=H()).json()
        b = c.post("/ask", json={"question": "hi", "session_id": "shared"}, headers=H(KEY_B)).json()
        assert a["session_id"] == b["session_id"] == "shared"      # client sees its own id back
    assert [s["sid"] for s in seen] == ["alice:shared", "bob:shared"], seen
    with client(create_app(fake_pipeline, make())) as c:
        for bad in ("../etc/passwd", "a b", "x" * 65, "a:b", "a\nb"):
            r = c.post("/ask", json={"question": "hi", "session_id": bad}, headers=H())
            assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_session_id", (bad, r.text)
        r = c.post("/ask", json={"question": "hi"}, headers=H())    # no session -> server makes one
        assert r.status_code == 200 and len(r.json()["session_id"]) == 32

@test
def pipeline_exception_becomes_safe_502():
    with client(create_app(fake_pipeline, make())) as c:
        r = c.post("/ask", json={"question": "BOOM"}, headers=H())
        assert r.status_code == 502 and r.json()["error"]["code"] == "pipeline_error"
        assert "SECRETPASSWORD" not in r.text and "mongodb" not in r.text, r.text
        assert "RuntimeError" in r.json()["error"]["message"]
        assert c.post("/ask", json={"question": "ok"}, headers=H()).status_code == 200   # slot not leaked

@test
def graph_rate_limited_flag_maps_to_429():
    with client(create_app(fake_pipeline, make())) as c:
        r = c.post("/ask", json={"question": "LIMITED"}, headers=H())
        assert r.status_code == 429 and "Retry-After" in r.headers, (r.status_code, r.text)

@test
def api_rate_limiter_blocks_and_fails_open():
    counts = {}
    def limiter(uid):
        counts[uid] = counts.get(uid, 0) + 1
        return counts[uid] <= 3, counts[uid]
    with client(create_app(fake_pipeline, make(), rate_limit_fn=limiter)) as c:
        codes = [c.post("/ask", json={"question": "hi"}, headers=H()).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429], codes
        r = c.post("/ask", json={"question": "hi"}, headers=H())
        assert r.headers["Retry-After"] == "60"
        assert c.post("/ask", json={"question": "hi"}, headers=H(KEY_B)).status_code == 200  # per-user
        bad = c.post("/ask", json={"question": ""}, headers=H(KEY_B))                       # invalid bodies count too
        assert bad.status_code == 422 and counts["bob"] == 2, counts
    def broken(uid): raise ConnectionError("redis down")
    with client(create_app(fake_pipeline, make(), rate_limit_fn=broken)) as c:
        assert c.post("/ask", json={"question": "hi"}, headers=H()).status_code == 200

@test
def oversized_body_rejected():
    with client(create_app(fake_pipeline, make())) as c:
        r = c.post("/ask", content=b'{"question":"' + b"a" * 20000 + b'"}',
                   headers={**H(), "Content-Type": "application/json"})
        assert r.status_code == 413 and r.json()["error"]["code"] == "body_too_large", r.status_code

@test
def health_and_readiness():
    calls = {"n": 0}
    def good(): calls["n"] += 1
    def bad(): raise ConnectionError("redis://:HUNTER2@host:6379 refused")
    def hang(): time.sleep(1.0)
    with client(create_app(fake_pipeline, make())) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
    with client(create_app(fake_pipeline, make(), readiness_checks={"redis": good})) as c:
        r1, r2 = c.get("/readyz"), c.get("/readyz")
        assert r1.status_code == 200 and r1.json()["checks"] == {"redis": "ok"}
        assert calls["n"] == 1, "second call should come from cache"
    with client(create_app(fake_pipeline, make(), readiness_checks={"redis": bad, "mongo": good})) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["status"] == "not_ready"
        assert r.json()["checks"] == {"redis": "fail: ConnectionError", "mongo": "ok"}, r.json()
        assert "HUNTER2" not in r.text
        assert c.get("/healthz").status_code == 200       # liveness unaffected by a sick dependency
    with client(create_app(fake_pipeline, make(ready_check_timeout=0.3), readiness_checks={"neo4j": hang})) as c:
        t0 = time.time(); r = c.get("/readyz"); dt = time.time() - t0
        assert r.status_code == 503 and r.json()["checks"]["neo4j"] == "fail: timeout" and dt < 0.9, (dt, r.json())

@test
def request_id_handling():
    with client(create_app(fake_pipeline, make())) as c:
        r = c.get("/healthz", headers={"X-Request-ID": "trace-abc12345"})
        assert r.headers["X-Request-ID"] == "trace-abc12345"
        r = c.get("/healthz", headers={"X-Request-ID": "bad id!<script>"})
        assert r.headers["X-Request-ID"] != "bad id!<script>" and len(r.headers["X-Request-ID"]) == 32

@test
def docs_hidden_and_error_shapes():
    with client(create_app(fake_pipeline, make())) as c:
        for path in ("/docs", "/openapi.json", "/nope"):
            r = c.get(path); assert r.status_code == 404 and "error" in r.json(), (path, r.status_code)
        r = c.get("/ask", headers=H()); assert r.status_code == 405 and "error" in r.json()
    with client(create_app(fake_pipeline, make(enable_docs=True))) as c:
        assert c.get("/docs").status_code == 200

@test
def statusz_requires_auth():
    with client(create_app(fake_pipeline, make())) as c:
        assert c.get("/statusz").status_code == 401
        assert c.get("/statusz", headers=H()).json() == {"in_flight": 0, "waiting": 0, "max_concurrent": 1}

@test
def key_loading_rules():
    def load(v):
        os.environ["TEST_KEYS"] = v
        return load_keys_from_env("TEST_KEYS")
    assert load("abcdefghijklmnop:rodney, qrstuvwxyz123456:friend-1") == {
        "abcdefghijklmnop": "rodney", "qrstuvwxyz123456": "friend-1"}
    for bad in ("", "   ", "short:rodney", "abcdefghijklmnop", "abcdefghijklmnop:bad user", "abcdefghijklmnop:"):
        try: load(bad); raise AssertionError(f"accepted {bad!r}")
        except ValueError: pass
    os.environ.pop("TEST_KEYS", None)
    try: load_keys_from_env("TEST_KEYS"); raise AssertionError("unset var accepted")
    except ValueError: pass

passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
