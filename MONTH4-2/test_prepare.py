"""Tests for prepare_monitoring.py, the container-file changes and print silencing.  Run:  python test_prepare.py"""
import contextlib
import importlib
import io
import os
import shutil
import sys
import tempfile
import textwrap
import traceback
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import httpx
import uvicorn

import make_container_files as mcf
import prepare_monitoring as pm
import serve
from testkit import API_KEY, API_KEY2, METRICS_KEY, free_port

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


def project(env_text=None, tools=True, serve_version="new", pipeline_src=None, dockerfile=None):
    d = tempfile.mkdtemp(prefix="d20p_")
    os.makedirs(os.path.join(d, "rag_pipeline"))
    open(os.path.join(d, "rag_pipeline", "__init__.py"), "w").write("")
    open(os.path.join(d, "rag_pipeline", "pipeline.py"), "w").write(pipeline_src or "X = 1\n\ndef run_with_entity_memory(q, s, u):\n    return {}\n")
    if tools:
        for f in pm.TOOL_FILES:
            shutil.copy(os.path.join(HERE, f), os.path.join(d, f))
    if serve_version == "old":
        open(os.path.join(d, "serve.py"), "w").write("# day 18\n")
    open(os.path.join(d, ".env"), "w").write(env_text if env_text is not None else f"RAG_API_KEYS={API_KEY}:rodney\nRAG_METRICS_KEY={METRICS_KEY}\n")
    if dockerfile is not None:
        open(os.path.join(d, "Dockerfile"), "w").write(dockerfile)
    return d


@test
def ensure_key_adds_one_line_keeps_a_copy_and_prints_nothing_secret():
    d = project(env_text=f"RAG_API_KEYS={API_KEY}:rodney\nGROQ_API_KEY=abc")          # no trailing newline
    p = os.path.join(d, ".env")
    before = open(p).read()
    msg = pm.ensure_metrics_key(p)
    after = open(p).read()
    assert after.startswith(before) and after.count("\n") == before.count("\n") + 2 and "RAG_METRICS_KEY=" in after
    key = after.split("RAG_METRICS_KEY=")[1].strip()
    assert len(key) >= 32 and key not in msg and key != API_KEY
    assert open(p + ".before_metrics_key").read() == before
    assert "already in .env" in pm.ensure_metrics_key(p) and open(p).read() == after          # idempotent
    from dotenv import dotenv_values
    assert dotenv_values(p)["GROQ_API_KEY"] == "abc" and dotenv_values(p)["RAG_METRICS_KEY"] == key


@test
def ensure_key_refuses_bad_existing_values_and_odd_files():
    d = project(env_text="RAG_METRICS_KEY=short\n")
    for text, word in (("RAG_METRICS_KEY=short\n", "shorter"), (f"RAG_API_KEYS={API_KEY}:r\nRAG_METRICS_KEY={API_KEY}\n", "same as")):
        open(os.path.join(d, ".env"), "w").write(text)
        try:
            pm.ensure_metrics_key(os.path.join(d, ".env"))
        except RuntimeError as e:
            assert word in str(e), e
            assert open(os.path.join(d, ".env")).read() == text                               # untouched
        else:
            raise AssertionError("accepted")
    open(os.path.join(d, ".env"), "wb").write(b"\xef\xbb\xbfA=1\n")
    try:
        pm.ensure_metrics_key(os.path.join(d, ".env"))
    except RuntimeError as e:
        assert "byte-order mark" in str(e)
    else:
        raise AssertionError("BOM accepted")
    try:
        pm.ensure_metrics_key(os.path.join(d, "nope.env"))
    except FileNotFoundError:
        pass


@test
def risky_lines_found_by_name():
    d = project(pipeline_src=textwrap.dedent('''
        import logging
        log = logging.getLogger("x")
        def f(question, n):
            print("starting")
            print(f"Question: {question}")
            log.info("got %s", question)
            log.info("count %s", n)
            logging.warning(f"answer={n}")
    '''))
    lines = pm.risky_log_lines(os.path.join(d, "rag_pipeline", "pipeline.py"))
    assert [(n, k) for n, _, k in lines] == [(6, "print"), (7, "log")] and "print(f\"Question" in lines[0][1], lines      # words inside a string are not variables: not flagged


@test
def ready_check_all_good():
    d = project(dockerfile="COPY serve.py api_layer.py docker_healthcheck.py monitoring.py ./\n")
    rep = pm.check_monitoring_ready(d, "rag_pipeline", ".env")
    assert rep.ok and "ready to rebuild" in rep.summary(), rep.summary()
    assert not any("❌" in l for l in rep.summary().splitlines()[:-1])


@test
def ready_check_tells_prints_from_logging_calls():
    src = "import logging\nlog = logging.getLogger('x')\ndef f(question):\n    print(question)\n"
    rep = pm.check_monitoring_ready(project(pipeline_src=src))
    assert "1 print() call(s)" in rep.summary() and "silenced" in rep.summary() and "no logging call in the pipeline" in rep.summary()
    src += "    log.info('q %s', question)\n"
    rep = pm.check_monitoring_ready(project(pipeline_src=src))
    assert "1 logging call(s)" in rep.summary() and "WILL reach the log" in rep.summary(), rep.summary()


