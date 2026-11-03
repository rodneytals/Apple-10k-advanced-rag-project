"""
prepare_monitoring.py -- Day 20: get the project folder ready, BEFORE you rebuild the image.

    from prepare_monitoring import ensure_metrics_key, check_monitoring_ready
    print(ensure_metrics_key(".env"))                       # adds RAG_METRICS_KEY to .env if it is missing (never prints it)
    rep = check_monitoring_ready(".", "rag_pipeline", ".env")
    print(rep.summary())

check_monitoring_ready only READS. ensure_metrics_key changes .env, and only by ADDING one line (a copy is kept first).
"""
import ast
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MIN_KEY = 16
SENSITIVE_NAMES = re.compile(r"\b(question|answer|query|prompt|messages|context|response|user_input|history|text)\b", re.I)
TOOL_FILES = ("monitoring.py", "serve.py", "api_layer.py", "alert_rules.py", "log_report.py", "check_monitoring.py")


@dataclass
class ReadyReport:
    items: List[Tuple[Optional[bool], str]] = field(default_factory=list)

    def add(self, ok: Optional[bool], text: str) -> None:
        self.items.append((ok, text))

    @property
    def ok(self) -> bool:
        return all(ok is not False for ok, _ in self.items)

    def summary(self) -> str:
        out = [("✅ " if ok else "❌ " if ok is False else "⚠️  ") + t for ok, t in self.items]
        out.append("")
        out.append("✅ ready to rebuild" if self.ok else "❌ fix the ❌ lines first")
        return "\n".join(out)


def _read_env(env_file: str) -> dict:
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(env_file).items() if v is not None}


