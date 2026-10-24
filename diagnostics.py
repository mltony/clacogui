"""Progress and thread stacks for reads that stop reporting progress."""

import atexit
import itertools
import logging
import sys
import threading
import time

log = logging.getLogger(__name__)
REPORT_SEC = 5.0
STALL_SEC = 30.0
STACK_INTERVAL_SEC = 60.0
_ids = itertools.count(1)
_active = {}
_lock = threading.Lock()
_stop = threading.Event()
_monitor = None


def _start_monitor():
    global _monitor
    with _lock:
        if _monitor is None:
            _monitor = threading.Thread(target=_monitor_loop, name="clacogui-diagnostics", daemon=True)
            _monitor.start()


def _monitor_loop():
    while not _stop.wait(REPORT_SEC):
        report_pending()


def report_pending():
    now = time.monotonic()
    with _lock:
        states = [trace.snapshot(now) for trace in _active.values()]
    for state in states:
        if state["elapsed"] < REPORT_SEC:
            continue
        log.info(
            "Read pending: id=%s path=%s phase=%s received=%d expected=%s "
            "elapsed=%.1fs phase_age=%.1fs idle=%.1fs worker=%s state=%s",
            state["id"], state["path"], state["phase"], state["received"],
            state["expected"], state["elapsed"], state["phase_age"], state["idle"],
            state["worker"], state["extra"],
        )
        if state["idle"] >= STALL_SEC:
            with _lock:
                trace = _active.get(state["id"])
                dump = trace is not None and now - trace.last_stack >= STACK_INTERVAL_SEC
                if dump:
                    trace.last_stack = now
            if dump:
                log.warning("Read stalled: id=%s phase=%s path=%s idle=%.1fs",
                            state["id"], state["phase"], state["path"], state["idle"])
                _thread_stacks(state["id"], state["thread_id"])


def _thread_stacks(read_id, worker_id):
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    for ident, frame in sys._current_frames().items():
        current = frame
        related = ident == worker_id
        while current is not None and not related:
            related = current.f_code.co_name in ("_prefetch_thread", "_read_prefetch", "_read_response")
            current = current.f_back
        if related:
            # Stack locations only: never dump local variables or transcript data.
            locations = []
            current = frame
            while current is not None and len(locations) < 20:
                locations.append(f'  File "{current.f_code.co_filename}", '
                                 f'line {current.f_lineno}, in {current.f_code.co_name}\n')
                current = current.f_back
            log.warning("Read stack: id=%s thread=%s\n%s", read_id,
                        names.get(ident, ident), "".join(reversed(locations)))


class ReadTrace:
    def __init__(self, path, offset):
        self.id = next(_ids)
        self.path = path
        self.offset = offset
        self.received = 0
        self.expected = None
        self.started = self.changed = self.progressed = time.monotonic()
        self.current_phase = "opening"
        self.last_stack = float("-inf")
        self.thread_id = threading.get_ident()
        self.worker = threading.current_thread().name
        self.extra = None
        self.level = logging.INFO if path.lower().endswith(".jsonl") else logging.DEBUG

    def __enter__(self):
        _start_monitor()
        with _lock:
            _active[self.id] = self
        log.log(self.level, "Read start: id=%s path=%s offset=%d", self.id, self.path, self.offset)
        return self

    def phase(self, phase):
        with _lock:
            self.current_phase = phase
            self.changed = self.progressed = time.monotonic()

    def advance(self, count):
        with _lock:
            self.received += count
            self.progressed = time.monotonic()

    def snapshot(self, now):
        try:
            extra = self.extra() if self.extra else ""
        except Exception:
            extra = "unavailable"
        return dict(id=self.id, path=self.path, phase=self.current_phase,
                    received=self.received, expected=self.expected,
                    elapsed=now-self.started, phase_age=now-self.changed,
                    idle=now-self.progressed, worker=self.worker,
                    thread_id=self.thread_id, extra=extra)

    def __exit__(self, kind, error, tb):
        with _lock:
            _active.pop(self.id, None)
        elapsed = time.monotonic() - self.started
        if error is not None:
            level = logging.DEBUG if isinstance(error, FileNotFoundError) else logging.WARNING
            log.log(level, "Read failed: id=%s path=%s phase=%s received=%d elapsed=%.1fs error=%s",
                    self.id, self.path, self.current_phase, self.received, elapsed, error)
        else:
            log.log(self.level, "Read complete: id=%s path=%s received=%d elapsed=%.1fs",
                    self.id, self.path, self.received, elapsed)


atexit.register(_stop.set)
