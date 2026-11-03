"""Tests for bounded.py and make_bounded.py.  Run:  python test_bounded.py"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bounded as b
import make_bounded as mb
import audit_globals as ag

results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
    except Exception:
        results.append((fn.__name__, False, traceback.format_exc(limit=4)))
    return fn


@test
def list_keeps_the_newest_and_counts_drops():
    x = b.BoundedList(maxlen=3, name="x")
    for i in range(10):
        x.append(i)
    assert x == [7, 8, 9] and x.dropped == 7 and isinstance(x, list)
    x.extend([10, 11])
    assert x == [9, 10, 11]
    x += [12]
    assert x == [10, 11, 12] and x[-1] == 12 and x[-2:] == [11, 12]
    x.insert(0, "new")
    assert len(x) == 3
    assert json.dumps(b.BoundedList([1, 2], maxlen=5)) == "[1, 2]"
    assert b.BoundedList(range(100), maxlen=10) == list(range(90, 100))


@test
def dict_drops_oldest_key_and_updating_does_not_evict():
    d = b.BoundedDict(maxsize=3, name="d")
    for k in "abcd":
        d[k] = 1
    assert list(d) == ["b", "c", "d"] and d.dropped == 1
    d["b"] = 2                                   # existing key: no eviction
    assert list(d) == ["b", "c", "d"] and d["b"] == 2
    d.update({"e": 5, "f": 6})
    assert list(d) == ["d", "e", "f"]
    assert d.setdefault("z", 9) == 9 and len(d) == 3 and "z" in d
    assert isinstance(d, dict) and json.dumps(b.BoundedDict({"a": 1}, maxsize=5)) == '{"a": 1}'


@test
def dict_with_default_factory_behaves_like_defaultdict():
    counts = b.BoundedDict(maxsize=100, default_factory=int)
    counts["a"] += 1
    counts["a"] += 1
    assert counts["a"] == 2
    lists = b.BoundedDict(maxsize=100, default_factory=list)
    lists["k"].append(1)
    lists["k"].append(2)
    assert lists["k"] == [1, 2]
    plain = b.BoundedDict(maxsize=5)
    try:
        plain["missing"]
    except KeyError:
        pass
    else:
        raise AssertionError("a plain bounded dict must still raise KeyError")
    assert counts.get("never") is None and "never" not in counts


@test
def set_drops_oldest():
    s = b.BoundedSet(maxsize=3)
    for i in range(6):
        s.add(i)
    assert s == {3, 4, 5} and s.dropped == 3
    s.add(4)                                      # already there: nothing changes
    assert s == {3, 4, 5}
    s.discard(3)
    s.add(6)
    s.add(7)
    assert s == {5, 6, 7}, s
    s |= {8}
    assert s == {6, 7, 8}
    assert s.pop() in (6, 7, 8) and len(s) == 2
    s.clear()
    assert len(s) == 0 and not s._order


@test
def many_threads_cannot_break_it():
    lst, dct, st = b.BoundedList(maxlen=500), b.BoundedDict(maxsize=500), b.BoundedSet(maxsize=500)
    errors = []

    def work(n):
        try:
            for i in range(3000):
                lst.append(i)
                dct[(n, i)] = i
                st.add((n, i))
        except Exception as e:
            errors.append(repr(e))
    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors[:3]
    assert len(lst) == 500 and len(dct) == 500 and len(st) == 500 and len(st._order) == 500
    assert lst.dropped == 8 * 3000 - 500


@test
def stats_lists_live_containers():
    x = b.BoundedList(maxlen=4, name="STATS_TEST")
    x.extend(range(6))
    row = [r for r in b.stats() if r[0] == "STATS_TEST"]
    assert row == [("STATS_TEST", 4, 4, 2)], row


# ------------------------------------------------------------- the patcher --
SOURCE = '''\
"""A frozen pipeline (test)."""
import collections
from collections import defaultdict, OrderedDict, Counter, deque

EVENT_LOG = []
SEEN = {}
TAGS = set()
BY_USER = defaultdict(list)
HITS = defaultdict(int)
ORDERED = OrderedDict()
HISTORY = collections.deque()
RECENT = deque([1, 2])
ALREADY = deque(maxlen=5)
COUNTS = Counter()
NOT_GROWING = []                       # read only: must be left alone
CHAINED_A = CHAINED_B = []
CACHE = dict(a=1)


def run(q, sid):
    EVENT_LOG.append((q, sid))
    SEEN[sid] = q
    TAGS.add(q)
    BY_USER[sid].append(q)
    HITS[sid] += 1
    ORDERED[sid] = q
    HISTORY.append(q)
    RECENT.append(q)
    ALREADY.append(q)
    COUNTS[q] += 1
    CHAINED_A.append(q)
    CACHE.update({sid: q})
    return len(NOT_GROWING)
'''


def make_package(src=SOURCE):
    root = tempfile.mkdtemp(prefix="d20b_")
    pkg = os.path.join(root, "bpkg_" + os.path.basename(root))
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").write("")
    open(os.path.join(pkg, "pipeline.py"), "w", encoding="utf-8").write(src)
    return root, pkg


def load(root, pkg):
    sys.path.insert(0, root)
    importlib.invalidate_caches()
    return importlib.import_module(os.path.basename(pkg) + ".pipeline")


@test
def patch_rewrites_each_shape_and_the_code_still_runs():
    root, pkg = make_package()
    path = os.path.join(pkg, "pipeline.py")
    before = {f["name"] for f in ag.audit_source(SOURCE) if f["kind"] == "GROWS_FOREVER"}
    assert {"EVENT_LOG", "SEEN", "TAGS", "BY_USER", "HITS", "ORDERED", "HISTORY", "RECENT", "COUNTS", "CACHE"} <= before, before
    rep = mb.make_bounded(path, maxlen=4, maxsize=3)
    bounded = {n for n, _, _ in rep.bounded}
    assert bounded == {"EVENT_LOG", "SEEN", "TAGS", "BY_USER", "HITS", "ORDERED", "HISTORY", "RECENT", "CACHE"}, rep.summary()
    skipped = [n for n, _ in rep.skipped]
    assert set(skipped) == {"COUNTS", "CHAINED_A"} and rep.already == ["ALREADY"], rep.summary()
    assert "ALREADY" not in bounded and "NOT_GROWING" not in bounded
    assert os.path.exists(path + ".before_bounded_patch") and os.path.exists(os.path.join(pkg, "bounded.py"))
    after = set(mb.remaining_growers(path))
    assert after == {"COUNTS", "CHAINED_A"}, after                 # what is left is exactly what was reported as skipped
    m = load(root, pkg)
    for i in range(20):
        m.run(f"q{i}", f"s{i}")
    assert len(m.EVENT_LOG) == 4 and m.EVENT_LOG[-1] == ("q19", "s19")
    assert len(m.SEEN) == 3 and list(m.SEEN)[-1] == "s19"
    assert len(m.TAGS) == 3 and len(m.BY_USER) == 3 and len(m.HITS) == 3 and len(m.ORDERED) == 3 and len(m.CACHE) == 3
    assert len(m.HISTORY) == 4 and m.HISTORY.maxlen == 4 and len(m.RECENT) == 4 and m.RECENT.maxlen == 4
    assert m.HITS["s19"] == 1 and m.BY_USER["s19"] == ["q19"]
    assert m.ALREADY.maxlen == 5
    assert m.CACHE.get("s19") == "q19"
    shutil.rmtree(root, ignore_errors=True)


@test
def patch_is_idempotent_and_only_touches_container_lines():
    root, pkg = make_package()
    path = os.path.join(pkg, "pipeline.py")
    mb.make_bounded(path)
    once = open(path).read()
    rep2 = mb.make_bounded(path)
    assert not rep2.changed and open(path).read() == once
    changed = [l for l in once.splitlines() if l not in SOURCE.splitlines()]
    assert all(("Bounded" in l or "maxlen=" in l) for l in changed), changed
    # the function bodies were not touched
    assert SOURCE[SOURCE.index("def run"):] in once
    shutil.rmtree(root, ignore_errors=True)


@test
def nothing_to_do_changes_nothing():
    root, pkg = make_package("X = 1\nSMALL = []\n\ndef f():\n    return len(SMALL)\n")
    path = os.path.join(pkg, "pipeline.py")
    rep = mb.make_bounded(path)
    assert not rep.changed and not os.path.exists(path + ".before_bounded_patch") and not os.path.exists(os.path.join(pkg, "bounded.py"))
    assert "nothing to do" in rep.summary()
    shutil.rmtree(root, ignore_errors=True)


@test
def dry_run_writes_nothing():
    root, pkg = make_package()
    path = os.path.join(pkg, "pipeline.py")
    rep = mb.make_bounded(path, write=False)
    assert rep.changed and not rep.wrote and open(path).read() == SOURCE
    shutil.rmtree(root, ignore_errors=True)


@test
def multiline_assignment_and_comments_survive():
    src = 'LOG = [\n    "a",\n    "b",   # keep me\n]\n\n\ndef f(x):\n    LOG.append(x)\n'
    root, pkg = make_package(src)
    path = os.path.join(pkg, "pipeline.py")
    mb.make_bounded(path, maxlen=3)
    m = load(root, pkg)
    m.f("c")
    m.f("d")
    assert list(m.LOG) == ["b", "c", "d"], m.LOG
    shutil.rmtree(root, ignore_errors=True)


@test
def a_container_the_tool_does_not_know_is_reported_not_guessed():
    src = "from collections import ChainMap\nSTORE = ChainMap()\n\n\ndef f(x):\n    STORE.update({x: 1})\n"
    root, pkg = make_package(src)
    path = os.path.join(pkg, "pipeline.py")
    rep = mb.make_bounded(path)
    assert not rep.changed or rep.skipped == [], rep.summary()
    assert open(path).read() == src
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    bad = 0
    for name, ok, tb in results:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            bad += 1
            print(tb)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)
