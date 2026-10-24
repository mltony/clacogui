"""Short-lived JSON metadata cache using directory-listing signatures."""

from __future__ import annotations

import json
import threading
import time
import weakref

# FTP timestamps may have only second resolution. Periodic revalidation
# catches replacements whose mtime and size happen to be unchanged.
MAX_AGE_SEC = 2.0
_backends = weakref.WeakKeyDictionary()
_lock = threading.Lock()


def read_json(backend, rel, entry):
    signature = (entry.mtime, entry.size)
    now = time.monotonic()
    with _lock:
        files = _backends.setdefault(backend, {})
        cached = files.get(rel)
        if (cached is not None and entry.mtime
                and cached[0] == signature and now - cached[1] < MAX_AGE_SEC):
            return cached[2]
    try:
        value = json.loads(backend.read_text(rel))
    except (OSError, ValueError, TypeError):
        with _lock:
            files.pop(rel, None)
        raise
    with _lock:
        files[rel] = (signature, now, value)
    return value


def prune(backend, directory, entries):
    """Forget deleted files after a successful directory listing."""
    prefix = directory.rstrip("/") + "/"
    present = {prefix + entry.name for entry in entries if not entry.is_dir}
    with _lock:
        files = _backends.get(backend, {})
        for rel in list(files):
            if rel.startswith(prefix) and rel not in present:
                del files[rel]
