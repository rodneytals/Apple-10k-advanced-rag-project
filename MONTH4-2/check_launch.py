"""
check_launch.py -- Day 21: the launch check. It looks at your service from the OUTSIDE, the way a stranger would.

    python check_launch.py                       # the front door on this computer, your backups, your alert setup, your files
    python check_launch.py --public https://something.trycloudflare.com    # also test the public address
    python check_launch.py --ask                 # also ask ONE real question through the door (a few thousand tokens)
    python check_launch.py --no-docker           # skip the container check (for a computer without Docker running)

It reads .env but never prints a key. Exit code 0 = nothing failed, 1 = something must be fixed first. The last line says
whether EVERYTHING was checked ("READY TO LAUNCH") or whether something was left out of this run (no public address, no --ask, no Docker).

  1. the front door   : only /healthz and POST /ask get in; ~56 spellings of /metrics, /readyz, /statusz, /docs ... all get 404;
                        a question with no key / a wrong key is refused by the API behind the door; a huge body is refused by the door
  2. the public address: the same, over the internet, with the certificate checked
  3. containers        : proxy, rag, redis and watchdog are up and healthy; only this computer can reach any port
  4. backups           : a fresh, verified backup exists AND a restore has been practised
  5. alerts            : at least one way to reach your phone is set up (and a dead-man's switch, if you use one)
  6. secrets           : keys are long enough and different from each other, .env and backups are not in git, docs are off
  7. one real question : (--ask) answered through the door, with the door's request id on it; then the door's log is read:
                        none of your keys may be in it
"""
import argparse
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from make_frontdoor import CLOSED_PATHS
except Exception:                                      # the list is only a convenience: a short built-in one keeps this tool alive
    CLOSED_PATHS = ["/metrics", "//metrics", "/%6Detrics", "/ask/../metrics", "/readyz", "/statusz", "/docs", "/openapi.json", "/Ask", "/"]

USER_AGENT = "rag-launch-check/1.0"                    # Cloudflare turns away Python's default user agent
PUBLIC_SAMPLE = ["/metrics", "/METRICS", "//metrics", "/%6Detrics", "/ask/../metrics", "/readyz", "/statusz", "/docs", "/openapi.json", "/Ask", "/", "/.env"]
OPEN_LOOKING = (200, 201, 202, 204, 301, 302, 307, 308, 401, 405, 422, 429, 503)   # answers only the API itself would give
CLOSED_STATUS = (400, 403, 404, 410, 414, 421, 431)    # "no" from the door or from Cloudflare in front of it
WEAK_WORDS = ("changeme", "change-me", "password", "example", "test", "secret", "abcdefghijklmnop", "1234567890", "qwerty", "your-key", "yourkey")
BIG_BODY = b'{"question": "' + b"x" * 30000 + b'"}'    # bigger than the door's 20 KB limit


# ------------------------------------------------------------------ report ----
@dataclass
class Report:
    lines: List[Tuple[Optional[bool], str]] = field(default_factory=list)       # (True|False|None=warning, text); heading lines use ok="head"
    heads: Dict[int, str] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)                             # things this run did NOT look at (never a silent pass)

    def skip(self, what: str) -> None:
        self.skipped.append(what)

    def head(self, text: str) -> None:
        self.heads[len(self.lines)] = text

    def add(self, ok: Optional[bool], text: str) -> None:
        self.lines.append((ok, text))

    @property
    def failed(self) -> List[str]:
        return [t for ok, t in self.lines if ok is False]

    @property
    def warned(self) -> List[str]:
        return [t for ok, t in self.lines if ok is None]

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        out: List[str] = []
        for i, (ok, t) in enumerate(self.lines):
            if i in self.heads:
                out += ["", self.heads[i]]
            out.append(("✅ " if ok else "❌ " if ok is False else "⚠️  ") + t)
        out.append("")
        if self.ok and self.skipped:
            out.append("✅ NOTHING FAILED in what was checked.   NOT checked this time: " + "; ".join(self.skipped) + ".")
            out.append("   (this is not yet a full launch check: run it again with those included)")
        elif self.ok:
            out.append("✅ READY TO LAUNCH" + (f"  ({len(self.warned)} warning(s) above: read them, none stops you)" if self.warned else ""))
        else:
            out.append(f"❌ NOT READY: {len(self.failed)} thing(s) to fix first:")
            out += [f"   - {t}" for t in self.failed]
        if self.ok:
            out.append("Only YOU can confirm two things: the test alert (Cell 5) really arrived on your phone, and .env is saved in your password manager.")
        return "\n".join(out)


