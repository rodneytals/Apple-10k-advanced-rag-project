"""
backup_tools.py -- Day 21: backups you can trust, because they are restored and checked.

    python backup_tools.py backup                # a new dated folder under ./backups, verified; keeps the newest 7
    python backup_tools.py schedule              # prints the one Windows command that makes this run every day
    python backup_tools.py backup --with-models  # also ONE copy of the models volume (a few GB; re-downloading costs data)
    python backup_tools.py list
    python backup_tools.py verify [folder]       # re-checks the files (no Docker needed)
    python backup_tools.py drill  [folder]       # restores into THROWAWAY volumes and checks the data really comes back
    python backup_tools.py restore FOLDER --yes  # the real thing: stops the stack, takes a safety copy, restores into your volumes

What is saved, and how:
  checkpoints (and any other state folder)  every SQLite file through SQLite's own online-backup call, so the copy is consistent
                                            even while the API is writing (copying a live WAL database file by hand is not safe)
  redis_data                                an RDB snapshot taken with `redis-cli --rdb` from the running Redis
  models (only with --with-models)          one tar.gz, replaced each time (the files never change, so one copy is enough)
NOT saved on purpose: .env (it holds your keys: keep a copy in a password manager, never next to the backups).

Everything runs through the `docker` command; helper containers use the images your compose file already names (nothing new to download).
Standard library plus PyYAML (already needed by Day 19).
"""
import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Callable

TOOL_VERSION = 1
STAMP = re.compile(r"^\d{8}-\d{6}$")
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
NOT_STATE_VOLUMES = {"models", "redis_data", "caddy_data", "caddy_config"}


class BackupError(Exception):
    """A message that is safe to show (no secrets in it)."""


# ------------------------------------------------------------ docker access --
def run(cmd: List[str], cwd: Optional[str] = None, timeout: float = 900, ok_codes=(0,)) -> Tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise BackupError("the `docker` command was not found: start Docker Desktop first")
    except subprocess.TimeoutExpired:
        raise BackupError(f"`{' '.join(cmd[:3])} ...` did not finish within {timeout:.0f}s")
    out = (r.stdout + r.stderr).strip()
    if r.returncode not in ok_codes:
        raise BackupError(f"`{' '.join(cmd[:4])} ...` failed ({r.returncode}): {out[-400:]}")
    return r.returncode, out


def docker_ready() -> Optional[str]:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"cannot run docker ({type(e).__name__})"
    return None if r.returncode == 0 else "Docker is not running: start Docker Desktop and wait until it says it is running"


# ------------------------------------------------------------ the project ---
@dataclass
class Project:
    dir: str
    name: str
    app_image: str
    redis_image: Optional[str]
    state_volumes: Dict[str, str]          # short volume name -> path inside the app container (e.g. checkpoints -> /app/checkpoints)
    has_models: bool
    has_redis: bool

    def vol(self, short: str) -> str:
        return f"{self.name}_{short}"


def load_project(project_dir: str = ".", name: Optional[str] = None) -> Project:
    import yaml
    path = os.path.join(project_dir, "docker-compose.yml")
    if not os.path.exists(path):
        raise BackupError(f"{path} not found: run this from the project folder")
    doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    services = doc.get("services") or {}
    rag = services.get("rag") or {}
    app_image = rag.get("image")
    if not app_image:
        raise BackupError("docker-compose.yml: service 'rag' has no image name")
    redis = services.get("redis") or {}
    state: Dict[str, str] = {}
    has_models = False
    for entry in rag.get("volumes") or []:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        vol, mount = entry.split(":")[0], entry.split(":")[1]
        if "/" in vol or "." in vol[:1]:                    # a bind mount (./folder), not a named volume
            continue
        if vol == "models":
            has_models = True
        elif vol not in NOT_STATE_VOLUMES:
            state[vol] = mount
    return Project(os.path.abspath(project_dir), name or doc.get("name") or os.path.basename(os.path.abspath(project_dir)).lower(),
                   app_image, redis.get("image"), state, has_models, bool(redis))


def volume_exists(name: str) -> bool:
    code, _ = run(["docker", "volume", "inspect", name], ok_codes=(0, 1))
    return code == 0


def _image_user_ids(image: str, user: str) -> Optional[Tuple[int, int]]:
    try:
        _, out = run(["docker", "run", "--rm", "--entrypoint", "id", image, "-u", user])
        uid = int(out.split()[-1])
        _, out = run(["docker", "run", "--rm", "--entrypoint", "id", image, "-g", user])
        return uid, int(out.split()[-1])
    except (BackupError, ValueError):
        return None


