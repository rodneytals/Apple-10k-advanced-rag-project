"""
manage_keys.py -- Day 21 (optional): give a person access, take it away, or change a key. Without editing .env by hand.

    python manage_keys.py list                 # who has a key (names and the LAST 4 characters only)
    python manage_keys.py add amina            # a new key for "amina"; written to new_key_amina.txt (never printed)
    python manage_keys.py revoke amina         # her key stops working after the restart below
    python manage_keys.py rotate amina         # a new key for her; the old one stops working
    python manage_keys.py --env path/to/.env ...

Every change keeps the old file as .env.before_keys. The API reads its keys when the container is CREATED, so after a change:
    docker compose up -d --force-recreate --no-deps rag
The last key can never be removed (the API refuses to start with no keys). The key goes into a file, not onto the screen, so it
does not stay in a notebook or a terminal history; give it to the person, then delete the file.
"""
import argparse
import os
import re
import secrets
import shutil
import sys
from typing import Dict, List, Optional, Tuple

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")            # same rule as the API (api_layer.ID_PATTERN)
WEAK = ("changeme", "password", "example", "test", "secret", "1234567890", "qwerty")
LINE = re.compile(r"^(\s*(?:export\s+)?RAG_API_KEYS\s*=\s*)(['\"]?)(.*?)(['\"]?)(\s*)$")


class KeyError_(Exception):
    pass


def new_key() -> str:
    while True:
        k = secrets.token_urlsafe(24)                          # 32 characters, letters digits - _
        if not any(w in k.lower() for w in WEAK):
            return k


def _read(path: str) -> Tuple[List[str], str]:
    raw = open(path, "rb").read().decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in raw else "\n"                      # keep Windows line endings as they are
    return raw.split(nl), nl


def _find(lines: List[str]) -> Tuple[int, "re.Match"]:
    hits = [(i, LINE.match(l)) for i, l in enumerate(lines) if LINE.match(l)]
    if not hits:
        raise KeyError_("RAG_API_KEYS is not in this .env file")
    return hits[-1]                                             # the last one wins, like a normal .env loader


def read_keys(env_path: str) -> Dict[str, str]:
    """{user: key}, in file order."""
    lines, _ = _read(env_path)
    _, m = _find(lines)
    out: Dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in m.group(3).split(","))):
        if ":" not in pair:
            raise KeyError_("RAG_API_KEYS has an entry without a name (it must look like key:name)")
        key, user = pair.split(":", 1)
        out[user] = key
    return out


def _write(env_path: str, keys: Dict[str, str]) -> None:
    lines, nl = _read(env_path)
    i, m = _find(lines)
    lines[i] = m.group(1) + m.group(2) + ",".join(f"{k}:{u}" for u, k in keys.items()) + m.group(4) + m.group(5)
    shutil.copyfile(env_path, env_path + ".before_keys")
    tmp = env_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(nl.join(lines).encode("utf-8"))
    os.replace(tmp, env_path)


def _key_file(env_path: str, user: str, key: str) -> str:
    folder = os.path.dirname(os.path.abspath(env_path))
    path = os.path.join(folder, f"new_key_{user}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    gi = os.path.join(folder, ".gitignore")
    if os.path.exists(gi) and "new_key_*.txt" not in open(gi, encoding="utf-8").read():
        with open(gi, "a", encoding="utf-8") as f:
            f.write("\nnew_key_*.txt\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def masked(key: str) -> str:
    return f"{len(key)} characters, ends …{key[-4:]}"


def add(env_path: str, user: str) -> str:
    if not ID_PATTERN.match(user):
        raise KeyError_(f"the name {user!r} may only use letters, digits, - and _ (up to 64)")
    keys = read_keys(env_path)
    if user in keys:
        raise KeyError_(f"{user!r} already has a key. Use rotate to replace it")
    keys[user] = new_key()
    _write(env_path, keys)
    return _key_file(env_path, user, keys[user])


def rotate(env_path: str, user: str) -> str:
    keys = read_keys(env_path)
    if user not in keys:
        raise KeyError_(f"{user!r} has no key (names: {', '.join(keys) or 'none'})")
    keys[user] = new_key()
    _write(env_path, keys)
    return _key_file(env_path, user, keys[user])


def revoke(env_path: str, user: str) -> None:
    keys = read_keys(env_path)
    if user not in keys:
        raise KeyError_(f"{user!r} has no key (names: {', '.join(keys) or 'none'})")
    if len(keys) == 1:
        raise KeyError_("that is the last key: the API refuses to start with no keys. Add another person first")
    del keys[user]
    _write(env_path, keys)


RESTART = "Now restart the API so it reads the new list:  docker compose up -d --force-recreate --no-deps rag"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Day 21 key manager")
    ap.add_argument("--env", default=".env")
    ap.add_argument("command", choices=["list", "add", "revoke", "rotate"])
    ap.add_argument("user", nargs="?")
    a = ap.parse_args(argv)
    try:
        if not os.path.exists(a.env):
            raise KeyError_(f"{a.env} not found (run this in your project folder)")
        if a.command == "list":
            for u, k in read_keys(a.env).items():
                print(f"  {u:<20} {masked(k)}")
            return 0
        if not a.user:
            raise KeyError_(f"{a.command} needs a name:  python manage_keys.py {a.command} amina")
        if a.command == "revoke":
            revoke(a.env, a.user)
            print(f"✅ {a.user}'s key removed from .env (the old file is .env.before_keys).")
        else:
            path = (add if a.command == "add" else rotate)(a.env, a.user)
            print(f"✅ new key for {a.user} is in {os.path.basename(path)} (not printed here). Give it to them, then delete that file.")
        print(RESTART)
        return 0
    except KeyError_ as e:
        print(f"❌ {e}")
        return 1


def _utf8_stdio():
    """Windows hands a program a pipe that only knows cp1252, which cannot print the marks used here: ask for UTF-8 instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _utf8_stdio()
    raise SystemExit(main())
