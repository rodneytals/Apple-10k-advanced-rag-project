"""Tests for the Day 19 tools.  Run:  python test_container.py
No Docker needed: these check the code that WRITES the Docker files and the checks that run before them."""
import ast
import importlib
import importlib.metadata as md
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import traceback
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import container_patch as cp
import container_check as cc
import make_requirements as mr
import make_container_files as mcf

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=4)))
    return fn


def tmp():
    return tempfile.mkdtemp(prefix="d19_")


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w", encoding="utf-8", newline="").write(textwrap.dedent(text).lstrip("\n") if not text.startswith("\ufeff") else text)


def run_patched(source, env):
    """Execute pipeline source against a fake `redis` module and return the arguments each constructor received."""
    calls = []
    fake = types.ModuleType("redis")

    class Redis:
        def __init__(self, *a, **k):
            calls.append(("Redis", a, k))

        @classmethod
        def from_url(cls, *a, **k):
            calls.append(("from_url", a, k))
            return object.__new__(cls)
    fake.Redis = Redis
    fake.StrictRedis = Redis
    fake.from_url = Redis.from_url
    old = sys.modules.get("redis")
    sys.modules["redis"] = fake
    saved = {k: os.environ.get(k) for k in ("REDIS_HOST", "REDIS_PORT", "REDIS_URL")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        exec(compile(source, "<pipeline>", "exec"), {"__name__": "pipeline"})
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        if old is None:
            sys.modules.pop("redis", None)
        else:
            sys.modules["redis"] = old
    return calls


def patch_source(source):
    d = tmp()
    p = os.path.join(d, "pipeline.py")
    write(p, source)
    msg = cp.make_redis_env_driven(p)
    return open(p, encoding="utf-8").read(), msg, p


# ----------------------------------------------------------- redis patch ----
SHAPES = {
    "kwargs": ('import redis\nredis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)\n',
               [("Redis", (), {"host": "localhost", "port": 6379, "decode_responses": True})]),
    "positional": ('import redis\nredis_client = redis.Redis("127.0.0.1", 6380)\n',
                   [("Redis", ("127.0.0.1", 6380), {})]),
    "bare": ('import redis\nredis_client = redis.Redis()\n',
             [("Redis", (), {"host": "localhost"})]),
    "only_kwargs_no_host": ('import redis\nredis_client = redis.Redis(decode_responses=True, db=2)\n',
                            [("Redis", (), {"decode_responses": True, "db": 2, "host": "localhost"})]),
    "from_import": ('from redis import Redis\nrc = Redis(host="localhost", port=6379, db=1)\n',
                    [("Redis", (), {"host": "localhost", "port": 6379, "db": 1})]),
    "from_url": ('import redis\nrc = redis.from_url("redis://localhost:6379/1", decode_responses=True)\n',
                 [("from_url", ("redis://localhost:6379/1",), {"decode_responses": True})]),
    "constants": ('import redis\nREDIS_HOST = "localhost"\nREDIS_PORT = 6379\nrc = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)\n',
                  [("Redis", (), {"host": "localhost", "port": 6379})]),
    "multiline_trailing_comma": ('import redis\nrc = redis.Redis(\n    decode_responses=True,\n)\n',
                                 [("Redis", (), {"decode_responses": True, "host": "localhost"})]),
    "strict_alias": ('import redis as r\nrc = r.StrictRedis(host="localhost")\n',
                     [("Redis", (), {"host": "localhost"})]),
}


@test
def patch_every_shape_keeps_default_behaviour():
    for name, (src, expected) in SHAPES.items():
        before = run_patched(src, {})
        assert before is not None
        new, msg, _ = patch_source(src)
        after = run_patched(new, {})
        assert [(c[0], c[1], c[2]) for c in after] == expected, (name, after, expected)
        # and without the patch the call was the same thing (bare Redis() defaults to localhost)
        if name not in ("bare", "only_kwargs_no_host", "multiline_trailing_comma"):
            assert before == after, (name, before, after)


@test
def patch_every_shape_obeys_the_environment():
    env = {"REDIS_HOST": "redis", "REDIS_PORT": "6390", "REDIS_URL": "redis://redis:6379/1"}
    for name, (src, _) in SHAPES.items():
        new, _, _ = patch_source(src)
        calls = run_patched(new, env)
        flat = repr(calls)
        if name == "from_url":
            assert "redis://redis:6379/1" in flat and "localhost" not in flat, (name, flat)
        else:
            assert "redis" in flat and "localhost" not in flat and "127.0.0.1" not in flat, (name, flat)
            if "port" in src.lower() and name not in ("bare", "only_kwargs_no_host", "multiline_trailing_comma", "strict_alias"):
                assert "6390" in flat, (name, flat)


@test
def patch_touches_nothing_else():
    src = textwrap.dedent('''
        """Module docstring."""
        import redis   # keep this comment
        X = {"a":   1,
             "b": 2}     # odd spacing stays
        def f(a,b):
            return a+b
        redis_client = redis.Redis(host = "localhost" , port=6379)
        Y = "localhost-is-fine-in-this-string"
    ''').lstrip("\n")
    new, _, _ = patch_source(src)
    old_lines, new_lines = src.splitlines(), new.splitlines()
    changed = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
    assert len(new_lines) == len(old_lines) + 1                       # only the added `import os`
    assert any(l == "import os" for l in new_lines)
    assert new_lines[0] == '"""Module docstring."""' and new_lines[1] == "import os"
    assert "# keep this comment" in new and 'Y = "localhost-is-fine-in-this-string"' in new
    assert "def f(a,b):" in new and '"b": 2}     # odd spacing stays' in new


@test
def patch_is_idempotent_and_leaves_backup():
    src = SHAPES["kwargs"][0]
    new, msg, p = patch_source(src)
    assert os.path.exists(p + ".before_container_patch")
    assert open(p + ".before_container_patch").read() == src
    again = cp.make_redis_env_driven(p)
    assert again.startswith("nothing to do"), again
    assert open(p).read() == new


@test
def patch_leaves_existing_environment_driven_code_alone():
    src = 'import os, redis\nrc = redis.Redis(host=os.getenv("MY_REDIS", "localhost"), port=int(os.getenv("MY_PORT", "6379")))\n'
    d = tmp(); p = os.path.join(d, "pipeline.py"); write(p, src)
    assert cp.make_redis_env_driven(p).startswith("nothing to do")
    assert open(p).read() == src
    an = cp.analyze_redis(src)
    assert an.env_vars == {"MY_REDIS": "host", "MY_PORT": "port"}, an.env_vars


@test
def patch_leaves_remote_redis_alone():
    src = 'import redis\nrc = redis.Redis(host="redis-1234.c1.example-cloud.com", port=18000, password=None)\n'
    d = tmp(); p = os.path.join(d, "pipeline.py"); write(p, src)
    msg = cp.make_redis_env_driven(p)
    assert "remote" in msg and open(p).read() == src
    assert cp.analyze_redis(src).all_remote


@test
def patch_refuses_unknown_shapes_and_changes_nothing():
    cases = {
        "attr": 'import redis\ncfg = object()\nrc = redis.Redis(host=cfg.host, port=6379)\n',
        "kwargs_splat": 'import redis\nopts = {}\nrc = redis.Redis(**opts)\n',
        "computed": 'import redis\nrc = redis.Redis(host="local" + "host")\n',
        "function_result": 'import redis\ndef h():\n    return "localhost"\nrc = redis.Redis(host=h())\n',
    }
    for name, src in cases.items():
        d = tmp(); p = os.path.join(d, "pipeline.py"); write(p, src)
        try:
            cp.make_redis_env_driven(p)
        except cp.ContainerPatchError as e:
            assert "nothing was changed" in str(e), (name, str(e))
        else:
            raise AssertionError(f"{name}: should have been refused")
        assert open(p).read() == src, name
        assert not os.path.exists(p + ".before_container_patch")


@test
def patch_refuses_when_no_redis_is_found():
    d = tmp(); p = os.path.join(d, "pipeline.py"); write(p, "x = 1\n")
    try:
        cp.make_redis_env_driven(p)
    except cp.ContainerPatchError as e:
        assert "no Redis connection" in str(e)
    else:
        raise AssertionError("should refuse")


@test
def patch_inserts_import_os_after_future_import_and_docstring():
    src = '"""doc"""\nfrom __future__ import annotations\nimport redis\nrc = redis.Redis(host="localhost")\n'
    new, _, _ = patch_source(src)
    lines = new.splitlines()
    assert lines[:3] == ['"""doc"""', "from __future__ import annotations", "import os"], lines[:4]
    compile(new, "x", "exec")


@test
def patch_adds_import_os_when_the_existing_one_comes_too_late():
    """Frozen files often hold `import os` far down (it came from a later notebook cell). The Redis line runs before it."""
    src = textwrap.dedent('''
        # --- from notebook cell 2 ---
        import redis

        # --- from notebook cell 2 ---
        redis_client = redis.Redis(host="localhost", port=6379)

        # --- from notebook cell 9 ---
        import sys, os
    ''').lstrip("\n")
    new, _, _ = patch_source(src)
    lines = new.splitlines()
    assert lines.index("import os") < lines.index('redis_client = redis.Redis(host=os.getenv("REDIS_HOST", \'localhost\'), port=int(os.getenv("REDIS_PORT", "6379")))'), new
    run_patched(new, {})                                   # it really runs: no NameError
    assert new.count("import sys, os") == 1


@test
def patch_does_not_add_a_second_import_os_when_one_comes_first():
    src = "import os\nimport redis\nrc = redis.Redis(host=\"localhost\")\n"
    new, _, _ = patch_source(src)
    assert new.splitlines().count("import os") == 1, new


@test
def patch_ignores_a_redis_lookalike_that_is_not_the_redis_library():
    src = 'class Redis:\n    def __init__(self, host): pass\nrc = Redis(host="localhost")\n'
    assert cp.analyze_redis(src).sites == []


# ---------------------------------------------------------- container check ----
def make_project(pipeline, env_text=None, files=()):
    d = tmp()
    write(os.path.join(d, "rag_pipeline", "pipeline.py"), pipeline)
    if env_text is not None:
        open(os.path.join(d, ".env"), "wb").write(env_text.encode("utf-8") if isinstance(env_text, str) else env_text)
    for f in files:
        os.makedirs(os.path.dirname(os.path.join(d, f)) or d, exist_ok=True)
        open(os.path.join(d, f), "w").write("x")
    return d


GOOD_PIPELINE = '''
    import os, sqlite3
    import redis
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    redis_client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379)
    mongo_uri = os.getenv("MONGO_URI")
    groq_key = os.getenv("GROQ_API_KEY")
    mode = os.getenv("APP_MODE", "dev")
    embed = HuggingFaceEmbedding(model_name="BAAI/bge-m3")
    CHECKPOINT_DB = "checkpoints/rag_checkpoints.db"
    FILINGS = "data/apple_10k.json"
'''


@test
def check_clean_project_has_no_blockers_and_lists_what_docker_must_do():
    d = make_project(GOOD_PIPELINE, "MONGO_URI=mongodb+srv://x\nGROQ_API_KEY=abc\n", files=["data/apple_10k.json"])
    r = cc.check_container_readiness("rag_pipeline", d)
    assert r.ok, r.summary()
    assert "checkpoints" in r.volume_paths, r.volume_paths
    assert r.copy_paths == ["data/apple_10k.json"], r.copy_paths
    assert r.models == ["BAAI/bge-m3"]
    assert r.redis_mode == "compose"
    assert r.env_read["APP_MODE"] is True and r.env_read["MONGO_URI"] is False
    assert not r.env_missing, r.env_missing


@test
def check_flags_localhost_windows_paths_and_secrets():
    pipe = '''
        import os, redis
        from pymongo import MongoClient
        m = MongoClient("mongodb://localhost:27017")
        P = "C:\\\\Users\\\\rodne\\\\Desktop\\\\data.json"
        K = "gsk_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"
        r = redis.Redis(host="localhost")
    '''
    d = make_project(pipe, "X=1\n")
    r = cc.check_container_readiness("rag_pipeline", d)
    text = "\n".join(r.blockers)
    assert not r.ok
    assert "points at this computer" in text and "mongodb://localhost" in text, text
    assert "Windows/personal path" in text, text
    assert "line 5" in text, text                       # the typed secret
    assert "gsk_" not in text, "a secret value must never be printed"


@test
def check_flags_a_bare_localhost_host_string_and_a_loopback_ip():
    for host in ("localhost", "127.0.0.1"):
        d = make_project(f'NEO_HOST = "{host}"\n', "X=1\n")
        r = cc.check_container_readiness("rag_pipeline", d)
        assert not r.ok and "points at this computer" in "\n".join(r.blockers), (host, r.summary())


@test
def check_redis_localhost_is_an_action_not_a_blocker():
    d = make_project('import redis\nr = redis.Redis(host="localhost", port=6379)\n', "X=1\n")
    r = cc.check_container_readiness("rag_pipeline", d)
    assert r.ok and r.redis_mode == "compose", r.summary()
    assert any("written into the code" in a for a in r.actions)


@test
def check_unknown_redis_shape_is_a_blocker():
    d = make_project('import redis\nclass C: host="x"\nr = redis.Redis(host=C.host)\n', "X=1\n")
    r = cc.check_container_readiness("rag_pipeline", d)
    assert not r.ok and "Redis address" in "\n".join(r.blockers)


@test
def check_env_file_problems():
    d = make_project(GOOD_PIPELINE, b"\xef\xbb\xbfMONGO_URI=abc\nexport GROQ_API_KEY=k\nBAD = 1\nMONGO_URI=again\n", files=["data/apple_10k.json"])
    names, issues = cc.read_env_names(os.path.join(d, ".env"))
    text = "\n".join(issues)
    assert "byte-order mark" in text and "export" in text and "spaces around" in text and "already defined" in text, text
    assert {"MONGO_URI", "GROQ_API_KEY", "BAD"} <= names
    r = cc.check_container_readiness("rag_pipeline", d)
    assert not r.ok
    fixed = cc.fix_env_file(os.path.join(d, ".env"))
    assert fixed and not open(os.path.join(d, ".env"), "rb").read().startswith(b"\xef\xbb\xbf")
    assert os.path.exists(os.path.join(d, ".env.before_bom_fix"))
    assert cc.fix_env_file(os.path.join(d, ".env")) == []


@test
def check_secret_values_are_found_by_name_and_never_include_plain_settings():
    d = make_project("x = 1\n", "GROQ_API_KEY=gsk_abcdefghijkl\nMONGO_URI=mongodb+srv://u:pw@c0.example.net/db\nREDIS_URL=redis://localhost:6379/0\n"
                                "RAG_PORT=8000\nAPP_MODE=prod\nSHORT_KEY=abc\n")
    vals = cc.secret_values(os.path.join(d, ".env"))
    assert sorted(vals) == sorted(["gsk_abcdefghijkl", "mongodb+srv://u:pw@c0.example.net/db"]), vals


@test
def check_missing_env_var_without_default_is_reported():
    d = make_project('import os\na = os.getenv("NEEDED")\nb = os.getenv("OPTIONAL", "x")\nc = os.environ["ALSO_NEEDED"]\n', "X=1\n")
    r = cc.check_container_readiness("rag_pipeline", d)
    assert sorted(r.env_missing) == ["ALSO_NEEDED", "NEEDED"], r.env_missing


@test
def check_missing_data_file_is_noted_not_copied():
    d = make_project('F = "data/not_here.json"\n', "X=1\n")
    r = cc.check_container_readiness("rag_pipeline", d)
    assert r.copy_paths == [] and any("does not exist" in n for n in r.notes), r.notes


@test
def check_docstrings_and_prose_do_not_trigger_findings():
    d = make_project('"""Talks to localhost and C:\\\\Users in the docs. See notes.txt for more."""\nPROMPT = "Answer using the file data.json if it helps"\n', "X=1\n")
    r = cc.check_container_readiness("rag_pipeline", d)
    assert r.ok and not r.copy_paths and not r.volume_paths, r.summary()


@test
def check_all_remote_redis_means_no_redis_container():
    d = make_project('import redis\nr = redis.Redis(host="redis-1.cloud.example.com", port=1234)\n', "X=1\n")
    r = cc.check_container_readiness("rag_pipeline", d)
    assert r.redis_mode == "external"


# ------------------------------------------------------------ requirements ----
def make_dist(root, name, version, requires=(), modules=(), direct_url=None):
    """A tiny, real, installed distribution (dist-info + importable module) on a temp sys.path entry."""
    di = os.path.join(root, f"{name.replace('-', '_')}-{version}.dist-info")
    os.makedirs(di)
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)
    open(os.path.join(di, "METADATA"), "w").write(meta)
    record = []
    for m in modules:
        mp = os.path.join(root, *m.split("."))
        os.makedirs(mp, exist_ok=True)
        init = os.path.join(mp, "__init__.py")
        open(init, "w").write("")
        record.append(os.path.relpath(init, root).replace(os.sep, "/") + ",,")
    open(os.path.join(di, "RECORD"), "w").write("\n".join(record) + ("\n" if record else ""))
    if direct_url:
        open(os.path.join(di, "direct_url.json"), "w").write(direct_url)