# -------------------------------------------------------------------- http ----
def call(base: str, method: str, path: str, headers: Optional[Dict[str, str]] = None, body: Optional[bytes] = None,
         timeout: float = 10.0) -> Tuple[Optional[int], Dict[str, str], bytes]:
    """One request with the path sent EXACTLY as written (no tidying up). Returns (status, headers, body) or (None, {}, b'ErrorClass')."""
    u = urllib.parse.urlsplit(base)
    host, https = u.hostname or "127.0.0.1", u.scheme == "https"
    port = u.port or (443 if https else 80)
    try:
        conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
                if https else http.client.HTTPConnection(host, port, timeout=timeout))
        try:
            h = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})}
            if body is not None:
                h["Content-Type"] = "application/json"
            conn.request(method, (u.path.rstrip("/") + path) if u.path.strip("/") else path, body=body, headers=h)
            r = conn.getresponse()
            data = r.read(65536)
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            conn.close()
    except Exception as e:                              # connection refused, timeout, bad certificate ...: the class name is enough
        return None, {}, type(e).__name__.encode()


def _why(raw: bytes) -> str:
    return raw.decode("ascii", "replace")


# ------------------------------------------------------------ 1. the door ----
def check_door(rep: Report, base: str, ask_key: str = "", paths: Optional[List[str]] = None, name: str = "front door") -> bool:
    """False when the door cannot even be reached (nothing else can be tested then)."""
    paths = CLOSED_PATHS if paths is None else paths
    st, hdr, raw = call(base, "GET", "/healthz")
    if st is None:
        rep.add(False, f"cannot reach the {name} at {base}: {_why(raw)}. Is it running?  docker compose ps   /   docker compose logs proxy")
        return False
    rep.add(st == 200, f"the {name} answers /healthz (HTTP {st})" + ("" if st == 200 else ": the API behind it is not healthy (docker compose logs rag)"))
    st2, _, _ = call(base, "HEAD", "/healthz")
    rep.add(st2 == 200, f"HEAD /healthz works too, for uptime services (HTTP {st2})")

    leaks: List[str] = []
    for p in paths:
        for method in ("GET", "POST"):
            s, _, raw = call(base, method, p, body=b"{}" if method == "POST" else None)
            if s != 404:
                leaks.append(f"{method} {p} -> {s if s is not None else _why(raw)}")
    if leaks:
        rep.add(False, f"the {name} lets {len(leaks)} closed route(s) through, for example: " + "; ".join(leaks[:4]) + (" ..." if len(leaks) > 4 else "") +
                "  (regenerate the Caddyfile with make_frontdoor, then: docker compose up -d --force-recreate --no-deps proxy)")
    else:
        rep.add(True, f"{len(paths)} spellings of closed routes (/metrics, /readyz, /statusz, /docs ...) all answer 404, for GET and for POST")
    s, _, _ = call(base, "GET", "/ask")
    rep.add(s == 404, f"GET /ask is closed (HTTP {s}): only POST gets in")
    st404, h404, _ = call(base, "GET", "/metrics")
    leaked_server = h404.get("server") or hdr.get("server")
    rep.add(not leaked_server, "the door does not tell strangers what software it is (no Server header)" if not leaked_server
            else f"the door answers with 'Server: {leaked_server}', which tells strangers what software to attack (regenerate the Caddyfile with make_frontdoor)")

    s, _, raw = call(base, "POST", "/ask", body=b'{"question": "launch check", "session_id": "launchcheck"}')
    if s == 401:
        rep.add(True, "a question with no key is refused by the API behind the door (HTTP 401): the door reaches the API and the API checks keys")
    elif s in (502, 503, 504):
        rep.add(None, f"the API behind the door is not ready yet (HTTP {s}): wait for it to finish loading and run this again")
    elif s == 404:
        rep.add(False, "POST /ask is closed at the door (HTTP 404): the allow-list has no /ask. Regenerate the Caddyfile")
    elif s == 200:
        rep.add(False, "the API ANSWERED a question without any key (HTTP 200)! Check RAG_API_KEYS in .env right now")
    else:
        rep.add(False, f"a question with no key got HTTP {s}; expected 401")
    s, _, _ = call(base, "POST", "/ask", headers={"X-API-Key": "this-is-definitely-not-a-key-0123456789"}, body=b'{"question": "launch check", "session_id": "launchcheck"}')
    if s in (502, 503, 504):
        rep.add(None, f"the API behind the door is not ready yet (HTTP {s}), so the wrong-key test could not run: wait for it to finish loading and run this again")
    else:
        rep.add(s in (401, 403, 429), f"a question with a wrong key is refused (HTTP {s})" if s in (401, 403, 429)
                else f"a question with a wrong key got HTTP {s if s else 'no answer'}; expected 401")
    s, _, _ = call(base, "POST", "/ask", headers={"X-API-Key": "this-is-definitely-not-a-key-0123456789"}, body=BIG_BODY)
    rep.add(s == 413, f"a 30 KB question is refused (HTTP {s}; 413 = Payload Too Large). The door lets at most 20 KB of any question through to the API" if s == 413
            else f"a 30 KB question was not refused (HTTP {s}): the size limit is not working")
    return True


