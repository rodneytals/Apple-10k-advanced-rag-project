"""Tests for manage_keys.py.  Run:  python test_manage_keys.py"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):      # on Windows a pipe is cp1252 by default and cannot print the marks used here
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
import contextlib
import io
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import manage_keys as mk
import api_layer

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=5)))
    return fn


K1, K2 = "alpha-key-aaaaaaaaaaaaaaaa", "bravo-key-bbbbbbbbbbbbbbbb"


def env(text, crlf=False):
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".env")
    open(p, "wb").write(text.replace("\n", "\r\n" if crlf else "\n").encode())
    return p


def run(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mk.main(list(args))
    return rc, buf.getvalue()


def accepted_by_the_api(p):
    old = os.environ.get("RAG_API_KEYS")
    os.environ["RAG_API_KEYS"] = ",".join(f"{k}:{u}" for u, k in mk.read_keys(p).items())
    try:
        return api_layer.load_keys_from_env()                 # the real parser the API uses at start-up
    finally:
        if old is None:
            os.environ.pop("RAG_API_KEYS", None)
        else:
            os.environ["RAG_API_KEYS"] = old


@test
def add_gives_a_long_key_in_a_file_and_never_on_the_screen():
    p = env(f"GROQ_API_KEY=gk-123\nRAG_API_KEYS={K1}:rodney\nOTHER=1\n")
    rc, out = run("--env", p, "add", "amina")
    assert rc == 0
    key = open(os.path.join(os.path.dirname(p), "new_key_amina.txt")).read().strip()
    assert len(key) >= 32 and key not in out and K1 not in out, out
    keys = accepted_by_the_api(p)
    assert keys == {K1: "rodney", key: "amina"}
    text = open(p).read()
    assert text.startswith("GROQ_API_KEY=gk-123\n") and text.endswith("OTHER=1\n"), text        # nothing else changed
    assert os.path.exists(p + ".before_keys") and "force-recreate --no-deps rag" in out


@test
def revoke_removes_one_and_refuses_to_remove_the_last():
    p = env(f"RAG_API_KEYS={K1}:rodney,{K2}:amina\n")
    assert run("--env", p, "revoke", "amina")[0] == 0
    assert accepted_by_the_api(p) == {K1: "rodney"}
    rc, out = run("--env", p, "revoke", "rodney")
    assert rc == 1 and "last key" in out and accepted_by_the_api(p) == {K1: "rodney"}
    rc, out = run("--env", p, "revoke", "nobody")
    assert rc == 1 and "rodney" in out


@test
def rotate_replaces_only_that_persons_key():
    p = env(f"RAG_API_KEYS={K1}:rodney,{K2}:amina\n")
    assert run("--env", p, "rotate", "amina")[0] == 0
    keys = accepted_by_the_api(p)
    assert K1 in keys and K2 not in keys and sorted(keys.values()) == ["amina", "rodney"]


@test
def list_shows_names_and_the_last_four_characters_only():
    p = env(f"RAG_API_KEYS={K1}:rodney,{K2}:amina\n")
    rc, out = run("--env", p, "list")
    assert rc == 0 and "rodney" in out and "amina" in out and "bbbb" in out
    assert K1 not in out and K2 not in out and "alpha" not in out and "bravo" not in out


@test
def quotes_windows_line_endings_and_a_bom_are_kept():
    p = env(f'RAG_API_KEYS="{K1}:rodney"\nX=1\n', crlf=True)
    assert run("--env", p, "add", "amina")[0] == 0
    raw = open(p, "rb").read()
    assert raw.count(b"\r\n") == 2 and b"\n" not in raw.replace(b"\r\n", b"")
    assert b'RAG_API_KEYS="' + K1.encode() in raw and raw.split(b"\r\n")[0].endswith(b'"')
    assert len(accepted_by_the_api(p)) == 2
    q = env(f"RAG_API_KEYS={K1}:rodney\n")
    body = open(q, "rb").read()
    open(q, "wb").write(b"\xef\xbb\xbf" + body)
    assert run("--env", q, "add", "amina")[0] == 0 and len(accepted_by_the_api(q)) == 2


@test
def bad_names_duplicates_and_missing_pieces_are_clear_errors_that_change_nothing():
    p = env(f"RAG_API_KEYS={K1}:rodney\n")
    before = open(p).read()
    for args, needle in ((("add", "rodney"), "already has a key"), (("add", "a b"), "letters, digits"), (("add", "x:y"), "letters, digits"),
                         (("add",), "needs a name"), (("rotate", "nobody"), "no key")):
        rc, out = run("--env", p, *args)
        assert rc == 1 and needle in out, (args, out)
    assert open(p).read() == before and not os.path.exists(p + ".before_keys")
    assert run("--env", "/nonexistent/.env", "list")[0] == 1
    noline = env("A=1\n")
    assert "not in this .env" in run("--env", noline, "list")[1]


@test
def the_key_file_is_added_to_gitignore_once_if_there_is_one():
    p = env(f"RAG_API_KEYS={K1}:rodney\n")
    gi = os.path.join(os.path.dirname(p), ".gitignore")
    open(gi, "w").write(".env\n")
    run("--env", p, "add", "amina")
    run("--env", p, "add", "joel")
    assert open(gi).read().count("new_key_*.txt") == 1
    assert accepted_by_the_api(p).__len__() == 3


@test
def generated_keys_are_long_unique_and_pass_the_launch_checks():
    ks = {mk.new_key() for _ in range(200)}
    assert len(ks) == 200 and all(len(k) >= 32 for k in ks)
    import check_launch as cl
    assert not any(w in k.lower() for k in ks for w in cl.WEAK_WORDS)


passed = sum(ok for _, ok, _ in results)
for name, ok, tb in results:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(tb)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