class FakeSite:
    def __enter__(self):
        self.root = tmp()
        sys.path.insert(0, self.root)
        return self.root

    def __exit__(self, *a):
        sys.path.remove(self.root)
        importlib.invalidate_caches()
        mr.reset_cache()
        for m in [m for m in sys.modules if m.split(".")[0].startswith("zz_")]:
            del sys.modules[m]


@test
def requirements_follow_imports_to_the_right_distribution_and_pin_closure():
    with FakeSite() as root:
        make_dist(root, "zz-app", "1.2.3", ["zz-lib>=1", "zz-winonly; sys_platform == 'win32'", "zz-linuxonly; sys_platform == 'linux'",
                                            "zz-fancy; extra == 'fancy'", "zz-notinstalled>=1; sys_platform == 'linux'"], ["zz_app"])
        make_dist(root, "zz-lib", "2.0.0+cu121", [], ["zz_lib"])
        make_dist(root, "zz-winonly", "9.9", [], ["zz_winonly"])
        make_dist(root, "zz-linuxonly", "3.1", [], ["zz_linuxonly"])
        make_dist(root, "zz-fancy", "1.0", [], ["zz_fancy"])
        importlib.invalidate_caches(); mr.reset_cache()
        proj = tmp()
        write(os.path.join(proj, "rag_pipeline", "pipeline.py"), "import os, json\nimport zz_app\nfrom .usage_context import UsageProxy\nfrom usage_context import X\n")
        write(os.path.join(proj, "serve.py"), "import logging\n")
        write(os.path.join(proj, "api_layer.py"), "x = 1\n")
        rep = mr.write_requirements("rag_pipeline", proj)
        txt = open(os.path.join(proj, "requirements.docker.txt")).read()
        assert "zz-app==1.2.3" in txt and "zz-lib==2.0.0" in txt, txt           # +cu121 stripped
        assert "zz-linuxonly==3.1" in txt
        assert "zz-winonly" not in txt and "zz-fancy" not in txt, txt           # Windows-only and not-requested extra
        assert "zz-notinstalled" in rep.not_installed_here
        assert rep.top_level == ["zz-app"], rep.top_level
        assert not os.path.exists(os.path.join(proj, "requirements.torch.txt"))


