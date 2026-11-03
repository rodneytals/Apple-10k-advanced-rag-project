"""Tests for the Day 21 compose / Docker files.  Run:  python test_launch_files.py
No Docker needed (one optional test uses `docker compose config` when Docker is installed)."""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import container_check as cc
import make_container_files as mcf
import make_frontdoor as mfd
import make_requirements as mr
import monitoring

try:
    import yaml
except ImportError:                                    # PyYAML ships with most conda environments; skip politely if not
    yaml = None

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=4)))
    return fn


PIPELINE = 'import redis\nrc = redis.Redis(host="localhost")\nCHECKPOINT_DB = "checkpoints/rag_checkpoints.db"\n'
LAUNCH_FILES = ("watchdog.py", "notifiers.py", "alert_rules.py", "monitoring.py")


def project(door=True, watchdog=True, files=()):
    d = tempfile.mkdtemp(prefix="d21_")
    os.makedirs(os.path.join(d, "rag_pipeline"))
    open(os.path.join(d, "rag_pipeline", "pipeline.py"), "w").write(PIPELINE)
    for f in (LAUNCH_FILES if watchdog else ("monitoring.py",)):
        open(os.path.join(d, f), "w").write("# stand-in\n")
    if door:
        open(os.path.join(d, "Caddyfile"), "w").write("# stand-in\n")
    for f in files:
        open(os.path.join(d, f), "w").write("x")
    return d


def generate(**kw):
    d = project(**kw)
    r = cc.ContainerReport(package="rag_pipeline")
    r.redis_mode, r.volume_paths, r.copy_paths = "compose", ["checkpoints"], []
    reqs = mr.RequirementsReport(python_version="3.11")
    mcf.make_container_files("rag_pipeline", d, r, reqs)
    return d


def load(d):
    return yaml.safe_load(open(os.path.join(d, "docker-compose.yml")))


def need_yaml():
    assert yaml is not None, "PyYAML is needed for this test (pip install pyyaml)"


def read_env_names(*files):
    """Every environment variable name the watchdog side reads: env.get("X"), env["X"], os.environ.get("X")."""
    names = set()
    for f in files:
        src = open(os.path.join(HERE, f), encoding="utf-8").read()
        names |= set(re.findall(r'(?:env|environ)(?:\.get)?[\[(]\s*"([A-Z][A-Z0-9_]+)"', src))
        names |= set(re.findall(r'_f\(env,\s*"([A-Z][A-Z0-9_]+)"', src))
    return names


# ------------------------------------------------------------------ tests ----
@test
def without_the_new_files_the_compose_file_is_exactly_what_day20_wrote():
    base = mcf.compose_text("compose", {"REDIS_HOST": "redis"}, ["checkpoints"])
    assert "proxy:" not in base and "watchdog:" not in base and "tunnel:" not in base and "caddy_data" not in base
    assert base.rstrip().endswith("redis_data:")
    d = generate(door=False, watchdog=False)
    assert open(os.path.join(d, "docker-compose.yml")).read() == mcf.compose_text("compose", mcf.redis_environment(PIPELINE), ["checkpoints"])


@test
def door_service_appears_only_when_a_caddyfile_exists_and_watchdog_only_when_all_three_files_exist():
    need_yaml()
    only_door = load(generate(door=True, watchdog=False))
    assert set(only_door["services"]) == {"redis", "rag", "proxy", "tunnel"}
    only_dog = load(generate(door=False, watchdog=True))
    assert set(only_dog["services"]) == {"redis", "rag", "watchdog"}
    assert "WATCH_DOOR_URL" not in only_dog["services"]["watchdog"]["environment"]
    both = load(generate(door=True, watchdog=True))
    assert set(both["services"]) == {"redis", "rag", "proxy", "tunnel", "watchdog"}
    assert both["services"]["watchdog"]["environment"]["WATCH_DOOR_URL"] == "http://proxy:8080"
    d = project(door=False, watchdog=False)                        # two of the three files are not enough
    for f in ("watchdog.py", "notifiers.py"):
        open(os.path.join(d, f), "w").write("#")
    assert "watchdog:" not in mcf.compose_text("compose", {}, ["checkpoints"], watchdog=all(os.path.exists(os.path.join(d, f)) for f in mcf.WATCHDOG_FILES))


@test
def nothing_new_is_reachable_from_outside_this_computer():
    need_yaml()
    c = load(generate())
    for name, svc in c["services"].items():
        for p in svc.get("ports", []):
            assert p.startswith("127.0.0.1:"), (name, p)
    assert c["services"]["proxy"]["ports"] == ["127.0.0.1:${RAG_DOOR_PORT:-8088}:8080"]
    assert "ports" not in c["services"]["tunnel"] and "ports" not in c["services"]["watchdog"]
    assert "ports" not in c["services"]["redis"]


