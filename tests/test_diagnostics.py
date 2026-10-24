"""Progress reports remain available while a worker is stuck in a read."""

import unittest
from unittest.mock import patch

import diagnostics


class TestReadDiagnostics(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.enterContext(patch.object(diagnostics, "_start_monitor"))
        self.enterContext(patch.object(diagnostics.time, "monotonic", side_effect=lambda: self.now))

    def test_heartbeat_reports_partial_bytes_before_read_completes(self):
        with self.assertLogs("diagnostics", level="INFO") as logs:
            with diagnostics.ReadTrace("sftp://me@host/session.jsonl", 4096) as trace:
                trace.expected = 10000
                trace.phase("reading")
                trace.advance(1200)
                self.now = 10
                diagnostics.report_pending()
        output = "\n".join(logs.output)
        for text in ("Read start:", "offset=4096", "Read pending:", "phase=reading",
                     "received=1200", "expected=10000", "Read complete:"):
            self.assertIn(text, output)
        self.assertNotIn(trace.id, diagnostics._active)

    def test_stall_reports_thread_stack_once_per_minute(self):
        with patch.object(diagnostics, "_thread_stacks") as stacks:
            with self.assertLogs("diagnostics", level="INFO") as logs:
                with diagnostics.ReadTrace("sftp://me@host/session.jsonl", 0) as trace:
                    trace.phase("prefetch")
                    trace.extra = lambda: {"pending_requests": 16}
                    self.now = 31
                    diagnostics.report_pending()
                    self.assertEqual(stacks.call_count, 1)
                    self.now = 36
                    diagnostics.report_pending()
                    self.assertEqual(stacks.call_count, 1)
                    self.now = 92
                    diagnostics.report_pending()
                    self.assertEqual(stacks.call_count, 2)
        self.assertIn("Read stalled:", "\n".join(logs.output))
        self.assertIn("pending_requests", "\n".join(logs.output))

    def test_receiving_more_data_resets_stall_detection(self):
        with patch.object(diagnostics, "_thread_stacks") as stacks:
            with self.assertLogs("diagnostics", level="INFO"):
                with diagnostics.ReadTrace("sftp://me@host/session.jsonl", 0) as trace:
                    self.now = 28
                    trace.advance(65536)
                    self.now = 40
                    diagnostics.report_pending()
        stacks.assert_not_called()

    def test_failed_reads_report_phase_and_unregister(self):
        with self.assertLogs("diagnostics", level="WARNING") as logs:
            with self.assertRaises(EOFError):
                with diagnostics.ReadTrace("sftp://me@host/session.jsonl", 0) as trace:
                    trace.phase("stat")
                    raise EOFError("connection dropped")
        self.assertNotIn(trace.id, diagnostics._active)
        self.assertIn("phase=stat", "\n".join(logs.output))
