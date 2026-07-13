"""Data models for clacogui.

Reads Claude Code conversation files (JSONL) and the per-session metadata
under ``<root>/sessions/*.json`` through an :class:`fs.FsBackend`, so the
same code drives local filesystems, SMB-mapped drives, and FTP.
"""

from __future__ import annotations

import json
import logging
import posixpath
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from fs import FsBackend

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """A single user prompt with the assistant text that followed.

    ``response`` may be empty if the assistant has not replied yet, or if the
    only assistant content was non-text (e.g. tool calls).  ``order`` is the
    position of the user prompt in the file, used as a stable sort key when
    multiple prompts share the same timestamp.
    """

    order: int
    user_uuid: str
    user_text: str
    user_timestamp: str = ""
    response: str = ""

    def preview(self, max_chars: int = 80) -> str:
        """Single-line preview suitable for a list control."""
        text = self.user_text.strip().replace("\r", " ").replace("\n", " ")
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "\u2026"
        return text or "(empty)"

    def cropped_user_text(self, max_chars: int = 10000) -> str:
        """User message cropped if it exceeds ``max_chars`` characters."""
        text = self.user_text
        if len(text) > max_chars:
            return text[:max_chars] + (
                f"\n\n... [truncated, original was {len(text)} characters]"
            )
        return text


@dataclass
class ConversationInfo:
    """Lightweight summary used by the open dialog.

    ``rel_path`` is the conversation's path relative to the backend root,
    using forward slashes (e.g. ``projects/-home-me-code/abc.jsonl``).
    """

    rel_path: str
    session_id: Optional[str]
    name: str
    mtime: float
    project_dir: str = ""

    def display_name(self) -> str:
        return self.name or posixpath.basename(self.rel_path)


@dataclass
class ConversationData:
    """Parsed contents of a JSONL conversation file."""

    session_id: Optional[str] = None
    name: str = ""
    custom_title: Optional[str] = None
    agent_name: Optional[str] = None
    turns: list[Turn] = field(default_factory=list)
    # uuids of every assistant entry whose text was merged into a turn's
    # response.  Lets the live view dedup incremental reads after a full
    # reload (see GUI ``_apply_incremental``).
    assistant_uuids: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Session-name lookup
# ---------------------------------------------------------------------------


def load_session_names(backend: FsBackend) -> dict[str, str]:
    """Build ``{sessionId: name}`` from every ``sessions/*.json``.

    Robust to missing dir, unreadable files, and malformed JSON.
    """
    out: dict[str, str] = {}
    try:
        if not backend.is_dir("sessions"):
            return out
    except OSError:
        log.debug("Could not check sessions/", exc_info=True)
        return out

    try:
        entries = backend.list_dir("sessions")
    except OSError:
        log.exception("Could not list sessions/")
        return out

    for entry in entries:
        if entry.is_dir or not entry.name.endswith(".json"):
            continue
        rel = posixpath.join("sessions", entry.name)
        try:
            text = backend.read_text(rel)
            data = json.loads(text)
        except (OSError, ValueError):
            log.debug("Skipping bad session file %s", rel, exc_info=True)
            continue
        sid = data.get("sessionId")
        nm = data.get("name")
        if sid and nm:
            out[sid] = nm
    return out


# ---------------------------------------------------------------------------
# Conversation listing for open dialog
# ---------------------------------------------------------------------------


def iter_conversation_files(
    backend: FsBackend,
) -> Iterator[tuple[str, str, float, int]]:
    """Yield ``(rel_path, project_dir, mtime, size)`` for every JSONL.

    The mtime/size come from the directory listing -- no extra ``stat()``
    per file -- so this is fast over FTP too.
    """
    try:
        if not backend.is_dir("projects"):
            return
    except OSError:
        log.debug("Could not check projects/", exc_info=True)
        return

    try:
        project_entries = backend.list_dir("projects")
    except OSError:
        log.exception("Could not list projects/")
        return

    for proj in project_entries:
        if not proj.is_dir:
            continue
        proj_rel = posixpath.join("projects", proj.name)
        try:
            files = backend.list_dir(proj_rel)
        except OSError:
            log.debug("Could not list %s", proj_rel, exc_info=True)
            continue
        for f in files:
            if f.is_dir or not f.name.endswith(".jsonl"):
                continue
            yield (
                posixpath.join(proj_rel, f.name),
                proj.name,
                f.mtime,
                f.size,
            )


