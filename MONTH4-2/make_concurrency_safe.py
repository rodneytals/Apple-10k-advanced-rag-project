"""
make_concurrency_safe.py -- Day 18

Rewrites <package>/pipeline.py so token tracking is per call instead of one shared global:

  module level:   usage_tracker = TokenUsage()
            ->    usage_tracker = UsageProxy(TokenUsage())

  every function that does  `global usage_tracker; usage_tracker = TokenUsage()`:
      def run_with_entity_memory(...):
          _usage_token = usage_tracker.begin_call(TokenUsage())
          try:
              ...the rest of the function, unchanged...
          finally:
              usage_tracker.end_call(_usage_token)

It refuses (and changes nothing) if the code does not look the way it expects.
"""
import ast
import os
import shutil
import sys
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))


class PatchError(RuntimeError):
    pass


def _is_tracker_assign(stmt: ast.stmt, name: str) -> bool:
    return (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == name and isinstance(stmt.value, ast.Call))


def _function_assigns(fn: ast.AST, name: str) -> List[ast.stmt]:
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            out.append(n)
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            out.append(n)
    return out


def _declares_global(fn: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Global) and name in n.names for n in ast.walk(fn))


def make_concurrency_safe(pipeline_path: str, tracker: str = "usage_tracker", write: bool = True) -> str:
    src = open(pipeline_path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    package_dir = os.path.dirname(os.path.abspath(pipeline_path))

    if "UsageProxy" in src:
        return "already patched: nothing to do"

    module_assign = [s for s in tree.body if _is_tracker_assign(s, tracker)]
    if len(module_assign) != 1:
        raise PatchError(f"expected exactly one module-level `{tracker} = SomeClass()` in {pipeline_path}, "
                         f"found {len(module_assign)}. Nothing was changed.")
    ctor = module_assign[0].value

    targets = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and _declares_global(n, tracker)]
    edits = []                                      # (start_line, end_line, new_text), 1-based inclusive
    changed_functions: List[str] = []

    for fn in targets:
        assigns = _function_assigns(fn, tracker)
        top_level = [s for s in fn.body if _is_tracker_assign(s, tracker)]
        if len(assigns) != 1 or len(top_level) != 1:
            raise PatchError(
                f"function `{fn.name}` assigns `{tracker}` in a shape this tool does not handle "
                f"({len(assigns)} assignment(s), {len(top_level)} at the top level of the function). "
                f"Nothing was changed. Send me that function and I will adapt the patch.")
        idx = fn.body.index(top_level[0])
        before, after = fn.body[:idx], fn.body[idx + 1:]
        # drop the `global usage_tracker` declaration (keep any other names it declares)
        new_before = []
        for s in before:
            if isinstance(s, ast.Global) and tracker in s.names:
                rest = [n for n in s.names if n != tracker]
                if rest:
                    new_before.append(ast.Global(names=rest))
                continue
            new_before.append(s)
        begin = ast.parse(f"_usage_token = {tracker}.begin_call(None)").body[0]
        begin.value.args[0] = top_level[0].value
        finally_stmt = ast.parse(f"{tracker}.end_call(_usage_token)").body[0]
        body_after = after or [ast.Pass()]
        try_node = ast.Try(body=body_after, handlers=[], orelse=[], finalbody=[finally_stmt])
        new_fn = type(fn)(**{f: getattr(fn, f) for f in fn._fields})
        new_fn.body = new_before + [begin, try_node]
        ast.fix_missing_locations(new_fn)
        start = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
        edits.append((start, fn.end_lineno, ast.unparse(new_fn)))
        changed_functions.append(fn.name)

    edits.append((module_assign[0].lineno, module_assign[0].end_lineno,
                  f"from .usage_context import UsageProxy\n{tracker} = UsageProxy({ast.unparse(ctor)})"))

    # apply bottom-up so earlier line numbers stay valid
    for start, end, text in sorted(edits, key=lambda e: -e[0]):
        lines[start - 1:end] = text.splitlines()
    new_src = "\n".join(lines) + "\n"
    try:
        compile(new_src, pipeline_path, "exec")     # never write something that does not even compile
    except SyntaxError as e:
        raise PatchError(f"the rewritten code would not compile ({e.msg} at line {e.lineno}). Nothing was changed. "
                         f"Send me the function {', '.join(changed_functions)} and I will patch it by hand.") from e

    if write:
        shutil.copyfile(pipeline_path, pipeline_path + ".before_concurrency_patch")
        with open(pipeline_path, "w", encoding="utf-8") as f:
            f.write(new_src)
        shutil.copyfile(os.path.join(HERE, "usage_context.py"), os.path.join(package_dir, "usage_context.py"))
    return (f"patched: module-level `{tracker}` is now a UsageProxy; per-call tracker in "
            f"{', '.join(changed_functions) or '(no function rebinding the global found)'}")


if __name__ == "__main__":
    print(make_concurrency_safe(sys.argv[1] if len(sys.argv) > 1 else "rag_pipeline/pipeline.py"))
