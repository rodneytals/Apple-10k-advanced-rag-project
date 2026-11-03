"""Tests for notifiers.py.  Run:  python test_notifiers.py
Every channel talks to a fake server on this computer that speaks the same protocol (fakeservices.py)."""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import email
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from email import policy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import notifiers as nt
from fakeservices import FakeHttp, FakeNtfy, FakeSmtp, FakeTelegram, free_port, make_self_signed

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


TOKEN = FakeTelegram.TOKEN
CRIT = nt.Message("CRITICAL dependency_down", "redis is not answering\n\nrag-api · 2026-10-21 13:14 UTC+3", "critical")
OK = nt.Message("RESOLVED dependency_down", "back to normal", "resolved")


def no_sleep(s):
    no_sleep.calls.append(s)


no_sleep.calls = []


@test
def n01_telegram_sends_the_right_request():
    fake = FakeTelegram()
    try:
        t = nt.TelegramNotifier(TOKEN, "424242", fake.url)
        t.send(CRIT)
        t.send(OK)
        m = fake.messages()
        assert len(m) == 2 and m[0]["chat_id"] == "424242"
        assert "CRITICAL dependency_down" in m[0]["text"] and "redis is not answering" in m[0]["text"] and "🔴" in m[0]["text"]
        assert m[0]["disable_notification"] is False and m[1]["disable_notification"] is True      # a "resolved" does not buzz the phone
        assert fake.requests[0]["path"] == f"/bot{TOKEN}/sendMessage"
        assert fake.requests[0]["headers"]["user-agent"] == nt.USER_AGENT
        long = nt.Message("t", "x" * 10000, "warning")
        t.send(long)
        assert len(fake.messages()[-1]["text"]) <= 4096                                              # Telegram's own limit
    finally:
        fake.stop()


@test
def n02_telegram_failures_are_classified_and_never_show_the_token_or_the_url():
    fake = FakeTelegram()
    try:
        fake.mode = "unauthorized"
        try:
            nt.TelegramNotifier(TOKEN, "424242", fake.url).send(CRIT)
            assert False
        except nt.NotifyError as e:
            assert not e.transient and "Unauthorized" in str(e) and "token" in str(e)
            assert TOKEN not in str(e) and fake.url not in str(e) and "bot" + TOKEN.split(":")[0] not in str(e)
        try:
            nt.TelegramNotifier(TOKEN, "999", fake.url)._call("sendMessage", {"chat_id": "999", "text": "x"}) if False else None
            fake.mode = "ok"
            nt.TelegramNotifier(TOKEN, "999", fake.url).send(CRIT)                                       # wrong chat id
            assert False
        except nt.NotifyError as e:
            assert not e.transient and "chat not found" in str(e), str(e)
        fake.mode = "server"
        try:
            nt.TelegramNotifier(TOKEN, "424242", fake.url).send(CRIT)
            assert False
        except nt.NotifyError as e:
            assert e.transient and "502" in str(e)
        fake.mode = "rate"
        fake.rate_left = 1
        try:
            nt.TelegramNotifier(TOKEN, "424242", fake.url).send(CRIT)
            assert False
        except nt.NotifyError as e:
            assert e.transient and e.retry_after == 1.0
    finally:
        fake.stop()
    dead = f"http://127.0.0.1:{free_port()}"
    try:
        nt.TelegramNotifier(TOKEN, "424242", dead).send(CRIT)
        assert False
    except nt.NotifyError as e:
        assert e.transient and "ConnectionRefusedError" in str(e), str(e)
        assert TOKEN not in str(e) and dead not in str(e) and "127.0.0.1" not in str(e)


@test
def n03_telegram_finds_the_chat_id_from_getupdates():
    fake = FakeTelegram()
    try:
        assert nt.TelegramNotifier(TOKEN, "0", fake.url).find_chats() == [("424242", "Rodney")]
        fake.updates = []
        assert nt.TelegramNotifier(TOKEN, "0", fake.url).find_chats() == []
    finally:
        fake.stop()


