"""
make_requirements.py -- Day 19: the list of packages the container must install.

    rep = write_requirements("rag_pipeline", project_dir=".")

Starts from what your frozen code actually imports (plus serve.py / api_layer.py), finds which installed package
provides each import, then adds everything those packages need, with the EXACT versions installed in this
environment (the ones you tested with). Two things differ from a plain `pip freeze`:

  * The target is Linux, so Windows-only requirements (pywin32, colorama on Windows...) are left out.
  * PyTorch is installed in its CPU build (a GPU build is about 2.5 GB bigger and useless in a container with no GPU).
    Local version tags like  2.5.1+cu121  are dropped (they do not exist on PyPI for Linux).

Writes requirements.docker.txt and, if PyTorch is needed, requirements.torch.txt.
"""
import ast
import importlib.metadata as md
import importlib.util
import os
import platform
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version
except ImportError:                                    # pragma: no cover
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name
    from pip._vendor.packaging.version import Version

TORCH_FAMILY = {"torch", "torchvision", "torchaudio"}
NEVER_INSTALL = {"triton", "pywin32", "pypiwin32", "pywin32-ctypes", "win32-setctime", "wmi", "comtypes"}   # GPU / Windows-only
NEVER_PREFIXES = ("nvidia-",)


@dataclass
class RequirementsReport:
    python_version: str
    modules: List[str] = field(default_factory=list)
    top_level: List[str] = field(default_factory=list)       # distributions found directly from imports
    pinned: Dict[str, str] = field(default_factory=dict)
    torch_pins: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    not_installed_here: List[str] = field(default_factory=list)   # needed on Linux but absent on this machine: pip fetches them
    files: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Python {self.python_version} | {len(self.top_level)} packages found from your imports, "
                 f"{len(self.pinned) + len(self.torch_pins)} pinned in total (everything they need, exact versions)"]
        if self.torch_pins:
            lines.append("PyTorch will be installed in its CPU build: " + ", ".join(f"{k}=={v}" for k, v in self.torch_pins.items()))
        if self.not_installed_here:
            lines.append(f"{len(self.not_installed_here)} Linux-only helper packages are not installed on this machine "
                         f"(pip fetches them inside the image): {', '.join(sorted(self.not_installed_here)[:10])}"
                         + (" ..." if len(self.not_installed_here) > 10 else ""))
        for w in self.warnings:
            lines.append("WARNING: " + w)
        lines.append("wrote: " + ", ".join(self.files))
        return "\n".join(lines)