def ensure_metrics_key(env_file: str = ".env") -> str:
    """Adds  RAG_METRICS_KEY=<random>  to .env when it is missing. The value is never printed."""
    if not os.path.exists(env_file):
        raise FileNotFoundError(f"{env_file} not found (run this in the project folder)")
    raw = open(env_file, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError(".env starts with a hidden byte-order mark: run the Day 19 preflight cell once (it fixes that), then try again")
    env = _read_env(env_file)
    existing = (env.get("RAG_METRICS_KEY") or "").strip()
    api_keys = [p.split(":", 1)[0].strip() for p in (env.get("RAG_API_KEYS") or "").split(",") if ":" in p]
    if existing:
        if len(existing) < MIN_KEY:
            raise RuntimeError(f"RAG_METRICS_KEY in {env_file} is shorter than {MIN_KEY} characters; delete that line and run this again")
        if existing in api_keys:
            raise RuntimeError("RAG_METRICS_KEY is the same as one of your API keys; delete that line and run this again")
        return "RAG_METRICS_KEY is already in .env: left unchanged"
    backup = env_file + ".before_metrics_key"
    if not os.path.exists(backup):
        shutil.copyfile(env_file, backup)
    text = raw.decode("utf-8")
    prefix = "" if (not text or text.endswith("\n")) else "\n"
    with open(env_file, "a", encoding="utf-8", newline="") as f:
        f.write(f"{prefix}RAG_METRICS_KEY={secrets.token_urlsafe(32)}\n")
    return "added RAG_METRICS_KEY to .env (a copy of the old file: .env.before_metrics_key). The value was not printed."


def risky_log_lines(pipeline_path: str) -> List[Tuple[int, str, str]]:
    """print() / logging calls in the pipeline that mention words like question or answer: (line, code, 'print' or 'log').
    A static guess, not proof: it only looks at variable names, not at what they hold."""
    src = open(pipeline_path, encoding="utf-8").read()
    lines = src.splitlines()
    out = set()
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        is_print = isinstance(f, ast.Name) and f.id == "print"
        is_log = isinstance(f, ast.Attribute) and f.attr in ("debug", "info", "warning", "error", "exception", "critical") \
            and isinstance(f.value, ast.Name) and f.value.id in ("logging", "log", "logger", "_log")
        if not (is_print or is_log):
            continue
        names = {x.id for a in n.args + [k.value for k in n.keywords] for x in ast.walk(a) if isinstance(x, ast.Name)}
        if any(SENSITIVE_NAMES.fullmatch(x) for x in names):
            out.add((n.lineno, lines[n.lineno - 1].strip()[:90], "print" if is_print else "log"))
    return sorted(out)


def check_monitoring_ready(project_dir: str = ".", package: str = "rag_pipeline", env_file: str = ".env") -> ReadyReport:
    rep = ReadyReport()
    missing = [f for f in TOOL_FILES if not os.path.exists(os.path.join(project_dir, f))]
    rep.add(not missing, "all Day 20 files are in the project folder" if not missing else
            "missing from the project folder: " + ", ".join(missing) + " (unzip day20_tools.zip into it; overwrite when asked)")
    serve_path = os.path.join(project_dir, "serve.py")
    if os.path.exists(serve_path):
        has = "_add_monitoring" in open(serve_path, encoding="utf-8").read()
        rep.add(has, "serve.py is the Day 20 version" if has else "serve.py is still the Day 18 version: copy the one from day20_tools.zip over it")
    env_path = os.path.join(project_dir, env_file)
    if not os.path.exists(env_path):
        rep.add(False, f"{env_file} not found")
    else:
        try:
            env = _read_env(env_path)
            keys = [p.split(":", 1)[0].strip() for p in (env.get("RAG_API_KEYS") or "").split(",") if ":" in p]
            mk = (env.get("RAG_METRICS_KEY") or "").strip()
            if not mk:
                rep.add(False, "RAG_METRICS_KEY is not in .env: run ensure_metrics_key() (without it /metrics stays OFF)")
            elif len(mk) < MIN_KEY or mk in keys:
                rep.add(False, f"RAG_METRICS_KEY must be at least {MIN_KEY} characters and different from every API key")
            else:
                rep.add(True, "RAG_METRICS_KEY is in .env and differs from your API keys")
        except Exception as e:
            rep.add(False, f"cannot read {env_file}: {type(e).__name__}")
    pipeline_path = os.path.join(project_dir, package, "pipeline.py")
    if not os.path.exists(pipeline_path):
        rep.add(False, f"{package}/pipeline.py not found")
        return rep
    try:
        from make_bounded import remaining_growers
        grow = remaining_growers(pipeline_path)
        rep.add(True if not grow else None, "nothing in the pipeline grows forever" if not grow else
                "still grows forever: " + ", ".join(grow) + "  (run make_bounded; names it skips need a hand fix)")
    except Exception as e:
        rep.add(None, f"could not audit growth: {type(e).__name__}")
    risky = risky_log_lines(pipeline_path)
    logs_ = [r for r in risky if r[2] == "log"]
    prints_ = [r for r in risky if r[2] == "print"]
    if logs_:
        rep.add(None, f"{len(logs_)} logging call(s) in the pipeline mention question/answer-like names (they WILL reach the log): "
                      + "; ".join(f"line {n}" for n, _, _ in logs_[:8]))
    else:
        rep.add(True, "no logging call in the pipeline mentions question/answer-like names")
    if prints_:
        rep.add(True, f"{len(prints_)} print() call(s) mention question/answer-like names: print() is silenced inside the container "
                      f"(RAG_PIPELINE_PRINTS=1 would show them)")
    dockerfile = os.path.join(project_dir, "Dockerfile")
    if os.path.exists(dockerfile):
        text = open(dockerfile, encoding="utf-8").read()
        rep.add("monitoring.py" in text, "the Dockerfile copies monitoring.py" if "monitoring.py" in text else
                "the Dockerfile does not copy monitoring.py yet: run the Day 19 generator cell again (it now includes it)")
    compose = os.path.join(project_dir, "docker-compose.yml")
    if os.path.exists(compose):
        text = open(compose, encoding="utf-8").read()
        rep.add(True if '"127.0.0.1:' in text else None, "the port is published to this computer only (127.0.0.1), so /metrics is not open to the network"
                if '"127.0.0.1:' in text else "the port is published on all network interfaces: anyone on your network can reach /metrics (it needs the key, but still)")
    return rep


if __name__ == "__main__":
    print(check_monitoring_ready(*(sys.argv[1:4] or [])).summary())
