"""Tests for backup_tools.py.  Run:  python test_backup.py

Unit tests need nothing. The live tests use REAL Docker: a small throwaway compose project (its own name, volumes and Redis) is
created, filled, backed up, damaged, restored and removed. They need Docker running and the images your Day 19/20 stack already
uses (rag-api:local, redis:7-alpine). Without them the live tests are reported as SKIP.
"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backup_tools as bt

results = []


class Skip(Exception):
    pass


def test(fn):
    try:
        fn()
        results.append((fn.__name__, "pass", ""))
    except Skip as e:
        results.append((fn.__name__, "skip", str(e)))
    except Exception:
        results.append((fn.__name__, "fail", traceback.format_exc(limit=6)))
    return fn


def sh(*cmd, cwd=None, check=True, timeout=300):
    r = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"{cmd[:4]} -> {r.returncode}: {(r.stdout + r.stderr)[-400:]}")
    return (r.stdout + r.stderr).strip()


def docker_and_images():
    if bt.docker_ready():
        return "Docker is not running"
    have = sh("docker", "images", "--format", "{{.Repository}}:{{.Tag}}", check=False).split()
    for img in ("rag-api:local", "redis:7-alpine"):
        if img not in have:
            return f"image {img} is not on this computer yet"
    return None


COMPOSE = """name: {name}
services:
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redis_data:/data
  rag:
    image: rag-api:local
    command: ["python", "-c", "import time; time.sleep(100000)"]
    volumes:
      - models:/models
      - checkpoints:/app/checkpoints
volumes:
  models:
  checkpoints:
  redis_data:
"""


class Mini:
    """A throwaway compose project with a SQLite database (WAL mode) in `checkpoints` and keys in Redis."""

    def __init__(self, rows=300, keys=7):
        why = docker_and_images()
        if why:
            raise Skip(why)
        self.name = "rag-bkt" + uuid.uuid4().hex[:6]
        self.dir = tempfile.mkdtemp(prefix="bkt_")
        self.dest = os.path.join(self.dir, "backups")
        open(os.path.join(self.dir, "docker-compose.yml"), "w").write(COMPOSE.format(name=self.name))
        sh("docker", "compose", "up", "-d", "redis", cwd=self.dir)
        sh("docker", "compose", "create", "rag", cwd=self.dir)
        self.py("""