@test
def requirements_honour_requested_extras():
    with FakeSite() as root:
        make_dist(root, "zz-web", "1.0", ["zz-http[fast]>=1"], ["zz_web"])
        make_dist(root, "zz-http", "5.0", ["zz-fastlib; extra == 'fast'", "zz-slowlib; extra == 'slow'"], ["zz_http"])
        make_dist(root, "zz-fastlib", "1.1", [], ["zz_fastlib"])
        make_dist(root, "zz-slowlib", "1.2", [], ["zz_slowlib"])
        importlib.invalidate_caches(); mr.reset_cache()
        rep = mr.RequirementsReport("3.11")
        res = mr.closure(["zz-web"], "3.11", rep)
        assert "zz-fastlib" in res and "zz-slowlib" not in res, res


@test
def requirements_split_out_pytorch_and_drop_gpu_and_windows_packages():
    with FakeSite() as root:
        make_dist(root, "zz-model", "1.0", ["torch>=2", "zz-win32-thing; sys_platform == 'win32'"], ["zz_model"])
        make_dist(root, "torch", "2.5.1+cu121", ["nvidia-cublas-cu12; sys_platform == 'linux'", "triton; sys_platform == 'linux'", "filelock"], ["torch"])
        make_dist(root, "torchvision", "0.20.1+cu121", ["torch"], ["torchvision"])
        make_dist(root, "filelock", "3.16.1", [], ["filelock"])
        make_dist(root, "nvidia-cublas-cu12", "12.1.3.1", [], ["nvidia_cublas"])
        make_dist(root, "triton", "3.0.0", [], ["triton"])
        importlib.invalidate_caches(); mr.reset_cache()
        proj = tmp()
        write(os.path.join(proj, "rag_pipeline", "pipeline.py"), "import zz_model\nimport torchvision\n")
        rep = mr.write_requirements("rag_pipeline", proj, extra_files=())
        main = open(os.path.join(proj, "requirements.docker.txt")).read()
        tor = open(os.path.join(proj, "requirements.torch.txt")).read()
        assert "torch==2.5.1" in tor and "torchvision==0.20.1" in tor and "+cu" not in tor + main, (tor, main)
        assert "nvidia" not in main and "triton" not in main and "filelock==3.16.1" in main
        assert "torch==2.5.1" in main                                            # satisfied by the CPU build installed first


