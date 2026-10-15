"""Check cache reuse, replacement, and incremental byte offsets."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fs import LocalBackend
import transcript_cache


class RecordingBackend(LocalBackend):
    cache_transcripts = True

    def __init__(self, root):
        super().__init__(str(root))
        self.offsets = []

    def read_bytes_from(self, rel, offset):
        self.offsets.append(offset)
        return super().read_bytes_from(rel, offset)


class TestTranscriptCache(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cache = root / "cache"
        self.remote = root / "remote"
        self.remote.mkdir()
        self.path = self.remote / "session.jsonl"
        self.backend = RecordingBackend(self.remote)
        self.enterContext(patch.object(transcript_cache, "CACHE_DIR", str(self.cache)))

    def cached_bytes(self):
        return next(self.cache.glob("*.jsonl")).read_bytes()

    def test_first_read_keeps_complete_copy(self):
        data = ("Привет, мир!\n" * 1000).encode("utf-8")
        self.path.write_bytes(data)
        self.assertEqual(transcript_cache.read_text(self.backend, "session.jsonl"), data.decode("utf-8"))
        self.assertEqual(self.backend.offsets, [0])
        self.assertEqual(self.cached_bytes(), data)

    def test_reopen_downloads_overlap_and_new_bytes(self):
        old = b"old record\n" * 1000
        self.path.write_bytes(old)
        transcript_cache.read_text(self.backend, "session.jsonl")
        self.backend.offsets.clear()
        new = old + "новая запись\n".encode("utf-8")
        self.path.write_bytes(new)
        self.assertEqual(transcript_cache.read_text(self.backend, "session.jsonl"), new.decode("utf-8"))
        self.assertEqual(self.backend.offsets, [len(old) - transcript_cache.OVERLAP])
        self.assertEqual(self.cached_bytes(), new)

    def test_truncated_remote_is_downloaded_again(self):
        old = b"old record\n" * 1000
        self.path.write_bytes(old)
        transcript_cache.read_text(self.backend, "session.jsonl")
        self.backend.offsets.clear()
        self.path.write_bytes(b"replacement\n")
        self.assertEqual(transcript_cache.read_text(self.backend, "session.jsonl"), "replacement\n")
        self.assertEqual(self.backend.offsets, [len(old) - transcript_cache.OVERLAP, 0])
        self.assertEqual(self.cached_bytes(), b"replacement\n")

    def test_changed_overlap_invalidates_cache(self):
        self.path.write_bytes(b"old record\n" * 1000)
        transcript_cache.read_text(self.backend, "session.jsonl")
        new = b"replacement record\n" * 1000
        self.path.write_bytes(new)
        self.assertEqual(transcript_cache.read_text(self.backend, "session.jsonl"), new.decode())
        self.assertEqual(self.cached_bytes(), new)

    def test_partial_line_is_not_duplicated_by_polling(self):
        first = b'{"type":"user"}\n'
        old = first + b'{"type":"assistant",'
        self.path.write_bytes(old)
        transcript_cache.read_text(self.backend, "session.jsonl")
        new = old + b'"message":"reply"}\n'
        self.path.write_bytes(new)
        for _ in range(2):
            tail = transcript_cache.read_text_from(self.backend, "session.jsonl", len(first))
            self.assertEqual(tail, new[len(first):].decode())
            self.assertEqual(self.cached_bytes(), new)

    def test_local_backend_bypasses_cache(self):
        self.path.write_text("local text", encoding="utf-8")
        local = LocalBackend(str(self.remote))
        self.assertEqual(transcript_cache.read_text(local, "session.jsonl"), "local text")
        self.assertEqual(transcript_cache.read_text_from(local, "session.jsonl", 6), "text")
        self.assertFalse(self.cache.exists())

    def test_unwritable_cache_does_not_break_remote_read(self):
        self.path.write_bytes(b"remote text")
        with (
            patch.object(transcript_cache.os, "makedirs", side_effect=OSError("Unavailable")),
            self.assertLogs("transcript_cache", level="WARNING"),
        ):
            self.assertEqual(transcript_cache.read_text(self.backend, "session.jsonl"), "remote text")

    def test_remote_failure_is_not_hidden_by_old_cache(self):
        self.path.write_bytes(b"old text")
        transcript_cache.read_text(self.backend, "session.jsonl")
        with patch.object(self.backend, "read_bytes_from", side_effect=OSError("Disconnected")):
            with self.assertRaisesRegex(OSError, "Disconnected"):
                transcript_cache.read_text(self.backend, "session.jsonl")


if __name__ == "__main__":
    unittest.main()