@test
def n04_ntfy_headers_are_ascii_and_the_body_keeps_its_unicode():
    fake = FakeNtfy()
    try:
        n = nt.NtfyNotifier(fake.topic, fake.url)
        n.send(nt.Message("CRITICAL ✱ dependency_down", "Redis ntakola 🔴 Ssebo", "critical"))
        r = fake.requests[0]
        assert r["path"] == "/" + fake.topic
        assert r["headers"]["priority"] == "5" and r["headers"]["tags"] == "rotating_light"
        assert r["headers"]["title"].isascii() and "dependency_down" in r["headers"]["title"]
        assert r["body"].decode("utf-8") == "Redis ntakola 🔴 Ssebo"
        n.send(OK)
        assert fake.requests[1]["headers"]["priority"] == "3"
        fake.need_token = "tk_secret"
        try:
            n.send(CRIT)
            assert False
        except nt.NotifyError as e:
            assert not e.transient and "403" in str(e)
        nt.NtfyNotifier(fake.topic, fake.url, "tk_secret").send(CRIT)
        fake.mode = "server"
        fake.need_token = None
        try:
            n.send(CRIT)
            assert False
        except nt.NotifyError as e:
            assert e.transient and fake.topic not in str(e) and fake.url not in str(e)
    finally:
        fake.stop()


def _parse(raw):
    return email.message_from_string(raw, policy=policy.default)


@test
def n05_email_plain_with_login():
    s = FakeSmtp(mode="plain")
    try:
        e = nt.EmailNotifier("127.0.0.1", s.port, s.user, s.password, "me@example.test", security="none")
        e.send(CRIT)
        assert s.logins == [True] and len(s.messages) == 1
        m = _parse(s.messages[0]["raw"])
        assert "dependency_down" in m["Subject"] and m["To"] == "me@example.test" and m["From"] == s.user
        assert "redis is not answering" in m.get_content()
        e2 = nt.EmailNotifier("127.0.0.1", s.port, s.user, "wrong-password", "me@example.test", security="none")
        try:
            e2.send(CRIT)
            assert False
        except nt.NotifyError as ex:
            assert not ex.transient and "login" in str(ex) and "wrong-password" not in str(ex) and s.user not in str(ex)
        assert len(s.messages) == 1
    finally:
        s.stop()
    try:
        nt.EmailNotifier("127.0.0.1", free_port(), "u", "p", "me@example.test", security="none").send(CRIT)
        assert False
    except nt.NotifyError as ex:
        assert ex.transient and "ConnectionRefusedError" in str(ex)


@test
def n06_email_over_starttls_and_over_ssl_with_certificate_checking():
    d = tempfile.mkdtemp()
    try:
        tls = make_self_signed(d)
        if not tls:
            raise Skip("no openssl program on this computer to make a test certificate")
        for mode, sec in (("starttls", "starttls"), ("ssl", "ssl")):
            s = FakeSmtp(mode=mode, tls=tls)
            try:
                nt.EmailNotifier("127.0.0.1", s.port, s.user, s.password, "me@example.test", security=sec, ssl_context=s.client_ctx).send(OK)
                assert s.logins == [True] and len(s.messages) == 1, (mode, s.logins)
                # the real default context does NOT trust a self-signed certificate: the mail must not go out (and the error names only a class)
                try:
                    nt.EmailNotifier("127.0.0.1", s.port, s.user, s.password, "me@example.test", security=sec).send(OK)
                    assert False, "an untrusted certificate was accepted"
                except nt.NotifyError as ex:
                    assert s.password not in str(ex)
                assert len(s.messages) == 1
            finally:
                s.stop()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def n07_one_broken_channel_does_not_stop_the_others_and_nothing_secret_is_logged():
    tg, nf = FakeTelegram(), FakeNtfy()
    tg.mode = "unauthorized"
    logs = []
    try:
        disp = nt.Dispatcher([nt.TelegramNotifier(TOKEN, "424242", tg.url), nt.NtfyNotifier(nf.topic, nf.url)], sleep=no_sleep, log=logs.append)
        res = disp.send(CRIT)
        assert res["ntfy"] == "ok" and res["telegram"].startswith("telegram: refused"), res
        assert len(nf.requests) == 1 and not disp.pending                                   # one way worked: nothing to keep
        blob = "\n".join(logs) + json.dumps(res)
        assert TOKEN not in blob and nf.topic not in blob and "127.0.0.1" not in blob
    finally:
        tg.stop()
        nf.stop()


