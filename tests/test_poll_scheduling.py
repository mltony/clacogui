"""Exercise real panel polling callbacks without starting a WebView."""

from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch

import gui


class Backend:
    cache_transcripts = False

    def __init__(self):
        self.text = b"existing\n"
        self.offsets = []

    def stat(self, rel):
        return (1, len(self.text))

    def read_text_from(self, rel, offset, encoding="utf-8"):
        self.offsets.append(offset)
        return self.text[offset:].decode(encoding)


class Job:
    def __init__(self, fn, done, options):
        self.fn, self.done, self.options = fn, done, options
        self.promotions = 0

    def promote(self):
        self.promotions += 1


class TestPollScheduling(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.backend = Backend()
        self.panel = SimpleNamespace(
            backend=self.backend, rel_path="session.jsonl", provider="claude",
            _loading=False, _polling=False, _poll_job=None, _load_job=None,
            _poll_again=False, _poll_again_foreground=False,
            _next_background_poll=0, _poll_started_at=None,
            _signature=(1, len(self.backend.text)),
            _poll_gen=0, _inflight_gen=0, _applied_gen=0,
            _poll_failures=0, _poll_quiet_count=0,
            _parse_fn=lambda: self.fail,
        )
        self.applied = []
        def apply(text, sig, offset):
            self.applied.append(text)
            self.panel._signature = (sig[0], offset + len(text.encode("utf-8")))
        self.panel._apply_incremental = apply
        self.panel.poll = MethodType(gui.ConversationPanel.poll, self.panel)
        self.enterContext(patch.object(gui, "submit_io", self.submit))
        self.enterContext(patch.object(gui.time, "monotonic", return_value=10))

    def submit(self, fn, done, **options):
        job = Job(fn, done, options)
        self.jobs.append(job)
        return job

    def finish(self, job):
        job.done(job.fn(), None)

    def test_active_reads_use_own_lane_and_bypass_background_interval(self):
        self.panel.poll()
        self.assertFalse(self.jobs[0].options["foreground"])
        self.finish(self.jobs[0])
        self.panel.poll()
        self.assertEqual(len(self.jobs), 1)
        self.panel.poll(foreground=True)
        self.assertTrue(self.jobs[1].options["foreground"])

    def test_selecting_queued_background_tab_promotes_without_duplicate(self):
        self.panel.poll()
        self.panel.poll(foreground=True)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0].promotions, 1)

    def test_completion_during_read_gets_followup_without_replaying_bytes(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.panel.provider = provider
                self.backend.text += b"first\n"
                self.panel.poll(foreground=True)
                old_job = self.jobs[-1]
                captured = old_job.fn()
                self.backend.text += "final reply\n".encode()
                self.panel.poll(foreground=True, urgent=True)
                before = len(self.jobs)
                old_job.done(captured, None)
                self.assertEqual(len(self.jobs), before + 1)
                self.assertTrue(self.jobs[-1].options["foreground"])
                self.finish(self.jobs[-1])
                self.assertEqual(self.applied[-2:], ["first\n", "final reply\n"])
                self.assertEqual(self.panel._signature[1], len(self.backend.text))

    def test_background_completion_bypasses_throttle_without_using_active_lane(self):
        self.panel._next_background_poll = 100
        self.panel.poll(urgent=True)
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(self.jobs[0].options["foreground"])
        captured = self.jobs[0].fn()
        self.panel.poll(urgent=True)
        self.jobs[0].done(captured, None)
        self.assertEqual(len(self.jobs), 2)
        self.assertFalse(self.jobs[1].options["foreground"])

    def test_completion_is_remembered_while_loading(self):
        self.panel._loading = True
        self.panel._load_job = Job(None, None, {})
        self.panel.poll(foreground=True, urgent=True)
        self.assertTrue(self.panel._poll_again)
        self.assertTrue(self.panel._poll_again_foreground)
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.panel._load_job.promotions, 1)

    def test_old_overlapping_callback_cannot_duplicate_tail(self):
        self.backend.text += b"reply\n"
        self.panel.poll(foreground=True)
        first = self.jobs[-1]
        first_result = first.fn()
        with patch.object(gui.time, "monotonic", return_value=50):
            with self.assertLogs("gui", level="WARNING"):
                self.panel.poll(foreground=True)
        second = self.jobs[-1]
        second_result = second.fn()
        first.done(first_result, None)
        second.done(second_result, None)
        self.assertEqual(self.applied, ["reply\n"])
        self.assertEqual(self.panel._signature[1], len(self.backend.text))


class TestCompletionDispatch(unittest.TestCase):
    def test_refresh_active_matching_tab_first_and_isolate_other_backend(self):
        backend, other = object(), object()
        calls = []
        class Panel:
            def __init__(self, source, name):
                self.backend, self.name = source, name
            def poll(self, **options):
                calls.append((self.name, options))
        pages = [Panel(backend, "background"), Panel(other, "other"), Panel(backend, "active")]
        frame = SimpleNamespace(notebook=SimpleNamespace(
            GetSelection=lambda: 2, GetPageCount=lambda: len(pages),
            GetPage=lambda index: pages[index],
        ))
        with patch.object(gui, "ConversationPanel", Panel):
            gui.MainFrame._refresh_completed_transcripts(frame, backend)
        self.assertEqual(calls, [
            ("active", {"foreground": True, "urgent": True}),
            ("background", {"foreground": False, "urgent": True}),
        ])

    def test_sound_requests_refresh_before_playing_and_keeps_dedup(self):
        calls = []
        backend = object()
        frame = SimpleNamespace(
            backend=backend, _seen_notifications=set(), _last_completion_ts=0,
            _refresh_completed_transcripts=lambda source: calls.append(("refresh", source)),
        )
        with patch.object(gui, "_play_notification_sound", side_effect=lambda **kw: calls.append(("sound", kw))), patch.object(gui, "submit_io_write"):
            gui.MainFrame._handle_new_notifications(frame, ["1_test__Stop.json"])
            gui.MainFrame._handle_new_notifications(frame, ["1_test__Stop.json"])
        self.assertEqual(calls, [("refresh", backend), ("sound", {"completion": True})])
