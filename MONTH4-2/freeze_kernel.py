"""
freeze_kernel.py -- Day 18

Turns the pipeline that is ALIVE in a notebook kernel into an importable package,
using the code you actually executed (the kernel's cell history) instead of anyone's memory.

How it works, in plain words:
  1. Read every cell you ran, split it into top-level statements.
  2. Start from the entry points (run_with_entity_memory, multihop_graph, ...) and follow
     every name they use, back to the statement that defined it ("tree shaking").
     Experiments, tests, prints and old versions are left behind automatically.
  3. For every name, only the LATEST definition survives (that is what your kernel uses).
  4. Check each function against the live object in the kernel: same bytecode or it is flagged.
  5. Refuse to write anything that contains a secret.
  6. Write  <out_dir>/pipeline.py  plus a human-readable FREEZE_REPORT.md and env.example.

Run it INSIDE the kernel that holds your working pipeline:
    from freeze_kernel import freeze_from_kernel
    report = freeze_from_kernel(get_ipython(), entry_points=[...], out_dir="rag_pipeline")
"""
from __future__ import annotations

import ast
import builtins
import copy
import datetime as _dt
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

MUTATING_METHODS = {"append", "extend", "update", "add", "insert", "setdefault", "remove",
                    "pop", "clear", "discard", "appendleft", "extendleft"}

# Calls that configure the process rather than compute something. Kept even though nothing "uses" them.
CONFIG_CALLS = {"load_dotenv", "nest_asyncio.apply", "warnings.filterwarnings", "warnings.simplefilter",
                "logging.basicConfig", "os.makedirs", "os.environ.setdefault", "os.environ.update"}

# Attribute/item assignments on these imported names are display-only, never configuration.
DISPLAY_ROOTS = {"pd", "plt", "matplotlib", "np", "sns", "display", "IPython"}

SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|URI|URL|DSN|CREDENTIAL)", re.I)
SECRET_PATTERNS = [
    ("Groq-style key", re.compile(r"gsk_[A-Za-z0-9]{16,}")),
    ("OpenAI-style key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("LangSmith key", re.compile(r"lsv2_[A-Za-z0-9_]{16,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("URL with embedded password", re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:[^@\s'\"]{3,}@")),
    ("Hard-coded credential assignment",
     re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token)\b\s*=\s*['\"][^'\"\s]{8,}['\"]")),
]
BUILTIN_NAMES = set(dir(builtins)) | {"get_ipython", "In", "Out", "_", "__", "___", "display", "__file__", "__name__"}


class SecretFoundError(RuntimeError):
    """Raised instead of writing a package that would contain a secret."""


# ------------------------------------------------------------------ parsing --
@dataclass
class Stmt:
    cell: int
    idx: int
    node: ast.stmt
    src: str
    kind: str                       # import | def | class | assign | augassign | mutation | config | compound | expr
    binds: Set[str] = field(default_factory=set)
    import_loads: Set[str] = field(default_factory=set)
    call_loads: Set[str] = field(default_factory=set)
    root: Optional[str] = None      # for mutation/augassign: the name being mutated

    @property
    def key(self) -> Tuple[int, int]:
        return (self.cell, self.idx)

    def label(self) -> str:
        head = self.src.strip().splitlines()[0] if self.src.strip() else ""
        return head[:90]


class _StoreCollector(ast.NodeVisitor):
    """Names a statement binds in the module scope (does not descend into def/class/lambda/comprehension scopes)."""
    def __init__(self):
        self.names: Set[str] = set()

    def visit_Name(self, n):
        if isinstance(n.ctx, (ast.Store, ast.Del)):
            self.names.add(n.id)

    def visit_FunctionDef(self, n):
        self.names.add(n.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, n):
        return

    def visit_ListComp(self, n):
        return

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_Import(self, n):
        for a in n.names:
            self.names.add(a.asname or a.name.split(".")[0])

    def visit_ImportFrom(self, n):
        for a in n.names:
            if a.name != "*":
                self.names.add(a.asname or a.name)

    def visit_ExceptHandler(self, n):
        if n.name:
            self.names.add(n.name)
        self.generic_visit(n)


def bound_names(node: ast.AST) -> Set[str]:
    c = _StoreCollector()
    c.visit(node)
    return c.names


class _LoadCollector(ast.NodeVisitor):
    """Global names a statement needs. import-time = needed the moment the statement runs;
    call-time = only needed when a function body runs later."""
    def __init__(self):
        self.imp: Set[str] = set()
        self.call: Set[str] = set()
        self._locals: List[Set[str]] = []      # stack of local-name sets for enclosing functions

    def _is_local(self, name: str) -> bool:
        return any(name in s for s in self._locals)

    def visit_Name(self, n):
        if isinstance(n.ctx, ast.Load) and not self._is_local(n.id):
            (self.call if self._locals else self.imp).add(n.id)

    def _args_locals(self, args: ast.arguments) -> Set[str]:
        names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        if args.vararg:
            names.add(args.vararg.arg)
        if args.kwarg:
            names.add(args.kwarg.arg)
        return names

    def _visit_def_header(self, n):
        for d in getattr(n, "decorator_list", []):
            self.visit(d)
        a = n.args
        for d in list(a.defaults) + [k for k in a.kw_defaults if k is not None]:
            self.visit(d)
        for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if getattr(n, "returns", None) is not None:
            self.visit(n.returns)

    def visit_FunctionDef(self, n):
        self._visit_def_header(n)
        declared_global: Set[str] = set()
        local = self._args_locals(n.args)
        for sub in ast.walk(n):
            if isinstance(sub, ast.Global):
                declared_global.update(sub.names)
        c = _StoreCollector()
        for stmt in n.body:
            c.visit(stmt)
        # comprehension targets and walrus targets are locals too (cheap over-approximation)
        for sub in ast.walk(n):
            if isinstance(sub, ast.comprehension):
                local |= bound_names(sub.target)
            if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                local.add(sub.target.id)
        local |= (c.names - declared_global)
        self._locals.append(local)
        for stmt in n.body:
            self.visit(stmt)
        self._locals.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, n):
        for d in list(n.args.defaults) + [k for k in n.args.kw_defaults if k is not None]:
            self.visit(d)
        self._locals.append(self._args_locals(n.args))
        self.visit(n.body)
        self._locals.pop()

    def visit_ClassDef(self, n):
        for d in n.decorator_list:
            self.visit(d)
        for b in n.bases:
            self.visit(b)
        for k in n.keywords:
            self.visit(k.value)
        for stmt in n.body:          # class body runs right now
            self.visit(stmt)


def collect_loads(node: ast.AST) -> Tuple[Set[str], Set[str]]:
    c = _LoadCollector()
    c.visit(node)
    return c.imp - BUILTIN_NAMES, c.call - BUILTIN_NAMES


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _root_name(node: ast.AST) -> Optional[str]:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _contains_config_call(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(sub, ast.Call) and _dotted(sub.func) in CONFIG_CALLS:
            return True
    return False


def classify(node: ast.stmt, imported_roots: Set[str]) -> Tuple[str, Optional[str]]:
    """Return (kind, root)."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "import", None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "def", None
    if isinstance(node, ast.ClassDef):
        return "class", None
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if isinstance(node, ast.AnnAssign) and node.value is None:
            return "expr", None
        if any(bound_names(t) for t in targets):
            return "assign", None
        root = _root_name(targets[0])
        return ("mutation", root) if root else ("expr", None)
    if isinstance(node, ast.AugAssign):
        root = _root_name(node.target)
        return ("augassign", root) if root else ("expr", None)
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        name = _dotted(call.func)
        if name in CONFIG_CALLS:
            return "config", None
        if isinstance(call.func, ast.Attribute) and call.func.attr in MUTATING_METHODS:
            root = _root_name(call.func.value)
            if root:
                return "mutation", root
        return "expr", None
    if isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With, ast.AsyncFor, ast.AsyncWith)):
        if bound_names(node):
            return "compound", None
        if _contains_config_call(node):
            return "config", None
        return "expr", None
    return "expr", None


def split_statements(cells: List[str]) -> Tuple[List[Stmt], List[int]]:
    stmts: List[Stmt] = []
    unparsable: List[int] = []
    imported_roots: Set[str] = set()
    for i, src in enumerate(cells):
        if not src or not src.strip():
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            unparsable.append(i)
            continue
        lines = src.splitlines()
        for j, node in enumerate(tree.body):
            decos = getattr(node, "decorator_list", [])
            if decos:
                start = min([node.lineno] + [d.lineno for d in decos])
                text = "\n".join(lines[start - 1: node.end_lineno])
            else:
                text = ast.get_source_segment(src, node) or ast.unparse(node)
            kind, root = classify(node, imported_roots)
            if kind == "import":
                imported_roots |= bound_names(node)
            st = Stmt(cell=i, idx=j, node=node, src=text, kind=kind, root=root)
            st.binds = bound_names(node) if kind in ("import", "def", "class", "assign", "compound", "augassign") else set()
            if kind == "augassign" and root:
                st.binds = {root}
            st.import_loads, st.call_loads = collect_loads(node)
            if kind == "augassign" and root:
                st.import_loads.add(root)
            stmts.append(st)
    return stmts, unparsable


# ------------------------------------------------------------- verification --
# Why not compare bytecode? CPython compiles `re.search(...)` differently depending on whether `import re`
# sits in the SAME file as the call (cells vs. a module), so identical code gives different bytecode.
# We compare the code's syntax tree instead, and fall back to a weaker bytecode summary only when a
# function has no source (e.g. created with exec).
def _weak_fp(code: types.CodeType):
    consts = tuple(_weak_fp(c) if isinstance(c, types.CodeType) else c for c in code.co_consts)
    return (code.co_names, code.co_varnames, consts)


def _ast_hash(fn_node: ast.AST) -> str:
    n2 = copy.deepcopy(fn_node)
    n2.decorator_list = []
    return hashlib.sha1(ast.dump(n2).encode()).hexdigest()[:12]


def _undecorated_code(node: ast.AST, name: str) -> Optional[types.CodeType]:
    n2 = copy.deepcopy(node)
    n2.decorator_list = []
    mod = ast.Module(body=[n2], type_ignores=[])
    ast.fix_missing_locations(mod)
    try:
        top = compile(mod, "<freeze-verify>", "exec", dont_inherit=True)
    except Exception:
        return None
    for c in top.co_consts:
        if isinstance(c, types.CodeType) and c.co_name == name:
            return c
    return None


def fingerprint_node(node: ast.AST, name: str) -> Dict[str, Optional[str]]:
    code = _undecorated_code(node, name)
    return {"ast": _ast_hash(node),
            "weak": hashlib.sha1(repr(_weak_fp(code)).encode()).hexdigest()[:12] if code else None}


def fingerprint_function(f: Any) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"ast": None, "weak": None}
    try:
        src = textwrap_dedent(inspect.getsource(f))
        tree = ast.parse(src)
        node = tree.body[0]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == f.__name__:
            out["ast"] = _ast_hash(node)
    except (OSError, TypeError, SyntaxError, IndentationError, IndexError):
        pass
    out["weak"] = hashlib.sha1(repr(_weak_fp(f.__code__)).encode()).hexdigest()[:12]
    return out


def textwrap_dedent(s: str) -> str:
    import textwrap
    return textwrap.dedent(s)


def fingerprints_equal(a: Dict[str, Optional[str]], b: Dict[str, Optional[str]]) -> bool:
    if a.get("ast") and b.get("ast"):
        return a["ast"] == b["ast"]
    return a.get("weak") == b.get("weak")


def _live_function(obj: Any):
    seen = 0
    while seen < 20:
        seen += 1
        if hasattr(obj, "__wrapped__"):
            obj = obj.__wrapped__
            continue
        if hasattr(obj, "func") and not hasattr(obj, "__code__"):      # LangChain @tool objects etc.
            obj = obj.func
            continue
        break
    obj = getattr(obj, "__func__", obj)
    return obj if hasattr(obj, "__code__") else None


def verify_statement(st: Stmt, live: Dict[str, Any]) -> Optional[str]:
    """Return None if faithful or not checkable; otherwise a short problem description."""
    if st.kind == "def":
        name = st.node.name
        if name not in live:
            return "not present in the live kernel (was it deleted?)"
        f = _live_function(live[name])
        if f is None:
            return None
        if not fingerprints_equal(fingerprint_function(f), fingerprint_node(st.node, name)):
            return "history source differs from the live function"
    elif st.kind == "class":
        name = st.node.name
        cls = live.get(name)
        if not isinstance(cls, type):
            return None if cls is None else "live object is not a class any more"
        for item in st.node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                member = cls.__dict__.get(item.name)
                if member is None:
                    return f"method {item.name} missing on the live class"
                f = _live_function(member)
                if f is not None and not fingerprints_equal(fingerprint_function(f), fingerprint_node(item, item.name)):
                    return f"method {item.name} differs from the live class"
    elif st.kind == "assign" and isinstance(st.node, ast.Assign) and len(st.node.targets) == 1 \
            and isinstance(st.node.targets[0], ast.Name):
        name = st.node.targets[0].id
        try:
            lit = ast.literal_eval(st.node.value)
        except Exception:
            return None
        if isinstance(lit, (list, dict, set)):
            return None                       # containers are SUPPOSED to change while the pipeline runs
        if name in live and type(live[name]) is type(lit) and live[name] != lit:
            return "literal in history differs from live value (changed at runtime?)"
    return None


# ----------------------------------------------------------------- secrets --
def scan_for_secrets(source: str, env: Optional[Dict[str, str]] = None) -> List[Tuple[int, str]]:
    """Return [(line_number, what)] -- never the secret itself."""
    findings: List[Tuple[int, str]] = []
    lines = source.splitlines()
    env = os.environ if env is None else env
    secret_values = []
    for k, v in env.items():
        if len(v) < 8 or v.isdigit() or v.lower() in ("true", "false", "localhost"):
            continue
        if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIAL)", k, re.I):
            secret_values.append(v)
        elif re.search(r"(URI|URL|DSN)", k, re.I) and "@" in v:      # only URLs that carry credentials
            secret_values.append(v)
    for n, line in enumerate(lines, 1):
        for label, rx in SECRET_PATTERNS:
            if rx.search(line):
                findings.append((n, label))
        for v in secret_values:
            if v in line:
                findings.append((n, "value of one of your environment variables appears literally"))
                break
    return sorted(set(findings))


# ------------------------------------------------------------ report pieces --
ENV_RX = [re.compile(r"""os\.getenv\(\s*['"]([A-Za-z0-9_]+)['"]"""),
          re.compile(r"""os\.environ\.get\(\s*['"]([A-Za-z0-9_]+)['"]"""),
          re.compile(r"""os\.environ\[\s*['"]([A-Za-z0-9_]+)['"]\s*\]""")]
PATH_HINT = re.compile(r"""(?i)(\.(db|sqlite|json|csv|pdf|png|jpe?g|txt|index|pkl|parquet)$|^[A-Za-z]:\\|\\\\|^\.{0,2}/|/)""")


def find_env_vars(source: str) -> List[str]:
    found: Set[str] = set()
    for rx in ENV_RX:
        found.update(rx.findall(source))
    return sorted(found)


def find_paths(source: str) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            s = n.value
            if 3 <= len(s) <= 160 and "\n" not in s and " " not in s.strip() and PATH_HINT.search(s) \
                    and not s.startswith(("http://", "https://", "mongodb", "neo4j", "redis://", "bolt")):
                if "/" in s or "\\" in s or re.search(r"\.[A-Za-z0-9]{2,7}$", s):
                    out.append((n.lineno, s))
    return sorted(set(out))


# ------------------------------------------------------------------ probes --
def _jsonable(x: Any, depth: int = 0):
    if depth > 4:
        return repr(x)[:80]
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, (int, str, bool)) or x is None:
        return x if not isinstance(x, str) else x[:200]
    if isinstance(x, (list, tuple)):
        return [_jsonable(i, depth + 1) for i in x]
    if isinstance(x, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    for attr in ("page_content", "text"):
        if hasattr(x, attr):
            return str(getattr(x, attr))[:80]
    if hasattr(x, "get_content"):
        try:
            return str(x.get_content())[:80]
        except Exception:
            pass
    return repr(x)[:80]


def compute_probe(ns: Dict[str, Any], graph_names: Iterable[str] = ("multihop_graph",),
                  probe_calls: Iterable[Tuple[str, tuple]] = ()) -> Dict[str, Any]:
    """Deterministic facts about a loaded pipeline. Computed in the kernel and again in a fresh
    process; any difference means the frozen package is not the same pipeline."""
    probe: Dict[str, Any] = {"graphs": {}, "calls": {}}
    for gname in graph_names:
        g = ns.get(gname)
        if g is None or not hasattr(g, "get_graph"):
            continue
        dg = g.get_graph()
        info: Dict[str, Any] = {
            "nodes": sorted(dg.nodes),
            "edges": sorted([e.source, e.target, bool(e.conditional)] for e in dg.edges),
            "node_code": {},
        }
        for name, node in getattr(g, "nodes", {}).items():
            fn = _live_function(getattr(getattr(node, "bound", node), "func", None))
            if fn is not None:
                info["node_code"][name] = fingerprint_function(fn)
        probe["graphs"][gname] = info
    for fname, args in probe_calls:
        fn = ns.get(fname)
        if fn is None:
            probe["calls"][fname] = {"error": "not defined"}
            continue
        try:
            probe["calls"][fname] = {"result": _jsonable(fn(*args))}
        except Exception as e:  # a failing probe is itself a fact worth comparing
            probe["calls"][fname] = {"error": type(e).__name__}
    return probe


def diff_probes(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    for gname, e in expected.get("graphs", {}).items():
        a = actual.get("graphs", {}).get(gname)
        if a is None:
            problems.append(f"graph {gname}: missing in the fresh process")
            continue
        if e["nodes"] != a["nodes"]:
            problems.append(f"graph {gname}: node sets differ: only-live={sorted(set(e['nodes']) - set(a['nodes']))} "
                            f"only-frozen={sorted(set(a['nodes']) - set(e['nodes']))}")
        if e["edges"] != a["edges"]:
            se, sa = {tuple(x) for x in e["edges"]}, {tuple(x) for x in a["edges"]}
            problems.append(f"graph {gname}: edges differ: only-live={sorted(se - sa)} only-frozen={sorted(sa - se)}")
        for node, h in e["node_code"].items():
            if node in a["node_code"] and not fingerprints_equal(a["node_code"][node], h):
                problems.append(f"graph {gname}: node '{node}' runs DIFFERENT CODE than the live graph")
    for fname, e in expected.get("calls", {}).items():
        a = actual.get("calls", {}).get(fname)
        if a != e:
            problems.append(f"probe call {fname}: live={json.dumps(e)[:160]} frozen={json.dumps(a)[:160]}")
    return problems


# ----------------------------------------------------------------- freezing --
@dataclass
class FreezeReport:
    out_dir: str
    kept: List[Stmt] = field(default_factory=list)
    unresolved: Dict[str, List[str]] = field(default_factory=dict)
    mismatches: List[str] = field(default_factory=list)
    unparsable_cells: List[int] = field(default_factory=list)
    dropped_calls: List[str] = field(default_factory=list)
    side_effects_kept: List[str] = field(default_factory=list)
    env_vars: List[str] = field(default_factory=list)
    paths: List[Tuple[int, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    cells_total: int = 0
    probe_file: Optional[str] = None
    ok: bool = True

    def summary(self) -> str:
        lines = [f"Frozen {len(self.kept)} statements from {self.cells_total} executed cells -> {self.out_dir}/pipeline.py"]
        n_def = sum(1 for s in self.kept if s.kind == "def")
        n_cls = sum(1 for s in self.kept if s.kind == "class")
        n_asg = sum(1 for s in self.kept if s.kind in ("assign", "compound"))
        n_imp = sum(1 for s in self.kept if s.kind == "import")
        lines.append(f"   {n_def} functions, {n_cls} classes, {n_asg} objects/constants, {n_imp} imports")
        if self.unresolved:
            lines.append("UNRESOLVED names (used by the pipeline, defined by no cell you ran):")
            for n, users in sorted(self.unresolved.items()):
                lines.append(f"   - {n}   (used by: {', '.join(sorted(users)[:4])})")
        if self.mismatches:
            lines.append("DIFFERS FROM LIVE KERNEL:")
            lines += [f"   - {m}" for m in self.mismatches]
        if self.side_effects_kept:
            lines.append("Configuration side effects kept (check they are wanted):")
            lines += [f"   - {s}" for s in self.side_effects_kept[:15]]
        if self.dropped_calls:
            lines.append(f"Top-level calls left behind ({len(self.dropped_calls)}); review the ones that set something up:")
            lines += [f"   - {s}" for s in self.dropped_calls[:25]]
        if self.env_vars:
            lines.append("Environment variables the package reads: " + ", ".join(self.env_vars))
        if self.paths:
            lines.append("File paths inside the code (these must exist wherever the package runs):")
            lines += [f"   - line {ln}: {p}" for ln, p in self.paths[:20]]
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        if self.unparsable_cells:
            lines.append(f"({len(self.unparsable_cells)} history cell(s) had syntax errors and were skipped)")
        return "\n".join(lines)


def _toposort(kept: Dict[Tuple[int, int], Stmt], deps: Dict[Tuple[int, int], Set[Tuple[int, int]]],
              warnings: List[str]) -> List[Stmt]:
    remaining = set(kept)
    done: Set[Tuple[int, int]] = set()
    order: List[Stmt] = []
    while remaining:
        ready = sorted(k for k in remaining if all((d in done or d not in kept or d == k) for d in deps.get(k, ())))
        if not ready:
            k = min(remaining)
            warnings.append(f"circular import-time dependency near: {kept[k].label()} (kept in original order)")
            ready = [k]
        k = ready[0]
        order.append(kept[k])
        done.add(k)
        remaining.discard(k)
    return order


def freeze(cells: List[str], live_ns: Dict[str, Any], entry_points: List[str], out_dir: str = "rag_pipeline",
           graph_names: Iterable[str] = ("multihop_graph",), probe_calls: Iterable[Tuple[str, tuple]] = (),
           exclude_roots: Iterable[str] = (), allow_unresolved: bool = False,
           env: Optional[Dict[str, str]] = None, write: bool = True) -> FreezeReport:
    report = FreezeReport(out_dir=out_dir, entry_points=list(entry_points), cells_total=len([c for c in cells if c and c.strip()]))
    stmts, report.unparsable_cells = split_statements(cells)
    exclude_roots = set(exclude_roots) | DISPLAY_ROOTS

    providers: Dict[str, List[Stmt]] = {}
    for s in stmts:
        if s.kind in ("import", "def", "class", "assign", "compound"):
            for n in s.binds:
                providers.setdefault(n, []).append(s)
    for lst in providers.values():
        lst.sort(key=lambda s: s.key)

    kept: Dict[Tuple[int, int], Stmt] = {}
    deps: Dict[Tuple[int, int], Set[Tuple[int, int]]] = {}
    unresolved: Dict[str, Set[str]] = {}
    needed_imports: Dict[Tuple[int, int], Set[str]] = {}
    imps_resolved: Dict[Tuple[int, int], Set[Tuple[int, int]]] = {}
    calls_resolved: Dict[Tuple[int, int], Set[Tuple[int, int]]] = {}
    stale: List[str] = []

    def provider_for(name: str, before: Optional[Stmt]) -> Optional[Stmt]:
        cands = providers.get(name, [])
        if before is not None:
            cands = [c for c in cands if c.key < before.key]
        return cands[-1] if cands else None

    def add(s: Stmt):
        if s.key in kept:
            return
        kept[s.key] = s
        deps.setdefault(s.key, set())
        imps_resolved.setdefault(s.key, set())
        calls_resolved.setdefault(s.key, set())
        for n in sorted(s.import_loads | s.call_loads):
            if n in s.binds and n not in s.import_loads:
                continue                                    # recursion / call-time self reference
            self_ref = n in s.binds and n in s.import_loads  # e.g.  f = decorate(f)  -> needs the PREVIOUS f
            p = provider_for(n, s if self_ref else None)
            if p is None:
                unresolved.setdefault(n, set()).add(s.label()[:40])
                continue
            (imps_resolved if n in s.import_loads else calls_resolved)[s.key].add(p.key)
            if n in s.import_loads:
                deps[s.key].add(p.key)
                if not self_ref and p.key > s.key and p.kind != "import":
                    stale.append(f"`{s.label()[:60]}` (cell {s.cell}) ran BEFORE `{n}` was last redefined "
                                 f"(cell {p.cell}). Your kernel still holds the old result; "
                                 f"the package rebuilds it from the newest `{n}`.")
            if p.kind == "import":
                needed_imports.setdefault(p.key, set()).add(n)
            add(p)

    for ep in entry_points:
        p = provider_for(ep, None)
        if p is None:
            unresolved.setdefault(ep, set()).add("(entry point)")
        else:
            if p.kind == "import":
                needed_imports.setdefault(p.key, set()).add(ep)
            add(p)

    imported_names = {n for s in stmts if s.kind == "import" for n in s.binds}

    # Configuration side effects: kept even though nothing "uses" them.
    for s in stmts:
        if s.kind == "config" and not (_dotted(getattr(getattr(s.node, "value", None), "func", None)) .split(".")[0] in exclude_roots):
            add(s)
            report.side_effects_kept.append(f"cell {s.cell}: {s.label()}")
        elif s.kind == "mutation" and s.root in imported_names and s.root not in exclude_roots \
                and not isinstance(s.node, ast.Expr):
            add(s)
            report.side_effects_kept.append(f"cell {s.cell}: {s.label()}")

    # Mutations / augmented assignments of kept objects, applied after the object's final binding.
    changed = True
    while changed:
        changed = False
        for s in stmts:
            if s.kind in ("mutation", "augassign") and s.key not in kept and s.root:
                fin = provider_for(s.root, None)
                if fin is not None and fin.key in kept and s.key > fin.key and s.root not in exclude_roots:
                    add(s)
                    deps[s.key].add(fin.key)
                    changed = True
    # keep the relative order of mutations that touch the same object
    by_root: Dict[str, List[Stmt]] = {}
    for s in kept.values():
        if s.kind in ("mutation", "augassign") and s.root:
            by_root.setdefault(s.root, []).append(s)
    for lst in by_root.values():
        lst.sort(key=lambda s: s.key)
        for a, b in zip(lst, lst[1:]):
            deps[b.key].add(a.key)

    # What was deliberately left behind but might matter.
    kept_name_set = kept_names(kept)
    for s in stmts:
        if s.key in kept or s.kind != "expr":
            continue
        node = s.node
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            callee = _dotted(node.value.func)
            if callee in ("print", "display", "assert", "get_ipython", "plt.show") or callee.startswith("get_ipython"):
                continue
            root = callee.split(".")[0] if callee else ""
            if root in kept_name_set or root in imported_names:
                line = f"cell {s.cell}: {s.label()}"
                if line not in report.dropped_calls:
                    report.dropped_calls.append(line)

    # A statement that runs code at import time (graph = build_graph()) needs everything that code
    # touches later (the node functions build_graph() reads) to be defined first.
    def closure_deps(keys: Iterable[Tuple[int, int]]) -> Set[Tuple[int, int]]:
        out: Set[Tuple[int, int]] = set()
        stack, seen = list(keys), set()
        while stack:
            k = stack.pop()
            if k in seen:
                continue
            seen.add(k)
            out.add(k)
            p = kept.get(k)
            if p is not None and p.kind in ("def", "class"):
                stack.extend(calls_resolved.get(k, ()))
                stack.extend(imps_resolved.get(k, ()))
        return out

    for s in list(kept.values()):
        if s.kind in ("def", "class"):
            dec_names: Set[str] = set()
            for d in getattr(s.node, "decorator_list", []):
                dec_names |= collect_loads(d)[0]
            starts = {provider_for(n, None).key for n in dec_names if provider_for(n, None) is not None
                      and provider_for(n, None).key in kept}
        else:
            starts = set(imps_resolved.get(s.key, ()))
        deps[s.key] |= closure_deps(starts) - {s.key}

    order = _toposort(kept, deps, report.warnings)

    report.warnings.extend(sorted(set(stale)))

    # Verification against the live kernel.
    mutated_roots = {s.root for s in order if s.kind in ("mutation", "augassign") and s.root}
    for s in order:
        if s.kind == "assign" and (s.binds & mutated_roots):
            continue                       # a literal that later cells deliberately changed
        problem = verify_statement(s, live_ns)
        if problem:
            name = s.node.name if hasattr(s.node, "name") else ",".join(sorted(s.binds))
            report.mismatches.append(f"{name} (cell {s.cell}): {problem}")

    report.kept = order
    report.unresolved = {k: sorted(v) for k, v in unresolved.items()}

    header = (f"# AUTO-GENERATED by freeze_kernel.py on {_dt.datetime.now():%Y-%m-%d %H:%M} "
              f"from {report.cells_total} executed notebook cells.\n"
              f"# Each block says which notebook cell it came from.\n"
              f"# Freezing again OVERWRITES this file (the previous one is kept as pipeline.py.previous).\n"
              f"# Once you start editing it by hand, stop freezing: this file becomes the source of truth.\n\n")
    body_parts = []
    for s in order:
        text = s.src
        if s.kind == "import":
            text = _pruned_import(s, needed_imports.get(s.key, set()))
        body_parts.append(f"# --- from notebook cell {s.cell} ---\n{text}\n")
    source = header + "\n".join(body_parts)

    report.env_vars = find_env_vars(source)
    report.paths = find_paths(source)
    findings = scan_for_secrets(source, env=env)
    if findings:
        report.ok = False
        msg = "\n".join(f"   line {ln}: {what}" for ln, what in findings)
        raise SecretFoundError(
            "REFUSING to write the package: it would contain secrets.\n" + msg +
            "\nMove each of these into your .env file and read it with os.getenv(...) in the notebook, "
            "then run the freeze again. (The values are deliberately not printed.)")

    if report.unresolved and not allow_unresolved:
        report.ok = False
    if report.mismatches:
        report.ok = False

    if write:
        os.makedirs(out_dir, exist_ok=True)
        target = os.path.join(out_dir, "pipeline.py")
        if os.path.exists(target):
            import shutil
            shutil.copyfile(target, target + ".previous")
        with open(target, "w", encoding="utf-8") as f:
            f.write(source)
        init = os.path.join(out_dir, "__init__.py")
        if not os.path.exists(init):
            with open(init, "w", encoding="utf-8") as f:
                f.write('"""Frozen RAG pipeline. Import  <package>.pipeline  to load it (this takes a while: it connects to '
                        'your databases and loads the embedding model)."""\n')
        with open(os.path.join(out_dir, "env.example"), "w", encoding="utf-8") as f:
            f.write("# Names only. Copy to .env and fill in the values. Never commit .env.\n")
            for v in report.env_vars:
                f.write(f"{v}=\n")
        _write_report_md(report, out_dir)
        probe = compute_probe(live_ns, graph_names=graph_names, probe_calls=probe_calls)
        report.probe_file = os.path.join(out_dir, "_parity_expected.json")
        with open(report.probe_file, "w", encoding="utf-8") as f:
            json.dump({"probe": probe, "graph_names": list(graph_names),
                       "probe_calls": [[n, list(a)] for n, a in probe_calls]}, f, indent=1)
        try:
            compile(source, os.path.join(out_dir, "pipeline.py"), "exec")
        except SyntaxError as e:
            report.ok = False
            report.warnings.append(f"generated file does not compile: {e}")
    return report


def _pruned_import(s: Stmt, needed: Set[str]) -> str:
    """Re-emit an import statement with only the names something actually uses."""
    node = s.node
    if not needed or any(a.name == "*" for a in node.names):
        return s.src
    keep = [a for a in node.names if (a.asname or a.name.split(".")[0] if isinstance(node, ast.Import)
                                      else (a.asname or a.name)) in needed]
    if not keep or len(keep) == len(node.names):
        return s.src
    n2 = copy.deepcopy(node)
    n2.names = keep
    return ast.unparse(n2)


def kept_names(kept: Dict[Tuple[int, int], Stmt]) -> Set[str]:
    out: Set[str] = set()
    for s in kept.values():
        out |= s.binds
    return out


def _write_report_md(r: FreezeReport, out_dir: str):
    with open(os.path.join(out_dir, "FREEZE_REPORT.md"), "w", encoding="utf-8") as f:
        f.write("# Freeze report\n\n")
        f.write(r.summary() + "\n\n## Everything that was kept (in file order)\n\n")
        for s in r.kept:
            f.write(f"- cell {s.cell}: `{s.label()}`\n")


# ------------------------------------------------------- kernel convenience --
def history_cells(ip) -> List[str]:
    """Executed cell sources of the running IPython kernel, magics already translated to Python."""
    hm = ip.history_manager
    parsed = list(getattr(hm, "input_hist_parsed", []))
    return parsed


def freeze_from_kernel(ip, entry_points: List[str], out_dir: str = "rag_pipeline", **kw) -> FreezeReport:
    cells = history_cells(ip)
    report = freeze(cells, ip.user_ns, entry_points, out_dir=out_dir, **kw)
    print(report.summary())
    print("\n" + ("✅ freeze complete" if report.ok else "⚠️  freeze wrote files but needs your attention (see above)"))
    return report


def verify_in_fresh_process(out_dir: str = "rag_pipeline", timeout: int = 900) -> List[str]:
    """Import the frozen package in a brand-new Python process and compare it with what the kernel recorded."""
    pkg = os.path.basename(os.path.abspath(out_dir))
    parent = os.path.dirname(os.path.abspath(out_dir))
    with open(os.path.join(out_dir, "_parity_expected.json"), encoding="utf-8") as f:
        spec = json.load(f)
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "freeze_kernel.py")
    code = (
        "import importlib, json, sys\n"
        f"sys.path.insert(0, {parent!r}); sys.path.insert(0, {os.path.dirname(runner)!r})\n"
        "from freeze_kernel import compute_probe\n"
        f"m = importlib.import_module({pkg + '.pipeline'!r})\n"
        f"spec = json.load(open({os.path.join(out_dir, '_parity_expected.json')!r}))\n"
        "probe = compute_probe(vars(m), graph_names=spec['graph_names'], probe_calls=[(n, tuple(a)) for n, a in spec['probe_calls']])\n"
        "print('@@PROBE@@' + json.dumps(probe))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout, cwd=parent)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout)[-1500:]
        return [f"the frozen package FAILED TO IMPORT in a fresh process:\n{tail}"]
    line = [l for l in r.stdout.splitlines() if l.startswith("@@PROBE@@")]
    if not line:
        return ["fresh process printed no probe result"]
    actual = json.loads(line[-1][len("@@PROBE@@"):])
    return diff_probes(spec["probe"], actual)