# ------------------------------------------------------- helper containers ---
BACKUP_SCRIPT = r'''
import hashlib, json, os, shutil, sqlite3, sys
src, out = "/data", "/tmp/out"
os.makedirs(out, exist_ok=True)
files = {}
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
for root, dirs, names in os.walk(src):
    for name in sorted(names):
        if name.endswith(("-wal", "-shm", "-journal")):
            continue                              # these belong to a database that the backup call below copies as a whole
        p = os.path.join(root, name)
        rel = os.path.relpath(p, src).replace(os.sep, "/")
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        entry = {"kind": "raw"}
        if name.endswith((".db", ".sqlite", ".sqlite3")):
            s = sqlite3.connect(p, timeout=60)
            d = sqlite3.connect(dst)
            try:
                s.backup(d)                      # SQLite's online backup: a consistent copy even while another process writes
                d.execute("PRAGMA journal_mode=DELETE")
                entry = {"kind": "sqlite", "integrity": d.execute("PRAGMA integrity_check").fetchone()[0], "tables": {}}
                for (t,) in d.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                    entry["tables"][t] = d.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
            finally:
                d.close()
                s.close()
        else:
            shutil.copy2(p, dst)
        entry["size"] = os.path.getsize(dst)
        entry["sha256"] = sha(dst)
        files[rel] = entry
print("RESULT " + json.dumps(files))
'''

TAR_SCRIPT = r'''
import hashlib, json, os, tarfile
with tarfile.open("/tmp/models.tar.gz", "w:gz", compresslevel=1) as t:
    t.add("/data", arcname=".")
h = hashlib.sha256()
with open("/tmp/models.tar.gz", "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print("RESULT " + json.dumps({"size": os.path.getsize("/tmp/models.tar.gz"), "sha256": h.hexdigest()}))
'''

RESTORE_SCRIPT = r'''
import json, os, shutil, sqlite3, sys, tarfile
uid, gid, mode = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
data, inbox = "/data", "/tmp/in"
if mode == "tar":
    has_content = os.path.exists(os.path.join(inbox, "models.tar.gz"))
else:
    has_content = any(names for _, _, names in os.walk(inbox))
if not has_content:                                # NEVER empty a volume to put nothing back
    print("RESULT " + json.dumps({"ok": False, "error": "the backup holds no files for this volume: nothing was changed"}))
    sys.exit(3)
for name in os.listdir(data):                      # start from an empty volume: nothing left over from before
    p = os.path.join(data, name)
    shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
if mode == "tar":
    with tarfile.open(os.path.join(inbox, "models.tar.gz")) as t:
        t.extractall(data)
else:
    for root, dirs, names in os.walk(inbox):
        rel = os.path.relpath(root, inbox)
        target = data if rel == "." else os.path.join(data, rel)
        os.makedirs(target, exist_ok=True)
        for n in names:
            shutil.copy2(os.path.join(root, n), os.path.join(target, n))
for root, dirs, names in os.walk(data):
    for n in dirs + names:
        os.chown(os.path.join(root, n), uid, gid)
os.chown(data, uid, gid)
print("RESULT " + json.dumps({"ok": True}))
'''

CHECK_SQLITE_SCRIPT = r'''
import hashlib, json, os, sqlite3
out = {}
for root, dirs, names in os.walk("/data"):
    for n in sorted(names):
        p = os.path.join(root, n)
        rel = os.path.relpath(p, "/data").replace(os.sep, "/")
        h = hashlib.sha256()                                   # first the bytes, THEN (maybe) open it as a database
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        e = {"size": os.path.getsize(p), "sha256": h.hexdigest()}
        if n.endswith((".db", ".sqlite", ".sqlite3")):
            c = sqlite3.connect(p, timeout=30)
            try:
                e["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
                e["tables"] = {}
                for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                    e["tables"][t] = c.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
            finally:
                c.close()
        out[rel] = e
print("RESULT " + json.dumps(out))
'''


def _result(out: str) -> dict:
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    raise BackupError("the helper container printed no result: " + out[-300:])


class Helper:
    """A short-lived container that mounts one volume. Always removed, even on errors."""

    def __init__(self, image: str, volume: str, script: str, args: Tuple[str, ...] = (), user: Optional[str] = None, readonly: bool = False):
        cmd = ["docker", "create", "--entrypoint", "python", "-v", f"{volume}:/data" + (":ro" if readonly else "")]
        if user:
            cmd += ["--user", user]
        cmd += [image, "-c", script, *args]
        _, out = run(cmd)
        self.cid = out.strip().splitlines()[-1]

    def put(self, host_path: str, inside: str) -> None:
        run(["docker", "cp", host_path, f"{self.cid}:{inside}"])

    def start(self, timeout: float = 3600) -> str:
        _, out = run(["docker", "start", "-a", self.cid], timeout=timeout)
        return out

    def get(self, inside: str, host_path: str) -> None:
        run(["docker", "cp", f"{self.cid}:{inside}", host_path])

    def close(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.cid], capture_output=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ------------------------------------------------------------ files & hashes -
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_stamp(ts: Optional[float] = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts or time.time()))


