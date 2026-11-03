"""
container_patch.py -- Day 19: let the container find Redis.

Inside a container, "localhost" means the container itself, not your computer and not the Redis container.
So wherever the frozen pipeline says  Redis(host="localhost", port=6379)  the address must come from an
environment variable instead, with the SAME default as before (so your notebook and your terminal keep working
exactly as they do today).

    analyze_redis(source)                  -> what the pipeline does today (no changes)
    make_redis_env_driven(path, write=True)-> rewrites only the host / port / url, keeps everything else byte for byte

Handled shapes (anything else is refused, and NOTHING is changed):
    redis.Redis(host="localhost", port=6379, ...)      redis.StrictRedis / Redis / ConnectionPool / BlockingConnectionPool
    redis.Redis()                                      (no host: Redis' own default is localhost)
    redis.from_url("redis://localhost:6379/0")
    REDIS_HOST = "localhost"  ...  redis.Redis(host=REDIS_HOST)   (module level constants are rewritten where they are defined)
    host=os.getenv("REDIS_HOST", ...)                  (already driven by the environment: left alone)
    host="some-remote-host.example.com"                (a real remote server: left alone)
"""
import ast
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
REDIS_CLASSES = {"Redis", "StrictRedis", "ConnectionPool", "BlockingConnectionPool"}
ENV_FUNCS = {("os", "getenv"), ("os", "environ")}


class ContainerPatchError(RuntimeError):
    pass


@dataclass
class Part:
    """What one argument (host / port / url) of a Redis call is."""
    state: str                      # literal | env | remote | missing | unknown
    value: object = None            # the literal value, or the env var name
    node: Optional[ast.AST] = None  # the literal's node (the thing to replace)
    note: str = ""
    default: object = None          # for env: the fallback literal in os.getenv(name, default), if any


@dataclass
class Site:
    line: int
    call: str
    kind: str                       # "host" | "url"
    host: Part = None
    port: Part = None
    url: Part = None
    call_node: Optional[ast.Call] = None


@dataclass
class RedisAnalysis:
    sites: List[Site] = field(default_factory=list)
    has_redis_import: bool = False

    @property
    def env_vars(self) -> Dict[str, str]:
        """env var names the pipeline already reads for Redis -> 'host' | 'port' | 'url'."""
        out = {}
        for s in self.sites:
            for what, part in (("host", s.host), ("port", s.port), ("url", s.url)):
                if part is not None and part.state == "env":
                    out[str(part.value)] = what
        return out

    @property
    def needs_patch(self) -> bool:
        for s in self.sites:
            if s.kind == "url":
                if s.url.state == "literal":
                    return True
            elif s.host.state != "remote":
                if s.host.state in ("literal", "missing") or s.port.state == "literal":
                    return True
        return False

    @property
    def problems(self) -> List[str]:
        out = []
        for s in self.sites:
            parts = [s.url] if s.kind == "url" else [s.host, s.port]
            for p in parts:
                if p is not None and p.state == "unknown":
                    out.append(f"line {s.line}: {s.call}(...) -> {p.note}")
        return out

    @property
    def all_remote(self) -> bool:
        if not self.sites:
            return False
        for s in self.sites:
            p = s.url if s.kind == "url" else s.host
            if p.state != "remote":
                return False
        return True


# ---------------------------------------------------------------- analysis --
def _is_env_read(node: ast.AST) -> Optional[str]:
    """os.getenv("X", ...) / os.environ.get("X", ...) / os.environ["X"]  ->  "X"."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        f = node.func
        if isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr == "getenv" and node.args:
            a = node.args[0]
            return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else "?"
        if (f.attr == "get" and isinstance(f.value, ast.Attribute) and f.value.attr == "environ"
                and isinstance(f.value.value, ast.Name) and f.value.value.id == "os" and node.args):
            a = node.args[0]
            return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else "?"
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
        s = node.slice
        return s.value if isinstance(s, ast.Constant) and isinstance(s.value, str) else "?"
    # int(os.getenv("REDIS_PORT", "6379"))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("int", "str") and node.args:
        return _is_env_read(node.args[0])
    return None


def _env_default(node: ast.AST) -> object:
    """The fallback literal of  os.getenv("X", "fallback")  /  int(os.getenv("X", "6379")), else None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("int", "str") and node.args:
        return _env_default(node.args[0])
    if isinstance(node, ast.Call) and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        return node.args[1].value
    return None