@test
def requirements_warn_about_packages_not_from_pypi():
    with FakeSite() as root:
        make_dist(root, "zz-mine", "0.1", [], ["zz_mine"], direct_url='{"url": "file:///C:/Users/me/zz-mine", "dir_info": {"editable": true}}')
        importlib.invalidate_caches(); mr.reset_cache()
        proj = tmp()
        write(os.path.join(proj, "rag_pipeline", "pipeline.py"), "import zz_mine\n")
        rep = mr.write_requirements("rag_pipeline", proj, extra_files=())
        assert any("zz-mine" in w and "local folder" in w for w in rep.warnings), rep.warnings


@test
def requirements_namespace_packages_resolve_to_the_specific_distribution():
    with FakeSite() as root:
        # two distributions sharing the namespace  zz_ns  (like llama_index.*)
        make_dist(root, "zz-ns-core", "1.0", [], ["zz_ns.core"])
        make_dist(root, "zz-ns-extra", "2.0", [], ["zz_ns.extra"])
        importlib.invalidate_caches(); mr.reset_cache()
        proj = tmp()
        write(os.path.join(proj, "rag_pipeline", "pipeline.py"), "from zz_ns.extra import thing\n")
        rep = mr.write_requirements("rag_pipeline", proj, extra_files=())
        assert rep.top_level == ["zz-ns-extra"], rep.top_level