@test
def tunnel_is_off_unless_asked_for_and_points_at_the_door_not_at_the_api():
    need_yaml()
    t = load(generate())["services"]["tunnel"]
    assert t["profiles"] == ["tunnel"]
    assert t["command"][-1] == "http://proxy:8080" and "rag:8000" not in " ".join(t["command"])
    assert "env_file" not in t and "environment" not in t        # it never sees your keys
    assert "--no-autoupdate" in t["command"]


@test
def proxy_gets_only_the_caddyfile_read_only_and_never_your_env_file():
    need_yaml()
    services = load(generate())["services"]
    p = services["proxy"]
    assert "env_file" not in p and "environment" not in p
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in p["volumes"]
    assert set(p["volumes"]) == {"./Caddyfile:/etc/caddy/Caddyfile:ro", "caddy_data:/data", "caddy_config:/config"}
    assert p["cap_drop"] == ["ALL"] and "no-new-privileges:true" in p["security_opt"]
    assert p["cap_add"] == ["NET_BIND_SERVICE"], "the official caddy program has this one file permission: without it, with ALL dropped, it cannot start"
    for other in ("rag", "redis", "watchdog", "tunnel"):
        assert "cap_add" not in (services.get(other) or {}), f"{other} must not get extra permissions"
    assert p["depends_on"] == ["rag"]
    assert f"{mfd.CADDY_IMAGE}" == p["image"], (mfd.CADDY_IMAGE, p["image"])


@test
def caddyfile_upstream_matches_the_compose_service_name_and_port():
    need_yaml()
    c = load(generate())
    text = mfd.caddyfile_text()
    assert "rag:8000" in text
    assert "rag" in c["services"] and c["services"]["rag"]["environment"]["RAG_PORT"] == "8000"
    assert ":8080" in text and c["services"]["proxy"]["ports"][0].endswith(":8080")


@test
def watchdog_receives_only_what_it_needs_and_none_of_the_apps_secrets():
    need_yaml()
    w = load(generate())["services"]["watchdog"]
    assert "env_file" not in w                                   # NOT the whole .env
    env = w["environment"]
    for forbidden in ("GROQ_API_KEY", "MONGO_URI", "MONGODB_URI", "NEO4J_PASSWORD", "NEO4J_URI", "LANGSMITH_API_KEY", "RAG_API_KEYS", "REDIS_URL", "REDIS_HOST"):
        assert forbidden not in env, forbidden
    assert w["image"] == "rag-api:local" and "build" not in w   # the same image, no second build
    assert w["command"] == ["python", "watchdog.py"]
    assert w["read_only"] is True and w["tmpfs"] == ["/tmp"] and w["cap_drop"] == ["ALL"]
    assert w["volumes"] == ["./backups:/backups:ro"]            # it can read the backups, never change them
    assert w["healthcheck"]["test"] == ["CMD", "python", "watchdog.py", "--selfcheck"]   # the image's API-port check is replaced
    assert w["restart"] == "unless-stopped" and w["init"] is True


@test
def the_metrics_key_is_required_and_everything_else_is_optional():
    need_yaml()
    env = load(generate())["services"]["watchdog"]["environment"]
    assert env["RAG_METRICS_KEY"].startswith("${RAG_METRICS_KEY:?")
    for k, v in env.items():
        if k in ("RAG_METRICS_KEY",) or not v.startswith("${"):
            continue
        assert ":-" in v and ":?" not in v, (k, v)             # a missing optional value must never stop the stack from starting


@test
def every_variable_given_to_the_watchdog_is_read_by_it_and_every_one_it_reads_is_given():
    need_yaml()
    env = set(load(generate())["services"]["watchdog"]["environment"])
    read = read_env_names("watchdog.py", "notifiers.py", "alert_rules.py")
    test_only = {"TELEGRAM_API_BASE", "WATCHDOG_ALIVE_FILE", "WATCH_INTERVAL", "WATCH_STARTUP_GRACE"}      # knobs for tests and for the curious
    never_given_on_purpose = {"RAG_PORT", "RAG_HOST"}
    missing = read - env - test_only - never_given_on_purpose
    unused = env - read
    assert not missing, f"read by the code but not passed by compose: {sorted(missing)}"
    assert not unused, f"passed by compose but never read: {sorted(unused)}"


@test
def the_secret_names_given_to_the_watchdog_are_masked_by_the_log_filter():
    need_yaml()
    env = load(generate())["services"]["watchdog"]["environment"]
    fake = {k: "Zq9-secret-value-%s-0123456789" % k for k in env}
    found = set(monitoring.collect_secrets(fake))
    for name in ("TELEGRAM_BOT_TOKEN", "NTFY_TOPIC_SECRET", "NTFY_TOKEN", "SMTP_PASSWORD", "HEARTBEAT_URL_SECRET", "RAG_METRICS_KEY"):
        assert fake[name] in found, f"{name} would NOT be hidden in logs"


