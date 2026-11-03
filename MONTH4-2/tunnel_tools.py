"""
tunnel_tools.py -- Day 21 (optional): use the FREE Cloudflare quick tunnel to give a friend a public https address.

    python tunnel_tools.py            # reads the tunnel's log, finds its address, saves it as PUBLIC_URL in .env

A quick tunnel is for testing and sharing with a few people: its address is random and CHANGES every time the tunnel container
restarts. After a restart run this again, then:  docker compose up -d --force-recreate --no-deps watchdog   (the watchdog reads PUBLIC_URL when it starts).
The address is not a secret by itself, but anybody who has it can reach /ask (they still need an API key).
"""
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Optional

URL = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.trycloudflare\.com", re.I)
NOT_A_TUNNEL = {"api", "www", "dash", "developers", "blog", "status", "support", "community"}      # Cloudflare's own addresses (cloudflared names
                                                                                                      # api.trycloudflare.com in its ERROR lines when it cannot register)


def find_tunnel_url(text: str) -> Optional[str]:
    """The newest quick-tunnel address in a log (a restarted tunnel prints a new one below the old one)."""
    found = [m.group(0).lower() for m in URL.finditer(text or "") if m.group(1).lower() not in NOT_A_TUNNEL]
    return found[-1] if found else None


def set_env_value(env_path: str, name: str, value: str, backup_suffix: str = ".before_public_url") -> str:
    """'added' | 'changed' | 'unchanged'. Replaces the NAME=... line (the last one, like a .env loader) or adds it; keeps the other lines and the line endings."""
    raw = open(env_path, "rb").read().decode("utf-8-sig") if os.path.exists(env_path) else ""
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl) if raw else []
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    idx = [i for i, l in enumerate(lines) if pat.match(l)]
    new = f"{name}={value}"
    if idx and lines[idx[-1]].strip() == new:
        return "unchanged"
    if os.path.exists(env_path):
        shutil.copyfile(env_path, env_path + backup_suffix)
    if idx:
        lines[idx[-1]] = new
        status = "changed"
    else:
        if lines and lines[-1] == "":
            lines.pop()
        lines.append(new)
        lines.append("")
        status = "added"
    tmp = env_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(nl.join(lines).encode("utf-8"))
    os.replace(tmp, env_path)
    return status


def wait_for_url(get_logs: Callable[[], str], timeout: float = 90.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, since: str = "") -> Optional[str]:
    """Polls the tunnel's log until an address appears. `since` = an address you already know is OLD: it is ignored."""
    end = clock() + timeout
    while True:
        url = find_tunnel_url(get_logs())
        if url and url != since.lower().rstrip("/"):
            return url
        if clock() >= end:
            return None
        sleep(2)


def main(argv=None) -> int:
    env_path = (argv or sys.argv[1:] or [".env"])[0]
    def logs() -> str:
        r = subprocess.run(["docker", "compose", "logs", "--no-color", "tunnel"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.stdout + r.stderr
    url = wait_for_url(logs, 60)
    if not url:
        print("❌ no tunnel address in the log yet. Start it first:  docker compose --profile tunnel up -d tunnel   (then wait a few seconds)")
        errors = [l.strip() for l in logs().splitlines() if " ERR " in l or "failed to" in l.lower()]
        if errors:
            print("   the tunnel's own last complaint (it may not be able to reach Cloudflare):  " + errors[-1][-220:])
        return 1
    status = set_env_value(env_path, "PUBLIC_URL", url)
    print(f"✅ PUBLIC_URL {status} in {env_path}: {url}")
    print("Now: docker compose up -d --force-recreate --no-deps watchdog   (so it watches the new address)")
    return 0


def _utf8_stdio():
    """Windows hands a program a pipe that only knows cp1252, which cannot print the marks used here: ask for UTF-8 instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _utf8_stdio()
    raise SystemExit(main())