# ------------------------------------------------------- 2. public address ----
def check_public(rep: Report, url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    local = host in ("127.0.0.1", "localhost", "::1")
    if not url.lower().startswith("https://") and not local:
        rep.add(False, f"{url} is not https: keys would travel in clear text. Use the https:// address")
    elif local:
        rep.add(None, "the public address is a local one (testing only)")
        rep.skip("the real public address (the one given is a local test address)")
    st, _, raw = call(url, "GET", "/healthz", timeout=15)
    if st is None:
        extra = ""
        if raw in (b"SSLCertVerificationError", b"SSLError"):
            extra = " The certificate is not accepted: wrong name, expired, or not a real certificate yet."
        rep.add(False, f"cannot reach the public address ({_why(raw)}).{extra} A free quick-tunnel address changes every time the tunnel restarts: "
                       "run  python tunnel_tools.py  to save the new one in .env, then  docker compose up -d --force-recreate --no-deps watchdog")
        return
    rep.add(st == 200, f"the public address answers /healthz (HTTP {st})")
    bad = []
    for p in PUBLIC_SAMPLE:
        s, _, _ = call(url, "GET", p, timeout=15)
        if s not in CLOSED_STATUS:
            bad.append(f"{p} -> {s}")
    rep.add(not bad, f"{len(PUBLIC_SAMPLE)} closed routes are closed from the internet too" if not bad else "reachable from the internet: " + "; ".join(bad[:4]))
    s, _, _ = call(url, "POST", "/ask", body=b'{"question": "launch check", "session_id": "launchcheck"}', timeout=15)
    rep.add(s == 401, f"a question with no key is refused from the internet (HTTP {s})")


# ------------------------------------------------------- 3. the containers ----
def _docker_ps(project_dir: str) -> List[dict]:
    r = subprocess.run(["docker", "compose", "ps", "--all", "--format", "json"], cwd=project_dir, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "docker compose ps failed").strip().splitlines()[-1])
    text = r.stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def check_compose_ports(rep: Report, project_dir: str) -> None:
    path = os.path.join(project_dir, "docker-compose.yml")
    if not os.path.exists(path):
        rep.add(False, "docker-compose.yml is missing: run Day 19 Cell 8 again")
        return
    try:
        import yaml
        c = yaml.safe_load(open(path, encoding="utf-8"))
    except ImportError:
        rep.add(None, "cannot read docker-compose.yml (pip install pyyaml), so the port check is skipped")
        return
    open_ports = [f"{n}: {p}" for n, s in (c.get("services") or {}).items() for p in (s.get("ports") or []) if not str(p).startswith("127.0.0.1:")]
    rep.add(not open_ports, "every published port is limited to this computer (127.0.0.1)" if not open_ports
            else "ports open to your whole network: " + "; ".join(open_ports) + "  (start them with 127.0.0.1: in front)")


