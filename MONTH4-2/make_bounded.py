"""
make_bounded.py -- Day 20

Fixes the GROWS_FOREVER findings of audit_globals.py: module-level lists / dicts / sets that requests only ever add to.

    python make_bounded.py rag_pipeline/pipeline.py

    EVENT_LOG = []                      ->   EVENT_LOG = BoundedList([], maxlen=1000, name='EVENT_LOG')
    SEEN = {}                           ->   SEEN = BoundedDict({}, maxsize=5000, name='SEEN')
    HISTORY = collections.deque()       ->   HISTORY = collections.deque(maxlen=1000)

Only the one line that creates the container is changed. A copy of the original is kept as pipeline.py.before_bounded_patch.
It refuses (changes nothing) when a container is built in a way it does not recognise, and tells you which.
"""
import ast
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_globals import audit_source  # noqa: E402


class BoundedPatchError(RuntimeError):
    pass


@dataclass
class BoundedReport:
    bounded: List[Tuple[str, str, int]] = field(default_factory=list)       # (name, what it became, limit)
    skipped: List[Tuple[str, str]] = field(default_factory=list)            # (name, why)
    already: List[str] = field(default_factory=list)
    wrote: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.bounded)

    def summary(self) -> str:
        out = []
        for name, kind, limit in self.bounded:
            out.append(f"bounded  {name:<22} -> {kind} (keeps the newest {limit})")
        for name in self.already:
            out.append(f"already  {name:<22} (already bounded)")
        for name, why in self.skipped:
            out.append(f"SKIPPED  {name:<22} {why}")
        if not out:
            out.append("nothing to do: no list/dict/set that only ever grows")
        return "\n".join(out)


def _call_name(node: ast.AST) -> str:
    f = node.func if isinstance(node, ast.Call) else None
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")


def _plan(value: ast.AST, src: str, name: str, maxlen: int, maxsize: int):
    """-> (new_value_text, class_name_to_import or None, description, limit)  or raises BoundedPatchError(reason)"""
    seg = ast.get_source_segment(src, value)
    cname = _call_name(value)
    if isinstance(value, (ast.List, ast.ListComp)) or cname == "list":
        return f"BoundedList({seg}, maxlen={maxlen}, name={name!r})", "BoundedList", "BoundedList", maxlen
    if cname == "defaultdict":
        if value.keywords or len(value.args) > 1:
            raise BoundedPatchError("a defaultdict with unusual arguments")
        if not value.args:
            return f"BoundedDict(maxsize={maxsize}, name={name!r})", "BoundedDict", "BoundedDict", maxsize
        factory = ast.get_source_segment(src, value.args[0])
        return f"BoundedDict(maxsize={maxsize}, name={name!r}, default_factory={factory})", "BoundedDict", "BoundedDict", maxsize
    if isinstance(value, (ast.Dict, ast.DictComp)) or cname in ("dict", "OrderedDict"):
        return f"BoundedDict({seg}, maxsize={maxsize}, name={name!r})", "BoundedDict", "BoundedDict", maxsize
    if isinstance(value, (ast.Set, ast.SetComp)) or cname == "set":
        return f"BoundedSet({seg}, maxsize={maxsize}, name={name!r})", "BoundedSet", "BoundedSet", maxsize
    if cname == "deque":
        if any(k.arg == "maxlen" for k in value.keywords) or len(value.args) >= 2:
            raise BoundedPatchError("already bounded")
        inner = seg[:-1].rstrip()
        sep = "" if inner.endswith("(") else ", "
        return f"{inner}{sep}maxlen={maxlen})", None, "deque with maxlen", maxlen
    if cname == "Counter":
        raise BoundedPatchError("a Counter: bound it by hand (it supports arithmetic that a bounded copy would not)")
    raise BoundedPatchError(f"a container built by `{cname or type(value).__name__}`, which this tool does not recognise")


def make_bounded(pipeline_path: str, maxlen: int = 1000, maxsize: int = 5000, write: bool = True) -> BoundedReport:
    src = open(pipeline_path, encoding="utf-8").read()
    tree = ast.parse(src)
    package_dir = os.path.dirname(os.path.abspath(pipeline_path))
    growing = [f["name"] for f in audit_source(src) if f["kind"] == "GROWS_FOREVER"]
    rep = BoundedReport()
    edits: List[Tuple[int, int, str]] = []
    needed: List[str] = []
    for name in growing:
        assigns = [s for s in tree.body if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in s.targets)]
        if len(assigns) != 1 or len(assigns[0].targets) != 1:
            rep.skipped.append((name, "assigned more than once or in a chained assignment (A = B = [])"))
            continue
        stmt = assigns[0]
        try:
            text, imp, kind, limit = _plan(stmt.value, src, name, maxlen, maxsize)
        except BoundedPatchError as e:
            if str(e) == "already bounded":
                rep.already.append(name)
            else:
                rep.skipped.append((name, str(e)))
            continue
        edits.append((stmt.lineno, stmt.end_lineno, f"{name} = {text}"))
        if imp and imp not in needed:
            needed.append(imp)
        rep.bounded.append((name, kind, limit))
    if not edits:
        return rep

    lines = src.splitlines()
    first = min(e[0] for e in edits)
    for start, end, text in sorted(edits, key=lambda e: -e[0]):
        new = text.splitlines()
        if start == first and needed:
            new = [f"from .bounded import {', '.join(sorted(needed))}"] + new
        lines[start - 1:end] = new
    new_src = "\n".join(lines) + ("\n" if src.endswith("\n") else "")
    try:
        compile(new_src, pipeline_path, "exec")
    except SyntaxError as e:
        raise BoundedPatchError(f"the rewritten code would not compile ({e.msg} at line {e.lineno}). Nothing was changed.") from e
    if write:
        backup = pipeline_path + ".before_bounded_patch"
        if not os.path.exists(backup):
            shutil.copyfile(pipeline_path, backup)
        with open(pipeline_path, "w", encoding="utf-8", newline="") as f:
            f.write(new_src)
        if needed:
            shutil.copyfile(os.path.join(HERE, "bounded.py"), os.path.join(package_dir, "bounded.py"))
        rep.wrote = True
    return rep


def remaining_growers(pipeline_path: str) -> List[str]:
    """What the Day 18 audit would still list as GROWS_FOREVER, minus containers that are now bounded
    (the old audit does not know that `deque(maxlen=N)` or a Bounded* container cannot grow forever)."""
    src = open(pipeline_path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = []
    for f in audit_source(src):
        if f["kind"] != "GROWS_FOREVER":
            continue
        assigns = [s for s in tree.body if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == f["name"] for t in s.targets)]
        bounded = False
        for a in assigns:
            name = _call_name(a.value)
            if name.startswith("Bounded") or (name == "deque" and (any(k.arg == "maxlen" for k in a.value.keywords) or len(a.value.args) >= 2)):
                bounded = True
        if not bounded:
            out.append(f["name"])
    return out


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "rag_pipeline/pipeline.py"
    print(make_bounded(path).summary())