import sqlite3
c = sqlite3.connect('/data/rag_checkpoints.db'); c.execute('PRAGMA journal_mode=WAL')
c.execute('create table checkpoints(id integer primary key, thread text, blob text)')
c.execute('create table writes(id integer primary key, v text)')
c.executemany('insert into checkpoints(thread, blob) values (?,?)', [('t' + str(i), 'x' * 200) for i in range(__ROWS__)])
c.executemany('insert into writes(v) values (?)', [('w' + str(i),) for i in range(20)])
c.commit()
""".replace("__ROWS__", str(rows)))
        self.wait_redis()
        for i in range(keys):
            self.redis("SET", f"key{i}", f"value{i}")
        self.keys = keys
        self.rows = rows

    def py(self, code, volume="checkpoints", user=None):
        cmd = ["docker", "run", "--rm", "--entrypoint", "python", "-v", f"{self.name}_{volume}:/data"]
        if user:
            cmd += ["--user", user]
        return sh(*cmd, "rag-api:local", "-c", code)

    def wait_redis(self):
        for _ in range(60):
            if sh("docker", "compose", "exec", "-T", "redis", "redis-cli", "ping", cwd=self.dir, check=False).endswith("PONG"):
                return
            time.sleep(0.5)
        raise AssertionError("redis did not start")

    def redis(self, *args):
        return sh("docker", "compose", "exec", "-T", "redis", "redis-cli", *args, cwd=self.dir)

    def count_rows(self, table="checkpoints"):
        return int(self.py(f"import sqlite3; print(sqlite3.connect('/data/rag_checkpoints.db').execute('select count(*) from {table}').fetchone()[0])").splitlines()[-1])

    def backup(self, **kw):
        return bt.backup(self.dir, self.dest, **kw)

    def newest(self):
        return os.path.join(self.dest, bt.list_backups(self.dest)[-1])

    def left_over(self):
        vols = sh("docker", "volume", "ls", "--format", "{{.Name}}").split()
        ctrs = sh("docker", "ps", "-a", "--format", "{{.Names}}").split()
        return [v for v in vols if "drill" in v and self.name in v] + [c for c in ctrs if self.name in c and ("restore-redis" in c or "check-redis" in c)]

    def close(self):
        for c in sh("docker", "ps", "-aq", "--filter", f"name={self.name}", check=False).split():      # helper containers first: they hold the network
            sh("docker", "rm", "-f", c, check=False)
        sh("docker", "compose", "down", "-v", "--remove-orphans", cwd=self.dir, check=False)
        for c in sh("docker", "ps", "-aq", "--filter", f"name={self.name}", check=False).split():
            sh("docker", "rm", "-f", c, check=False)
        for v in sh("docker", "volume", "ls", "-q", "--filter", f"name={self.name}", check=False).split():
            sh("docker", "volume", "rm", "-f", v, check=False)
        for n in sh("docker", "network", "ls", "-q", "--filter", f"name={self.name}", check=False).split():
            sh("docker", "network", "rm", n, check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


def live(fn):
    def run():
        m = Mini.__new__(Mini)
        try:
            Mini.__init__(m)
            fn(m)
        finally:
            if hasattr(m, "dir"):
                m.close()
    run.__name__ = fn.__name__
    return run


# ------------------------------------------------------------------ unit ----
def make_fake_backup(root, stamp="20260101-120000", rows=5):
    folder = os.path.join(root, stamp)
    os.makedirs(os.path.join(folder, "checkpoints"))
    p = os.path.join(folder, "checkpoints", "a.db")
    c = sqlite3.connect(p)
    c.execute("create table t(x)")
    c.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    c.commit()
    c.close()
    manifest = {"created_at": 1.0, "parts": {"checkpoints": {"files": {"a.db": {"kind": "sqlite", "integrity": "ok", "tables": {"t": rows},
                "size": os.path.getsize(p), "sha256": bt.sha256_file(p)}}}}}
    json.dump(manifest, open(os.path.join(folder, "MANIFEST.json"), "w"))
    return folder


@test
def u01_verify_accepts_a_good_backup_and_names_each_kind_of_damage():
    root = tempfile.mkdtemp()
    try:
        f = make_fake_backup(root)
        assert bt.verify_backup(f).ok
        p = os.path.join(f, "checkpoints", "a.db")
        raw = open(p, "rb").read()
        open(p, "wb").write(raw[:-1] + bytes([raw[-1] ^ 1]))              # one flipped bit
        r = bt.verify_backup(f)
        assert not r.ok and "checksum differs" in r.summary(), r.summary()
        open(p, "wb").write(raw)
        assert bt.verify_backup(f).ok
        os.remove(p)
        assert "file is missing" in bt.verify_backup(f).summary()
        shutil.rmtree(os.path.join(f, "checkpoints"))
        os.makedirs(os.path.join(f, "checkpoints"))
        assert not bt.verify_backup(os.path.join(root, "nothing")).ok
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def u02_verify_catches_a_database_whose_rows_do_not_match_the_manifest():
    root = tempfile.mkdtemp()
    try:
        f = make_fake_backup(root, rows=5)
        m = json.load(open(os.path.join(f, "MANIFEST.json")))
        m["parts"]["checkpoints"]["files"]["a.db"]["tables"] = {"t": 6}
        json.dump(m, open(os.path.join(f, "MANIFEST.json"), "w"))
        r = bt.verify_backup(f)
        assert not r.ok and "row counts differ" in r.summary()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def u03_a_corrupt_sqlite_file_is_not_called_ok_even_when_its_checksum_was_recorded_after_the_damage():
    root = tempfile.mkdtemp()
    try:
        f = make_fake_backup(root)
        p = os.path.join(f, "checkpoints", "a.db")
        open(p, "wb").write(b"this is not a database" * 100)
        m = json.load(open(os.path.join(f, "MANIFEST.json")))
        e = m["parts"]["checkpoints"]["files"]["a.db"]
        e["size"], e["sha256"] = os.path.getsize(p), bt.sha256_file(p)
        json.dump(m, open(os.path.join(f, "MANIFEST.json"), "w"))
        assert not bt.verify_backup(f).ok
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def u04_retention_keeps_the_newest_and_leaves_unknown_folders_alone():
    root = tempfile.mkdtemp()
    try:
        for s in ("20260101-000001", "20260102-000001", "20260103-000001", "20260104-000001"):
            make_fake_backup(root, s)
        os.makedirs(os.path.join(root, "my_own_notes"))
        os.makedirs(os.path.join(root, "before_restore_20260101-000000"))
        os.makedirs(os.path.join(root, "models"))
        gone = bt.apply_retention(root, 2)
        assert gone == ["20260101-000001", "20260102-000001"], gone
        assert bt.list_backups(root) == ["20260103-000001", "20260104-000001"]
        for keepme in ("my_own_notes", "before_restore_20260101-000000", "models"):
            assert os.path.isdir(os.path.join(root, keepme)), keepme
        assert bt.apply_retention(root, 0) == []                              # 0 = keep everything
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def u05_backup_status_reads_latest_json_and_says_what_is_wrong():
    root = tempfile.mkdtemp()
    try:
        assert bt.backup_status(root)[0] == "missing"
        open(os.path.join(root, "LATEST.json"), "w").write("{broken")
        assert bt.backup_status(root)[0] == "broken"
        json.dump({"folder": "x", "created_at": 1000.0}, open(os.path.join(root, "LATEST.json"), "w"))
        st, age, _ = bt.backup_status(root, now=1000.0 + 7200)
        assert st == "ok" and age == 7200
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def u06_project_is_read_from_the_compose_file():
    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "docker-compose.yml"), "w").write(COMPOSE.format(name="rag-api").replace(
            "      - checkpoints:/app/checkpoints", "      - checkpoints:/app/checkpoints\n      - ./notes:/app/notes\n      - sessions:/app/sessions").replace(
            "volumes:\n  models:", "volumes:\n  sessions:\n  models:"))
        p = bt.load_project(d)
        assert p.name == "rag-api" and p.app_image == "rag-api:local" and p.redis_image == "redis:7-alpine"
        assert p.state_volumes == {"checkpoints": "/app/checkpoints", "sessions": "/app/sessions"}, p.state_volumes
        assert p.has_models and p.has_redis and p.vol("checkpoints") == "rag-api_checkpoints"
        os.remove(os.path.join(d, "docker-compose.yml"))
        try:
            bt.load_project(d)
            assert False
        except bt.BackupError as e:
            assert "not found" in str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def u07_the_task_command_is_short_quotes_paths_with_spaces_and_refuses_what_windows_cannot_take():
    proj = r"C:\Users\rodne\Desktop\4 MONTHS JOURNEY\rag-project"
    cmd = bt.schedule_command(proj, r"C:\Users\rodne\miniconda3\envs\rag-Ai\python.exe", "13:00")
    assert cmd == r'schtasks /Create /SC DAILY /ST 13:00 /TN rag-backup /TR "\"' + proj + r'\run_backup.cmd\"" /F', cmd
    argv = bt.schedule_argv(proj + r"\run_backup.cmd", "9:05")
    assert argv[:8] == ["schtasks", "/Create", "/SC", "DAILY", "/ST", "09:05", "/TN", "rag-backup"] and argv[-1] == "/F"
    assert argv[9] == '"' + proj + r'\run_backup.cmd"', "the task command is the wrapper file, in quotes, nothing else"
    for bad in ("25", "1pm", "13:0", "13:00; calc", "24:00", "12:60", ""):
        try:
            bt.schedule_command(".", "python", bad)
            assert False, bad
        except bt.BackupError:
            pass
    for bad in ("rag backup", 'x"y', "a;b", ""):
        try:
            bt.schedule_argv("C:\\x.cmd", "13:00", bad)
            assert False, bad
        except bt.BackupError:
            pass
    try:
        bt.schedule_command("C:\\" + "very-long-folder-name\\" * 12 + "x", "C:\\python.exe")
        assert False
    except bt.BackupError as e:
        assert "too long" in str(e)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bt.main(["schedule", "--at", "02:30"])
    assert rc == 0 and "schtasks /Create" in buf.getvalue() and "02:30" in buf.getvalue() and "--install" in buf.getvalue()


@test
def u07b_the_wrapper_file_holds_all_the_quoting_and_refuses_unsafe_paths():
    proj = r"C:\Users\rodne\Desktop\4 MONTHS JOURNEY\rag-project"
    py = r"C:\Users\rodne\miniconda3\envs\rag-Ai\python.exe"
    text = bt.wrapper_text(proj, py)
    assert text.count("\r\n") == len(text.splitlines()) and "\n" not in text.replace("\r\n", ""), "Windows line endings throughout"
    assert f'cd /d "{proj}"' in text and f'"{py}" backup_tools.py backup >> backups\\backup_task.log 2>&1' in text and "set PYTHONUTF8=1" in text
    env_dir = r"C:\Users\rodne\miniconda3\envs\rag-Ai"
    assert f'set "PATH={env_dir};{env_dir}\\Library\\mingw-w64\\bin;{env_dir}\\Library\\usr\\bin;{env_dir}\\Library\\bin;{env_dir}\\Scripts;%PATH%"' in text, text
    for bad in ('C:\\a"b', "C:\\100%\\x", "C:\\a\r\nb", "C:\\Users\\Zoë\\project"):
        try:
            bt.wrapper_text(bad, py)
            assert False, bad
        except bt.BackupError:
            pass


@test
def u07c_install_writes_the_wrapper_asks_windows_without_a_shell_and_reports_what_happened():
    d = tempfile.mkdtemp()
    calls = []
    try:
        def runner(argv, **kw):
            calls.append(argv)
            return type("R", (), {"returncode": 0, "stdout": "SUCCESS", "stderr": ""})()
        rep = bt.install_schedule(d, "python.exe", "13:00", runner=runner, windows=True)
        assert rep.ok, rep.summary()
        w = os.path.join(d, "run_backup.cmd")
        assert open(w, "rb").read().startswith(b"@echo off\r\n"), "line endings must stay Windows ones"
        assert calls[0][0] == "schtasks" and calls[0][8] == "/TR" and calls[0][9] == '"' + w + '"' and calls[0][10] == "/F", calls[0]
        assert isinstance(calls[0], list), "no shell string: nothing for PowerShell or cmd to quote differently"
        assert calls[1][0] == "powershell" and "AllowStartIfOnBatteries" in calls[1][-1] and "rag-backup" in calls[1][-1]
        assert "also runs on battery" in rep.summary()
        # Windows refuses
        calls.clear()
        rep = bt.install_schedule(d, "python.exe", "13:00", runner=lambda argv, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": "ERROR: Access is denied."})(), windows=True)
        assert not rep.ok and "Windows refused the task: ERROR: Access is denied." in rep.summary()
        # the two extra options cannot be set: the task is still made, and the person is told what to click
        def runner2(argv, **kw):
            return type("R", (), {"returncode": 0 if argv[0] == "schtasks" else 1, "stdout": "", "stderr": ""})()
        rep = bt.install_schedule(d, "python.exe", "13:00", runner=runner2, windows=True)
        assert rep.ok and "Task Scheduler" in rep.summary() and "AC power" in rep.summary()
        def runner3(argv, **kw):
            if argv[0] == "powershell":
                raise FileNotFoundError("powershell")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        assert "Task Scheduler" in bt.install_schedule(d, "python.exe", "13:00", runner=runner3, windows=True).summary()
        # bad time: nothing written, nothing asked
        os.remove(w)
        calls.clear()
        rep = bt.install_schedule(d, "python.exe", "25:00", runner=runner, windows=True)
        assert not rep.ok and not calls and not os.path.exists(w)
        # not Windows
        rep = bt.install_schedule(d, "python.exe", "13:00", runner=runner, windows=False)
        assert not rep.ok and "WINDOWS" in rep.summary() and not calls
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def u08_a_part_that_holds_no_files_is_a_failure_not_a_pass():
    root = tempfile.mkdtemp()
    try:
        folder = make_fake_backup(root)
        assert bt.verify_backup(folder).ok
        man = json.load(open(os.path.join(folder, "MANIFEST.json")))
        man["parts"]["writes"] = {"files": {}}
        json.dump(man, open(os.path.join(folder, "MANIFEST.json"), "w"))
        rep = bt.verify_backup(folder)
        assert not rep.ok and "writes: the backup holds no files for it" in rep.summary(), rep.summary()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ live ----
@test
@live
def l01_a_backup_taken_while_the_database_is_being_written_is_consistent(m):
    # A writer keeps inserting (one commit per row) in WAL mode while the backups run.  It never checkpoints, so every new row
    # lives ONLY in the -wal file and the main file stays as it was: a backup that just copies the main file misses all of them.
    writer = sh("docker", "run", "-d", "--entrypoint", "python", "-v", f"{m.name}_checkpoints:/data", "rag-api:local", "-c", """