def _write_json_atomic(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def list_backups(dest: str) -> List[str]:
    """Dated backup folders (newest last). Anything else in the folder is not ours and is left alone."""
    if not os.path.isdir(dest):
        return []
    return sorted(d for d in os.listdir(dest) if STAMP.match(d) and os.path.exists(os.path.join(dest, d, "MANIFEST.json")))


def apply_retention(dest: str, keep: int) -> List[str]:
    names = list_backups(dest)
    gone = names[:-keep] if keep > 0 and len(names) > keep else []
    for n in gone:
        shutil.rmtree(os.path.join(dest, n), ignore_errors=True)
    return gone


def backup_status(dest: str, now: Optional[float] = None) -> Tuple[str, Optional[float], str]:
    """('ok'|'missing'|'broken', age_in_seconds, text) from LATEST.json. This is what the watchdog looks at."""
    p = os.path.join(dest, "LATEST.json")
    if not os.path.exists(p):
        return "missing", None, "no backup has been made yet"
    try:
        d = json.load(open(p, encoding="utf-8"))
        created = float(d["created_at"])
    except Exception:
        return "broken", None, "LATEST.json cannot be read"
    return "ok", (now or time.time()) - created, d.get("folder", "")


# ----------------------------------------------------------------- verify ----
@dataclass
class Report:
    ok: bool = True
    lines: List[str] = field(default_factory=list)

    def good(self, text):
        self.lines.append("✅ " + text)

    def bad(self, text):
        self.ok = False
        self.lines.append("❌ " + text)

    def note(self, text):
        self.lines.append("⚠️  " + text)

    def summary(self) -> str:
        return "\n".join(self.lines)


def verify_backup(folder: str) -> Report:
    """Host-side check, no Docker: every file is there, its checksum matches, every SQLite file opens and has the recorded rows."""
    rep = Report()
    mpath = os.path.join(folder, "MANIFEST.json")
    if not os.path.exists(mpath):
        rep.bad(f"{folder}: no MANIFEST.json (not a backup folder)")
        return rep
    m = json.load(open(mpath, encoding="utf-8"))
    n_files = 0
    for part, info in (m.get("parts") or {}).items():
        files = info.get("files") or {}
        if not files:
            rep.bad(f"{part}: the backup holds no files for it (a restore would have nothing to put back)")
        for rel, e in files.items():
            path = os.path.join(folder, part, *rel.split("/"))
            n_files += 1
            if not os.path.exists(path):
                rep.bad(f"{part}/{rel}: file is missing")
                continue
            if os.path.getsize(path) != e["size"] or sha256_file(path) != e["sha256"]:
                rep.bad(f"{part}/{rel}: the file changed since it was saved (checksum differs)")
                continue
            if e.get("kind") == "sqlite":
                try:
                    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                    try:
                        integ = c.execute("PRAGMA integrity_check").fetchone()[0]
                        tabs = {t: c.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
                                for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                    finally:
                        c.close()
                except sqlite3.Error as ex:
                    rep.bad(f"{part}/{rel}: SQLite cannot open it ({type(ex).__name__})")
                    continue
                if integ != "ok" or e.get("integrity") != "ok":
                    rep.bad(f"{part}/{rel}: SQLite integrity check says {integ!r}")
                elif tabs != e.get("tables"):
                    rep.bad(f"{part}/{rel}: row counts differ from the manifest")
                else:
                    rep.good(f"{part}/{rel}: SQLite integrity ok, {sum(tabs.values())} rows in {len(tabs)} table(s)")
            else:
                rep.good(f"{part}/{rel}: {e['size']:,} bytes, checksum ok")
    r = (m.get("parts") or {}).get("redis")
    if r and r.get("files"):
        rep.good(f"redis: snapshot of {r.get('dbsize', '?')} key(s)")
    if n_files == 0:
        rep.bad("the backup contains no files")
    return rep


# ----------------------------------------------------------------- backup ----
def _redis_container(project: Project) -> str:
    _, out = run(["docker", "compose", "ps", "-q", "redis"], cwd=project.dir)
    cid = out.strip().splitlines()[0] if out.strip() else ""
    if not cid:
        raise BackupError("Redis is not running (start it with `docker compose up -d`, or back up with --no-redis)")
    return cid


def _backup_files_volume(project: Project, short: str, out_dir: str) -> dict:
    vol = project.vol(short)
    if not volume_exists(vol):
        raise BackupError(f"volume {vol} does not exist (has the stack ever been started?)")
    with Helper(project.app_image, vol, BACKUP_SCRIPT) as h:
        files = _result(h.start())
        os.makedirs(out_dir, exist_ok=True)
        h.get("/tmp/out/.", out_dir)
    return {"volume": vol, "files": files}


def _keyspace(cid: str) -> Tuple[int, int]:
    """(keys, keys-with-a-time-limit) in database 0, from INFO keyspace."""
    _, out = run(["docker", "exec", cid, "redis-cli", "INFO", "keyspace"])
    m = re.search(r"^db0:keys=(\d+),expires=(\d+)", out, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _backup_redis(project: Project, out_dir: str) -> dict:
    cid = _redis_container(project)
    k0, e0 = _keyspace(cid)
    run(["docker", "exec", cid, "redis-cli", "--rdb", "/tmp/backup.rdb"])
    k1, e1 = _keyspace(cid)
    _, size = run(["docker", "exec", cid, "redis-cli", "DBSIZE"])
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "dump.rdb")
    run(["docker", "cp", f"{cid}:/tmp/backup.rdb", dst])
    run(["docker", "exec", cid, "rm", "-f", "/tmp/backup.rdb"], ok_codes=(0, 1, 126, 127))
    with open(dst, "rb") as f:
        if f.read(5) != b"REDIS":
            raise BackupError("the Redis snapshot does not start with REDIS: not a valid RDB file")
    m = re.search(r"\d+", size)
    # The app keeps adding keys and some keys (rate limits, caches) run out by themselves, so the snapshot holds somewhere between
    # "the keys that never run out" and "the most keys seen": the restore check accepts anything in that range.
    return {"volume": project.vol("redis_data"), "dbsize": int(m.group()) if m else None,
            "keys_max": max(k0, k1), "persistent_min": max(0, min(k0 - e0, k1 - e1)),
            "files": {"dump.rdb": {"kind": "rdb", "size": os.path.getsize(dst), "sha256": sha256_file(dst)}}}


def backup_models(project: Project, dest: str) -> dict:
    vol = project.vol("models")
    if not volume_exists(vol):
        raise BackupError(f"volume {vol} does not exist")
    out_dir = os.path.join(dest, "models")
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, "models.tar.gz")
    with Helper(project.app_image, vol, TAR_SCRIPT, readonly=True) as h:
        info = _result(h.start(timeout=7200))
        h.get("/tmp/models.tar.gz", final + ".partial")
    if sha256_file(final + ".partial") != info["sha256"]:
        os.remove(final + ".partial")
        raise BackupError("the models copy was damaged on the way out (checksum differs)")
    os.replace(final + ".partial", final)
    meta = {"created_at": time.time(), "size": info["size"], "sha256": info["sha256"]}
    _write_json_atomic(os.path.join(out_dir, "META.json"), meta)
    return meta


def backup(project_dir: str = ".", dest: str = "backups", keep: int = 7, with_models: bool = False, redis: bool = True,
           project_name: Optional[str] = None, safety_copy: bool = False) -> Report:
    """safety_copy=True is used by restore(): the folder is called before_restore_<time>, is never deleted by the retention rule
    and does not count as 'the latest backup'."""
    rep = Report()
    problem = docker_ready()
    if problem:
        rep.bad(problem)
        return rep
    try:
        project = load_project(project_dir, project_name)
    except BackupError as e:
        rep.bad(str(e))
        return rep
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    stamp = _now_stamp()
    while os.path.exists(os.path.join(dest, stamp)) or os.path.exists(os.path.join(dest, "before_restore_" + stamp)):
        time.sleep(1)                                          # two backups in the same second would share a name
        stamp = _now_stamp()
    final = os.path.join(dest, ("before_restore_" if safety_copy else "") + stamp)
    work = final + ".partial"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    parts: Dict[str, dict] = {}
    t0 = time.time()
    try:
        for short in sorted(project.state_volumes):
            parts[short] = _backup_files_volume(project, short, os.path.join(work, short))
        if redis and project.has_redis:
            parts["redis"] = _backup_redis(project, os.path.join(work, "redis"))
        manifest = {"tool_version": TOOL_VERSION, "created_at": t0, "created_iso": datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="seconds"),
                    "project": project.name, "app_image": project.app_image, "parts": parts}
        _write_json_atomic(os.path.join(work, "MANIFEST.json"), manifest)
        check = verify_backup(work)
        if not check.ok:
            rep.lines += check.lines
            raise BackupError("the new backup failed its own check (see above); it was NOT kept")
        os.rename(work, final)
    except BackupError as e:
        shutil.rmtree(work, ignore_errors=True)
        rep.bad(str(e))
        return rep
    except Exception as e:                                   # never leave a half-written folder behind
        shutil.rmtree(work, ignore_errors=True)
        rep.bad(f"unexpected {type(e).__name__} while backing up")
        return rep
    rep.lines += check.lines
    if not safety_copy:
        _write_json_atomic(os.path.join(dest, "LATEST.json"), {"folder": stamp, "created_at": t0, "parts": sorted(parts), "verified": True})
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(final) for f in fs)
    rep.good(f"backup {stamp} saved and verified ({size / 1024 ** 2:.1f} MB, {time.time() - t0:.0f}s) in {final}")
    if with_models and project.has_models:
        try:
            meta = backup_models(project, dest)
            rep.good(f"models copied ({meta['size'] / 1024 ** 2:.0f} MB, one copy, replaced each time)")
        except BackupError as e:
            rep.bad("models: " + str(e))
    gone = [] if safety_copy else apply_retention(dest, keep)
    if gone:
        rep.lines.append(f"🧹 removed {len(gone)} older backup(s), keeping the newest {keep}")
    return rep


