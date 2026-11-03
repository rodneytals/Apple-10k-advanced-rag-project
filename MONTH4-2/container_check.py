"""
container_check.py -- Day 19: before building anything, find what will break inside a container.

    report = check_container_readiness("rag_pipeline", project_dir=".", env_file=".env")
    print(report.summary())

It only READS: the frozen pipeline.py, the .env file (names only, values are never printed) and the folder.
Four kinds of finding:
    BLOCKER   the container cannot work until this is fixed (a secret typed in code, a Windows path, "localhost")
    ACTION    something the generated Docker files must handle (files to copy, state to keep in a volume)
    NOTE      worth knowing (models that download on first start, environment variables with no value yet)
"""
import ast
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from container_patch import LOCAL_HOSTS, analyze_redis  # noqa: E402

try:                                                    # Day 18 tool, reused so there is ONE definition of "secret"
    from freeze_kernel import scan_for_secrets
except Exception:                                       # pragma: no cover
    scan_for_secrets = None

STATE_EXT = (".db", ".sqlite", ".sqlite3")
DATA_EXT = (".json", ".jsonl", ".pdf", ".txt", ".csv", ".pkl", ".pickle", ".faiss", ".index", ".parquet", ".md", ".yaml", ".yml", ".html", ".htm")
MODEL_CALLS = {"HuggingFaceEmbedding", "SentenceTransformer", "CrossEncoder", "HuggingFaceInferenceAPIEmbedding"}
FROM_PRETRAINED = {"from_pretrained"}
ABS_PATH_RX = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/Users/|/home/|/mnt/|~[\\/])|[\\/]Users[\\/]")
URL_RX = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


@dataclass
class ContainerReport:
    package: str
    blockers: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    env_read: Dict[str, bool] = field(default_factory=dict)       # name -> has a default in the code
    env_missing: List[str] = field(default_factory=list)          # read with no default and absent from .env
    copy_paths: List[str] = field(default_factory=list)           # relative files/folders that exist and must be copied in
    volume_paths: List[str] = field(default_factory=list)         # relative state files (sqlite) that need a volume
    models: List[str] = field(default_factory=list)
    redis_mode: str = "unknown"                                   # compose | external | unknown
    redis_env: Dict[str, str] = field(default_factory=dict)       # env var -> host|port|url
    env_file_issues: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def summary(self) -> str:
        out = []
        for title, items in (("BLOCKERS (fix these first)", self.blockers),
                             ("What the Docker files will handle", self.actions),
                             ("Good to know", self.notes)):
            if items:
                out.append(title + ":")
                out += ["   - " + i for i in items]
        out.append("")
        out.append("✅ nothing blocks the container" if self.ok else "⚠️  fix the blockers above, then run this cell again")
        return "\n".join(out)


# ------------------------------------------------------------------- helpers --
def _docstring_nodes(tree: ast.AST) -> Set[int]:
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = getattr(n, "body", [])
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                ids.add(id(b[0].value))
    return ids


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def read_env_names(env_file: str) -> Tuple[Set[str], List[str]]:
    """Names (never values) defined in a .env file, plus formatting problems that break Docker Compose."""
    names, issues = set(), []
    if not os.path.exists(env_file):
        return names, [f"{env_file} does not exist"]
    raw = open(env_file, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        issues.append(".env starts with a hidden byte-order mark (Windows Notepad does this). Docker would read the first "
                      "variable under a wrong name. Re-save it as plain UTF-8 (the fix cell does this for you).")
    text = raw.decode("utf-8-sig", errors="replace")
    seen = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            issues.append(f".env line {n}: starts with 'export' (Docker Compose does not understand it)")
            s = s[len("export "):]
        if "=" not in s:
            issues.append(f".env line {n}: no '=' sign")
            continue
        k, v = s.split("=", 1)
        if k != k.strip() or (k.strip() and (k.endswith(" ") or v.startswith(" "))):
            issues.append(f".env line {n}: spaces around '=' ({k.strip()})")
        k = k.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", k):
            issues.append(f".env line {n}: '{k[:20]}' is not a valid variable name")
            continue
        if k in seen:
            issues.append(f".env line {n}: {k} is already defined on line {seen[k]} (the last one wins)")
        seen[k] = n
        names.add(k)
    return names, issues


def secret_values(env_file: str) -> List[str]:
    """Values in .env that look like secrets (keys, tokens, passwords, URLs carrying credentials). Never print them."""
    out = []
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(env_file)
    except Exception:
        vals = {}
    for k, v in vals.items():
        if not v or len(v) < 8 or v.isdigit() or v.lower() in ("true", "false", "localhost"):
            continue
        if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIAL)", k, re.I) or (re.search(r"(URI|URL|DSN)", k, re.I) and "@" in v):
            out.append(v)
    return out