def _module_assigns(tree: ast.Module) -> Dict[str, ast.AST]:
    out: Dict[str, ast.AST] = {}
    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            out[st.targets[0].id] = st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            out[st.target.id] = st.value
    return out


def _classify(node: Optional[ast.AST], assigns: Dict[str, ast.AST], what: str) -> Part:
    if node is None:
        return Part("missing")
    env = _is_env_read(node)
    if env is not None:
        return Part("env", env, node, default=_env_default(node))
    if isinstance(node, ast.Constant):
        v = node.value
        if what == "port":
            return Part("literal", v, node) if isinstance(v, int) and not isinstance(v, bool) else Part("unknown", note=f"port is {v!r}")
        if what == "host":
            if isinstance(v, str):
                return Part("literal" if v in LOCAL_HOSTS else "remote", v, node)
            return Part("unknown", note=f"host is {v!r}")
        if what == "url":
            if isinstance(v, str):
                local = any(h in v for h in ("localhost", "127.0.0.1", "[::1]", "//0.0.0.0"))
                return Part("literal" if local else "remote", v, node)
            return Part("unknown", note=f"url is {v!r}")
    if isinstance(node, ast.Name):
        if node.id in assigns:
            inner = _classify(assigns[node.id], {}, what)       # one level only: constants, not chains
            if inner.state in ("literal", "remote", "env"):
                inner.note = f"via the constant {node.id}"
                return inner
        return Part("unknown", note=f"{what} comes from the name `{node.id}`, which is not a plain constant")
    return Part("unknown", note=f"{what} is `{ast.unparse(node)[:60]}` (not a constant or an environment variable)")


def analyze_redis(source: str) -> RedisAnalysis:
    tree = ast.parse(source)
    res = RedisAnalysis()
    names_from_redis, redis_modules = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] == "redis":
                    redis_modules.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] == "redis" and n.level == 0:
            for a in n.names:
                names_from_redis.add(a.asname or a.name)
    res.has_redis_import = bool(redis_modules or names_from_redis)
    if not res.has_redis_import:
        return res
    assigns = _module_assigns(tree)

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name, base = None, None
        if isinstance(f, ast.Attribute):
            name = f.attr
            b = f.value
            while isinstance(b, ast.Attribute):          # redis.asyncio.Redis -> base redis
                b = b.value
            base = b.id if isinstance(b, ast.Name) else None
            from_redis = base in redis_modules or (base in names_from_redis)
        elif isinstance(f, ast.Name):
            name = f.id
            from_redis = f.id in names_from_redis
        else:
            continue
        if not from_redis:
            continue
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        if any(k.arg is None for k in n.keywords):
            res.sites.append(Site(n.lineno, name, "host", Part("unknown", note="host/port come from **kwargs"), Part("unknown", note="**kwargs"), None, n))
            continue
        if name == "from_url":
            node = n.args[0] if n.args else kw.get("url")
            res.sites.append(Site(n.lineno, "from_url", "url", None, None, _classify(node, assigns, "url"), n))
        elif name in REDIS_CLASSES:
            if any(isinstance(a, ast.Starred) for a in n.args):
                res.sites.append(Site(n.lineno, name, "host", Part("unknown", note="*args"), Part("unknown", note="*args"), None, n))
                continue
            if "connection_pool" in kw or "unix_socket_path" in kw or "path" in kw:
                continue                                  # not a host/port based connection
            host_node = kw.get("host", n.args[0] if len(n.args) > 0 else None)
            port_node = kw.get("port", n.args[1] if len(n.args) > 1 else None)
            res.sites.append(Site(n.lineno, name, "host", _classify(host_node, assigns, "host"),
                                  _classify(port_node, assigns, "port"), None, n))
    return res


# ------------------------------------------------------------------- patch --
def _offsets(source: str):
    starts, pos = [], 0
    for line in source.splitlines(keepends=True):
        starts.append(pos)
        pos += len(line.encode("utf-8"))
    return starts


def _span(source_bytes: bytes, starts, node: ast.AST) -> Tuple[int, int]:
    return starts[node.lineno - 1] + node.col_offset, starts[node.end_lineno - 1] + node.end_col_offset