# ------------------------------------------------------- restore and drill ---
def _restore_files(project: Project, image_for_helper: str, volume: str, source: str, uid: int, gid: int, mode: str = "files") -> None:
    with Helper(image_for_helper, volume, RESTORE_SCRIPT, (str(uid), str(gid), mode), user="root") as h:
        h.put(source, "/tmp/in")
        _result(h.start())


def _wait(fn, seconds: float, what: str):
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            if fn():
                return
        except BackupError as e:
            last = str(e)
        time.sleep(0.5)
    raise BackupError(f"timed out waiting for {what}. {last}"[:400])


def _restore_redis(project: Project, volume: str, folder: str) -> None:
    """RDB -> a volume that Redis starts from with `--appendonly yes` (the compose setting). Redis 7 ignores a lone dump.rdb when the
    append-only file is on, so: load the RDB with AOF off, switch AOF on (Redis writes the append-only file from memory), shut down."""
    ids = _image_user_ids(project.redis_image, "redis") or (0, 0)
    _restore_files(project, project.app_image, volume, os.path.join(folder, "redis"), ids[0], ids[1])
    name = f"{project.name}-restore-redis-{uuid.uuid4().hex[:8]}"
    run(["docker", "run", "-d", "--name", name, "-v", f"{volume}:/data", project.redis_image, "redis-server",
         "--appendonly", "no", "--save", "", "--dir", "/data", "--dbfilename", "dump.rdb"])
    try:
        _wait(lambda: run(["docker", "exec", name, "redis-cli", "ping"], ok_codes=(0, 1))[1].endswith("PONG"), 60, "Redis to load the snapshot")
        run(["docker", "exec", name, "redis-cli", "CONFIG", "SET", "appendonly", "yes"])

        def aof_ready():
            info = run(["docker", "exec", name, "redis-cli", "INFO", "persistence"])[1]
            return "aof_rewrite_in_progress:0" in info and "aof_enabled:1" in info and "aof_last_bgrewrite_status:ok" in info
        _wait(aof_ready, 60, "Redis to write its append-only file")
        subprocess.run(["docker", "exec", name, "redis-cli", "SHUTDOWN", "NOSAVE"], capture_output=True, timeout=60)   # the connection dies with Redis: any exit code is normal
        run(["docker", "wait", name], timeout=60)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _restore_all(project: Project, folder: str, volumes: Dict[str, str], rep: Report, models: bool = False) -> None:
    m = json.load(open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8"))
    app_ids = _image_user_ids(project.app_image, "app")
    if not app_ids:
        raise BackupError("cannot find the 'app' user in the image: run `docker compose build` first")
    for short, info in (m.get("parts") or {}).items():
        if not info.get("files"):
            raise BackupError(f"the backup holds no files for {short}: your {short} volume was not touched")
        if short == "redis":
            _restore_redis(project, volumes["redis_data"], folder)
            rep.good("redis: snapshot loaded and the append-only file written")
        else:
            _restore_files(project, project.app_image, volumes[short], os.path.join(folder, short), *app_ids)
            rep.good(f"{short}: files restored with the app user as owner")


def _check_restored(project: Project, folder: str, volumes: Dict[str, str], rep: Report) -> None:
    m = json.load(open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8"))
    for short, info in (m.get("parts") or {}).items():
        if short == "redis":
            name = f"{project.name}-check-redis-{uuid.uuid4().hex[:8]}"
            run(["docker", "run", "-d", "--name", name, "-v", f"{volumes['redis_data']}:/data", project.redis_image, "redis-server", "--appendonly", "yes"])
            try:
                _wait(lambda: run(["docker", "exec", name, "redis-cli", "ping"], ok_codes=(0, 1))[1].endswith("PONG"), 60, "Redis to start on the restored volume")
                _, size = run(["docker", "exec", name, "redis-cli", "DBSIZE"])
                got = int(re.search(r"\d+", size).group())
                low, high, saved = info.get("persistent_min"), info.get("keys_max"), info.get("dbsize")
                if got == saved:
                    rep.good(f"redis: started the way compose starts it (--appendonly yes) and holds {got} key(s), as saved")
                elif low is not None and high is not None and low <= got <= high:
                    rep.good(f"redis: started the way compose starts it (--appendonly yes) and holds {got} key(s) (between {low} that never run out and {high} at most "
                             "were saved: keys with a time limit, like rate limits, run out by themselves)")
                else:
                    rep.bad(f"redis: {got} key(s) after restore, {saved} were saved" + (f" (at least {low} must survive)" if low is not None else ""))
            finally:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        else:
            with Helper(project.app_image, volumes[short], CHECK_SQLITE_SCRIPT) as h:
                found = _result(h.start())
            for rel, e in info["files"].items():
                f = found.get(rel)
                if not f:
                    rep.bad(f"{short}/{rel}: the file is not in the restored volume")
                    continue
                if f.get("sha256") != e.get("sha256") or f.get("size") != e.get("size"):
                    rep.bad(f"{short}/{rel}: the restored file is not the saved one (checksum differs)")
                    continue
                if e.get("kind") != "sqlite":
                    rep.good(f"{short}/{rel}: restored, {f['size']:,} bytes, checksum equal to the saved one")
                    continue
                if f["integrity"] != "ok" or f["tables"] != e["tables"]:
                    rep.bad(f"{short}/{rel}: restored database differs (integrity {f['integrity']!r}, rows {sum(f['tables'].values())} vs {sum(e['tables'].values())})")
                else:
                    rep.good(f"{short}/{rel}: opened inside the app image; integrity ok, {sum(f['tables'].values())} rows, as saved")


def drill(project_dir: str = ".", folder: Optional[str] = None, dest: str = "backups", project_name: Optional[str] = None) -> Report:
    """Restore the newest (or the given) backup into THROWAWAY volumes, check the data came back, delete the volumes."""
    rep = Report()
    problem = docker_ready()
    if problem:
        rep.bad(problem)
        return rep
    try:
        project = load_project(project_dir, project_name)
        dest = os.path.abspath(dest)
        if folder is None:
            names = list_backups(dest)
            if not names:
                raise BackupError("there is no backup to practise on: run a backup first")
            folder = os.path.join(dest, names[-1])
        folder = os.path.abspath(folder)
        rep.lines.append(f"practising on {folder}")
        check = verify_backup(folder)
        if not check.ok:
            rep.lines += check.lines
            raise BackupError("the backup does not pass its own check, so it cannot be restored (see above)")
        m = json.load(open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8"))
        tag = uuid.uuid4().hex[:8]
        volumes = {}
        for short in m["parts"]:
            volumes["redis_data" if short == "redis" else short] = f"{project.name}-drill-{tag}-{short}"
        created = []
        try:
            for v in volumes.values():
                run(["docker", "volume", "create", "--label", "rag-drill=1", v])
                created.append(v)
            _restore_all(project, folder, volumes, rep)
            _check_restored(project, folder, volumes, rep)
        finally:
            for v in created:
                subprocess.run(["docker", "volume", "rm", "-f", v], capture_output=True)
            left = [v for v in created if volume_exists(v)]
            if left:
                rep.bad("could not delete the throwaway volume(s): " + ", ".join(left))
            else:
                rep.lines.append("🧹 throwaway volumes deleted; your real volumes were never touched")
    except BackupError as e:
        rep.bad(str(e))
    try:
        _write_json_atomic(os.path.join(os.path.abspath(dest), "LAST_DRILL.json"),
                           {"ok": rep.ok, "at": time.time(), "folder": os.path.basename(folder) if folder else None})
    except OSError:
        pass
    return rep


def restore(folder: str, project_dir: str = ".", dest: str = "backups", safety_copy: bool = True, project_name: Optional[str] = None) -> Report:
    """The real restore. Stops the stack (`docker compose down`: data volumes are kept), saves what is there now as a safety copy,
    then replaces the contents of your volumes with the backup. Start again with `docker compose up -d`."""
    rep = Report()
    problem = docker_ready()
    if problem:
        rep.bad(problem)
        return rep
    try:
        project = load_project(project_dir, project_name)
        folder = os.path.abspath(folder)
        check = verify_backup(folder)
        if not check.ok:
            rep.lines += check.lines
            raise BackupError("the backup does not pass its own check: nothing was changed")
        m = json.load(open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8"))
        if m.get("project") and m["project"] != project.name:
            raise BackupError(f"this backup was made by the project {m['project']!r} but this folder is the project {project.name!r}: nothing was changed "
                              f"(if you really mean it: --project-name {m['project']})")
        if safety_copy:
            run(["docker", "compose", "stop", "rag"], cwd=project.dir, ok_codes=(0, 1))          # nothing writes while the safety copy is made
            run(["docker", "compose", "up", "-d", "redis"], cwd=project.dir, ok_codes=(0, 1))     # Redis must be up to be copied
            time.sleep(3)
            s = backup(project_dir, dest, project_name=project_name, safety_copy=True)
            if not s.ok:
                rep.lines += s.lines
                raise BackupError("could not save the current state first, so nothing was changed. (Use --no-safety-copy only if you accept losing it.)")
            rep.good("current state saved first: " + s.lines[-1].split(" in ")[-1])
        run(["docker", "compose", "down"], cwd=project.dir)
        volumes = {}
        for short in m["parts"]:
            key = "redis_data" if short == "redis" else short
            volumes[key] = project.vol(key)
            if not volume_exists(volumes[key]):
                run(["docker", "volume", "create", "--label", f"com.docker.compose.project={project.name}",
                     "--label", f"com.docker.compose.volume={key}", volumes[key]])
        _restore_all(project, folder, volumes, rep)
        _check_restored(project, folder, volumes, rep)
        if rep.ok:
            rep.lines.append("Now start everything again:  docker compose up -d")
    except BackupError as e:
        rep.bad(str(e))
    return rep


def restore_models(project_dir: str = ".", dest: str = "backups", project_name: Optional[str] = None) -> Report:
    rep = Report()
    try:
        project = load_project(project_dir, project_name)
        tar = os.path.join(os.path.abspath(dest), "models", "models.tar.gz")
        meta_p = os.path.join(os.path.abspath(dest), "models", "META.json")
        if not os.path.exists(tar) or not os.path.exists(meta_p):
            raise BackupError("no models copy found (make one with: python backup_tools.py backup --with-models)")
        meta = json.load(open(meta_p, encoding="utf-8"))
        if sha256_file(tar) != meta["sha256"]:
            raise BackupError("the models copy is damaged (checksum differs)")
        run(["docker", "compose", "stop", "rag"], cwd=project.dir, ok_codes=(0, 1))
        vol = project.vol("models")
        if not volume_exists(vol):
            run(["docker", "volume", "create", "--label", f"com.docker.compose.project={project.name}", "--label", "com.docker.compose.volume=models", vol])
        ids = _image_user_ids(project.app_image, "app")
        if not ids:
            raise BackupError("cannot find the 'app' user in the image")
        with Helper(project.app_image, vol, RESTORE_SCRIPT, (str(ids[0]), str(ids[1]), "tar"), user="root") as h:
            tmp = tempfile.mkdtemp()
            try:
                shutil.copy(tar, os.path.join(tmp, "models.tar.gz"))
                h.put(tmp, "/tmp/in")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            _result(h.start(timeout=7200))
        rep.good("models volume restored. Start again:  docker compose up -d")
    except BackupError as e:
        rep.bad(str(e))
    return rep


# ------------------------------------------------------------- scheduling ---
TASK_NAME = "rag-backup"
WRAPPER = "run_backup.cmd"
PS_SETTINGS = ("$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
               "Set-ScheduledTask -TaskName '{name}' -Settings $s | Out-Null")


def _check_time(at: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", at or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise BackupError("the time must look like 13:00 (hours 0-23, minutes 00-59)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _check_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name or ""):
        raise BackupError("the task name may only hold letters, digits, - and _")
    return name


def wrapper_text(project_dir: str, python_exe: str) -> str:
    """The small file Windows runs every day. All the quoting lives in this file, so the task command itself stays short and simple
    (the task command may only be 261 characters long, and PowerShell and Command Prompt quote it differently)."""
    for part in (project_dir, python_exe):
        if any(c in part for c in '"%\r\n'):
            raise BackupError("the project folder or the python path contains a quote or a % sign, which a Windows batch file cannot take safely: use a plain folder name")
        if not part.isascii():
            raise BackupError("the project folder or the python path contains letters with accents or other special characters, which a Windows batch file can get wrong: use a plain folder name")
    env_dir = ntpath.dirname(python_exe)
    lines = ["@echo off",
             "rem Written by:  python backup_tools.py schedule --install     Windows runs this file once a day (task " + TASK_NAME + ").",
             "rem The next line does what 'conda activate' does for the program search path (a conda python started without it can miss its own DLLs).",
             f'set "PATH={env_dir};{env_dir}\\Library\\mingw-w64\\bin;{env_dir}\\Library\\usr\\bin;{env_dir}\\Library\\bin;{env_dir}\\Scripts;%PATH%"',
             "set PYTHONUTF8=1",
             f'cd /d "{project_dir}"',
             "if not exist backups mkdir backups",
             f'"{python_exe}" backup_tools.py backup >> backups\\backup_task.log 2>&1',
             "exit /b %errorlevel%"]
    return "\r\n".join(lines) + "\r\n"


def schedule_argv(wrapper_path: str, at: str, name: str = TASK_NAME) -> List[str]:
    """The task command as a LIST (no shell quoting involved). The task's command is the wrapper file in quotes (it may hold spaces)."""
    if len(wrapper_path) + 2 > 261:
        raise BackupError("the path of the project folder is too long for a Windows task (the limit is 261 characters): move the project to a shorter folder name")
    return ["schtasks", "/Create", "/SC", "DAILY", "/ST", _check_time(at), "/TN", _check_name(name), "/TR", f'"{wrapper_path}"', "/F"]


def schedule_command(project_dir: str = ".", python_exe: Optional[str] = None, at: str = "13:00", name: str = TASK_NAME) -> str:
    """The same command as TEXT for Command Prompt (cmd.exe; not PowerShell). It only BUILDS the text; nothing is scheduled here.
    `python backup_tools.py schedule --install` does it without any quoting by you."""
    win = bool(re.match(r"^[A-Za-z]:[\\/]", project_dir))
    proj = project_dir.rstrip("\\/") if win else os.path.abspath(project_dir)
    sep = "\\" if win else os.sep
    wrapper = f"{proj}{sep}{WRAPPER}"
    argv = schedule_argv(wrapper, at, name)                                        # validates the time, the name and the length
    return f'schtasks /Create /SC DAILY /ST {argv[5]} /TN {argv[7]} /TR "\\"{wrapper}\\"" /F'


def install_schedule(project_dir: str = ".", python_exe: Optional[str] = None, at: str = "13:00", name: str = TASK_NAME,
                     runner: Callable = subprocess.run, windows: Optional[bool] = None) -> Report:
    """Writes run_backup.cmd next to docker-compose.yml and asks Windows to run it every day at `at` (Task Scheduler)."""
    rep = Report()
    if not (os.name == "nt" if windows is None else windows):
        rep.bad("this sets up a WINDOWS task: run it on the Windows computer where Docker Desktop runs (on Linux or macOS use cron)")
        return rep
    try:
        proj = os.path.abspath(project_dir)
        wrapper = os.path.join(proj, WRAPPER)
        argv = schedule_argv(wrapper, at, name)                                    # validates the time, the name and the length first
        with open(wrapper, "w", newline="") as f:                                  # newline="": keep the Windows line endings as written
            f.write(wrapper_text(proj, python_exe or sys.executable))
        rep.good(f"wrote {WRAPPER} next to docker-compose.yml (it runs the backup and writes backups\\backup_task.log)")
        r = runner(argv, capture_output=True, text=True, errors="replace", timeout=120)
        if r.returncode != 0:
            raise BackupError("Windows refused the task: " + (r.stdout + r.stderr).strip()[-300:])
        rep.good(f"task '{name}' created: a backup every day at {_check_time(at)}  (check: schtasks /Query /TN {name}   remove: schtasks /Delete /TN {name} /F)")
        ps = ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SETTINGS.format(name=name)]
        try:
            r2 = runner(ps, capture_output=True, text=True, errors="replace", timeout=120)
            failed = r2.returncode != 0
        except (OSError, subprocess.SubprocessError):
            failed = True
        if failed:
            rep.lines.append("⚠️  could not change two task options. Open Task Scheduler > rag-backup > Properties. Conditions: untick 'Start the task only if the "
                             "computer is on AC power'. Settings: tick 'Run task as soon as possible after a scheduled start is missed'. Without them a laptop on "
                             "battery, or switched off at that hour, skips the backup.")
        else:
            rep.good("it also runs on battery power, and as soon as possible after a missed start (computer was off)")
        rep.lines.append("Docker Desktop must be running at that time. If a backup is missed for 2 days the watchdog tells your phone.")
    except (BackupError, OSError, subprocess.SubprocessError) as e:
        rep.bad(str(e) if isinstance(e, BackupError) else f"{type(e).__name__}: {e}")
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["backup", "list", "verify", "drill", "restore", "restore-models", "schedule"])
    ap.add_argument("folder", nargs="?")
    ap.add_argument("--dir", default="backups", help="where backups live (default ./backups)")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--keep", type=int, default=7)
    ap.add_argument("--with-models", action="store_true")
    ap.add_argument("--no-redis", action="store_true")
    ap.add_argument("--yes", action="store_true", help="restore / restore-models: really do it")
    ap.add_argument("--project-name", default=None, help="the compose project name (default: the name: line of docker-compose.yml)")
    ap.add_argument("--no-safety-copy", action="store_true")
    ap.add_argument("--at", default="13:00", help="schedule: the time of day (a time your computer is usually on, with Docker running)")
    ap.add_argument("--install", action="store_true", help="schedule: really create the Windows task (default: only show the command)")
    a = ap.parse_args(argv)
    if a.command == "schedule":
        if a.install:
            rep = install_schedule(a.project_dir, None, a.at)
            print(rep.summary())
            return 0 if rep.ok else 1
        try:
            print(schedule_command(a.project_dir, None, a.at))
        except BackupError as e:
            print(f"❌ {e}")
            return 1
        print("\nThat is the command for Command Prompt (cmd.exe, NOT PowerShell). Easier and safer: let this program do it for you:\n"
              "    python backup_tools.py schedule --install --at " + a.at)
        return 0
    if a.command == "list":
        names = list_backups(os.path.abspath(a.dir))
        st = backup_status(os.path.abspath(a.dir))
        for n in names:
            print(n)
        print(f"{len(names)} backup(s). Newest is {st[1] / 3600:.1f} h old." if st[0] == "ok" else f"{len(names)} backup(s). {st[2]}")
        return 0
    if a.command == "verify":
        target = a.folder or os.path.join(os.path.abspath(a.dir), (list_backups(os.path.abspath(a.dir)) or ["?"])[-1])
        rep = verify_backup(target)
    elif a.command == "backup":
        rep = backup(a.project_dir, a.dir, a.keep, a.with_models, not a.no_redis, project_name=a.project_name)
    elif a.command == "drill":
        rep = drill(a.project_dir, a.folder, a.dir, project_name=a.project_name)
    elif a.command == "restore-models":
        if not a.yes:
            print("restore-models needs --yes. It REPLACES everything in your models volume with the saved copy (the models are downloaded again if you do not have one).")
            return 2
        rep = restore_models(a.project_dir, a.dir, project_name=a.project_name)
    else:
        if not a.folder or not a.yes:
            print("restore needs a folder and --yes. It REPLACES the contents of your data volumes (a safety copy is taken first).")
            return 2
        rep = restore(a.folder, a.project_dir, a.dir, safety_copy=not a.no_safety_copy, project_name=a.project_name)
    print(rep.summary())
    return 0 if rep.ok else 1


def _utf8_stdio():
    """Windows hands a program a pipe that only knows cp1252, which cannot print the marks used here: ask for UTF-8 instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _utf8_stdio()
    raise SystemExit(main())