def fix_env_file(env_file: str) -> List[str]:
    """Safe fixes only: strip the BOM. Everything else is reported, not rewritten."""
    raw = open(env_file, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        backup = env_file + ".before_bom_fix"
        if not os.path.exists(backup):
            open(backup, "wb").write(raw)
        open(env_file, "wb").write(raw[3:])
        return ["removed the hidden byte-order mark from .env (backup: .env.before_bom_fix, delete it when you are happy)"]
    return []


# --------------------------------------------------------------------- main --
def check_container_readiness(package: str = "rag_pipeline", project_dir: str = ".", env_file: str = ".env") -> ContainerReport:
    rep = ContainerReport(package=package)
    pipeline_path = os.path.join(project_dir, package, "pipeline.py")
    if not os.path.exists(pipeline_path):
        rep.blockers.append(f"{pipeline_path} does not exist: run the Day 18 freeze first")
        return rep
    source = open(pipeline_path, encoding="utf-8").read()
    tree = ast.parse(source)
    doc_ids = _docstring_nodes(tree)

    # 1) secrets typed in code ---------------------------------------------------------------------
    if scan_for_secrets is not None:
        try:
            from dotenv import dotenv_values
            env_vals = {k: v for k, v in dotenv_values(os.path.join(project_dir, env_file)).items() if v}
        except Exception:
            env_vals = {}
        env_vals = {**env_vals, **os.environ}
        for line, what in scan_for_secrets(source, env=env_vals):
            rep.blockers.append(f"{package}/pipeline.py line {line}: {what}. A Docker image keeps everything copied into it: "
                                f"move it to .env and read it with os.getenv(...).")

    # 2) addresses that mean "this computer" ------------------------------------------------------
    redis = analyze_redis(source)
    redis_lines = set()
    for s in redis.sites:
        redis_lines.add(s.line)
    if redis.has_redis_import and redis.sites:
        rep.redis_env = redis.env_vars
        for p in redis.problems:
            rep.blockers.append("Redis address is not something I can make environment-driven: " + p)
        if redis.all_remote:
            rep.redis_mode = "external"
            rep.notes.append("Redis is on a remote server: no Redis container will be added.")
        elif redis.needs_patch:
            rep.redis_mode = "compose"
            rep.actions.append("The Redis host/port are written into the code (localhost). The next cell makes them come from "
                               "REDIS_HOST / REDIS_PORT (same defaults as today), and compose adds a Redis container.")
        else:
            rep.redis_mode = "compose"
            names = ", ".join(sorted(redis.env_vars)) or "environment variables"
            rep.actions.append(f"Redis address already comes from {names}: compose sets them to the Redis container.")
    else:
        rep.notes.append("Could not find where redis_client is created in the pipeline file. Compose will still start a Redis "
                         "container, but you must make sure your code reaches it by the name 'redis'.")
        rep.redis_mode = "compose"

    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            v = n.value
            line = n.lineno
            if line in redis_lines:
                continue
            low = v.lower()
            if v in LOCAL_HOSTS or (URL_RX.match(v) and re.search(r"//(?:[^/@]*@)?(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/|$)", low)):
                rep.blockers.append(f"line {line}: points at this computer ('{v[:50]}'). Inside a container 'localhost' is the "
                                    f"container itself. Use a hosted service (Atlas, Aura) via an environment variable.")
            if ABS_PATH_RX.search(v):
                rep.blockers.append(f"line {line}: Windows/personal path '{v[:60]}' will not exist in the container. "
                                    f"Use a relative path.")

    # 3) files the code opens ----------------------------------------------------------------------
    seen_paths = set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in doc_ids:
            continue
        v = n.value.strip()
        if not v or len(v) > 200 or "\n" in v or "{" in v or URL_RX.match(v) or ABS_PATH_RX.search(v) or " " in v and "/" not in v:
            continue
        looks_like_file = v.lower().endswith(STATE_EXT + DATA_EXT) and re.fullmatch(r"[\w .\-\\/]+", v) is not None
        exists = os.path.exists(os.path.join(project_dir, v.replace("\\", "/")))
        is_dir_like = "/" in v and exists and os.path.isdir(os.path.join(project_dir, v.replace("\\", "/")))
        if not (looks_like_file or is_dir_like):
            continue
        norm = v.replace("\\", "/").lstrip("./")
        if norm in seen_paths or norm.startswith(("http", "text/", "application/")):
            continue
        seen_paths.add(norm)
        if norm.lower().endswith(STATE_EXT) or norm.split("/")[0] in ("checkpoints", "data_state", "state"):
            top = norm.split("/")[0] if "/" in norm else None
            rep.volume_paths.append(top if top else norm)
            rep.actions.append(f"'{norm}' is state the app writes: it goes in a Docker volume so it survives restarts "
                               f"(a bind mount from Windows breaks SQLite file locking).")
        elif exists:
            rep.copy_paths.append(norm)
            rep.actions.append(f"'{norm}' exists here and the code opens it: it will be copied into the image.")
        else:
            rep.notes.append(f"line {line_of(n)}: the code mentions '{norm}' but it does not exist in this folder. "
                             f"If the code really opens it, the container will fail the same way your terminal run would.")
    rep.volume_paths = sorted(set(rep.volume_paths))
    rep.copy_paths = sorted(set(rep.copy_paths))

    # 4) models that download on first start ------------------------------------------------------
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = _call_name(n)
            cand = None
            if name in MODEL_CALLS or name in FROM_PRETRAINED:
                kw = {k.arg: k.value for k in n.keywords if k.arg}
                cand = kw.get("model_name") or kw.get("model_name_or_path") or (n.args[0] if n.args else None)
            if isinstance(cand, ast.Constant) and isinstance(cand.value, str):
                rep.models.append(cand.value)
    rep.models = sorted(set(rep.models))
    if rep.models:
        rep.notes.append("Models downloaded on the first start (kept in the 'models' volume afterwards): " + ", ".join(rep.models)
                         + ". The first start can take several minutes and a few GB.")

    # 5) environment variables --------------------------------------------------------------------
    names, issues = read_env_names(os.path.join(project_dir, env_file))
    rep.env_file_issues = [i for i in issues if i != f"{env_file} does not exist"]
    if f"{env_file} does not exist" in issues:
        rep.blockers.append(f"{env_file} does not exist in {os.path.abspath(project_dir)}")
    for i in rep.env_file_issues:
        rep.blockers.append(i) if "byte-order" in i or "export" in i or "spaces" in i else rep.notes.append(i)

    for n in ast.walk(tree):
        var, has_default = None, False
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            f = n.func
            is_getenv = isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr == "getenv"
            is_environ_get = (f.attr == "get" and isinstance(f.value, ast.Attribute) and f.value.attr == "environ")
            if (is_getenv or is_environ_get) and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                var = n.args[0].value
                has_default = len(n.args) > 1 or any(k.arg == "default" for k in n.keywords)
        elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "environ"
              and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
            var = n.slice.value
        if var:
            rep.env_read[var] = rep.env_read.get(var, False) or has_default
    infra = {"REDIS_HOST", "REDIS_PORT", "REDIS_URL"} if rep.redis_mode == "compose" else set()
    for var, has_default in sorted(rep.env_read.items()):
        if var in names or var in infra:
            continue
        if has_default:
            continue
        if var in os.environ:
            rep.notes.append(f"{var} is read by the code and is set in your Windows environment but NOT in .env: "
                             f"the container will not see it. Add it to .env.")
        else:
            rep.env_missing.append(var)
            rep.notes.append(f"{var} is read by the code but is not in .env (no default): the container will get nothing for it.")
    return rep


def line_of(n: ast.AST) -> int:
    return getattr(n, "lineno", 0)
