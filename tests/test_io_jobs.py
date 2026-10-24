"""Check independent read lanes and safe promotion under blocked IO."""

import queue
import threading
import unittest

from io_jobs import ReadWorkers


class TestReadWorkers(unittest.TestCase):
    def setUp(self):
        self.deliveries = queue.Queue()
        self.workers = ReadWorkers(lambda fn, *args: self.deliveries.put((fn, args)))
        self.gate = threading.Event()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.gate.set()
        self.workers.close()
        for thread in self.workers.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def deliver(self):
        fn, args = self.deliveries.get(timeout=2)
        fn(*args)

    def block_background(self):
        started = threading.Event()

        def slow():
            started.set()
            self.gate.wait(2)

        self.workers.submit(slow, lambda *_: None)
        self.assertTrue(started.wait(2))

    def test_active_read_finishes_while_background_is_blocked(self):
        self.block_background()
        received = []
        self.workers.submit(lambda: "active", lambda value, exc: received.append((value, exc)), foreground=True)
        self.deliver()
        self.assertEqual(received, [("active", None)])

    def test_promoted_job_runs_once_and_no_longer_blocks_background(self):
        self.block_background()
        executions, received = [], []
        job = self.workers.submit(lambda: executions.append("read"), lambda *_: received.append("done"))
        self.assertTrue(job.promote())
        self.deliver()
        self.assertFalse(job.promote())
        self.gate.set()
        # Barrier proves the background worker skipped the old queue entry.
        barrier = threading.Event()
        self.workers.submit(lambda: barrier.set(), lambda *_: None)
        self.assertTrue(barrier.wait(2))
        self.assertEqual(executions, ["read"])
        self.assertEqual(received, ["done"])

    def test_running_job_cannot_be_promoted_or_duplicated(self):
        started = threading.Event()
        def work():
            started.set()
            self.gate.wait(2)
            return "read"
        job = self.workers.submit(work, lambda *_: None)
        self.assertTrue(started.wait(2))
        self.assertFalse(job.promote())

    def test_worker_survives_exception_and_delivers_error(self):
        received = []
        def fail():
            raise OSError("unavailable")
        with self.assertLogs("io_jobs", level="ERROR"):
            self.workers.submit(fail, lambda value, exc: received.append((value, exc)), foreground=True)
            self.deliver()
        self.assertIsNone(received[0][0])
        self.assertIsInstance(received[0][1], OSError)
        self.workers.submit(lambda: "next", lambda value, exc: received.append((value, exc)), foreground=True)
        self.deliver()
        self.assertEqual(received[-1], ("next", None))

    def test_timing_includes_queue_execution_ui_wait_and_callback(self):
        self.workers.submit(lambda: "ok", lambda *_: None, foreground=True, label="probe")
        with self.assertLogs("io_jobs", level="INFO") as logs:
            # Zero threshold makes this deterministic without sleeping.
            from unittest.mock import patch
            with patch("io_jobs.SLOW_JOB_SEC", 0):
                self.deliver()
        output = " ".join(logs.output)
        for fragment in ("probe", "lane=active", "queue=", "work=", "ui_wait=", "callback="):
            self.assertIn(fragment, output)