@test
def ready_check_catches_each_problem():
    d = project(tools=False)
    t = pm.check_monitoring_ready(d).summary()
    assert "missing from the project folder" in t and "❌ fix" in t, t
    d = project(serve_version="old")
    assert "still the Day 18 version" in pm.check_monitoring_ready(d).summary()
    d = project(env_text=f"RAG_API_KEYS={API_KEY}:rodney\n")
    assert "RAG_METRICS_KEY is not in .env" in pm.check_monitoring_ready(d).summary()
    d = project(env_text=f"RAG_API_KEYS={API_KEY}:rodney\nRAG_METRICS_KEY={API_KEY}\n")
    assert "different from every API key" in pm.check_monitoring_ready(d).summary()
    d = project(dockerfile="COPY serve.py api_layer.py ./\n")
    assert "does not copy monitoring.py yet" in pm.check_monitoring_ready(d).summary()
    d = project(pipeline_src="LOG = []\n\ndef f(x):\n    LOG.append(x)\n")
    assert "still grows forever: LOG" in pm.check_monitoring_ready(d).summary()


@test
def compose_published_to_all_interfaces_is_warned():
    d = project()
    open(os.path.join(d, "docker-compose.yml"), "w").write('ports:\n  - "8080:8000"\n')
    assert "all network interfaces" in pm.check_monitoring_ready(d).summary()
    open(os.path.join(d, "docker-compose.yml"), "w").write('ports:\n  - "127.0.0.1:8080:8000"\n')
    assert "this computer only" in pm.check_monitoring_ready(d).summary()


# ----------------------------------------------------- the Dockerfile changes --
@test
def dockerfile_copies_monitoring_only_when_asked():
    plain = mcf.dockerfile_text("rag_pipeline", "3.11", False, [], ["checkpoints"])
    assert "COPY --chown=app:app serve.py api_layer.py docker_healthcheck.py ./\n" in plain and "monitoring" not in plain
    withmon = mcf.dockerfile_text("rag_pipeline", "3.11", False, [], ["checkpoints"], ["monitoring.py"])
    assert "COPY --chown=app:app serve.py api_layer.py docker_healthcheck.py monitoring.py ./\n" in withmon
    assert withmon.replace(" monitoring.py", "") == plain
    ig = mcf.dockerignore_text("rag_pipeline", [], ["monitoring.py"])
    assert "!monitoring.py\n" in ig and "monitoring" not in mcf.dockerignore_text("rag_pipeline", [])


@test
def generator_adds_monitoring_py_when_it_sits_next_to_serve():
    import container_check as cc
    import make_requirements as mr
    d = project()
    for f in ("serve.py", "api_layer.py"):
        shutil.copy(os.path.join(HERE, f), os.path.join(d, f))
    check = cc.check_container_readiness("rag_pipeline", d, os.path.join(d, ".env"))
    reqs = mr.write_requirements("rag_pipeline", d)
    mcf.make_container_files("rag_pipeline", d, check, reqs)
    assert "monitoring.py" in open(os.path.join(d, "Dockerfile")).read()
    assert "!monitoring.py" in open(os.path.join(d, ".dockerignore")).read()
    os.remove(os.path.join(d, "monitoring.py"))
    mcf.make_container_files("rag_pipeline", d, check, reqs)
    assert "monitoring" not in open(os.path.join(d, "Dockerfile")).read()


# ------------------------------------------------------------ silencing print --
PRINTY = '''
import os

def run_with_entity_memory(question, session_id, user_id):
    print(f"Question: {question}")
    return {"answer": "A" * 40, "classification": "numeric"}

def check_rate_limit(uid):
    return True, 1
'''


def serve_pkg(src, env):
    d = tempfile.mkdtemp(prefix="d20q_")
    name = "pk" + os.path.basename(d)[-6:]
    os.makedirs(os.path.join(d, name))
    open(os.path.join(d, name, "__init__.py"), "w").write("")
    open(os.path.join(d, name, "pipeline.py"), "w").write(src)
    sys.path.insert(0, d)
    importlib.invalidate_caches()
    m = importlib.import_module(name + ".pipeline")
    full = {"RAG_API_KEYS": f"{API_KEY}:rodney", "RAG_METRICS_KEY": METRICS_KEY, "RAG_MAX_CONCURRENT": "1", **env}
    app, cfg = serve.build_app(full, m)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    import threading
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    return app, server, t, f"http://127.0.0.1:{port}"


def ask_with_stdout_captured(src, env):
    app, server, t, url = serve_pkg(src, env)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            r = httpx.post(url + "/ask", json={"question": "SECRET-QUESTION-TEXT?"}, headers={"X-API-Key": API_KEY}, timeout=20)
        assert r.status_code == 200, r.text
    finally:
        server.should_exit = True
        t.join(10)
    return buf.getvalue()


@test
def pipeline_prints_are_silenced_by_default():
    out = ask_with_stdout_captured(PRINTY, {})
    assert "SECRET-QUESTION-TEXT" not in out, out


@test
def pipeline_prints_come_back_when_asked():
    out = ask_with_stdout_captured(PRINTY, {"RAG_PIPELINE_PRINTS": "1"})
    assert "SECRET-QUESTION-TEXT" in out, out


@test
def the_startup_note_counts_the_prints():
    import monitoring
    d = tempfile.mkdtemp(prefix="d20r_")
    name = "pr" + os.path.basename(d)[-6:]
    os.makedirs(os.path.join(d, name))
    open(os.path.join(d, name, "__init__.py"), "w").write("")
    open(os.path.join(d, name, "pipeline.py"), "w").write(PRINTY + "\nprint('a')\nprint('b')\n")
    sys.path.insert(0, d)
    importlib.invalidate_caches()
    m = importlib.import_module(name + ".pipeline")
    note = monitoring.Monitor({}).silence_prints(m)
    assert "3 found" in note, note
    assert monitoring.Monitor({}).silence_prints(m) is None            # a module that already has its own `print` is left alone


if __name__ == "__main__":
    bad = 0
    for name, ok, tb in results:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
            print(tb)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)
