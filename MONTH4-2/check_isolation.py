"""
check_isolation.py -- Day 18

Proves, with REAL calls, that two overlapping questions do not share one token tracker.

    python check_isolation.py rag_pipeline "first question" "second question"

It records which tracker object every token-usage .add() landed on, per thread:
  * each question must use exactly ONE tracker, and no two questions may share one.
Exit code 0 = isolated, 1 = shared (the old bug), 2 = something crashed.
"""
import collections
import importlib
import sys
import threading

DEFAULT_QUESTIONS = ["What was Apple's total net sales for fiscal year 2024?",
                     "What was Apple's net income in fiscal year 2024?"]


def main(argv):
    package = argv[1] if len(argv) > 1 else "rag_pipeline"
    questions = argv[2:] or DEFAULT_QUESTIONS
    m = importlib.import_module(package + ".pipeline")
    if not hasattr(m, "TokenUsage") or not hasattr(m, "run_with_entity_memory"):
        print("❌ the package has no TokenUsage / run_with_entity_memory")
        return 2

    seen = collections.defaultdict(set)
    original_add = m.TokenUsage.add

    def spy(self, usage):
        seen[threading.current_thread().name].add(id(self))
        return original_add(self, usage)

    m.TokenUsage.add = spy
    barrier = threading.Barrier(len(questions))
    errors = []

    def run(i, q):
        try:
            barrier.wait(timeout=60)
            m.run_with_entity_memory(q, f"isolation-{i}", "isolation-check")
        except Exception as e:                       # report it, do not hide it
            errors.append((i, type(e).__name__, str(e)[:200]))

    threads = [threading.Thread(name=f"call-{i}", target=run, args=(i, q)) for i, q in enumerate(questions)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    if errors:
        for e in errors:
            print(f"❌ call {e[0]} crashed: {e[1]}: {e[2]}")
        return 2
    per_call = {name: len(ids) for name, ids in seen.items()}
    all_ids = set().union(*seen.values()) if seen else set()
    print(f"trackers used per call: {per_call}; distinct trackers overall: {len(all_ids)}")
    tracker = getattr(m, "usage_tracker", None)
    hits = getattr(tracker, "fallback_hits", None)
    if hits is not None:
        print(f"calls that fell back to the shared tracker: {hits}")
    if len(seen) != len(questions):
        print("❌ some calls never recorded any usage; cannot judge")
        return 2
    if all(n == 1 for n in per_call.values()) and len(all_ids) == len(questions) and not hits:
        print("✅ ISOLATED: each overlapping call counted only its own tokens")
        return 0
    print("❌ SHARED: overlapping calls wrote into the same tracker (run make_concurrency_safe.py, then check again)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
