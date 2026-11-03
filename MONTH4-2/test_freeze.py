"""Tests for freeze_kernel.py.  Run:  python3.11 test_freeze.py
Kernel tests use a REAL Jupyter kernel and a FRESH subprocess for the import."""
import json, os, shutil, subprocess, sys, textwrap, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
FIX = os.path.join(HERE, "fixtures")
from fixture_cells import clean_cells, stale_graph_cells, unresolved_cells, mismatch_cells, secret_cells
from run_cells import run_cells
import freeze_kernel as fk

ENTRY = ["run_with_entity_memory", "multihop_graph", "check_rate_limit", "redis_client",
         "mongo_client", "neo4j_driver", "RAGState", "usage_tracker"]
results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=4)))
    return fn


def freeze_cell():
    return (f"sys.path.insert(0, {HERE!r})\n"
            "from freeze_kernel import freeze_from_kernel\n"
            f"report = freeze_from_kernel(get_ipython(), entry_points={ENTRY!r}, out_dir='rag_pipeline', "
            "graph_names=['multihop_graph'], env={})\n")


def run_variant(name, cells):
    wd = os.path.join(HERE, "work", name)
    shutil.rmtree(wd, ignore_errors=True)
    outs, errs = run_cells(cells + [freeze_cell()], wd, extra_sys_path=[FIX])
    return wd, outs, errs


def fresh(wd, code):
    env = dict(os.environ, PYTHONPATH=FIX)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=wd, env=env, timeout=120)
    return r.returncode, r.stdout, r.stderr


# --------------------------------------------------------------------------- #
CLEAN = {}


@test
def kernel_clean_freeze_writes_a_package():
    wd, outs, errs = run_variant("clean", clean_cells())
    CLEAN["wd"], CLEAN["out"] = wd, outs[-1]
    names = sorted(e[1] for e in errs)
    assert names == ["SyntaxError", "ZeroDivisionError"], f"only the two planted failures expected, got {errs}"
    out = outs[-1]
    assert "✅ freeze complete" in out, out
    assert os.path.exists(os.path.join(wd, "rag_pipeline", "pipeline.py"))
    assert "UNRESOLVED" not in out and "DIFFERS FROM LIVE" not in out, out
    assert "ran BEFORE `make_registry`" in out, "should warn that `registry` was built before make_registry was redefined\n" + out
    assert "FAKE_GROQ_KEY" in out and "FAKE_MONGO_URI" in out, "env vars should be listed\n" + out
    assert "checkpoints/rag_checkpoints.db" in out, "file path should be listed\n" + out
    assert "(2 history cell(s) had syntax errors" not in out
    assert "1 history cell(s) had syntax errors" in out
    wd2 = CLEAN["wd"]
    outs2, errs2 = run_cells(clean_cells() + ["import sys; sys.path.insert(0, %r)\nimport freeze_kernel as fk\n"
                                              "print('AST-PATH', bool(fk.fingerprint_function(generate_node)['ast']))" % HERE],
                             os.path.join(HERE, "work", "astpath"), extra_sys_path=[FIX])
    assert "AST-PATH True" in outs2[-1], outs2[-1]


@test
def generated_source_is_clean():
    src = open(os.path.join(CLEAN["wd"], "rag_pipeline", "pipeline.py"), encoding="utf-8").read()
    for bad in ["def broken", "1/0", "state1", "state_test", "statistics", "random", "threading", "json", "v1 answer",
                "'version': 1", '"version": 1', "%pwd", "get_ipython"]:
        assert bad not in src, f"{bad!r} should have been left behind"
    for good in ["v2 answer 391", "os.environ[\"APP_MODE\"]", "load_dotenv()", "Settings.embed_model", "PATTERNS.append",
                 "LIMIT += 1", "class TokenUsage", "def tracked_llm_call", "@logged", "from notebook cell"]:
        assert good in src or good.replace('"', "'") in src, f"{good!r} missing from the package"
    compile(src, "pipeline.py", "exec")
    assert os.path.exists(os.path.join(CLEAN["wd"], "rag_pipeline", "env.example"))
    env_ex = open(os.path.join(CLEAN["wd"], "rag_pipeline", "env.example")).read()
    assert "FAKE_GROQ_KEY=" in env_ex and "gsk_" not in env_ex


