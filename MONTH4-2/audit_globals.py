"""
audit_globals.py -- Day 18

Reads <package>/pipeline.py WITHOUT running it and lists state that is shared between requests:

  REBINDS_GLOBAL   a function does `global X; X = ...`   -> two overlapping requests overwrite each other
  GROWS_FOREVER    a module-level list/dict/set only ever gets added to -> memory grows with every request
  SHARED_MUTABLE   a module-level list/dict/set is changed by functions   -> requests can see each other's data
"""
import ast
import sys
from typing import Dict, List, Set

MUTABLE_CALLS = {"list", "dict", "set", "defaultdict", "deque", "OrderedDict", "Counter"}
ADDERS = {"append", "extend", "add", "insert", "update", "setdefault", "appendleft", "extendleft"}
REMOVERS = {"clear", "pop", "popleft", "remove", "discard", "popitem"}


def _mutable_value(v: ast.AST) -> bool:
    if isinstance(v, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(v, ast.Call):
        f = v.func
        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        return name in MUTABLE_CALLS
    return False


def _locals_of(fn) -> Set[str]:
    a = fn.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    for x in (a.vararg, a.kwarg):
        if x:
            names.add(x.arg)
    declared = {n for s in ast.walk(fn) if isinstance(s, ast.Global) for n in s.names}
    for s in ast.walk(fn):
        if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Store):
            names.add(s.id)
    return names - declared


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def audit_source(src: str) -> List[Dict]:
    tree = ast.parse(src)
    mutable: Set[str] = set()
    for s in tree.body:
        if isinstance(s, ast.Assign) and _mutable_value(s.value):
            for t in s.targets:
                if isinstance(t, ast.Name):
                    mutable.add(t.id)
    adds: Dict[str, Set[str]] = {}
    removes: Dict[str, Set[str]] = {}
    rebinds: Dict[str, Set[str]] = {}

    funcs = []
    for s in tree.body:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(s)
        elif isinstance(s, ast.ClassDef):
            funcs += [m for m in s.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]

    for fn in funcs:
        local = _locals_of(fn)
        declared_global = {n for s in ast.walk(fn) if isinstance(s, ast.Global) for n in s.names}
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                r = _root(n.func.value)
                if r in mutable and r not in local:
                    if n.func.attr in ADDERS:
                        adds.setdefault(r, set()).add(fn.name)
                    if n.func.attr in REMOVERS:
                        removes.setdefault(r, set()).add(fn.name)
            if isinstance(n, (ast.Assign, ast.AugAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if isinstance(t, ast.Subscript):
                        r = _root(t)
                        if r in mutable and r not in local:
                            adds.setdefault(r, set()).add(fn.name)
                    if isinstance(t, ast.Name) and t.id in declared_global:
                        rebinds.setdefault(t.id, set()).add(fn.name)
            if isinstance(n, ast.Delete):
                for t in n.targets:
                    r = _root(t)
                    if r in mutable and r not in local:
                        removes.setdefault(r, set()).add(fn.name)

    findings: List[Dict] = []
    for name, fns in sorted(rebinds.items()):
        findings.append({"kind": "REBINDS_GLOBAL", "name": name, "functions": sorted(fns),
                         "advice": "Each overlapping request overwrites this. Make it per-call (ContextVar) or pass it through the state."})
    for name in sorted(mutable):
        a, r = adds.get(name, set()), removes.get(name, set())
        if a and not r:
            findings.append({"kind": "GROWS_FOREVER", "name": name, "functions": sorted(a),
                             "advice": "Only ever added to: memory grows with every request. Use collections.deque(maxlen=N) or log it elsewhere."})
        elif a or r:
            findings.append({"kind": "SHARED_MUTABLE", "name": name, "functions": sorted(a | r),
                             "advice": "Changed by functions and shared by all requests. Fine for a cache; wrong for per-request data."})
    return findings


def audit(path: str) -> List[Dict]:
    return audit_source(open(path, encoding="utf-8").read())


def print_audit(findings: List[Dict]) -> None:
    if not findings:
        print("✅ no shared mutable state found")
        return
    for f in findings:
        print(f"⚠️  {f['kind']:<15} {f['name']:<22} used by: {', '.join(f['functions'])}")
        print(f"      {f['advice']}")


if __name__ == "__main__":
    print_audit(audit(sys.argv[1] if len(sys.argv) > 1 else "rag_pipeline/pipeline.py"))
