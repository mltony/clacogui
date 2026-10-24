"""Read workers with a separate lane for visible transcript updates."""

from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger(__name__)
SLOW_JOB_SEC = 0.25


class ReadJob:
    def __init__(self, owner, fn, on_done, label, foreground, index=False):
        self.owner = owner
        self.fn = fn
        self.on_done = on_done
        self.label = label
        self.foreground = foreground
        self.index = index
        self.submitted = time.monotonic()
        self._lock = threading.Lock()
        self._started = False

    def promote(self):
        """Move a waiting job to the foreground; never execute it twice."""
        with self._lock:
            if self._started or self.foreground or self.index:
                return False
            self.foreground = True
            self.owner.foreground.put(self)
            return True

    def claim(self, foreground, index=False):
        with self._lock:
            if self._started or self.foreground != foreground or self.index != index:
                return False
            self._started = True
            return True


class ReadWorkers:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.foreground = queue.Queue()
        self.background = queue.Queue()
        self.index = queue.Queue()
        self.threads = []
        for foreground, index in ((True, False), (False, False), (False, True)):
            thread = threading.Thread(
                target=self._loop, args=(foreground, index), daemon=True,
                name="clacogui-read-" + ("index" if index else "active" if foreground else "background"),
            )
            thread.start()
            self.threads.append(thread)

    def submit(self, fn, on_done, *, foreground=False, index=False, label=None):
        if foreground and index:
            raise ValueError("A read cannot use both the active and index lanes")
        job = ReadJob(self, fn, on_done, label or fn.__qualname__, foreground, index)
        (self.index if index else self.foreground if foreground else self.background).put(job)
        return job

    def close(self):
        self.foreground.put(None)
        self.background.put(None)
        self.index.put(None)

    def _loop(self, foreground, index=False):
        pending = self.index if index else self.foreground if foreground else self.background
        while True:
            job = pending.get()
            if job is None:
                return
            if not job.claim(foreground, index):
                continue
            started = time.monotonic()
            try:
                result, exc = job.fn(), None
            except BaseException as error:
                log.exception("Background read failed: %s", job.label)
                result, exc = None, error
            finished = time.monotonic()
            try:
                self.dispatch(self._deliver, job, result, exc, started, finished)
            except Exception:
                log.exception("Read callback dispatch failed: %s", job.label)

    @staticmethod
    def _deliver(job, result, exc, started, finished):
        delivered = time.monotonic()
        try:
            job.on_done(result, exc)
        finally:
            ended = time.monotonic()
            durations = (
                started - job.submitted, finished - started,
                delivered - finished, ended - delivered,
            )
            if max(durations) >= SLOW_JOB_SEC:
                log.info(
                    "IO timing: %s lane=%s queue=%.1fms work=%.1fms "
                    "ui_wait=%.1fms callback=%.1fms",
                    job.label, "index" if job.index else "active" if job.foreground else "background",
                    *(value * 1000 for value in durations),
                )