def check_containers(rep: Report, project_dir: str, public: bool, ps: Optional[Callable[[], List[dict]]] = None) -> None:
    try:
        rows = ps() if ps else _docker_ps(project_dir)
    except Exception as e:
        rep.add(False, f"could not ask Docker about the containers ({type(e).__name__}: {e}). Open Docker Desktop and run this again "
                       "(--no-docker skips this part, and then the check cannot call the launch complete)")
        return
    by = {r.get("Service") or r.get("Name"): r for r in rows}
    needed = ["rag", "redis", "proxy", "watchdog"]
    for s in needed:
        r = by.get(s)
        if r is None:
            rep.add(False, f"service {s!r} is not in this project: docker compose up -d")
            continue
        state, health = (r.get("State") or "").lower(), (r.get("Health") or "").lower()
        good = state == "running" and health in ("", "healthy")
        rep.add(good, f"{s}: {state or '?'}" + (f", {health}" if health else "") + ("" if good else f"   (docker compose logs {s})"))
    tun = by.get("tunnel")
    if tun and (tun.get("State") or "").lower() == "running":
        rep.add(None, "the free quick tunnel is running: fine for testing; its address changes on every restart")
    elif public:
        rep.add(None, "you are testing a public address but the tunnel service is not running here (fine if you use your own server)")


# ------------------------------------------------------------ 4. backups ----
def check_backups(rep: Report, backup_dir: str, max_age_hours: float = 48.0, drill_max_days: float = 35.0, now: Optional[float] = None) -> None:
    try:
        import backup_tools as bt
    except Exception as e:
        rep.add(False, f"backup_tools.py is missing next to this file ({type(e).__name__})")
        return
    now = now or time.time()
    st, age, text = bt.backup_status(backup_dir, now)
    if st == "missing":
        rep.add(False, "no backup yet: python backup_tools.py backup")
        return
    if st == "broken":
        rep.add(False, "backups/LATEST.json cannot be read: make a new backup (python backup_tools.py backup)")
        return
    rep.add(age is not None and age <= max_age_hours * 3600, f"the newest backup is {age / 3600:.1f} hours old (limit {max_age_hours:g})")
    folder = os.path.join(backup_dir, text) if text else ""
    if folder and os.path.isdir(folder):
        v = bt.verify_backup(folder)
        rep.add(v.ok, f"the newest backup passes its own check (files, checksums, databases open)" if v.ok else "the newest backup FAILS its check: " + next((l for l in v.lines if l.startswith("❌")), "see: python backup_tools.py verify"))
        leaked = [f for _, _, files in os.walk(folder) for f in files if f.startswith(".env")]
        rep.add(not leaked, "no .env file inside the backup (keys are kept elsewhere, on purpose)" if not leaked else "a .env file is inside the backup: remove it")
    else:
        rep.add(False, f"LATEST.json points to {text!r} but that folder is not there")
    p = os.path.join(backup_dir, "LAST_DRILL.json")
    if not os.path.exists(p):
        rep.add(False, "you have never practised a restore: python backup_tools.py drill   (a backup nobody has restored is a hope, not a backup)")
        return
    try:
        d = json.load(open(p, encoding="utf-8"))
        d_age = (now - float(d["at"])) / 86400
        ok = bool(d.get("ok"))
    except Exception:
        rep.add(False, "LAST_DRILL.json cannot be read: run python backup_tools.py drill")
        return
    rep.add(ok and d_age <= drill_max_days, f"a restore was practised {d_age:.0f} day(s) ago and worked" if ok else "the last restore practice FAILED: python backup_tools.py drill")


