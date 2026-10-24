"""Metadata cache invalidation and both providers' real state readers."""

import json
import time
import unittest
from unittest.mock import patch

import codex_models
import gui
import metadata_cache
from fs import FsEntry


class Backend:
    def __init__(self):
        self.text = json.dumps({"sessionId": "session", "status": "busy", "name": "alias"})
        self.entry = FsEntry("one.json", False, len(self.text), time.time())
        self.reads = 0

    def list_dir(self, directory):
        return [self.entry]

    def read_text(self, rel):
        self.reads += 1
        return self.text


class TestMetadataCache(unittest.TestCase):
    def test_unchanged_metadata_is_reused_for_both_providers(self):
        backend = Backend()
        with patch("metadata_cache.time.monotonic", return_value=1):
            for _ in range(3):
                self.assertEqual(gui._read_claude_session_state(backend), ({"session": "busy"}, {"session": "alias"}))
                self.assertEqual(codex_models.read_launcher_states(backend)["session"].status, "busy")
        # One read per provider directory, rather than six downloads.
        self.assertEqual(backend.reads, 2)

    def test_changed_signature_is_read_immediately(self):
        backend = Backend()
        for field in ("mtime", "size"):
            with self.subTest(field=field), patch("metadata_cache.time.monotonic", return_value=1):
                metadata_cache.read_json(backend, "session.json", backend.entry)
                before = backend.reads
                setattr(backend.entry, field, getattr(backend.entry, field) + 1)
                metadata_cache.read_json(backend, "session.json", backend.entry)
                self.assertEqual(backend.reads, before + 1)

    def test_same_signature_is_periodically_revalidated(self):
        backend = Backend()
        with patch("metadata_cache.time.monotonic", return_value=1):
            self.assertEqual(metadata_cache.read_json(backend, "session.json", backend.entry)["status"], "busy")
        backend.text = backend.text.replace("busy", "idle")
        with patch("metadata_cache.time.monotonic", return_value=1 + metadata_cache.MAX_AGE_SEC):
            self.assertEqual(metadata_cache.read_json(backend, "session.json", backend.entry)["status"], "idle")
        self.assertEqual(backend.reads, 2)

    def test_missing_timestamps_are_not_trusted(self):
        backend = Backend()
        backend.entry.mtime = 0
        for _ in range(2):
            metadata_cache.read_json(backend, "session.json", backend.entry)
        self.assertEqual(backend.reads, 2)

    def test_stable_metadata_survives_a_scan_longer_than_two_seconds(self):
        backend = Backend()
        backend.entry.mtime = 100
        with patch("metadata_cache.time.time", return_value=1000), patch("metadata_cache.time.monotonic", return_value=1):
            metadata_cache.read_json(backend, "one.json", backend.entry)
        with patch("metadata_cache.time.monotonic", return_value=12):
            metadata_cache.read_json(backend, "one.json", backend.entry)
        self.assertEqual(backend.reads, 1)
        with patch("metadata_cache.time.monotonic", return_value=1 + metadata_cache.STABLE_MAX_AGE_SEC):
            metadata_cache.read_json(backend, "one.json", backend.entry)
        self.assertEqual(backend.reads, 2)

    def test_recent_same_second_rewrite_is_confirmed_before_long_caching(self):
        backend = Backend()
        backend.entry.mtime = 100
        with patch("metadata_cache.time.time", return_value=100.2), patch("metadata_cache.time.monotonic", return_value=1):
            metadata_cache.read_json(backend, "one.json", backend.entry)
        backend.text = backend.text.replace("busy", "idle")
        with patch("metadata_cache.time.time", return_value=103), patch("metadata_cache.time.monotonic", return_value=4):
            self.assertEqual(metadata_cache.read_json(backend, "one.json", backend.entry)["status"], "idle")
        with patch("metadata_cache.time.monotonic", return_value=20):
            self.assertEqual(metadata_cache.read_json(backend, "one.json", backend.entry)["status"], "idle")
        self.assertEqual(backend.reads, 2)

    def test_deleted_and_recreated_file_does_not_reuse_old_data(self):
        backend = Backend()
        metadata_cache.read_json(backend, "sessions/one.json", backend.entry)
        metadata_cache.prune(backend, "sessions", [])
        backend.text = backend.text.replace("busy", "idle")
        self.assertEqual(metadata_cache.read_json(backend, "sessions/one.json", backend.entry)["status"], "idle")

    def test_invalid_json_is_retried_and_backend_caches_are_isolated(self):
        first, second = Backend(), Backend()
        first.text = "{"
        with self.assertRaises(ValueError):
            metadata_cache.read_json(first, "one.json", first.entry)
        first.text = second.text.replace("busy", "idle")
        self.assertEqual(metadata_cache.read_json(first, "one.json", first.entry)["status"], "idle")
        self.assertEqual(metadata_cache.read_json(second, "one.json", second.entry)["status"], "busy")