@test
def requirements_real_packages_in_this_environment():
    proj = tmp()
    write(os.path.join(proj, "rag_pipeline", "pipeline.py"), "import fastapi, uvicorn, httpx\nfrom dotenv import load_dotenv\n")
    rep = mr.write_requirements("rag_pipeline", proj, extra_files=())
    txt = open(os.path.join(proj, "requirements.docker.txt")).read()
    for must in ("fastapi==", "uvicorn==", "httpx==", "python-dotenv==", "starlette==", "pydantic=="):
        assert must in txt, (must, txt[:400])
    assert "pywin32" not in txt and "colorama" not in txt


# ------------------------------------------------------------ container files ----
def fake_check(redis_mode="compose", volumes=("checkpoints",), copy=()):
    r = cc.ContainerReport(package="rag_pipeline")
    r.redis_mode = redis_mode
    r.volume_paths = list(volumes)
    r.copy_paths = list(copy)
    return r


def fake_reqs(torch=False):
    r = mr.RequirementsReport(python_version="3.11")
    if torch:
        r.torch_pins = {"torch": "2.5.1"}
    return r


def generate(pipeline_src, redis_mode="compose", volumes=("checkpoints",), copy=(), torch=False):
    d = make_project(pipeline_src, "X=1\n")
    mcf.make_container_files("rag_pipeline", d, fake_check(redis_mode, volumes, copy), fake_reqs(torch))
    return d


