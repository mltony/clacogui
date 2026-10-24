"""Local copies of conversation transcripts, so a restart only downloads what is new.

Transcripts are append-only JSONL, so a local copy stays valid for as long as
the remote file still starts with it.  Opening a tab re-reads the last
``OVERLAP`` cached bytes together with everything after them -- one request --
and if the overlap still matches, only the new bytes are added to the copy.
If it doesn't (the file was replaced or truncated), the whole file is
downloaded again.  Tail reads made while polling are appended too, so the
copy keeps up with the live conversation.

The cache is best effort: any local IO error is logged and the remote data is
used as if there were no cache.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time

from fs import FsBackend

log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache"),
    "clacogui",
    "transcripts",
)
OVERLAP = 4096
MAX_AGE_SEC = 30 * 24 * 3600

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _cache_path(backend: FsBackend, rel: str) -> str:
    key = hashlib.sha1(backend.display_path(rel).encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, key + ".jsonl")


def _lock_for(path: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def _load(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""
    except OSError:
        log.warning("Could not read transcript cache %s", path, exc_info=True)
        return b""


def _replace(path: str, data: bytes) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        log.warning("Could not write transcript cache %s", path, exc_info=True)


def _append(path: str, data: bytes) -> None:
    try:
        with open(path, "ab") as f:
            f.write(data)
    except OSError:
        log.warning("Could not append to transcript cache %s", path, exc_info=True)


def read_text(backend: FsBackend, rel: str, encoding: str = "utf-8") -> str:
    """The whole transcript, downloading only what the local copy lacks."""
    if not backend.cache_transcripts:
        return backend.read_text(rel, encoding)
    path = _cache_path(backend, rel)
    started = time.monotonic()
    log.info("Cache read start: path=%s", backend.display_path(rel))
    with _lock_for(path):
        log.info("Cache lock acquired: path=%s wait=%.1fms", rel,
                 (time.monotonic() - started) * 1000)
        loaded = time.monotonic()
        cached = _load(path)
        start = max(0, len(cached) - OVERLAP)
        log.info("Cache loaded: path=%s bytes=%d load=%.1fms validate_offset=%d",
                 rel, len(cached), (time.monotonic() - loaded) * 1000, start)
        fresh = backend.read_bytes_from(rel, start)
        overlap = len(cached) - start
        if cached and fresh[:overlap] == cached[start:]:
            new = fresh[overlap:]
            if new:
                _append(path, new)
            data = cached + new
            log.info(
                "Transcript cache hit for %s: %d cached, %d new bytes",
                rel, len(cached), len(new),
            )
        else:
            if cached:
                log.info("Transcript cache for %s no longer matches; downloading it again", rel)
            data = fresh if start == 0 else backend.read_bytes_from(rel, 0)
            log.info("Cache write start: path=%s bytes=%d", rel, len(data))
            _replace(path, data)
            log.info("Cache write complete: path=%s bytes=%d", rel, len(data))
    log.info("Cache decode start: path=%s bytes=%d", rel, len(data))
    return data.decode(encoding, errors="replace")


def read_text_from(backend: FsBackend, rel: str, offset: int, encoding: str = "utf-8") -> str:
    """Text from byte ``offset`` on; whatever extends the local copy is appended to it.

    The panel's offset can trail the copy (it only consumes complete lines,
    the copy also keeps a trailing partial one), so the part of ``fresh``
    the copy already has is checked and skipped rather than required to be
    empty.
    """
    if not backend.cache_transcripts:
        return backend.read_text_from(rel, offset, encoding)
    path = _cache_path(backend, rel)
    with _lock_for(path):
        fresh = backend.read_bytes_from(rel, offset)
        try:
            cached_size = os.path.getsize(path)
        except OSError:
            cached_size = -1
        known = cached_size - offset
        if 0 <= known < len(fresh) and _tail_matches(path, offset, fresh[:known]):
            _append(path, fresh[known:])
    return fresh.decode(encoding, errors="replace")


def _tail_matches(path: str, offset: int, expected: bytes) -> bool:
    if not expected:
        return True
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read() == expected
    except OSError:
        return False


def prune(max_age_sec: float = MAX_AGE_SEC) -> None:
    """Delete local copies that haven't been read or appended to for ``max_age_sec``."""
    cutoff = time.time() - max_age_sec
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    for name in names:
        path = os.path.join(CACHE_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                log.info("Pruned old transcript cache %s", name)
        except OSError:
            log.debug("Could not prune %s", path, exc_info=True)