@test
def n08_a_notifier_that_blows_up_is_contained_and_its_message_is_not_repeated():
    class Bad(nt.Notifier):
        name = "bad"

        def send(self, msg):
            raise RuntimeError("secret-in-message " + TOKEN)
    logs = []
    disp = nt.Dispatcher([Bad()], sleep=no_sleep, log=logs.append)
    res = disp.send(CRIT)
    assert res == {"bad": "bad: unexpected RuntimeError"}, res
    assert TOKEN not in "\n".join(logs) + str(res) and "secret-in-message" not in str(res) + "\n".join(logs)


@test
def n09_transient_errors_are_retried_with_backoff_permanent_ones_are_not():
    tg = FakeTelegram()
    no_sleep.calls = []
    try:
        tg.mode = "rate"
        tg.rate_left = 1
        disp = nt.Dispatcher([nt.TelegramNotifier(TOKEN, "424242", tg.url)], retry_waits=(2.0, 8.0), sleep=no_sleep, log=lambda s: None)
        assert disp.send(CRIT) == {"telegram": "ok"}
        assert no_sleep.calls == [1.0]                                                      # obeyed Telegram's own retry_after
        tg.mode = "server"
        no_sleep.calls = []
        res = disp.send(CRIT)
        assert res["telegram"].startswith("telegram: its server had a problem")
        assert no_sleep.calls == [2.0, 8.0]
        tg.mode = "unauthorized"
        no_sleep.calls = []
        disp.send(CRIT)
        assert no_sleep.calls == []                                                         # wrong token: retrying would never help
    finally:
        tg.stop()


@test
def n10_when_nothing_gets_through_messages_are_kept_and_delivered_late_in_order():
    tg = FakeTelegram()
    try:
        tg.mode = "server"
        disp = nt.Dispatcher([nt.TelegramNotifier(TOKEN, "424242", tg.url)], retry_waits=(), sleep=no_sleep, log=lambda s: None)
        disp.send(nt.Message("first", "a", "critical"))
        disp.send(nt.Message("second", "b", "warning"))
        assert len(disp.pending) == 2
        assert disp.flush() == 0 and len(disp.pending) == 2                               # still down: nothing lost, nothing sent
        tg.mode = "ok"
        assert disp.flush() == 2 and not disp.pending
        texts = [m["text"] for m in tg.messages()][-2:]                                   # (earlier entries are the attempts that were refused)
        assert "first" in texts[0] and "second" in texts[1] and "delivered late" in texts[0] and "delivered late" in texts[1]
    finally:
        tg.stop()


@test
def n11_the_kept_queue_is_bounded():
    class Down(nt.Notifier):
        name = "down"

        def send(self, msg):
            raise nt.NotifyError("down: no", transient=False)
    disp = nt.Dispatcher([Down()], max_pending=5, sleep=no_sleep, log=lambda s: None)
    for i in range(12):
        disp.send(nt.Message(f"m{i}", "x"))
    assert len(disp.pending) == 5 and disp.dropped == 7
    assert [m.title for _, m in disp.pending] == ["m7", "m8", "m9", "m10", "m11"]       # the newest are kept


@test
def n12_env_setup_is_read_without_printing_values_and_half_setups_are_noticed():
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "NTFY_TOPIC_SECRET": "tpc-1234", "SMTP_HOST": "smtp.example.test", "SMTP_USER": "me", "ALERT_EMAIL_TO": "me@example.test"}
    names = [n.name for n in nt.from_env(env)]
    assert names == ["ntfy", "email"], names                                              # telegram lacks a chat id; email works without a password
    text = "\n".join(nt.describe(env))
    assert "telegram: half set up, missing TELEGRAM_CHAT_ID" in text and "easy to guess" in text and "email: half set up, missing SMTP_PASSWORD" in text
    for secret in (TOKEN, "tpc-1234", "smtp.example.test", "me@example.test"):
        assert secret not in text, secret
    assert nt.describe({}) == ["–  telegram: not set up", "–  ntfy: not set up", "–  email: not set up"]


