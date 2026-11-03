"""
notifiers.py -- Day 21: get an alert onto your phone or into your inbox.

Three ways out. Fill in the ones you want in .env; the watchdog uses every one that is complete:

  Telegram   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID                 (a bot you create with @BotFather; it messages you)
  ntfy       NTFY_TOPIC_SECRET  [NTFY_SERVER=https://ntfy.sh]     (free app on your phone; the topic name IS the password)
  e-mail     SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO  [SMTP_PORT=587, SMTP_SECURITY=starttls|ssl|none, SMTP_FROM]

    python notifiers.py --status            which ways are set up (names only, never values)
    python notifiers.py --setup-ntfy        makes a hard-to-guess topic, saves it in .env and in ntfy_topic.txt
    python notifiers.py --telegram-chat     after you pressed Start in your bot: shows the chat id to put in .env
    python notifiers.py --test              sends a test message through every way and says which arrived
    docker compose exec -T watchdog python notifiers.py --test --from-environment     the same, from INSIDE the watchdog container

Rules this file keeps (each one has a test):
  * A failed delivery never raises into the watchdog and never prints a token, topic, password or URL: only the CHANNEL NAME and the
    error CLASS (for example "telegram: cannot reach (TimeoutError)"). Telegram puts the token inside the URL, so printing a URL or
    an exception message would leak it.
  * A temporary problem (no internet, a 5xx, 'slow down') is retried a few times; a permanent one (wrong token) is not.
  * If nothing could be delivered the message is kept (the newest 50) and tried again on the next round.

Standard library only.
"""
import argparse
import json
import os
import secrets
import shutil
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable, Deque, Dict, List, Optional, Tuple

USER_AGENT = "rag-api-watchdog/1.0"
EMOJI = {"critical": "🔴", "warning": "🟠", "resolved": "🟢", "info": "ℹ️"}
NTFY_PRIORITY = {"critical": "5", "warning": "4", "resolved": "3", "info": "2"}
NTFY_TAGS = {"critical": "rotating_light", "warning": "warning", "resolved": "white_check_mark", "info": "information_source"}


@dataclass
class Message:
    title: str
    body: str
    severity: str = "info"                    # critical | warning | resolved | info


class NotifyError(Exception):
    """Text is safe to print: channel name and error class only."""

    def __init__(self, text: str, transient: bool = True, retry_after: Optional[float] = None):
        super().__init__(text)
        self.transient, self.retry_after = transient, retry_after


def stamp(env: Optional[Dict[str, str]] = None, now: Optional[float] = None) -> str:
    env = os.environ if env is None else env
    try:
        off = float(env.get("ALERT_UTC_OFFSET_HOURS", "0"))
    except ValueError:
        off = 0.0
    t = datetime.fromtimestamp(now or time.time(), timezone.utc) + timedelta(hours=off)
    label = "UTC" if off == 0 else f"UTC{off:+g}"
    return t.strftime("%Y-%m-%d %H:%M ") + label


def _http(method: str, url: str, data: Optional[bytes], headers: Dict[str, str], timeout: float, who: str) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                                  # an answer, but not a good one
        try:
            body = e.read(20000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except urllib.error.URLError as e:                                    # e.reason is the real exception: print its CLASS only
        raise NotifyError(f"{who}: cannot reach it ({type(e.reason).__name__})")
    except Exception as e:                                                # OSError, ValueError, http.client.InvalidURL / BadStatusLine ...:
        raise NotifyError(f"{who}: cannot reach it ({type(e).__name__})")   # their TEXT can contain the address with the token: print the class only


class Notifier:
    name = "?"

    def send(self, msg: Message) -> None:
        raise NotImplementedError


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, api_base: str = "https://api.telegram.org", timeout: float = 10.0):
        self._token, self.chat_id, self.api_base, self.timeout = token, str(chat_id), api_base.rstrip("/"), timeout

    def _call(self, method: str, payload: dict) -> dict:
        status, body = _http("POST", f"{self.api_base}/bot{self._token}/{method}", json.dumps(payload).encode(),
                             {"Content-Type": "application/json"}, self.timeout, self.name)
        try:
            d = json.loads(body)
        except ValueError:
            d = {}
        if status == 200 and d.get("ok"):
            return d
        desc = str(d.get("description", ""))[:80]                          # Telegram's own words: "Unauthorized", "chat not found"...
        if status == 429:
            wait = (d.get("parameters") or {}).get("retry_after")
            raise NotifyError(f"{self.name}: asked us to slow down", transient=True, retry_after=float(wait) if wait else None)
        if status >= 500:
            raise NotifyError(f"{self.name}: its server had a problem (HTTP {status})", transient=True)
        hint = " (is the token right? did you press Start in the bot?)" if status in (400, 401, 403, 404) else ""
        raise NotifyError(f"{self.name}: refused (HTTP {status}: {desc}){hint}", transient=False)

    def send(self, msg: Message) -> None:
        text = f"{EMOJI.get(msg.severity, '')} {msg.title}\n\n{msg.body}"[:3900]
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "disable_notification": msg.severity in ("resolved", "info")})

    def find_chats(self) -> List[Tuple[str, str]]:
        d = self._call("getUpdates", {"timeout": 0})
        seen: Dict[str, str] = {}
        for u in d.get("result", []):
            chat = ((u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}).get("chat")) or {}
            if "id" in chat:
                seen[str(chat["id"])] = chat.get("title") or chat.get("first_name") or chat.get("username") or "?"
        return sorted(seen.items())