def _os_imported_before(tree: ast.Module, line: int) -> bool:
    """Is there a module-level `import os` (or `import sys, os`) BEFORE this line? Frozen files often import os late."""
    for st in tree.body:
        if isinstance(st, ast.Import) and st.lineno < line and any(a.name == "os" and a.asname in (None, "os") for a in st.names):
            return True
    return False


def make_redis_env_driven(pipeline_path: str, write: bool = True) -> str:
    """Rewrite local Redis host/port/url so the environment can override them. Returns a one line summary."""
    with open(pipeline_path, encoding="utf-8") as f:
        source = f.read()
    analysis = analyze_redis(source)
    if not analysis.has_redis_import or not analysis.sites:
        raise ContainerPatchError("no Redis connection was found in the pipeline file (nothing was changed). "
                                  "Send me the notebook cell where redis_client is created.")
    if analysis.problems:
        raise ContainerPatchError("cannot make the Redis address environment-driven (nothing was changed):\n  "
                                  + "\n  ".join(analysis.problems))
    if not analysis.needs_patch:
        return "nothing to do: Redis is already " + ("on a remote server" if analysis.all_remote else "driven by the environment")

    tree = ast.parse(source)
    sb = source.encode("utf-8")
    starts = _offsets(source)
    edits: List[Tuple[int, int, str]] = []          # (start, end, replacement)
    seen = set()

    def replace(node, text):
        s, e = _span(sb, starts, node)
        if (s, e) not in seen:
            seen.add((s, e))
            edits.append((s, e, text))

    for s in analysis.sites:
        if s.kind == "url":
            if s.url.state == "literal":
                replace(s.url.node, f'os.getenv("REDIS_URL", {ast.unparse(s.url.node)})')
            continue
        if s.host.state == "literal":
            replace(s.host.node, f'os.getenv("REDIS_HOST", {ast.unparse(s.host.node)})')
        elif s.host.state == "missing":
            call = s.call_node
            if call.args or call.keywords:
                last = max([*call.args, *[k.value for k in call.keywords]], key=lambda x: (x.end_lineno, x.end_col_offset))
                _, e = _span(sb, starts, last)
                edits.append((e, e, ', host=os.getenv("REDIS_HOST", "localhost")'))
            else:
                # Redis()  ->  Redis(host=...)   (the character right after the function name must be "(")
                _, fe = _span(sb, starts, call.func)
                if sb[fe:fe + 1] != b"(":
                    raise ContainerPatchError(f"line {s.line}: unusual spacing in {s.call}() (nothing was changed)")
                edits.append((fe + 1, fe + 1, 'host=os.getenv("REDIS_HOST", "localhost")'))
        if s.port.state == "literal" and s.host.state != "remote":
            replace(s.port.node, f'int(os.getenv("REDIS_PORT", "{s.port.value}"))')

    # keyword arguments given as positional host/port stay positional: the replacement is an expression, so it works there too.
    new = sb
    for st, en, text in sorted(edits, key=lambda x: -x[0]):
        new = new[:st] + text.encode("utf-8") + new[en:]
    text = new.decode("utf-8")

    first_edit_line = min(sb[:st].count(b"\n") + 1 for st, _, _ in edits)
    if not _os_imported_before(tree, first_edit_line):
        lines = text.splitlines(keepends=True)
        tree2 = ast.parse(text)
        first = None
        for st in tree2.body:
            is_doc = isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)
            is_future = isinstance(st, ast.ImportFrom) and st.module == "__future__"
            if not (is_doc or is_future):
                first = st
                break
        at = (first.lineno - 1) if first is not None else len(lines)
        while at > 0 and lines[at - 1].lstrip().startswith("#"):      # stay above the comment that introduces that statement
            at -= 1
        lines.insert(at, "import os\n")
        text = "".join(lines)

    try:
        compile(text, pipeline_path, "exec")
    except SyntaxError as e:
        raise ContainerPatchError(f"the rewritten file would not compile ({e}); nothing was changed")

    if write:
        backup = pipeline_path + ".before_container_patch"
        if not os.path.exists(backup):
            shutil.copyfile(pipeline_path, backup)
        with open(pipeline_path, "w", encoding="utf-8") as f:
            f.write(text)
    return f"patched {len(edits)} place(s): Redis host/port/url now come from REDIS_HOST / REDIS_PORT / REDIS_URL (same defaults as before)"