@test
def n13_ntfy_topic_is_made_once_kept_in_a_file_and_never_overwritten():
    d = tempfile.mkdtemp()
    try:
        envp, topicp = os.path.join(d, ".env"), os.path.join(d, "ntfy_topic.txt")
        open(envp, "w").write("GROQ_API_KEY=abc")                                         # NO trailing newline: the new line must not glue onto it
        msg = nt.ensure_ntfy_topic(envp, topicp)
        env = nt.read_env_file(envp)
        topic = env["NTFY_TOPIC_SECRET"]
        assert env["GROQ_API_KEY"] == "abc" and len(topic) >= 30 and topic.startswith("rag-alerts-")
        assert topic in open(topicp).read() and topic not in msg and "created" in msg
        assert open(envp + ".before_ntfy_topic").read() == "GROQ_API_KEY=abc"
        assert "already there" in nt.ensure_ntfy_topic(envp, topicp) and nt.read_env_file(envp)["NTFY_TOPIC_SECRET"] == topic
        open(envp, "a").write("")
        open(envp, "w").write("NTFY_TOPIC_SECRET=abc\n")
        try:
            nt.ensure_ntfy_topic(envp, topicp)
            assert False
        except ValueError:
            pass
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def n14_ensure_env_values_adds_only_what_is_missing():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, ".env")
        open(p, "w").write("A=1\nALERT_UTC_OFFSET_HOURS=5\n")
        added = nt.ensure_env_values(p, {"ALERT_UTC_OFFSET_HOURS": "3", "ALERT_LABEL": "rag-api"})
        assert added == ["ALERT_LABEL"] and nt.read_env_file(p) == {"A": "1", "ALERT_UTC_OFFSET_HOURS": "5", "ALERT_LABEL": "rag-api"}
        assert nt.ensure_env_values(p, {"ALERT_LABEL": "x"}) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def n15_time_stamp_uses_the_offset():
    t = 1792560000.0                                    # 2026-10-21 05:20:00 UTC
    assert nt.stamp({}, t).endswith("UTC") and "05:20" in nt.stamp({}, t)
    assert nt.stamp({"ALERT_UTC_OFFSET_HOURS": "3"}, t) == "2026-10-21 08:20 UTC+3"
    assert nt.stamp({"ALERT_UTC_OFFSET_HOURS": "junk"}, t).endswith("UTC")


@test
def n16_selftest_reports_each_channel_alone_and_leaves_a_record():
    tg, nf = FakeTelegram(), FakeNtfy()
    d = tempfile.mkdtemp()
    try:
        nf.mode = "server"
        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "424242", "TELEGRAM_API_BASE": tg.url,
               "NTFY_TOPIC_SECRET": nf.topic, "NTFY_SERVER": nf.url, "ALERT_LABEL": "rag-api-test"}
        res = nt.selftest(env, sleep=no_sleep)
        assert res["telegram"] == "ok" and res["ntfy"].startswith("ntfy: not accepted"), res
        assert "rag-api-test" in tg.messages()[0]["text"]
        rec = os.path.join(d, "alert_test.json")
        nt.write_test_record(rec, res)
        j = json.load(open(rec))
        assert j["ok_channels"] == ["telegram"] and j["failed_channels"] == ["ntfy"] and time.time() - j["at"] < 60
    finally:
        tg.stop()
        nf.stop()
        shutil.rmtree(d, ignore_errors=True)


