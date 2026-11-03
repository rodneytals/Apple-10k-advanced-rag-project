"""
bounded.py -- Day 20

Containers that stop growing. Your pipeline had module-level lists/dicts that every request adds to and nothing
ever removes from (the Day 18 audit called them GROWS_FOREVER). In a notebook that is harmless; in a service that
runs for weeks it is a slow memory leak.

These behave like the plain list / dict / set they replace, with one difference: when they are full, the OLDEST
entry is dropped to make room for a new one. Good for logs and caches. If some code needs EVERY entry forever,
it would have run out of memory anyway; keep that data in a database.

make_bounded.py puts this file next to your pipeline.py and swaps the containers for you.
"""
import collections
import threading
import weakref

_LIVE = {}                          # every bounded container (weak references), so monitoring can show how full each one is


def _register(obj):
    key = id(obj)
    _LIVE[key] = weakref.ref(obj, lambda _r, k=key: _LIVE.pop(k, None))


def stats():
    """[(name, items, limit, dropped_so_far)] for every bounded container alive right now."""
    alive = [r() for r in list(_LIVE.values())]
    return sorted((c.name, len(c), c.limit, c.dropped) for c in alive if c is not None)


class BoundedList(list):
    def __init__(self, iterable=(), maxlen=1000, name="list"):
        self.limit, self.name, self.dropped = maxlen, name, 0
        self._lock = threading.RLock()
        super().__init__(iterable)
        self._trim()
        _register(self)

    def _trim(self):
        with self._lock:
            excess = len(self) - self.limit
            if excess > 0:
                del self[:excess]
                self.dropped += excess

    def append(self, x):
        with self._lock:
            super().append(x)
            self._trim()

    def extend(self, it):
        with self._lock:
            super().extend(it)
            self._trim()

    def insert(self, i, x):
        with self._lock:
            super().insert(i, x)
            self._trim()

    def __iadd__(self, it):
        self.extend(it)
        return self

    def __setitem__(self, i, v):
        with self._lock:
            super().__setitem__(i, v)
            self._trim()


class BoundedDict(dict):
    """Keeps insertion order; when full, the oldest KEY is dropped. Also does what defaultdict does when given default_factory."""

    def __init__(self, *args, maxsize=5000, name="dict", default_factory=None, **kwargs):
        self.limit, self.name, self.dropped = maxsize, name, 0
        self.default_factory = default_factory
        self._lock = threading.RLock()
        super().__init__(*args, **kwargs)
        self._trim()
        _register(self)

    def _trim(self):
        with self._lock:
            while len(self) > self.limit:
                dict.__delitem__(self, next(iter(self)))
                self.dropped += 1

    def __setitem__(self, k, v):
        with self._lock:
            super().__setitem__(k, v)
            self._trim()

    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)
        value = self.default_factory()
        self[key] = value
        return value

    def setdefault(self, k, default=None):
        with self._lock:
            if k not in self:
                self[k] = default
            return dict.__getitem__(self, k)

    def update(self, *a, **kw):
        with self._lock:
            super().update(*a, **kw)
            self._trim()

    def __ior__(self, other):
        self.update(other)
        return self


class BoundedSet(set):
    def __init__(self, iterable=(), maxsize=5000, name="set"):
        self.limit, self.name, self.dropped = maxsize, name, 0
        self._lock = threading.RLock()
        self._order = collections.OrderedDict()
        super().__init__()
        _register(self)
        self.update(iterable)

    def _trim(self):
        while len(self) > self.limit:
            oldest = next(iter(self._order))
            self._order.pop(oldest, None)
            set.discard(self, oldest)
            self.dropped += 1

    def add(self, x):
        with self._lock:
            if x not in self:
                self._order[x] = None
            super().add(x)
            self._trim()

    def update(self, *others):
        with self._lock:
            for o in others:
                for x in o:
                    self.add(x)

    def __ior__(self, other):
        self.update(other)
        return self

    def discard(self, x):
        with self._lock:
            self._order.pop(x, None)
            super().discard(x)

    def remove(self, x):
        with self._lock:
            if x not in self:
                raise KeyError(x)
            self.discard(x)

    def pop(self):
        with self._lock:
            x = set.pop(self)
            self._order.pop(x, None)
            return x

    def clear(self):
        with self._lock:
            self._order.clear()
            super().clear()