@test
def files_redis_environment_follows_the_patched_code():
    patched, _, _ = patch_source('import redis\nrc = redis.Redis(host="localhost", port=6379)\n')
    assert mcf.redis_environment(patched) == {"REDIS_HOST": "redis", "REDIS_PORT": "6379"}
    url, _, _ = patch_source('import redis\nrc = redis.from_url("redis://localhost:6379/3")\n')
    assert mcf.redis_environment(url) == {"REDIS_URL": "redis://redis:6379/3"}
    custom = 'import os, redis\nrc = redis.Redis(host=os.getenv("MY_REDIS", "localhost"), port=int(os.getenv("MY_PORT", "6379")))\n'
    assert mcf.redis_environment(custom) == {"MY_REDIS": "redis", "MY_PORT": "6379"}
    assert set(mcf.redis_environment("x = 1\n")) == {"REDIS_HOST", "REDIS_PORT", "REDIS_URL"}


@test
def files_written_with_lf_endings_no_secrets_and_backups_on_rerun():
    d = generate('import redis\nrc = redis.Redis(host="localhost")\n', copy=("data/x.json",))
    for f in ("Dockerfile", "docker-compose.yml", ".dockerignore", "docker_healthcheck.py"):
        raw = open(os.path.join(d, f), "rb").read()
        assert b"\r\n" not in raw, f
    mcf.make_container_files("rag_pipeline", d, fake_check(copy=("data/x.json",)), fake_reqs())
    assert os.path.exists(os.path.join(d, "Dockerfile.previous"))
    df = open(os.path.join(d, "Dockerfile")).read()
    assert "COPY --chown=app:app data/x.json ./data/x.json" in df
    assert "USER app" in df and "HEALTHCHECK" in df and "FROM python:3.11-slim" in df
    assert "requirements.torch.txt" not in df