def extract_name_from_head(text: str) -> Optional[str]:
    """Extract a conversation name from the first few lines of a JSONL.

    Looks for ``custom-title`` or ``agent-name`` entries which are
    typically near the start of the file.  Returns the first found,
    or None.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        etype = entry.get("type")
        if etype == "custom-title":
            title = entry.get("customTitle")
            if isinstance(title, str) and title.strip():
                return title.strip()
        if etype == "agent-name":
            name = entry.get("agentName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def summarize_conversation(
    rel_path: str,
    session_names: dict[str, str],
    project_dir: str,
    mtime: float,
) -> ConversationInfo:
    """Build a :class:`ConversationInfo` *without* reading the JSONL body.

    By Claude Code convention the JSONL filename (minus ``.jsonl``) is the
    ``sessionId``; sessions/*.json gives the human-readable name when the
    user has run ``/rename``.  We never need to download the conversation
    itself for the open dialog -- crucial for FTP and slow shares.
    """
    base = posixpath.basename(rel_path)
    sid, _ = posixpath.splitext(base)
    name = session_names.get(sid) or sid
    return ConversationInfo(
        rel_path=rel_path,
        session_id=sid,
        name=name,
        mtime=mtime,
        project_dir=project_dir,
    )


# ---------------------------------------------------------------------------
# Conversation parsing (for an open conversation tab)
# ---------------------------------------------------------------------------


def parse_conversation(backend: FsBackend, rel_path: str) -> ConversationData:
    """Parse a Claude Code JSONL file into :class:`ConversationData`.

    Raises :class:`OSError` on read failure.  Malformed JSON lines are
    skipped so a partial write at the tail of an actively-being-written
    file doesn't break the whole conversation.
    """
    text = backend.read_text(rel_path)

    data = ConversationData()
    pending: Optional[Turn] = None
    response_chunks: list[str] = []
    order = 0

    def finalize() -> None:
        nonlocal pending, response_chunks
        if pending is not None:
            pending.response = "\n\n".join(c for c in response_chunks if c).strip()
            data.turns.append(pending)
        pending = None
        response_chunks = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            log.debug("Skipping malformed JSON line in %s", rel_path)
            continue

        if data.session_id is None:
            sid = entry.get("sessionId")
            if sid:
                data.session_id = sid

        etype = entry.get("type")
        if etype == "custom-title":
            data.custom_title = entry.get("customTitle") or data.custom_title
            continue
        if etype == "agent-name":
            data.agent_name = entry.get("agentName") or data.agent_name
            continue

        if etype == "user":
            user_text = _extract_user_text(entry)
            if user_text is None:
                continue
            finalize()
            pending = Turn(
                order=order,
                user_uuid=entry.get("uuid", ""),
                user_text=user_text,
                user_timestamp=entry.get("timestamp", ""),
            )
            order += 1
            continue

        if etype == "assistant" and pending is not None:
            chunk = _extract_assistant_text(entry)
            if chunk:
                response_chunks.append(chunk)
                a_uuid = entry.get("uuid", "")
                if a_uuid:
                    data.assistant_uuids.add(a_uuid)
            continue

    finalize()

    data.name = (
        data.custom_title
        or data.agent_name
        or (data.turns[0].preview(60) if data.turns else "")
        or posixpath.splitext(posixpath.basename(rel_path))[0]
    )
    return data


def _extract_user_text(entry: dict) -> Optional[str]:
    """Return the user-typed text, or ``None`` if this isn't a real prompt.

    Filters out ``tool_result`` echoes, attachments, interrupt notices, and
    other synthetic entries that share ``type: "user"``.
    """
    if entry.get("type") != "user":
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    if msg.get("role") != "user":
        return None
    if not entry.get("promptId"):
        return None  # tool_results / synthetic entries don't have one

    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                return None  # not a typed prompt
            if item.get("type") == "text":
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        text = "\n".join(parts).strip()
    else:
        return None

    if not text.strip():
        return None
    if text.strip().startswith("[Request interrupted"):
        return None
    return text


def _extract_assistant_text(entry: dict) -> str:
    """Concatenate every top-level ``text`` block in an assistant entry."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text", "")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def file_signature(
    backend: FsBackend, rel_path: str
) -> Optional[tuple[float, int]]:
    """Cheap change-detector: ``(mtime, size)`` or ``None`` on error.

    Errors are logged at DEBUG so a flaky share doesn't spam the log,
    but they're not entirely swallowed -- the message includes both the
    relative path and the exception so silent polling regressions are
    easy to spot.
    """
    try:
        return backend.stat(rel_path)
    except OSError as e:
        log.debug("file_signature(%s) failed: %r", rel_path, e)
        return None


def format_timestamp(ts: float) -> str:
    """Human-friendly timestamp for the open dialog."""
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OSError, ValueError):
        return ""
