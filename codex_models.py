"""Codex CLI conversation reading for clacogui.

Codex keeps one rollout per thread under its home directory (``~/.codex``
by default; ``$CODEX_HOME`` overrides it)::

    sessions/YYYY/MM/DD/rollout-<local-timestamp>-<thread-uuid>.jsonl

Every line is ``{"timestamp": ..., "type": ..., "payload": {...}}``.  The
entries clacogui needs:

* ``event_msg`` / ``item_completed`` with ``item.type == "UserMessage"`` --
  the text the user typed (older Codex builds log ``event_msg`` /
  ``user_message`` instead).  ``response_item`` user messages are *not*
  used: they also carry the injected AGENTS.md / environment context.
* ``response_item`` / ``message`` with ``role == "assistant"`` -- reply text.
* ``event_msg`` / ``task_started`` and ``task_complete`` / ``turn_aborted``
  -- whether a turn is in progress.

Thread names live in ``session_index.jsonl`` (``id``, ``thread_name``,
append-only, latest line per id wins).  The clacogui Codex launcher
publishes its live state under ``clacogui_sessions/<pid>.json``.

All reads go through an :class:`fs.FsBackend`, so local paths, SMB drives
and FTP behave the same.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from fs import FsBackend
from models import ConversationData, ConversationInfo, Turn

log = logging.getLogger(__name__)

SESSIONS_DIR = "sessions"
SESSION_INDEX = "session_index.jsonl"
LAUNCHER_SESSIONS_DIR = "clacogui_sessions"

# Launcher state files are rewritten at least this often while the
# launcher runs; anything older belongs to a launcher that died without
# cleaning up.
LAUNCHER_STALE_SEC = 120

# The open dialog only walks this many of the newest day directories
# unless the user asks for everything -- each directory listing is a
# round trip on FTP.
RECENT_DAY_DIRS = 14

_ROLLOUT_RE = re.compile(
    r"^rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')

_TURN_END_EVENTS = frozenset({"task_complete", "turn_aborted"})


def thread_id_from_rel(rel_path: str) -> Optional[str]:
    m = _ROLLOUT_RE.match(posixpath.basename(rel_path))
    return m.group(1).lower() if m else None


@dataclass
class CodexConversationData(ConversationData):
    """:class:`ConversationData` plus the turn state Codex exposes."""

    busy: bool = False
    open_turns: set = field(default_factory=set)
    user_ids: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Line consumption (shared by full parse and incremental tail reads)
# ---------------------------------------------------------------------------


def _user_text_from_item(item: dict) -> Optional[str]:
    parts: list[str] = []
    for c in item.get("content") or []:
        if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
            t = c.get("text")
            if isinstance(t, str):
                parts.append(t)
    text = "\n".join(parts).strip()
    return text or None


def _assistant_text(payload: dict) -> str:
    chunks: list[str] = []
    for c in payload.get("content") or []:
        if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
            t = c.get("text")
            if isinstance(t, str) and t:
                chunks.append(t)
    return "\n\n".join(chunks)


def _append_user(data: CodexConversationData, uid: str, text: str, ts: str) -> None:
    if uid and uid in data.user_ids:
        return
    last = data.turns[-1] if data.turns else None
    if last is not None and not last.response and last.user_text == text:
        # Same prompt logged twice (legacy ``user_message`` alongside the
        # item event in builds that wrote both).
        return
    data.turns.append(
        Turn(order=len(data.turns), user_uuid=uid, user_text=text, user_timestamp=ts)
    )
    if uid:
        data.user_ids.add(uid)


def consume_entry(data: CodexConversationData, entry: dict) -> None:
    """Apply one rollout line to ``data``."""
    etype = entry.get("type")
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return
    ts = entry.get("timestamp") or ""

    if etype == "session_meta":
        sid = payload.get("id") or payload.get("session_id")
        if isinstance(sid, str) and sid and data.session_id is None:
            data.session_id = sid
        return

    if etype == "event_msg":
        ptype = payload.get("type")
        if ptype == "task_started":
            data.open_turns.add(payload.get("turn_id") or "")
            data.busy = True
        elif ptype in _TURN_END_EVENTS:
            data.open_turns.discard(payload.get("turn_id") or "")
            data.busy = bool(data.open_turns)
        elif ptype == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                text = _user_text_from_item(item)
                if text:
                    _append_user(data, item.get("id") or "", text, ts)
        elif ptype == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text.strip():
                _append_user(data, "", text.strip(), ts)
        return

    if etype == "response_item":
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            return
        if not data.turns:
            return
        mid = payload.get("id") or ""
        if mid and mid in data.assistant_uuids:
            return
        chunk = _assistant_text(payload)
        if not chunk:
            return
        turn = data.turns[-1]
        turn.response = (turn.response + "\n\n" + chunk) if turn.response else chunk
        if mid:
            data.assistant_uuids.add(mid)


def consume_text(data: CodexConversationData, text: str) -> None:
    """Apply every complete JSON line in ``text``; malformed lines are skipped."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            consume_entry(data, entry)