@test
def files_torch_block_only_when_torch_is_needed():
    d = generate('import redis\nrc = redis.Redis(host="localhost")\n', torch=True)
    df = open(os.path.join(d, "Dockerfile")).read()
    assert "download.pytorch.org/whl/cpu" in df and "--no-deps" in df
    assert df.index("COPY requirements.torch.txt") < df.index("COPY requirements.docker.txt")


@test
def files_dockerignore_is_an_allow_list_that_keeps_env_out():
    d = generate('import redis\nrc = redis.Redis(host="localhost")\n', copy=("data/x.json",))
    lines = open(os.path.join(d, ".dockerignore")).read().splitlines()
    rules = [l for l in lines if l and not l.startswith("#")]
    assert rules[0] == "*"
    for needed in ("!rag_pipeline", "!serve.py", "!api_layer.py", "!docker_healthcheck.py", "!requirements.docker.txt", "!data/x.json"):
        assert needed in rules, needed
    assert "!.env" not in rules and "**/.env" in rules


@test
def files_compose_is_valid_yaml_with_the_expected_wiring():
    import yaml
    d = generate('import redis\nrc = redis.Redis(host="localhost")\n')
    c = yaml.safe_load(open(os.path.join(d, "docker-compose.yml")))
    rag, redis = c["services"]["rag"], c["services"]["redis"]
    assert rag["depends_on"]["redis"]["condition"] == "service_healthy"
    assert rag["environment"]["RAG_HOST"] == "0.0.0.0" and rag["environment"]["REDIS_HOST"] == "redis"
    assert rag["env_file"] == [".env"]
    assert rag["ports"] == ["127.0.0.1:${RAG_HOST_PORT:-8080}:8000"]
    assert "models:/models" in rag["volumes"] and "checkpoints:/app/checkpoints" in rag["volumes"]
    assert "ports" not in redis and redis["image"].startswith("redis:")
    assert set(c["volumes"]) == {"models", "checkpoints", "redis_data"}
    assert rag["init"] is True and rag["cap_drop"] == ["ALL"]