import sqlite3, time
c = sqlite3.connect('/data/rag_checkpoints.db', timeout=30)
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
while True:
    c.execute("insert into checkpoints(thread, blob) values ('live', 'y' * 200)")
    c.commit()
    time.sleep(0.002)
""")
    try:
        time.sleep(1.5)
        before = m.count_rows()
        reps = [m.backup(keep=0) for _ in range(3)]
        after = m.count_rows()
    finally:
        sh("docker", "rm", "-f", writer, check=False)
    assert all(r.ok for r in reps), [r.summary() for r in reps]
    assert before > m.rows + 100, "the writer had not added enough rows before the backups started"
    assert after > before + 50, "the writer was not actually writing during the backups"
    for name in bt.list_backups(m.dest):
        man = json.load(open(os.path.join(m.dest, name, "MANIFEST.json")))
        e = man["parts"]["checkpoints"]["files"]["rag_checkpoints.db"]
        assert e["integrity"] == "ok" and before <= e["tables"]["checkpoints"] <= after, (e["tables"], before, after)
        assert bt.verify_backup(os.path.join(m.dest, name)).ok
    assert len(bt.list_backups(m.dest)) == 3


@test
@live
def l02_backup_writes_manifest_latest_and_redis_snapshot(m):
    r = m.backup()
    assert r.ok, r.summary()
    f = m.newest()
    man = json.load(open(os.path.join(f, "MANIFEST.json")))
    assert man["parts"]["redis"]["dbsize"] == m.keys
    assert open(os.path.join(f, "redis", "dump.rdb"), "rb").read(5) == b"REDIS"
    assert man["parts"]["checkpoints"]["files"]["rag_checkpoints.db"]["tables"] == {"checkpoints": m.rows, "writes": 20}
    st, age, folder = bt.backup_status(m.dest)
    assert st == "ok" and age < 120 and folder == os.path.basename(f)
    assert not [d for d in os.listdir(m.dest) if d.endswith(".partial")]
    # the backup file is a plain, standalone SQLite file a person can open with any tool
    c = sqlite3.connect(os.path.join(f, "checkpoints", "rag_checkpoints.db"))
    assert c.execute("pragma journal_mode").fetchone()[0] == "delete"
    assert c.execute("select count(*) from checkpoints").fetchone()[0] == m.rows


@test
@live
def l03_the_drill_restores_into_throwaway_volumes_and_cleans_up(m):
    assert m.backup().ok
    before_vols = sorted(sh("docker", "volume", "ls", "-q").split())
    r = bt.drill(m.dir, None, m.dest)
    assert r.ok, r.summary()
    text = r.summary()
    assert "opened inside the app image" in text and "--appendonly yes" in text and "throwaway volumes deleted" in text, text
    assert sorted(sh("docker", "volume", "ls", "-q").split()) == before_vols, "the drill left a volume behind"
    assert not m.left_over(), m.left_over()
    assert m.count_rows() == m.rows and m.redis("DBSIZE").endswith(str(m.keys))              # the real data was not touched
    d = json.load(open(os.path.join(m.dest, "LAST_DRILL.json")))
    assert d["ok"] is True and time.time() - d["at"] < 300


@test
@live
def l04_a_real_restore_brings_back_deleted_data_with_the_right_owner_and_keeps_a_safety_copy(m):
    assert m.backup().ok
    good_folder = m.newest()
    m.py("import sqlite3; c = sqlite3.connect('/data/rag_checkpoints.db'); c.execute('delete from checkpoints'); c.commit()")
    m.redis("FLUSHALL")
    assert m.count_rows() == 0 and m.redis("DBSIZE").endswith("0")
    assert bt.restore(good_folder, m.dir, m.dest).ok is True
    r = bt.restore(good_folder, m.dir, m.dest)                       # the same restore again: must also work (idempotent)
    assert r.ok, r.summary()
    safety = [d for d in os.listdir(m.dest) if d.startswith("before_restore_")]
    assert len(safety) == 2, safety
    first = sorted(safety)[0]
    man = json.load(open(os.path.join(m.dest, first, "MANIFEST.json")))
    assert man["parts"]["checkpoints"]["files"]["rag_checkpoints.db"]["tables"]["checkpoints"] == 0        # the safety copy holds the DAMAGED state
    assert bt.list_backups(m.dest) == [os.path.basename(good_folder)], "safety copies must not count as normal backups"
    assert bt.backup_status(m.dest)[2] == os.path.basename(good_folder)
    sh("docker", "compose", "up", "-d", "redis", cwd=m.dir)
    m.wait_redis()
    assert m.redis("DBSIZE").endswith(str(m.keys)), m.redis("DBSIZE")
    assert m.redis("GET", "key3") == "value3"
    assert m.count_rows() == m.rows
    owner = m.py("import os; s = os.stat('/data/rag_checkpoints.db'); print(s.st_uid, s.st_gid)").split()
    assert owner == ["10001", "10001"], owner
    # and the app user can WRITE to it (the real test of ownership)
    m.py("import sqlite3; c = sqlite3.connect('/data/rag_checkpoints.db'); c.execute(\"insert into checkpoints(thread, blob) values ('after', 'x')\"); c.commit()", user="10001:10001")
    assert m.count_rows() == m.rows + 1


@test
@live
def l05_a_damaged_backup_is_refused_by_both_the_drill_and_the_restore_and_nothing_changes(m):
    assert m.backup().ok
    f = m.newest()
    p = os.path.join(f, "checkpoints", "rag_checkpoints.db")
    raw = open(p, "rb").read()
    open(p, "wb").write(raw[:-10] + b"0123456789")
    r1 = bt.drill(m.dir, f, m.dest)
    assert not r1.ok and "does not pass its own check" in r1.summary(), r1.summary()
    r2 = bt.restore(f, m.dir, m.dest)
    assert not r2.ok and "nothing was changed" in r2.summary(), r2.summary()
    assert not [d for d in os.listdir(m.dest) if d.startswith("before_restore_")]
    assert m.count_rows() == m.rows and m.redis("DBSIZE").endswith(str(m.keys))
    assert not m.left_over()


@test
@live
def l06_a_failed_backup_leaves_no_half_folder_and_does_not_move_latest(m):
    assert m.backup().ok
    latest_before = open(os.path.join(m.dest, "LATEST.json")).read()
    names_before = bt.list_backups(m.dest)
    sh("docker", "compose", "stop", "redis", cwd=m.dir)
    time.sleep(1.1)
    r = m.backup()
    assert not r.ok and "Redis is not running" in r.summary(), r.summary()
    assert open(os.path.join(m.dest, "LATEST.json")).read() == latest_before
    assert bt.list_backups(m.dest) == names_before
    assert not [d for d in os.listdir(m.dest) if d.endswith(".partial")], os.listdir(m.dest)
    r2 = m.backup(redis=False)                                         # --no-redis: still possible, and says what it left out
    assert r2.ok and "redis" not in json.load(open(os.path.join(m.newest(), "MANIFEST.json")))["parts"]


@test
@live
def l07_retention_with_real_backups(m):
    for _ in range(3):
        assert m.backup(keep=2).ok
    assert len(bt.list_backups(m.dest)) == 2


@test
@live
def l08_models_get_one_copy_that_is_replaced_and_can_be_restored(m):
    m.py("import os; os.makedirs('/data/huggingface/sub'); open('/data/huggingface/sub/w.bin','wb').write(os.urandom(300000)); open('/data/note.txt','w').write('hello')",
         volume="models", user="root")
    r = m.backup(with_models=True)
    assert r.ok and "models copied" in r.summary(), r.summary()
    meta1 = json.load(open(os.path.join(m.dest, "models", "META.json")))
    assert m.backup(with_models=True).ok
    meta2 = json.load(open(os.path.join(m.dest, "models", "META.json")))
    assert meta2["created_at"] > meta1["created_at"]
    assert os.listdir(os.path.join(m.dest, "models")).count("models.tar.gz") == 1
    want = m.py("import hashlib; print(hashlib.sha256(open('/data/huggingface/sub/w.bin','rb').read()).hexdigest())", volume="models")
    m.py("import shutil, os; [shutil.rmtree(os.path.join('/data', n)) if os.path.isdir(os.path.join('/data', n)) else os.remove(os.path.join('/data', n)) for n in os.listdir('/data')]",
         volume="models", user="root")
    r = bt.restore_models(m.dir, m.dest)
    assert r.ok, r.summary()
    got = m.py("import hashlib; print(hashlib.sha256(open('/data/huggingface/sub/w.bin','rb').read()).hexdigest())", volume="models")
    assert got == want
    assert m.py("print(open('/data/note.txt').read())", volume="models") == "hello"
    assert m.py("import os; print(os.stat('/data/huggingface').st_uid)", volume="models") == "10001"


@test
@live
def l09_command_line_exit_codes_and_list(m):
    env = dict(os.environ)
    base = [sys.executable, os.path.join(HERE, "backup_tools.py")]
    r = subprocess.run(base + ["list", "--dir", m.dest], capture_output=True, text=True)
    assert r.returncode == 0 and "0 backup(s)" in r.stdout, r.stdout
    r = subprocess.run(base + ["backup", "--project-dir", m.dir, "--dir", m.dest], capture_output=True, text=True)
    assert r.returncode == 0 and "saved and verified" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(base + ["verify", "--dir", m.dest], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    r = subprocess.run(base + ["restore", m.newest(), "--project-dir", m.dir, "--dir", m.dest], capture_output=True, text=True)   # no --yes
    assert r.returncode == 2 and "--yes" in r.stdout
    assert m.count_rows() == m.rows


@test
@live
def l10_keys_with_a_time_limit_run_out_by_themselves_and_that_is_not_a_failed_restore(m):
    project = bt.load_project(m.dir)
    for i in range(4):
        m.redis("SET", f"temp{i}", "x", "EX", "10")                           # 10-second TTL so they survive the check
    out = tempfile.mkdtemp()
    info = bt._backup_redis(project, os.path.join(out, "redis"))               # snapshot captured with keys present
    assert info["keys_max"] == m.keys + 4 and info["persistent_min"] == m.keys and info["dbsize"] == m.keys + 4, info
    time.sleep(1.0)                                                           # wait for temporary keys to expire naturally
    json.dump({"project": project.name, "parts": {"redis": info}}, open(os.path.join(out, "MANIFEST.json"), "w"))
    vol = f"{m.name}_ttl"
    sh("docker", "volume", "create", vol)
    rep = bt.Report()
    bt._restore_all(project, out, {"redis_data": vol}, rep)
    bt._check_restored(project, out, {"redis_data": vol}, rep)
    assert rep.ok, rep.summary()
    
    # ... but LOSING a key that never runs out is still a failure
    info2 = dict(info, persistent_min=m.keys + 1)
    json.dump({"project": project.name, "parts": {"redis": info2}}, open(os.path.join(out, "MANIFEST.json"), "w"))
    rep2 = bt.Report()
    bt._check_restored(project, out, {"redis_data": vol}, rep2)
    assert not rep2.ok, "should have failed because a persistent key is missing"
    shutil.rmtree(out, ignore_errors=True)


@test
@live
def l11_a_restore_never_empties_a_volume_for_nothing_and_never_takes_a_stranger_backup(m):
    project = bt.load_project(m.dir)
    before = m.count_rows()
    empty = tempfile.mkdtemp()
    try:
        bt._restore_files(project, project.app_image, project.vol("checkpoints"), empty, 10001, 10001)
        raise AssertionError("an empty backup was allowed to empty the volume")
    except bt.BackupError as e:
        assert "nothing was changed" in str(e), str(e)
    assert m.count_rows() == before, "the volume was emptied"
    # a backup that was made by another project
    assert m.backup().ok
    folder = m.newest()
    man = json.load(open(os.path.join(folder, "MANIFEST.json")))
    man["project"] = "someone-else"
    json.dump(man, open(os.path.join(folder, "MANIFEST.json"), "w"))
    r = bt.restore(folder, m.dir, m.dest)
    assert not r.ok and "was made by the project 'someone-else'" in r.summary() and "nothing was changed" in r.summary(), r.summary()
    assert not [d for d in os.listdir(m.dest) if d.startswith("before_restore_")] and m.count_rows() == before
    # restore-models without --yes does nothing
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bt.main(["restore-models", "--dir", m.dest, "--project-dir", m.dir])
    assert rc == 2 and "--yes" in buf.getvalue()


@test
@live
def l12_every_restored_file_is_compared_by_checksum_not_only_the_databases(m):
    m.py("open('/data/notes.txt', 'w').write('keep me')", user="root")
    assert m.backup(redis=False).ok
    folder = m.newest()
    project = bt.load_project(m.dir)
    vol = f"{m.name}_tamper"
    sh("docker", "volume", "create", vol)
    bt._restore_files(project, project.app_image, vol, os.path.join(folder, "checkpoints"), 10001, 10001)
    rep = bt.Report()
    bt._check_restored(project, folder, {"checkpoints": vol}, rep)
    assert rep.ok and "notes.txt: restored" in rep.summary(), rep.summary()
    sh("docker", "run", "--rm", "--user", "root", "--entrypoint", "python", "-v", f"{vol}:/data", "rag-api:local", "-c", "open('/data/notes.txt', 'w').write('someone changed me')")
    rep = bt.Report()
    bt._check_restored(project, folder, {"checkpoints": vol}, rep)
    assert not rep.ok and "notes.txt: the restored file is not the saved one" in rep.summary(), rep.summary()
    sh("docker", "run", "--rm", "--user", "root", "--entrypoint", "python", "-v", f"{vol}:/data", "rag-api:local", "-c", "import os; os.remove('/data/notes.txt')")
    rep = bt.Report()
    bt._check_restored(project, folder, {"checkpoints": vol}, rep)
    assert not rep.ok and "notes.txt: the file is not in the restored volume" in rep.summary(), rep.summary()


@test
@live
def l13_a_restore_stops_the_app_before_it_copies_the_current_state(m):
    assert m.backup().ok
    good = m.newest()
    calls = []
    real = bt.run

    def spy(cmd, *a, **k):
        calls.append(list(cmd))
        return real(cmd, *a, **k)
    bt.run = spy
    try:
        assert bt.restore(good, m.dir, m.dest).ok
    finally:
        bt.run = real
    first = lambda pred: next(i for i, c in enumerate(calls) if pred(c))
    stop_rag = first(lambda c: c[:4] == ["docker", "compose", "stop", "rag"])
    dump = first(lambda c: "--rdb" in c)
    down = first(lambda c: c[:3] == ["docker", "compose", "down"])
    wipe = first(lambda c: c[:2] == ["docker", "cp"] and any("/tmp/in" in x for x in c))
    assert stop_rag < dump < down < wipe, (stop_rag, dump, down, wipe)


if __name__ == "__main__":
    bad = skipped = 0
    for name, status, tb in results:
        if status == "skip":
            skipped += 1
            print("SKIP", name, "-", tb)
            continue
        print(("PASS " if status == "pass" else "FAIL ") + name)
        if status == "fail":
            bad += 1
            print(tb)
    total = len(results) - skipped
    print(f"\n{total - bad}/{total} passed" + (f" ({skipped} skipped: {'; '.join(sorted({t for n, s, t in results if s == 'skip'}))})" if skipped else ""))
    sys.exit(1 if bad else 0)