class NtfyNotifier(Notifier):
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: Optional[str] = None, timeout: float = 10.0):
        self._topic, self.server, self._token, self.timeout = topic, server.rstrip("/"), token, timeout

    def send(self, msg: Message) -> None:
        ascii_title = msg.title.encode("ascii", "replace").decode("ascii")[:120]              # HTTP header values must be plain ASCII
        headers = {"Title": ascii_title, "Priority": NTFY_PRIORITY.get(msg.severity, "3"), "Tags": NTFY_TAGS.get(msg.severity, "bell")}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        status, body = _http("POST", f"{self.server}/{self._topic}", msg.body.encode("utf-8"), headers, self.timeout, self.name)
        if status == 200:
            return
        if status == 429 or status >= 500:
            raise NotifyError(f"{self.name}: not accepted right now (HTTP {status})", transient=True)
        raise NotifyError(f"{self.name}: refused (HTTP {status})", transient=False)


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, host: str, port: int, user: str, password: str, to: str, sender: Optional[str] = None,
                 security: str = "starttls", timeout: float = 20.0, ssl_context: Optional[ssl.SSLContext] = None):
        self.host, self.port, self._user, self._password = host, port, user, password
        self.to, self.sender, self.security, self.timeout = to, sender or user, security.lower(), timeout
        self.ctx = ssl_context

    def send(self, msg: Message) -> None:
        m = EmailMessage()
        m["Subject"] = f"{EMOJI.get(msg.severity, '')} {msg.title}"[:150]
        m["From"], m["To"] = self.sender, self.to
        m.set_content(msg.body)
        ctx = self.ctx or ssl.create_default_context()
        try:
            if self.security == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with server:
                server.ehlo()
                if self.security == "starttls":
                    server.starttls(context=ctx)
                    server.ehlo()
                if self._user and self._password:
                    server.login(self._user, self._password)
                server.send_message(m)
        except smtplib.SMTPAuthenticationError:
            raise NotifyError(f"{self.name}: the mail server refused the login (wrong user or app password)", transient=False)
        except smtplib.SMTPRecipientsRefused:
            raise NotifyError(f"{self.name}: the mail server refused the recipient address", transient=False)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            raise NotifyError(f"{self.name}: could not send ({type(e).__name__})")


class ConsoleNotifier(Notifier):
    """Always on: one line in the container log, so there is a record even if every other way fails."""
    name = "log"

    def __init__(self, write: Callable[[str], None] = lambda s: print(s, flush=True)):
        self.write = write

    def send(self, msg: Message) -> None:
        self.write(f"ALERT {msg.severity.upper()} {msg.title}: {msg.body.splitlines()[0] if msg.body else ''}")


# ------------------------------------------------------------ from .env -----
def from_env(env: Optional[Dict[str, str]] = None) -> List[Notifier]:
    env = os.environ if env is None else env
    out: List[Notifier] = []
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        out.append(TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], env.get("TELEGRAM_API_BASE", "https://api.telegram.org")))
    if env.get("NTFY_TOPIC_SECRET"):
        out.append(NtfyNotifier(env["NTFY_TOPIC_SECRET"], env.get("NTFY_SERVER", "https://ntfy.sh"), env.get("NTFY_TOKEN") or None))
    if env.get("SMTP_HOST") and env.get("ALERT_EMAIL_TO"):
        try:
            port = int(env.get("SMTP_PORT", "587"))
        except ValueError:
            port = 587
        out.append(EmailNotifier(env["SMTP_HOST"], port, env.get("SMTP_USER", ""), env.get("SMTP_PASSWORD", ""), env["ALERT_EMAIL_TO"],
                                 env.get("SMTP_FROM") or None, env.get("SMTP_SECURITY", "starttls")))
    return out


