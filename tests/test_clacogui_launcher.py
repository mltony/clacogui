"""Exercise transcript and send-gate logic without launching a terminal."""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_launcher():
    path = Path(__file__).resolve().parents[1] / "clacogui_launcher.py"
    spec = importlib.util.spec_from_file_location("_launcher_under_test", path)
    module = importlib.util.module_from_spec(spec)
    # The launcher runs on POSIX. Windows can still test its pure send-gate
    # logic; terminal I/O is deliberately outside these tests.
    stubs = {
        name: types.ModuleType(name)
        for name in ("fcntl", "pty", "termios", "tty")
    } if sys.platform == "win32" else {}
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


launcher_module = _load_launcher()
TURN_END = {"type": "system", "subtype": "turn_duration"}


class TestBackgroundAgentDelivery(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.transcript = Path(self.temp) / "session.jsonl"
        self.launcher = launcher_module.Launcher(["claude"])
        self.launcher._session_id = "session"
        self.launcher._claude_status = "busy"
        self.launcher._transcript = str(self.transcript)
        self.launcher._user = SimpleNamespace(
            estimate=lambda: 0, last_keystroke_ts=0
        )
        self.enterContext(patch.object(launcher_module.time, "monotonic", return_value=100))
        self.enterContext(patch.object(launcher_module, "_log"))

    def write_records(self, *records):
        self.transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_completed_main_turn_allows_busy_send(self):
        self.write_records({"type": "assistant"}, TURN_END)
        self.assertEqual(self.launcher._can_inject_now(), (True, "ok"))

    def test_new_main_message_blocks_busy_send(self):
        for kind in ("user", "assistant"):
            with self.subTest(kind=kind):
                self.write_records(TURN_END, {"type": kind})
                self.assertEqual(
                    self.launcher._can_inject_now(), (False, "claude_busy")
                )

    def test_sidechain_messages_do_not_reopen_main_turn(self):
        self.write_records(
            TURN_END,
            {"type": "assistant", "isSidechain": True},
            {"type": "user", "isSidechain": True},
        )
        self.assertTrue(launcher_module._main_turn_ended(str(self.transcript)))

    def test_other_records_after_turn_end_are_ignored(self):
        self.write_records(
            TURN_END, {"type": "progress"},
            {"type": "system", "subtype": "other"},
        )
        self.assertTrue(launcher_module._main_turn_ended(str(self.transcript)))

    def test_missing_or_empty_transcript_blocks_busy_send(self):
        self.assertFalse(launcher_module._main_turn_ended(None))
        self.assertFalse(launcher_module._main_turn_ended(str(self.transcript)))
        self.transcript.write_bytes(b"")
        self.assertFalse(launcher_module._main_turn_ended(str(self.transcript)))

    def test_partial_new_record_blocks_busy_send(self):
        self.write_records(TURN_END)
        with self.transcript.open("ab") as stream:
            stream.write(b'{"type":"user"')
        self.assertEqual(self.launcher._can_inject_now(), (False, "claude_busy"))

    def test_invalid_utf8_record_blocks_busy_send(self):
        self.write_records(TURN_END)
        with self.transcript.open("ab") as stream:
            stream.write(b'{"type":"user","message":"\xff"}\n')
        self.assertFalse(launcher_module._main_turn_ended(str(self.transcript)))

    def test_non_object_record_blocks_busy_send(self):
        self.write_records(TURN_END, ["unexpected record"])
        self.assertFalse(launcher_module._main_turn_ended(str(self.transcript)))

    def test_scan_discards_only_the_cut_first_record(self):
        self.transcript.write_bytes(
            b"x" * 200 + b"\n" + json.dumps(TURN_END).encode() + b"\n"
        )
        with patch.object(launcher_module, "_TURN_END_SCAN_BYTES", 100):
            self.assertTrue(launcher_module._main_turn_ended(str(self.transcript)))

    def test_no_completion_marker_in_scan_window_blocks_send(self):
        self.write_records(
            TURN_END, {"type": "progress", "data": "x" * 200}
        )
        with patch.object(launcher_module, "_TURN_END_SCAN_BYTES", 100):
            self.assertFalse(launcher_module._main_turn_ended(str(self.transcript)))

    def test_busy_without_confirmed_completion_stays_blocked(self):
        self.write_records({"type": "assistant"})
        self.assertEqual(self.launcher._can_inject_now(), (False, "claude_busy"))

    def test_other_statuses_keep_their_gates(self):
        self.write_records(TURN_END)
        for status in ("idle", "shell", "compacting", "unknown"):
            with self.subTest(status=status):
                self.launcher._claude_status = status
                expected = (True, "ok") if status in ("idle", "shell") else (
                    False, f"claude_{status}"
                )
                self.assertEqual(self.launcher._can_inject_now(), expected)

    def test_recent_keystroke_still_blocks_completed_turn(self):
        self.write_records(TURN_END)
        self.launcher._user.last_keystroke_ts = 99.9
        self.assertEqual(
            self.launcher._can_inject_now(), (False, "user_just_submitted")
        )

    def test_user_buffer_still_blocks_completed_turn(self):
        self.write_records(TURN_END)
        self.launcher._user.estimate = lambda: 3
        self.assertEqual(
            self.launcher._can_inject_now(), (False, "user_has_uncommitted_text")
        )

    def test_missing_session_still_blocks_completed_turn(self):
        self.write_records(TURN_END)
        self.launcher._session_id = None
        self.assertEqual(self.launcher._can_inject_now(), (False, "session_unknown"))

    def test_missing_metadata_clears_old_transcript(self):
        self.write_records(TURN_END)
        with patch.object(launcher_module, "_read_session_metadata", return_value=None):
            self.launcher._refresh_claude_status()
        self.assertIsNone(self.launcher._transcript)
        self.assertEqual(self.launcher._can_inject_now(), (False, "claude_busy"))

    def test_unconfirmed_session_clears_old_transcript(self):
        with (
            patch.object(launcher_module, "_read_session_metadata", return_value={"status": "busy"}),
            patch.object(launcher_module, "_canonical_session_id", return_value=None),
        ):
            self.launcher._refresh_claude_status()
        self.assertIsNone(self.launcher._transcript)

    def test_confirmed_session_refreshes_transcript(self):
        with (
            patch.object(launcher_module, "_read_session_metadata", return_value={"status": "busy"}),
            patch.object(launcher_module, "_canonical_session_id", return_value="resumed"),
            patch.object(launcher_module, "_transcript_path", return_value="resumed.jsonl"),
        ):
            self.launcher._refresh_claude_status()
        self.assertEqual(self.launcher._session_id, "resumed")
        self.assertEqual(self.launcher._transcript, "resumed.jsonl")


if __name__ == "__main__":
    unittest.main()
