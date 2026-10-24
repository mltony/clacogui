"""Index cache freshness, forced refresh and body-free dialog scans."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import conversation_index
from fs import FsEntry
import gui


class Backend:
    def __init__(self):
        self.listings = []
        self.dirs = {
            "projects": [FsEntry("project", True)],
            "projects/project": [FsEntry("session.jsonl", False, 100, 1)],
        }

    def list_dir(self, rel):
        self.listings.append(rel)
        return self.dirs.get(rel, [])


class TestIndexCache(unittest.TestCase):
    def test_listing_cache_reuses_directories_and_force_refresh_fetches_once(self):
        backend = Backend()
        view = conversation_index.ListingView(backend)
        self.assertTrue(view.is_dir("projects"))
        view.list_dir("projects")
        self.assertEqual(backend.listings, ["projects"])
        forced = conversation_index.ListingView(backend, force=True)
        forced.is_dir("projects")
        forced.list_dir("projects")
        self.assertEqual(backend.listings, ["projects", "projects"])

    def test_expired_listing_is_downloaded_and_other_backends_are_isolated(self):
        first, second = Backend(), Backend()
        with patch("conversation_index.time.monotonic", return_value=1):
            conversation_index.ListingView(first).list_dir("projects")
        with patch("conversation_index.time.monotonic", return_value=10):
            conversation_index.ListingView(first).list_dir("projects")
            conversation_index.ListingView(second).list_dir("projects")
        self.assertEqual(len(first.listings), 2)
        self.assertEqual(len(second.listings), 1)

    def test_optional_preview_updates_do_not_extend_listing_freshness(self):
        backend = Backend()
        with patch("conversation_index.time.monotonic", return_value=1):
            conversation_index.remember(backend, "claude", (["old"], set()))
        with patch("conversation_index.time.monotonic", return_value=10):
            conversation_index.remember(backend, "claude", (["renamed"], set()), preserve_age=True)
            result, age = conversation_index.snapshot(backend, "claude")
        self.assertEqual(age, 9)
        result[0].append("UI mutation")
        self.assertEqual(conversation_index.snapshot(backend, "claude")[0][0], ["renamed"])
        self.assertIsNone(conversation_index.snapshot(backend, "codex-None"))

    def test_claude_scan_uses_polled_names_without_reading_transcript_bodies(self):
        backend = Backend()
        dialog = SimpleNamespace(_have_active_sids=True, _cached_names={"session": "My name"}, _force_scan=False)
        rows, active = gui.OpenConversationDialog._scan(dialog, backend, {"session"})
        self.assertEqual(active, {"session"})
        self.assertEqual(rows[0].info.name, "My name")
        self.assertEqual(backend.listings, ["projects", "projects/project"])

    def test_codex_scan_defers_previews_and_uses_cached_names(self):
        backend = Backend()
        dialog = SimpleNamespace(_cached_names={"session": "My Codex"}, _force_scan=False,
                                 _day_limit=3, RECENT_ACTIVE_SEC=3600)
        info = SimpleNamespace(session_id="session", mtime=1)
        with patch.object(gui.codex_models, "read_launcher_states", return_value={}), \
             patch.object(gui.codex_models, "iter_conversation_files", return_value=[("path", "day", 1, 100)]), \
             patch.object(gui.codex_models, "summarize_conversation", return_value=info), \
             patch.object(gui.codex_models, "load_session_names") as names:
            rows, active = gui.OpenCodexConversationDialog._scan(dialog, backend, {"session"})
        names.assert_not_called()
        self.assertEqual(active, {"session"})
        self.assertEqual(rows[0].info, info)