# ------------------------------------------------------------ 5. alerts ----
def _check_test_alert(rep: Report, path: str, now: Optional[float] = None) -> None:
    """Cell 5 sends a test alert from inside the watchdog container and writes down which channels the server accepted."""
    if not os.path.exists(path):
        rep.add(None, "no test alert on record: run Cell 5 (it sends one from inside the watchdog container) and look at your phone")
        return
    try:
        d = json.load(open(path, encoding="utf-8"))
        age_days = ((now or time.time()) - float(d["at"])) / 86400
        good, bad = list(d.get("ok_channels") or []), list(d.get("failed_channels") or [])
    except Exception:
        rep.add(False, "alert_test.json cannot be read: run Cell 5 again (it sends a test alert)")
        return
    if bad or not good:
        rep.add(False, f"the last test alert FAILED for: {', '.join(bad) or 'every channel'}. Run Cell 5 again and read the line that says why")
    elif age_days > 30:
        rep.add(None, f"the last test alert was {age_days:.0f} days ago: send a fresh one (Cell 5)")
    else:
        rep.add(True, f"the last test alert ({age_days:.0f} day(s) ago) was accepted by: {', '.join(good)}  (only you can see whether it reached your phone)")


def check_alerts(rep: Report, env: Dict[str, str], project_dir: Optional[str] = None, now: Optional[float] = None) -> None:
    try:
        import notifiers
    except Exception as e:
        rep.add(False, f"notifiers.py is missing next to this file ({type(e).__name__})")
        return
    names = [n.name for n in notifiers.from_env(env)]
    if not names:
        rep.add(False, "no alert channel is set up: your phone would never hear about a problem. python notifiers.py --setup-ntfy")
    else:
        rep.add(True, f"alert channel(s) set up: {', '.join(names)}" + ("" if len(names) > 1 else "   (one channel is enough to start; a second one covers its outage)"))
        if project_dir:
            _check_test_alert(rep, os.path.join(project_dir, "alert_test.json"), now)
    hb = (env.get("HEARTBEAT_URL_SECRET") or "").strip()
    if hb:
        u = urllib.parse.urlparse(hb)
        if u.scheme in ("http", "https") and u.netloc and "." in u.netloc:
            rep.add(True, "a dead-man's switch address is set (it is pinged every 30 seconds; the uptime service tells you when the pings stop)")
        else:
            rep.add(False, "HEARTBEAT_URL_SECRET in .env is not a web address (it should look like https://hc-ping.com/...): the dead-man's switch would never be pinged")
    else:
        rep.add(None, "no dead-man's switch (HEARTBEAT_URL_SECRET): if the computer or its internet dies, nothing can tell you. A free healthchecks.io check does it")
    if not env.get("ALERT_UTC_OFFSET_HOURS"):
        rep.add(None, "ALERT_UTC_OFFSET_HOURS is not set, so alert times show UTC (Uganda is 3)")


# ------------------------------------------------------------ 6. secrets ----
def api_keys(env: Dict[str, str]) -> List[str]:
    return [p.split(":", 1)[0].strip() for p in (env.get("RAG_API_KEYS") or "").split(",") if ":" in p]


def _git_ignored(project_dir: str, path: str) -> Optional[bool]:
    if not shutil.which("git"):
        return None
    d = os.path.abspath(project_dir)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            break
        if os.path.dirname(d) == d:
            return None                                # not a git project: nothing to leak into
        d = os.path.dirname(d)
    rel = os.path.relpath(os.path.join(os.path.abspath(project_dir), path), d)
    r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=d, capture_output=True)
    return r.returncode == 0 if r.returncode in (0, 1) else None


