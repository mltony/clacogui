"""Read workers with a separate lane for visible transcript updates."""

from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger(__name__)
SLOW_JOB_SEC = 0.25


class ReadJob:
    def __init__(self, owner, fn, on_done, label, foreground):
        self.owner = owner
        self.fn = fn
        self.on_done = on_done
        self.label = label
        self.foreground = foreground
        self.submitted = time.monotonic()
        self._lock = threading.Lock()
        self._started = False

    def promote(self):
        """Move a waiting job to the foreground; never execute it twice."""
        with self._lock:
            if self._started or self.foreground:
                return False
            self.foreground = True
            self.owner.foreground.put(self)
            return True

    def claim(self, foreground):
        with self._lock:
            if self._started or self.foreground != foreground:
                return False
            self._started = True
            return True


class ReadWorkers:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.foreground = queue.Queue()
        self.background = queue.Queue()
        self.threads = []
        for foreground in (True, False):
            thread = threading.Thread(
                target=self._loop, args=(foreground,), daemon=True,
                name="clacogui-read-" + ("active" if foreground else "background"),
            )
            thread.start()
            self.threads.append(thread)

    def submit(self, fn, on_done, *, foreground=False, label=None):
        job = ReadJob(self, fn, on_done, label or fn.__qualname__, foreground)
        (self.foreground if foreground else self.background).put(job)
        return job

    def close(self):
        self.foreground.put(None)
        self.background.put(None)

    def _loop(self, foreground):
        pending = self.foreground if foreground else self.background
        while True:
            job = pending.get()
            if job is None:
                return
            if not job.claim(foreground):
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
                    job.label, "active" if job.foreground else "background",
                    *(value * 1000 for value in durations),
                )
