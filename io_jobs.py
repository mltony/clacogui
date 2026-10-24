"""Read workers with a separate lane for visible transcript updates."""

from __future__ import annotations

import logging
import itertools
import queue
import threading
import time

log = logging.getLogger(__name__)
SLOW_JOB_SEC = 0.25
_job_ids = itertools.count(1)


class ReadJob:
    def __init__(self, owner, fn, on_done, label, foreground, index=False, notifications=False):
        self.id = next(_job_ids)
        self.owner = owner
        self.fn = fn
        self.on_done = on_done
        self.label = label
        self.foreground = foreground
        self.index = index
        self.notifications = notifications
        self.submitted = time.monotonic()
        self._lock = threading.Lock()
        self._started = False

    def promote(self):
        """Move a waiting job to the foreground; never execute it twice."""
        with self._lock:
            if self._started or self.foreground or self.index or self.notifications:
                return False
            self.foreground = True
            self.owner.foreground.put(self)
            return True

    def claim(self, foreground, index=False, notifications=False):
        with self._lock:
            if self._started or self.foreground != foreground or self.index != index or self.notifications != notifications:
                return False
            self._started = True
            return True


class ReadWorkers:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.foreground = queue.Queue()
        self.background = queue.Queue()
        self.index = queue.Queue()
        self.notifications = queue.Queue()
        self.threads = []
        for foreground, index, notifications in ((True, False, False), (False, False, False), (False, True, False), (False, False, True)):
            thread = threading.Thread(
                target=self._loop, args=(foreground, index, notifications), daemon=True,
                name="clacogui-read-" + ("notifications" if notifications else "notifications" if notifications else "index" if index else "active" if foreground else "background"),
            )
            thread.start()
            self.threads.append(thread)

    def submit(self, fn, on_done, *, foreground=False, index=False, notifications=False, label=None):
        if sum((foreground, index, notifications)) > 1:
            raise ValueError("A read can only use one lane")
        job = ReadJob(self, fn, on_done, label or fn.__qualname__, foreground, index, notifications)
        log.debug("IO queued: job=%s label=%s lane=%s", job.id, job.label,
                  "index" if index else "active" if foreground else "background")
        (self.notifications if notifications else self.index if index else self.foreground if foreground else self.background).put(job)
        return job

    def close(self):
        self.foreground.put(None)
        self.background.put(None)
        self.index.put(None)
        self.notifications.put(None)

    def _loop(self, foreground, index=False, notifications=False):
        pending = self.notifications if notifications else self.index if index else self.foreground if foreground else self.background
        while True:
            job = pending.get()
            if job is None:
                return
            if not job.claim(foreground, index, notifications):
                continue
            started = time.monotonic()
            log.log(logging.INFO if job.label.startswith("reading conversation") else logging.DEBUG,
                    "IO start: job=%s label=%s lane=%s queue=%.1fms",
                    job.id, job.label, "notifications" if notifications else "index" if index else "active" if foreground else "background",
                    (started - job.submitted) * 1000)
            try:
                result, exc = job.fn(), None
            except BaseException as error:
                log.exception("Background read failed: %s", job.label)
                result, exc = None, error
            finished = time.monotonic()
            log.debug("IO finished: job=%s label=%s work=%.1fms error=%s", job.id,
                      job.label, (finished - started) * 1000, type(exc).__name__ if exc else "none")
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
                    "IO timing: %s job=%s lane=%s queue=%.1fms work=%.1fms "
                    "ui_wait=%.1fms callback=%.1fms",
                    job.label, job.id, "notifications" if job.notifications else "index" if job.index else "active" if job.foreground else "background",
                    *(value * 1000 for value in durations),
                )