def check_secrets(rep: Report, project_dir: str, env: Dict[str, str]) -> None:
    keys, mkey = api_keys(env), env.get("RAG_METRICS_KEY", "")
    if not keys:
        rep.add(False, "RAG_API_KEYS is not set in .env")
    else:
        short = [k for k in keys if len(k) < 16]
        weak = [k for k in keys + [mkey] if k and any(w in k.lower() for w in WEAK_WORDS)]
        rep.add(not short, f"{len(keys)} API key(s), all at least 16 characters" if not short else f"{len(short)} API key(s) shorter than 16 characters")
        rep.add(not weak, "no key looks like an example or a guessable word" if not weak else f"{len(weak)} key(s) contain an example word (test, password, secret, 1234 ...): make new ones (python manage_keys.py add)")
    if mkey:
        rep.add(len(mkey) >= 16 and mkey not in keys, "the metrics key is long enough and is not one of the API keys" if len(mkey) >= 16 and mkey not in keys
                else "RAG_METRICS_KEY is too short or the same as an API key: it must be its own long key")
    else:
        rep.add(False, "RAG_METRICS_KEY is not set in .env (Day 20 Cell 4): the watchdog cannot read /metrics")
    docs = (env.get("RAG_ENABLE_DOCS") or "").strip().lower() in ("1", "true", "yes", "on")
    rep.add(not docs, "the interactive API pages (/docs) are switched off" if not docs else "RAG_ENABLE_DOCS is on: the door hides /docs, but switch it off at the source too")
    for path, why in ((".env", "your keys"), ("backups/LATEST.json", "your conversations (the backups)")):
        ign = _git_ignored(project_dir, path)
        if ign is False:
            rep.add(False, f"git would commit {why}: add `{path.split('/')[0]}{'/' if path.startswith('backups') else ''}` to .gitignore")
        elif ign is True:
            rep.add(True, f"git ignores {path.split('/')[0]} ({why})")


# ------------------------------------------------------------ 7. one question ----
def check_ask(rep: Report, base: str, key: str, question: str) -> None:
    if not key:
        rep.add(False, "cannot ask: RAG_API_KEYS is not set in .env")
        return
    t0 = time.time()
    st, hdr, raw = call(base, "POST", "/ask", headers={"X-API-Key": key}, body=json.dumps({"question": question, "session_id": "launchcheck-" + str(int(t0))}).encode(), timeout=180)
    took = time.time() - t0
    if st != 200:
        rep.add(False, f"a real question through the door got HTTP {st if st else _why(raw)} after {took:.0f}s" +
                (" (the API is still loading models: wait and try again)" if st in (502, 503, 504) else ""))
        return
    try:
        j = json.loads(raw)
    except Exception:
        rep.add(False, "the answer is not JSON")
        return
    rep.add(bool((j.get("answer") or "").strip()), f"a real question was answered through the door in {took:.1f}s" if (j.get("answer") or "").strip() else "the answer was empty")
    rid = hdr.get("x-request-id", "")
    rep.add(bool(rid) and j.get("request_id") == rid and bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", rid)),
            "the answer carries the door's request id (the same id is in the door log and the API log)" if rid else "no X-Request-ID on the answer")


# ------------------------------------------------------------ everything ----
def _proxy_logs(project_dir: str) -> str:
    r = subprocess.run(["docker", "compose", "logs", "--no-color", "--tail", "5000", "proxy"], cwd=project_dir,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "docker compose logs failed").strip().splitlines()[-1])
    return r.stdout + r.stderr


