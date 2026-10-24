"""Short-lived index snapshots and directory/preview caches for open dialogs."""

import copy
import threading
import time
import weakref

FRESH_SEC = 5.0
_stores = weakref.WeakKeyDictionary()
_lock = threading.Lock()


def _store(backend):
    with _lock:
        return _stores.setdefault(backend, {"snapshots": {}, "directories": {}, "heads": {}})


def snapshot(backend, scope):
    store = _store(backend)
    with _lock:
        cached = store["snapshots"].get(scope)
        if cached is None:
            return None
        timestamp, result = cached
        return copy.deepcopy(result), time.monotonic() - timestamp


def remember(backend, scope, result, *, preserve_age=False):
    store = _store(backend)
    with _lock:
        cached = store["snapshots"].get(scope)
        timestamp = cached[0] if preserve_age and cached else time.monotonic()
        store["snapshots"][scope] = (timestamp, copy.deepcopy(result))


def preview(backend, rel, mtime):
    store = _store(backend)
    with _lock:
        cached = store["heads"].get(rel)
        if cached and cached[0] == mtime and time.monotonic() - cached[1] < 60:
            return cached[2]
    text = backend.read_text_head(rel, 4096)
    with _lock:
        store["heads"][rel] = (mtime, time.monotonic(), text)
    return text


class ListingView:
    def __init__(self, backend, *, force=False):
        self.backend = backend
        self.store = _store(backend)
        self.force = force
        self._seen = set()

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def list_dir(self, rel):
        with _lock:
            cached = self.store["directories"].get(rel)
            if (not self.force or rel in self._seen) and cached and time.monotonic() - cached[0] < FRESH_SEC:
                return list(cached[1])
        entries = self.backend.list_dir(rel)
        self._seen.add(rel)
        with _lock:
            self.store["directories"][rel] = (time.monotonic(), list(entries))
        return entries

    def is_dir(self, rel):
        try:
            self.list_dir(rel)
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False
