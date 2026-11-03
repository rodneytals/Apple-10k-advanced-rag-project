"""
usage_context.py -- Day 18

Your pipeline counts tokens in ONE shared object (`usage_tracker`) that run_with_entity_memory
replaces at the start of every call. With two requests running at once, request B replaces the
object while request A is still using it, so each request is billed for the other's tokens.

UsageProxy fixes that without touching any other line of your code. It looks like the old
global (`usage_tracker.add(...)`, `usage_tracker.report()`, `usage_tracker.prompt_tokens`), but
forwards every access to the tracker that belongs to THE CALL THAT IS RUNNING RIGHT NOW,
found through a ContextVar. Each thread/call has its own.
"""
import contextvars
import logging
import threading

log = logging.getLogger("usage_context")
_current = contextvars.ContextVar("rag_usage_tracker", default=None)
_add_lock = threading.Lock()          # two parallel graph branches may call .add() on the SAME tracker


class UsageProxy:
    def __init__(self, fallback):
        object.__setattr__(self, "_fallback", fallback)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_fallback_hits", 0)
        object.__setattr__(self, "_warned", False)

    # ---- the control API used by run_with_entity_memory
    def begin_call(self, tracker):
        return _current.set(tracker)

    def end_call(self, token):
        _current.reset(token)

    @property
    def active(self):
        return _current.get()

    @property
    def fallback_hits(self):
        """How many times something used the tracker with NO call in progress. Must stay 0 in production:
        anything above 0 means usage was recorded against a shared tracker (the old behaviour)."""
        return self._fallback_hits

    # ---- forwarding
    def _target(self):
        t = _current.get()
        if t is not None:
            return t
        with self._lock:
            object.__setattr__(self, "_fallback_hits", self._fallback_hits + 1)
            first = not self._warned
            object.__setattr__(self, "_warned", True)
        if first:
            log.warning("usage_tracker used with no call in progress (outside run_with_entity_memory, or the "
                        "context did not reach this thread): using a shared fallback tracker")
        return self._fallback

    def __getattr__(self, name):
        target = self._target()
        value = getattr(target, name)
        if name == "add":
            def locked_add(*a, **k):
                with _add_lock:
                    return value(*a, **k)
            return locked_add
        return value

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)

    def __repr__(self):
        return f"<UsageProxy -> {self._target()!r}>"