# ---------------------------------------------------------------- imports --
def imported_modules(paths: Iterable[str]) -> Tuple[Set[str], Set[str]]:
    """(real, maybe): module names imported in these files (absolute imports only).
    `maybe` are the  a.b.c  candidates from  `from a.b import c`  -- c may be a submodule or just a name."""
    real: Set[str] = set()
    maybe: Set[str] = set()
    for p in paths:
        if not os.path.exists(p):
            continue
        tree = ast.parse(open(p, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                real.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                real.add(n.module)
                maybe.update(f"{n.module}.{a.name}" for a in n.names if a.name != "*")
    return real, maybe - real


def _norm(p: str) -> str:
    # abspath (not realpath): thousands of files are looked up and realpath is slow on Windows. Both sides of every
    # comparison are built from the same sys.path entry, so they spell the path the same way.
    return os.path.normcase(os.path.abspath(p))


_FILE_INDEX: Optional[Dict[str, str]] = None


def _file_index() -> Dict[str, str]:
    """installed file -> distribution name (built once from every package's RECORD)."""
    global _FILE_INDEX
    if _FILE_INDEX is None:
        idx: Dict[str, str] = {}
        for d in md.distributions():
            name = d.metadata["Name"]
            if not name:
                continue
            for f in d.files or ():
                s = str(f)
                if s.endswith((".py", ".so", ".pyd")) and not s.startswith(".."):
                    idx[_norm(str(d.locate_file(f)))] = name
        _FILE_INDEX = idx
    return _FILE_INDEX


def reset_cache():
    global _FILE_INDEX
    _FILE_INDEX = None


def owners(module: str, local_modules: Set[str]) -> Tuple[Set[str], str]:
    """(distribution names providing this module, kind). kind: stdlib | local | dist | namespace | unknown.
    Nothing is imported: the module is located by looking for its file on sys.path and asking which package installed it."""
    parts = module.split(".")
    top = parts[0]
    if top in sys.stdlib_module_names:
        return set(), "stdlib"
    if top in local_modules:
        return set(), "local"
    idx = _file_index()
    for entry in sys.path:
        if not entry or not os.path.isdir(entry):
            continue
        base = os.path.join(entry, *parts)
        for cand in (base + ".py", os.path.join(base, "__init__.py")):
            name = idx.get(_norm(cand)) if os.path.exists(cand) else None
            if name:
                return {name}, "dist"
        d = os.path.dirname(base)
        if os.path.isdir(d):                                   # compiled modules:  name.cp311-win_amd64.pyd / name.cpython-311-x86_64-linux-gnu.so
            stem = parts[-1] + "."
            for fn in os.listdir(d):
                if fn.startswith(stem) and fn.endswith((".pyd", ".so")):
                    name = idx.get(_norm(os.path.join(d, fn)))
                    if name:
                        return {name}, "dist"
        if os.path.isdir(base) and not os.path.exists(os.path.join(base, "__init__.py")):
            return set(), "namespace"
    if len(parts) == 1:                                        # editable installs and single-file oddities
        pd = md.packages_distributions().get(top)
        if pd and len(pd) == 1:
            return set(pd), "dist"
    return set(), "unknown"


# ---------------------------------------------------------------- closure --
def _linux_env(py: str) -> Dict[str, str]:
    full = platform.python_version()                      # the image installs the newest patch release of the same x.y, so this is a safe floor
    if not full.startswith(py + "."):
        full = py + ".0"
    return {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
            "platform_python_implementation": "CPython", "implementation_name": "cpython",
            "python_version": py, "python_full_version": full,
            "platform_release": "", "platform_version": "", "implementation_version": full}


def _pin(version: str) -> str:
    return Version(version).public


def closure(start: Iterable[str], py: str, report: RequirementsReport) -> Dict[str, str]:
    """Walk 'Requires-Dist' from the start packages, evaluating markers as Linux. Returns canonical-name -> version."""
    env = _linux_env(py)
    result: Dict[str, str] = {}
    extras_for: Dict[str, Set[str]] = {}
    stack: List[Tuple[str, Set[str]]] = [(canonicalize_name(s), set()) for s in start]
    while stack:
        name, extras = stack.pop()
        if name in NEVER_INSTALL or name.startswith(NEVER_PREFIXES):
            continue
        known_extras = extras_for.setdefault(name, set())
        if name in result and extras <= known_extras:
            continue
        known_extras |= extras
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            if name not in report.not_installed_here:
                report.not_installed_here.append(name)
            continue
        if name not in result:
            result[name] = dist.version
            direct = dist.read_text("direct_url.json")
            if direct and ("file:" in direct or '"vcs_info"' in direct or '"dir_info"' in direct):
                report.warnings.append(f"{dist.metadata['Name']} {dist.version} was installed from a local folder or git, "
                                       f"not from PyPI. The image cannot reproduce that: install it by hand or publish it.")
        for r in dist.requires or ():
            try:
                req = Requirement(r)
            except Exception:
                continue
            if req.marker is not None:
                if not any(req.marker.evaluate({**env, "extra": e}) for e in (extras or {""})):
                    continue
            stack.append((canonicalize_name(req.name), set(req.extras)))
    return result


# ------------------------------------------------------------------- write --
def write_requirements(package: str = "rag_pipeline", project_dir: str = ".", extra_packages: Iterable[str] = (),
                       extra_files: Iterable[str] = ("serve.py", "api_layer.py", "monitoring.py"), out_dir: Optional[str] = None) -> RequirementsReport:
    out_dir = out_dir or project_dir
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    rep = RequirementsReport(python_version=py)
    files = [os.path.join(project_dir, package, "pipeline.py")] + [os.path.join(project_dir, f) for f in extra_files]
    local = {package, "usage_context", "api_layer", "serve", "monitoring", "container_patch", "freeze_kernel"}
    real, maybe = imported_modules(files)
    rep.modules = sorted(real | maybe)

    top: Set[str] = set()
    for m in sorted(real | maybe):
        found, kind = owners(m, local)
        if kind == "unknown" and m in maybe:
            continue                                           # `from a.b import name`: name is not a module, the parent covers it
        if kind == "unknown":
            rep.warnings.append(f"`import {m}` is not provided by any installed package here (skipped)")
        top.update(found)
    top.update(extra_packages)
    rep.top_level = sorted(canonicalize_name(t) for t in top)

    resolved = closure(rep.top_level, py, rep)
    for name, ver in sorted(resolved.items()):
        (rep.torch_pins if name in TORCH_FAMILY else rep.pinned)[name] = _pin(ver)

    header = (f"# Generated by make_requirements.py for Python {py} on Linux.\n"
              f"# Exact versions installed in the environment where the notebook ran. Edit with care: change, rebuild, test.\n")
    p1 = os.path.join(out_dir, "requirements.docker.txt")
    with open(p1, "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        for name, ver in sorted(rep.pinned.items()):
            f.write(f"{name}=={ver}\n")
        for name, ver in sorted(rep.torch_pins.items()):          # already satisfied by the CPU wheels installed first
            f.write(f"{name}=={ver}\n")
    rep.files.append("requirements.docker.txt")
    p2 = os.path.join(out_dir, "requirements.torch.txt")
    if rep.torch_pins:
        with open(p2, "w", encoding="utf-8", newline="\n") as f:
            f.write("# PyTorch CPU build, installed first from the PyTorch CPU index (see Dockerfile).\n")
            for name, ver in sorted(rep.torch_pins.items()):
                f.write(f"{name}=={ver}\n")
        rep.files.append("requirements.torch.txt")
    elif os.path.exists(p2):
        os.remove(p2)
    return rep