def check_proxy_log(rep: Report, project_dir: str, secrets: List[str], logs: Optional[Callable[[], str]] = None) -> None:
    """After real keys went through the door (the question above), read what the door wrote down: none of them may be in it.
    Only the NUMBER of hits is printed, never a key."""
    try:
        text = logs() if logs else _proxy_logs(project_dir)
    except Exception as e:
        rep.add(None, f"could not read the door's log ({type(e).__name__}: {e}): try  docker compose logs proxy  yourself and look for your keys")
        rep.skip("the door's log")
        return
    secrets = [x for x in secrets if x and len(x) >= 8]
    hits = sum(1 for x in secrets if x in text)
    if hits:
        rep.add(False, f"{hits} of your secret(s) appear in the door's log (docker compose logs proxy)! Change them (manage_keys.py rotate) and fix the Caddyfile (make_frontdoor.py)")
    else:
        rep.add(True, f"the door's log (last {len(text.splitlines())} lines) holds none of your keys, although real ones just went through it")


def check_launch(project_dir: str = ".", door: Optional[str] = None, public: Optional[str] = None, ask: bool = False, docker: bool = True,
                 ps: Optional[Callable[[], List[dict]]] = None, env: Optional[Dict[str, str]] = None,
                 logs: Optional[Callable[[], str]] = None,
                 question: str = "What was Apple's total net sales for fiscal year 2024?") -> Report:
    rep = Report()
    project_dir = os.path.abspath(project_dir)
    if env is None:
        try:
            import notifiers
            env = notifiers.read_env_file(os.path.join(project_dir, ".env")) if os.path.exists(os.path.join(project_dir, ".env")) else {}
        except Exception:
            env = {}
        if not env:
            rep.add(False, ".env is missing or empty (run Day 19 and Day 20 first)")
    door = door or f"http://127.0.0.1:{env.get('RAG_DOOR_PORT') or '8088'}"
    public = public if public is not None else env.get("PUBLIC_URL", "")
    keys = api_keys(env)
    rep.head("1. THE FRONT DOOR  (" + door + ")")
    reachable = check_door(rep, door)
    rep.head("2. THE PUBLIC ADDRESS")
    if public:
        check_public(rep, public)
    else:
        rep.add(None, "no public address given (--public https://... or PUBLIC_URL in .env): only the local door was tested")
        rep.skip("the public address")
    rep.head("3. THE CONTAINERS")
    check_compose_ports(rep, project_dir)
    if docker:
        check_containers(rep, project_dir, bool(public), ps)
    else:
        rep.add(None, "the containers were NOT looked at (--no-docker)")
        rep.skip("the containers and the door log")
    rep.head("4. THE BACKUPS")
    check_backups(rep, os.path.join(project_dir, "backups"), float(env.get("BACKUP_MAX_AGE_HOURS") or 48), float(env.get("DRILL_MAX_AGE_DAYS") or 35))
    rep.head("5. THE ALERTS")
    check_alerts(rep, env, project_dir)
    rep.head("6. THE SECRETS")
    check_secrets(rep, project_dir, env)
    if ask:
        rep.head("7. ONE REAL QUESTION")
        if reachable:
            check_ask(rep, door, keys[0] if keys else "", question)
        else:
            rep.add(False, "skipped: the door cannot be reached")
        if docker:
            check_proxy_log(rep, project_dir, [*keys, env.get("RAG_METRICS_KEY", ""), env.get("HEARTBEAT_URL_SECRET", "")], logs)
    else:
        rep.skip("a real question (add --ask)")
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Day 21 launch check")
    ap.add_argument("--project", default=".", help="the project folder (default: here)")
    ap.add_argument("--door", help="front door address (default http://127.0.0.1:8088)")
    ap.add_argument("--public", help="public https address to test too (default: PUBLIC_URL from .env)")
    ap.add_argument("--ask", action="store_true", help="also ask one real question (costs a few thousand tokens)")
    ap.add_argument("--no-docker", action="store_true", help="do not ask Docker about the containers")
    a = ap.parse_args(argv)
    rep = check_launch(a.project, a.door, a.public, a.ask, not a.no_docker)
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