def refresh_name(data: CodexConversationData, rel_path: str) -> None:
    data.name = (
        (data.turns[0].preview(60) if data.turns else "")
        or thread_id_from_rel(rel_path)
        or posixpath.splitext(posixpath.basename(rel_path))[0]
    )


def parse_conversation(backend: FsBackend, rel_path: str) -> CodexConversationData:
    """Parse a Codex rollout.  Raises :class:`OSError` on read failure."""
    text = backend.read_text(rel_path)
    data = CodexConversationData()
    consume_text(data, text)
    if data.session_id is None:
        data.session_id = thread_id_from_rel(rel_path)
    refresh_name(data, rel_path)
    return data


# ---------------------------------------------------------------------------
# Thread names and launcher state
# ---------------------------------------------------------------------------


def parse_session_index(text: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        tid, nm = entry.get("id"), entry.get("thread_name")
        if isinstance(tid, str) and tid and isinstance(nm, str) and nm.strip():
            names[tid] = nm.strip()
    return names


def load_session_names(backend: FsBackend) -> dict[str, str]:
    try:
        return parse_session_index(backend.read_text(SESSION_INDEX))
    except FileNotFoundError:
        return {}
    except OSError:
        log.debug("Could not read %s", SESSION_INDEX, exc_info=True)
        return {}


@dataclass
class LauncherState:
    """One live ``clacogui_codex_launcher`` as seen through its state file."""

    session_id: str
    status: str
    rollout: str = ""


def read_launcher_states(backend: FsBackend) -> dict[str, LauncherState]:
    """``{thread_id: LauncherState}`` from ``clacogui_sessions/*.json``.

    The freshest file wins when two launchers claim one thread.  Never
    raises; runs on the IO worker.
    """
    try:
        entries = backend.list_dir(LAUNCHER_SESSIONS_DIR)
    except FileNotFoundError:
        return {}
    except OSError:
        log.debug("list_dir(%s) failed", LAUNCHER_SESSIONS_DIR, exc_info=True)
        return {}
    cutoff = time.time() - LAUNCHER_STALE_SEC
    candidates = sorted(
        (
            (e.mtime or 0.0, e.name)
            for e in entries
            if not e.is_dir
            and e.name.endswith(".json")
            and not (e.mtime and e.mtime < cutoff)
        ),
        reverse=True,
    )
    out: dict[str, LauncherState] = {}
    for _mtime, fname in candidates:
        try:
            data = json.loads(backend.read_text(f"{LAUNCHER_SESSIONS_DIR}/{fname}"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        status = data.get("status")
        if not (isinstance(sid, str) and sid and isinstance(status, str) and status):
            continue
        rollout = data.get("rollout")
        out.setdefault(
            sid,
            LauncherState(sid, status, rollout if isinstance(rollout, str) else ""),
        )
    return out


# ---------------------------------------------------------------------------
# Conversation listing for the open dialog
# ---------------------------------------------------------------------------


def _sorted_subdirs(backend: FsBackend, rel: str) -> list[str]:
    try:
        entries = backend.list_dir(rel)
    except FileNotFoundError:
        return []
    except OSError:
        log.debug("Could not list %s", rel, exc_info=True)
        return []
    return sorted((e.name for e in entries if e.is_dir), reverse=True)


def iter_day_dirs(backend: FsBackend, limit: Optional[int]) -> Iterator[str]:
    """Yield ``sessions/YYYY/MM/DD`` paths, newest first, at most ``limit``."""
    n = 0
    for year in _sorted_subdirs(backend, SESSIONS_DIR):
        y_rel = posixpath.join(SESSIONS_DIR, year)
        for month in _sorted_subdirs(backend, y_rel):
            m_rel = posixpath.join(y_rel, month)
            for day in _sorted_subdirs(backend, m_rel):
                if limit is not None and n >= limit:
                    return
                n += 1
                yield posixpath.join(m_rel, day)


def iter_conversation_files(
    backend: FsBackend, day_limit: Optional[int] = RECENT_DAY_DIRS
) -> Iterator[tuple[str, str, float, int]]:
    """Yield ``(rel_path, day_dir, mtime, size)`` for each rollout."""
    for day_rel in iter_day_dirs(backend, day_limit):
        try:
            files = backend.list_dir(day_rel)
        except OSError:
            log.debug("Could not list %s", day_rel, exc_info=True)
            continue
        for f in files:
            if f.is_dir or not _ROLLOUT_RE.match(f.name):
                continue
            yield posixpath.join(day_rel, f.name), day_rel, f.mtime, f.size


def summarize_conversation(
    rel_path: str, names: dict[str, str], mtime: float
) -> ConversationInfo:
    tid = thread_id_from_rel(rel_path)
    return ConversationInfo(
        rel_path=rel_path,
        session_id=tid,
        name=(names.get(tid) if tid else None) or tid or posixpath.basename(rel_path),
        mtime=mtime,
        project_dir="",
    )


def cwd_from_head(text: str) -> str:
    """Pull ``session_meta.cwd`` out of the first bytes of a rollout."""
    m = _CWD_RE.search(text)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1) + '"')
    except ValueError:
        return m.group(1)