@test
def volumes_block_lists_every_named_volume_that_is_used_and_nothing_else():
    need_yaml()
    c = load(generate())
    declared = set(c["volumes"])
    used = set()
    for svc in c["services"].values():
        for v in svc.get("volumes", []):
            src = v.split(":", 1)[0]
            if not src.startswith((".", "/")):
                used.add(src)
    assert declared == used, (declared ^ used)
    assert declared == {"models", "checkpoints", "redis_data", "caddy_data", "caddy_config"}


@test
def dockerfile_copies_the_watchdog_files_and_the_ignore_list_lets_them_through():
    d = generate()
    docker = open(os.path.join(d, "Dockerfile")).read()
    ignore = open(os.path.join(d, ".dockerignore")).read().splitlines()
    for f in ("watchdog.py", "notifiers.py", "alert_rules.py", "monitoring.py"):
        assert f in docker, f
        assert "!" + f in ignore, f
    for never in ("backup_tools.py", "make_frontdoor.py", "Caddyfile", "backups", ".env"):
        assert "!" + never not in ignore, never
        assert never not in docker.replace("**/.env", ""), never
    assert ignore[ignore.index("*")] == "*"                      # still an allow-list: everything is excluded unless named


@test
def the_image_still_starts_the_api_by_default_so_the_watchdog_only_overrides_the_command():
    docker = open(os.path.join(generate(), "Dockerfile")).read()
    assert 'CMD ["python", "serve.py"]' in docker and "ENTRYPOINT" not in docker
    assert 'HEALTHCHECK' in docker and "docker_healthcheck.py" in docker


@test
def rerunning_keeps_the_previous_compose_file():
    d = generate()
    r = cc.ContainerReport(package="rag_pipeline")
    r.redis_mode, r.volume_paths, r.copy_paths = "compose", ["checkpoints"], []
    mcf.make_container_files("rag_pipeline", d, r, mr.RequirementsReport(python_version="3.11"))
    assert os.path.exists(os.path.join(d, "docker-compose.yml.previous"))
    raw = open(os.path.join(d, "docker-compose.yml"), "rb").read()
    assert b"\r\n" not in raw


@test
def external_redis_still_gives_a_valid_stack_with_door_and_watchdog():
    need_yaml()
    d = project()
    r = cc.ContainerReport(package="rag_pipeline")
    r.redis_mode, r.volume_paths, r.copy_paths = "external", ["checkpoints"], []
    mcf.make_container_files("rag_pipeline", d, r, mr.RequirementsReport(python_version="3.11"))
    c = load(d)
    assert "redis" not in c["services"] and "redis_data" not in c["volumes"]
    assert {"proxy", "tunnel", "watchdog"} <= set(c["services"])


@test
def docker_compose_accepts_the_file_if_docker_is_installed():
    if not shutil.which("docker"):
        return
    d = generate()
    open(os.path.join(d, ".env"), "w").write("RAG_API_KEYS=abcdefghijklmnopqrstuvwxyz:me\nRAG_METRICS_KEY=abcdefghijklmnopqrstuvwxyz0123\n")
    base = ["docker", "compose", "-f", os.path.join(d, "docker-compose.yml"), "--project-directory", d]
    r = subprocess.run(base + ["config", "-q"], capture_output=True, text=True)
    if "docker daemon" in (r.stderr or "").lower() and r.returncode != 0 and "interpolat" not in r.stderr:
        return                                                      # no daemon here; config itself never needs one, so only skip on that message
    assert r.returncode == 0, r.stderr
    r2 = subprocess.run(base + ["--profile", "tunnel", "config", "--services"], capture_output=True, text=True)
    assert r2.returncode == 0 and "tunnel" in r2.stdout.split(), r2.stdout + r2.stderr
    r3 = subprocess.run(base + ["config", "--services"], capture_output=True, text=True)
    assert "tunnel" not in r3.stdout.split(), r3.stdout                # without the profile the tunnel is not part of the stack
    os.remove(os.path.join(d, ".env"))
    open(os.path.join(d, ".env"), "w").write("RAG_API_KEYS=abcdefghijklmnopqrstuvwxyz:me\n")
    r4 = subprocess.run(base + ["config", "-q"], capture_output=True, text=True)
    assert r4.returncode != 0 and "RAG_METRICS_KEY" in r4.stderr, "a missing metrics key must stop `up` with a message that names the variable"


# --------------------------------------------------------------------------- #
passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