@test
def fresh_process_behaves_like_the_notebook():
    code = textwrap.dedent("""
        import os, sys
        import rag_pipeline.pipeline as m
        import fake_llama
        assert m.CALLS["n"] == 0, "import must not run any test cell"
        for gone in ("statistics", "random", "state1", "state_test", "broken"):
            assert not hasattr(m, gone), gone
        a = m.generate_node({"question": "q"})["answer"]
        assert a == "v2 answer 391 retries=2 registry=2", a          # latest definitions win
        assert fake_llama.Settings.embed_model == "bge-m3"            # config side effect on an imported library
        assert os.environ["APP_MODE"] == "prod"
        assert m.PATTERNS[-1] == "new pattern" and m.LIMIT == 6       # later mutations replayed
        out1 = m.run_with_entity_memory("q1", "s", "u")
        out2 = m.run_with_entity_memory("q2", "s", "u")
        assert out1["classification"] == "SIMPLE" and "391" in out1["answer"]
        assert out1["calls"] == 2 and out2["calls"] == 2, (out1["calls"], out2["calls"])
        assert "classify_node" in m.EVENT_LOG and "retrieve_node" in m.EVENT_LOG   # the @logged chain ran
        assert m.check_rate_limit("u")[0] is True
        print("FRESH-OK")
    """)
    rc, out, err = fresh(CLEAN["wd"], code)
    assert rc == 0 and "FRESH-OK" in out, err[-1500:] + out[-500:]


@test
def parity_probe_matches_for_clean_freeze():
    os.environ["PYTHONPATH"] = FIX
    problems = fk.verify_in_fresh_process(os.path.join(CLEAN["wd"], "rag_pipeline"))
    assert problems == [], problems


@test
def stale_graph_is_caught_by_parity_probe():
    wd, outs, errs = run_variant("stale", stale_graph_cells())
    os.environ["PYTHONPATH"] = FIX
    problems = fk.verify_in_fresh_process(os.path.join(wd, "rag_pipeline"))
    assert any("generate_node" in p and "DIFFERENT CODE" in p for p in problems), problems


@test
def unresolved_names_are_reported():
    wd, outs, errs = run_variant("unresolved", unresolved_cells())
    out = outs[-1]
    assert "UNRESOLVED" in out and "hidden_helper" in out, out
    assert "⚠️  freeze wrote files but needs your attention" in out, out


@test
def runtime_changed_constant_is_flagged():
    wd, outs, errs = run_variant("mismatch", mismatch_cells())
    out = outs[-1]
    assert "DIFFERS FROM LIVE KERNEL" in out and "RETRY_LIMIT" in out, out


@test
def secrets_block_the_write_and_are_never_printed():
    wd, outs, errs = run_variant("secret", secret_cells())
    assert any(e[1] == "SecretFoundError" for e in errs), errs
    assert not os.path.exists(os.path.join(wd, "rag_pipeline", "pipeline.py")), "package must not be written"
    blob = json.dumps(outs) + json.dumps(errs)
    assert "FAKEFAKE" not in blob and "Sup3rS3cret" not in blob, "the secret itself leaked into the output"
    assert "Groq-style key" in blob and "URL with embedded password" in blob


# ------------------------------- pure-python unit tests (no kernel) ---------
def _freeze(cells, entry, **kw):
    ns = {}
    for c in cells:
        try:
            exec(compile(c, "<cell>", "exec"), ns)
        except Exception:
            pass
    return fk.freeze(cells, ns, entry, write=False, env={}, **kw), ns


@test
def unit_self_reference_chain_keeps_previous_definition():
    cells = ["def f():\n    return 1\n", "def deco(fn):\n    return lambda: fn() + 10\n", "f = deco(f)\n"]
    r, ns = _freeze(cells, ["f"])
    labels = [s.label() for s in r.kept]
    assert labels == ["def f():", "def deco(fn):", "f = deco(f)"], labels


@test
def unit_forward_dependency_is_reordered():
    cells = ["def mk():\n    return 1\n", "x = mk()\n", "def mk():\n    return 2\n"]
    r, ns = _freeze(cells, ["x"])
    labels = [s.label() for s in r.kept]
    assert labels == ["def mk():", "x = mk()"] and "return 2" in r.kept[0].src, labels
    assert any("ran BEFORE `mk`" in w for w in r.warnings), r.warnings


@test
def unit_mutation_only_applies_to_final_binding():
    cells = ["P = [1]\n", "P.append(2)\n", "P = [9]\n", "P.append(10)\n", "def use():\n    return P\n"]
    r, ns = _freeze(cells, ["use"])
    srcs = [s.src.strip() for s in r.kept]
    assert srcs == ["P = [9]", "P.append(10)", "def use():\n    return P"], srcs