def describe(env: Optional[Dict[str, str]] = None) -> List[str]:
    """What is set up and what is half-set-up. Names only."""
    env = os.environ if env is None else env
    lines = []
    have = {n.name for n in from_env(env)}
    for name, needs in (("telegram", ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")), ("ntfy", ("NTFY_TOPIC_SECRET",)),
                        ("email", ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"))):
        missing = [k for k in needs if not env.get(k)]
        if name in have and not missing:
            lines.append(f"✅ {name}: set up")
        elif len(missing) < len(needs):
            lines.append(f"⚠️  {name}: half set up, missing {', '.join(missing)}")
        else:
            lines.append(f"–  {name}: not set up")
    topic = env.get("NTFY_TOPIC_SECRET", "")
    if topic and len(topic) < 20:
        lines.append("⚠️  ntfy: the topic is short and easy to guess; anyone who guesses it can read your alerts and send fake ones. Run --setup-ntfy")
    return lines


# ------------------------------------------------------------ delivery ------
class Dispatcher:
    """Sends every message through every channel. Never raises."""

    def __init__(self, notifiers: List[Notifier], retry_waits: Tuple[float, ...] = (2.0, 8.0), sleep: Callable[[float], None] = time.sleep,
                 max_pending: int = 50, log: Callable[[str], None] = lambda s: print(s, flush=True)):
        self.notifiers, self.retry_waits, self.sleep, self.log = notifiers, retry_waits, sleep, log
        self.pending: Deque[Tuple[float, Message]] = deque(maxlen=max_pending)
        self.dropped = 0

    def channels(self) -> List[str]:
        return [n.name for n in self.notifiers if n.name != "log"]

    def _one(self, n: Notifier, msg: Message) -> Optional[str]:
        """None = delivered; otherwise a printable reason."""
        waits = list(self.retry_waits)
        while True:
            try:
                n.send(msg)
                return None
            except NotifyError as e:
                if not e.transient or not waits:
                    return str(e)
                self.sleep(min(e.retry_after or waits.pop(0), 30.0))
                if e.retry_after:
                    waits.pop(0)
            except Exception as e:                                           # a bug in a notifier must not stop the watchdog
                return f"{n.name}: unexpected {type(e).__name__}"

    def send(self, msg: Message, remember: bool = True) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for n in self.notifiers:
            err = self._one(n, msg)
            result[n.name] = "ok" if err is None else err
            if err is not None:
                self.log(f"WARNING delivery failed: {err}")
        real = [v for k, v in result.items() if k != "log"]
        if real and not any(v == "ok" for v in real) and remember:
            if len(self.pending) == self.pending.maxlen:
                self.dropped += 1
            self.pending.append((time.time(), msg))
        return result

    def flush(self) -> int:
        """Try the kept messages again, oldest first. Stops at the first one that still cannot be delivered."""
        sent = 0
        while self.pending:
            ts, msg = self.pending[0]
            late = Message(msg.title, msg.body + f"\n(delivered late: this happened {max(1, int((time.time() - ts) / 60))} min ago)", msg.severity)
            real = {k: v for k, v in self.send(late, remember=False).items() if k != "log"}
            if real and not any(v == "ok" for v in real.values()):
                break
            self.pending.popleft()
            sent += 1
        return sent


def selftest(env: Optional[Dict[str, str]] = None, sleep: Callable[[float], None] = time.sleep) -> Dict[str, str]:
    """One test message through each channel on its own, so you learn which one works."""
    env = os.environ if env is None else env
    label = env.get("ALERT_LABEL", "rag-api")
    msg = Message(f"TEST from {label}", f"If you can read this, alerts from {label} reach you.\nSent {stamp(env)}.", "info")
    out = {}
    for n in from_env(env):
        d = Dispatcher([n], sleep=sleep, log=lambda s: None)
        out[n.name] = d.send(msg, remember=False)[n.name]
    return out


# ------------------------------------------------------------ .env helpers --
def read_env_file(path: str) -> Dict[str, str]:
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in (dotenv_values(path) or {}).items() if v is not None}
    except ImportError:
        out = {}
        for line in open(path, encoding="utf-8-sig"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("'\"")
        return out


def _append_env(path: str, line: str, backup_suffix: str) -> None:
    raw = open(path, "rb").read() if os.path.exists(path) else b""
    if raw:
        shutil.copyfile(path, path + backup_suffix)
    nl = b"" if not raw or raw.endswith(b"\n") else b"\n"                  # the last line may have no newline: never glue onto it
    with open(path, "ab") as f:
        f.write(nl + line.encode("utf-8") + b"\n")


def ensure_env_values(env_file: str, wanted: Dict[str, str]) -> List[str]:
    """Adds NAME=value lines that are not in .env yet. Never overwrites a value you set. Returns the names added."""
    have = read_env_file(env_file) if os.path.exists(env_file) else {}
    added = []
    for k, v in wanted.items():
        if k not in have:
            _append_env(env_file, f"{k}={v}", ".before_alert_setup")
            added.append(k)
    return added


def ensure_ntfy_topic(env_file: str, topic_file: str = "ntfy_topic.txt") -> str:
    """Creates NTFY_TOPIC_SECRET in .env if it is missing; writes the topic into topic_file (so it is never printed in a notebook)."""
    env = read_env_file(env_file) if os.path.exists(env_file) else {}
    topic = env.get("NTFY_TOPIC_SECRET")
    if topic and len(topic) >= 20:
        status = "already there"
    else:
        if topic:
            raise ValueError("NTFY_TOPIC_SECRET in .env is short: delete that line and run this again")
        topic = "rag-alerts-" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")
        _append_env(env_file, f"NTFY_TOPIC_SECRET={topic}", ".before_ntfy_topic")
        status = "created"
    with open(topic_file, "w", encoding="utf-8") as f:
        f.write("ntfy topic (this is the password: do not share it)\n" + topic + "\n\n"
                "On your phone: install the ntfy app, tap +, paste the topic, leave the server as ntfy.sh.\n")
    return f"NTFY_TOPIC_SECRET {status} in .env. The topic to type into your phone is in {topic_file}"


def write_test_record(path: str, results: Dict[str, str]) -> None:
    ok = sorted(k for k, v in results.items() if v == "ok")
    tmp = path + ".tmp"
    json.dump({"at": time.time(), "ok_channels": ok, "failed_channels": sorted(set(results) - set(ok))}, open(tmp, "w"), indent=2)
    os.replace(tmp, path)


# ----------------------------------------------------------------- CLI ------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--from-environment", action="store_true", help="use the settings of the running process instead of the .env file (inside the watchdog container)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--setup-ntfy", action="store_true")
    g.add_argument("--telegram-chat", action="store_true")
    g.add_argument("--test", action="store_true")
    a = ap.parse_args(argv)
    if a.setup_ntfy:
        print(ensure_ntfy_topic(a.env_file))
        return 0
    env = dict(os.environ) if a.from_environment else (read_env_file(a.env_file) if os.path.exists(a.env_file) else {})
    if a.status:
        print("\n".join(describe(env)))
        return 0
    if a.telegram_chat:
        if not env.get("TELEGRAM_BOT_TOKEN"):
            print("TELEGRAM_BOT_TOKEN is not in .env yet")
            return 2
        try:
            chats = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], "0", env.get("TELEGRAM_API_BASE", "https://api.telegram.org")).find_chats()
        except NotifyError as e:
            print(e)
            return 1
        except Exception as e:                                             # never a traceback: it could show the token
            print(f"telegram: failed ({type(e).__name__})")
            return 1
        if not chats:
            print("No chat found. Open your bot in Telegram, press Start (or send it any message), then run this again.")
            return 1
        for cid, name in chats:
            print(f"TELEGRAM_CHAT_ID={cid}    ({name})")
        return 0
    results = selftest(env)
    if not results:
        print("No way of sending is set up yet (see --status).")
        return 2
    for k, v in results.items():
        print(("✅ " if v == "ok" else "❌ ") + (f"{k}: delivered" if v == "ok" else v))
    if not a.from_environment:
        write_test_record(os.path.join(os.path.dirname(os.path.abspath(a.env_file)), "alert_test.json"), results)
    return 0 if all(v == "ok" for v in results.values()) else 1


def _utf8_stdio():
    """Windows hands a program a pipe that only knows cp1252, which cannot print the marks used here: ask for UTF-8 instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _utf8_stdio()
    raise SystemExit(main())