@test
def n17_command_line_status_and_test_print_no_secret_values():
    tg, nf = FakeTelegram(), FakeNtfy()
    d = tempfile.mkdtemp()
    try:
        envp = os.path.join(d, ".env")
        open(envp, "w").write(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=424242\nTELEGRAM_API_BASE={tg.url}\n"
                              f"NTFY_TOPIC_SECRET={nf.topic}\nNTFY_SERVER={nf.url}\n")
        py = [sys.executable, os.path.join(HERE, "notifiers.py"), "--env-file", envp]
        r = subprocess.run(py + ["--status"], capture_output=True, text=True)
        assert r.returncode == 0 and "✅ telegram" in r.stdout and "✅ ntfy" in r.stdout
        r2 = subprocess.run(py + ["--test"], capture_output=True, text=True)
        assert r2.returncode == 0 and "telegram: delivered" in r2.stdout and "ntfy: delivered" in r2.stdout, r2.stdout + r2.stderr
        r3 = subprocess.run(py + ["--telegram-chat"], capture_output=True, text=True)
        assert "TELEGRAM_CHAT_ID=424242" in r3.stdout
        for out in (r.stdout, r.stderr, r2.stdout, r2.stderr, r3.stderr):
            assert TOKEN not in out and nf.topic not in out, out
        assert os.path.exists(os.path.join(d, "alert_test.json"))
        tg.mode = "unauthorized"
        r4 = subprocess.run(py + ["--test"], capture_output=True, text=True)
        assert r4.returncode == 1 and "❌" in r4.stdout and TOKEN not in r4.stdout + r4.stderr
    finally:
        tg.stop()
        nf.stop()
        shutil.rmtree(d, ignore_errors=True)


@test
def n18_from_environment_uses_the_process_settings_not_the_env_file_and_writes_no_record():
    nf = FakeNtfy()
    d = tempfile.mkdtemp()
    try:
        env = dict(os.environ, NTFY_TOPIC_SECRET=nf.topic, NTFY_SERVER=nf.url)
        py = [sys.executable, os.path.join(HERE, "notifiers.py"), "--env-file", os.path.join(d, ".env"), "--from-environment"]
        r = subprocess.run(py + ["--test"], capture_output=True, text=True, env=env)
        assert r.returncode == 0 and "ntfy: delivered" in r.stdout and nf.topic not in r.stdout + r.stderr, r.stdout + r.stderr
        assert not os.path.exists(os.path.join(d, "alert_test.json")), "the container's filesystem is read-only: no record file there"
        r2 = subprocess.run(py + ["--status"], capture_output=True, text=True, env=env)
        assert "✅ ntfy" in r2.stdout and nf.topic not in r2.stdout
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("NTFY", "TELEGRAM", "SMTP"))}
        r3 = subprocess.run(py + ["--test"], capture_output=True, text=True, env=clean)
        assert r3.returncode == 2 and "No way of sending" in r3.stdout
    finally:
        nf.stop()
        shutil.rmtree(d, ignore_errors=True)


@test
def n19_a_token_with_a_typo_never_appears_in_a_message_or_a_traceback():
    token = "123456:ABC-secret token-with-a-space"          # a space inside the address makes http.client raise InvalidURL, whose TEXT holds the address
    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, ".env"), "w").write(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID=42\n")
        py = [sys.executable, os.path.join(HERE, "notifiers.py"), "--env-file", os.path.join(d, ".env")]
        for flag in ("--telegram-chat", "--test"):
            r = subprocess.run(py + [flag], capture_output=True, text=True)
            out = r.stdout + r.stderr
            assert "ABC-secret" not in out and "token-with-a-space" not in out and "Traceback" not in out, (flag, out)
            assert r.returncode != 0, (flag, out)
        try:                                                                  # the low-level call itself must turn ANY failure into a safe message
            nt._http("GET", "http://127.0.0.1:1/bot" + token + "/x", None, {}, 2, "telegram")
            raise AssertionError("expected a NotifyError")
        except nt.NotifyError as e:
            assert "ABC-secret" not in str(e) and "InvalidURL" in str(e), str(e)
        # and in the loop that sends real alerts
        seen = []
        disp = nt.Dispatcher([nt.TelegramNotifier(token, "42")], retry_waits=(), sleep=lambda s: None, log=seen.append)
        disp.send(nt.Message("x", "y", "warning"))
        disp.flush()
        assert not any("ABC-secret" in str(x) or "token-with-a-space" in str(x) for x in seen), seen
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
    print(f"\n{total - bad}/{total} passed" + (f" ({skipped} skipped)" if skipped else ""))
    sys.exit(1 if bad else 0)