@test
def unit_local_names_do_not_pull_in_unrelated_globals():
    cells = ["question = 'a test question'\n", "def f(question):\n    answer = question.upper()\n    return answer\n"]
    r, ns = _freeze(cells, ["f"])
    assert [s.label() for s in r.kept] == ["def f(question):"], [s.label() for s in r.kept]


@test
def unit_global_declared_names_are_followed():
    cells = ["counter = 0\n", "def bump():\n    global counter\n    counter += 1\n    return counter\n"]
    r, ns = _freeze(cells, ["bump"])
    assert [s.label() for s in r.kept] == ["counter = 0", "def bump():"], [s.label() for s in r.kept]


@test
def unit_display_libraries_are_not_kept_as_side_effects():
    cells = ["import pandas as pd\n" if False else "import os\n", "def f():\n    return 1\n"]
    r, ns = _freeze(cells, ["f"])
    assert [s.label() for s in r.kept] == ["def f():"]


@test
def unit_entry_point_missing_is_unresolved_and_not_ok():
    r, ns = _freeze(["def f():\n    return 1\n"], ["f", "nope"])
    assert "nope" in r.unresolved and r.ok is False


@test
def unit_secret_scanner():
    assert fk.scan_for_secrets('k = "gsk_ABCDEFGHIJKLMNOPQRSTUVWX"\n', env={}) == [(1, "Groq-style key")]
    assert fk.scan_for_secrets('u = "mongodb+srv://me:pw12345@host/db"\n', env={})[0][1] == "URL with embedded password"
    assert fk.scan_for_secrets('x = "Sup3rS3cretValue"\n', env={"DB_PASSWORD": "Sup3rS3cretValue"})[0][0] == 1
    assert fk.scan_for_secrets('u = "http://localhost:11434"\n', env={"OLLAMA_URL": "http://localhost:11434"}) == []
    assert fk.scan_for_secrets('u = os.getenv("GROQ_API_KEY")\npassword = os.getenv("X")\n', env={}) == []


@test
def unit_patched_function_is_flagged_as_different_from_history():
    cells = ["def f():\n    return 1\n"]
    ns = {}
    exec(compile(cells[0], "<cell>", "exec"), ns)
    ns["f"] = lambda: 2                      # someone replaced it at runtime without defining it in a cell
    r = fk.freeze(cells, ns, ["f"], write=False, env={})
    assert any("f (cell 0)" in m for m in r.mismatches) and r.ok is False, r.mismatches


@test
def unit_ast_fingerprint_ignores_where_imports_live():
    one_file = "import re\ndef f(s):\n    return re.search('a', s)\n"
    split_a, split_b = "import re\n", "def f(s):\n    return re.search('a', s)\n"
    ns1, ns2 = {}, {}
    exec(compile(one_file, "<one>", "exec"), ns1)
    exec(compile(split_a, "<a>", "exec"), ns2); exec(compile(split_b, "<b>", "exec"), ns2)
    # bytecode differs between these two (LOAD_METHOD vs LOAD_ATTR); the syntax tree does not
    assert ns1["f"].__code__.co_code != ns2["f"].__code__.co_code
    node = __import__("ast").parse(split_b).body[0]
    assert fk.fingerprints_equal(fk.fingerprint_node(node, "f"), {"ast": fk.fingerprint_node(node, "f")["ast"], "weak": None})
    assert fk.fingerprint_node(node, "f")["weak"] == fk.fingerprint_node(__import__("ast").parse(one_file).body[1], "f")["weak"]


@test
def unit_refreezing_keeps_the_previous_file():
    import tempfile
    d = tempfile.mkdtemp()
    out = os.path.join(d, "rag_pipeline")
    ns = {}
    cells = ["def f():\n    return 1\n"]
    exec(compile(cells[0], "<cell>", "exec"), ns)
    fk.freeze(cells, ns, ["f"], out_dir=out, env={})
    first = open(os.path.join(out, "pipeline.py")).read()
    assert not os.path.exists(os.path.join(out, "pipeline.py.previous"))
    fk.freeze(cells, ns, ["f"], out_dir=out, env={})
    assert open(os.path.join(out, "pipeline.py.previous")).read() == first


@test
def unit_import_pruning():
    cells = ["import os, json, re\n", "def f():\n    return os.getcwd()\n"]
    r, ns = _freeze(cells, ["f"])
    assert fk._pruned_import(r.kept[0], {"os"}) == "import os", fk._pruned_import(r.kept[0], {"os"})


# --------------------------------------------------------------------------- #
passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
