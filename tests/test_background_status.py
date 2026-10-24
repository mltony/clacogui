"""Check parsed turn state and its status-bar presentation."""

import json
import unittest
from types import SimpleNamespace

import gui
import models


TURN_END = {"type": "system", "subtype": "turn_duration"}
USER = {
    "type": "user", "promptId": "prompt-1", "uuid": "user-1",
    "message": {"role": "user", "content": "Hello"},
}
ASSISTANT = {
    "type": "assistant", "uuid": "assistant-1",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "Reply"}]},
}


def parse_records(*records):
    text = "".join(json.dumps(record) + "\n" for record in records)
    backend = SimpleNamespace(read_text=lambda path, encoding="utf-8": text, cache_transcripts=False)
    return models.parse_conversation(backend, "session.jsonl")


def status_text(data, status="busy", codex=False):
    panel = SimpleNamespace(_data=data, is_codex=codex)
    panel.main_turn_ended = lambda: gui.ConversationPanel.main_turn_ended(panel)
    panel.codex_busy = lambda: gui.ConversationPanel.codex_busy(panel)
    frame = SimpleNamespace(
        _claude_session_id_for_active_tab=lambda: "session",
        _current_panel=lambda: panel,
        _claude_status_map={"session": status},
        _codex_status_map={"session": status},
    )
    return gui.MainFrame._claude_status_text(frame)


class TestBackgroundStatus(unittest.TestCase):
    def test_unknown_transcript_keeps_busy_status(self):
        data = parse_records()
        self.assertIsNone(data.main_turn_open)
        self.assertEqual(status_text(data), "Claude: busy")

    def test_active_main_turn_stays_busy(self):
        data = parse_records(USER, ASSISTANT)
        self.assertTrue(data.main_turn_open)
        self.assertEqual(status_text(data), "Claude: busy")

    def test_completed_main_turn_is_background(self):
        data = parse_records(USER, ASSISTANT, TURN_END)
        self.assertFalse(data.main_turn_open)
        self.assertEqual(status_text(data), "Claude: background")
        self.assertEqual(len(data.turns), 1)
        self.assertEqual(data.turns[0].user_text, "Hello")
        self.assertEqual(data.turns[0].response, "Reply")

    def test_new_main_message_restores_busy(self):
        for record in (USER, ASSISTANT):
            with self.subTest(kind=record["type"]):
                data = parse_records(USER, ASSISTANT, TURN_END, record)
                self.assertTrue(data.main_turn_open)
                self.assertEqual(status_text(data), "Claude: busy")

    def test_sidechain_messages_do_not_change_background_status(self):
        data = parse_records(USER, ASSISTANT, TURN_END)
        for record in (USER, ASSISTANT, TURN_END):
            models.track_main_turn(data, {**record, "isSidechain": True})
        self.assertFalse(data.main_turn_open)
        self.assertEqual(status_text(data), "Claude: background")

    def test_incremental_updates_track_main_turn_transitions(self):
        data = parse_records(USER, ASSISTANT)
        models.track_main_turn(data, TURN_END)
        self.assertEqual(status_text(data), "Claude: background")
        models.track_main_turn(data, USER)
        self.assertEqual(status_text(data), "Claude: busy")
        models.track_main_turn(data, {**TURN_END, "isSidechain": True})
        self.assertEqual(status_text(data), "Claude: busy")

    def test_other_metadata_statuses_are_preserved(self):
        data = parse_records(USER, ASSISTANT, TURN_END)
        for status in ("idle", "shell", "compacting"):
            with self.subTest(status=status):
                self.assertEqual(status_text(data, status), f"Claude: {status}")

    def test_codex_status_is_preserved(self):
        data = parse_records(USER, ASSISTANT, TURN_END)
        self.assertEqual(status_text(data, codex=True), "Codex: busy")


if __name__ == "__main__":
    unittest.main()