@test
def files_external_redis_means_no_redis_service():
    import yaml
    d = generate('import redis\nrc = redis.Redis(host="redis-1.cloud.example.com")\n', redis_mode="external")
    c = yaml.safe_load(open(os.path.join(d, "docker-compose.yml")))
    assert "redis" not in c["services"] and "depends_on" not in c["services"]["rag"]
    assert not any(k.startswith("REDIS") for k in c["services"]["rag"]["environment"])
    assert "redis_data" not in c["volumes"]


@test
def files_state_file_in_project_root_is_refused_with_a_clear_message():
    d = make_project('import redis\nrc = redis.Redis(host="localhost")\n', "X=1\n")
    try:
        mcf.make_container_files("rag_pipeline", d, fake_check(volumes=("state.db",)), fake_reqs())
    except RuntimeError as e:
        assert "cannot be a volume" in str(e) or "file cannot be a volume" in str(e), str(e)
    else:
        raise AssertionError("should refuse")
    assert not os.path.exists(os.path.join(d, "Dockerfile"))


@test
def files_compose_config_is_accepted_by_docker_compose_if_installed():
    if not shutil.which("docker"):
        return
    d = generate('import redis\nrc = redis.Redis(host="localhost")\n')
    open(os.path.join(d, ".env"), "w").write("RAG_API_KEYS=abcdefghijklmnopqrstuvwxyz:me\n")
    r = subprocess.run(["docker", "compose", "-f", os.path.join(d, "docker-compose.yml"), "--project-directory", d, "config", "-q"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@test
def healthcheck_script_exits_1_when_nothing_listens_and_0_when_healthy():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = str(srv.server_address[1])
    ok = subprocess.run([sys.executable, os.path.join(HERE, "docker_healthcheck.py")], env=dict(os.environ, RAG_PORT=port)).returncode
    srv.shutdown()
    down = subprocess.run([sys.executable, os.path.join(HERE, "docker_healthcheck.py")], env=dict(os.environ, RAG_PORT=port)).returncode
    assert ok == 0 and down == 1, (ok, down)


# --------------------------------------------------------------------------- #
passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
