#!/usr/bin/env python3
"""clacogui_launcher - transparent PTY parent for Claude Code.

Run *instead of* ``claude`` so that:

* Your real terminal continues to render the TUI, exactly as if claude
  were invoked directly (we are byte-transparent, not a terminal
  emulator).
* The clacogui GUI can drop "send this message" files into
  ``~/.claude/clacogui_outgoing/`` and we will type their contents into
  claude's PTY.  Multi-line messages are emitted as raw LF (``\n``,
  Ctrl+J) between lines and a final CR (``\r``, Ctrl+M) to submit;
  bracketed paste was tried but did not take in claude's prompt.

The launcher is **stdlib only** (``pty``, ``select``, ``termios``,
``fcntl``, ``signal``, ``os``, ``json``, ``glob``, ``time``, ``uuid``).
This makes it cheap to ship inside any container / sandbox -- no pip
install inside the container.

Usage -- run it in place of ``claude``::

    python3 ~/.claude/clacogui_launcher.py [args passed to claude...]

For example, to resume a session::

    python3 ~/.claude/clacogui_launcher.py --resume <session-id>

Side-channel protocol -- see ``~/.claude/clacogui_outgoing/<id>.json``::

    # "Send a message" envelope (the default; ``action`` may be omitted).
    {
      "id":           "<unix-millis>_<rand>",
      "session_id":   "<claude session id>",
      "action":       "send",
      "text":         "<the message to type>",
      "force_clear":  false
    }

    # "Interrupt claude" envelope -- writes a single Esc (``\x1b``)
    # to the PTY master so claude cancels the current turn.  ESC is
    # idempotent in claude's TUI (unlike Ctrl+C, which exits on the
    # second hit) so this is safe to spam.  No ``text`` field; we
    # bypass all gates (claude_busy in particular -- interrupt is
    # *meant* for busy claude).
    {
      "id":           "<unix-millis>_<rand>",
      "session_id":   "<claude session id>",
      "action":       "interrupt"
    }

State the launcher writes back, by mutating the same file:

* file deleted             -> delivered
* ``<id>.json.failed``     -> error (with appended ``error`` field)
* ``<id>.json.needs_decision`` -> blocked because the user has
  uncommitted text in claude's input box; the GUI must rewrite the
  file with ``force_clear: true`` and rename back to ``<id>.json`` to
  authorize an erasure-then-paste.  Interrupt envelopes never use
  this state -- they bypass the user-buffer gate entirely.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import sys
import termios
import time
import tty
import uuid
from typing import Optional


# Claude's session_id is a canonical UUID v4.  ``-r``/``--resume`` on
# the claude CLI also accepts a *session alias* (the ``name`` field
# in the session metadata; e.g. ``aaa1`` after ``/rename aaa1``), so
# the argv value is **not** safe to use as a session_id directly.
# We use this regex to tell "real id" from "alias" and to know when
# we should fall back to argv if metadata never arrives.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _looks_like_uuid(s: str | None) -> bool:
    return bool(s) and bool(_UUID_RE.match(s or ""))


# ---------------------------------------------------------------------------
# Paths, constants
# ---------------------------------------------------------------------------


def _claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


OUTGOING_DIR = "clacogui_outgoing"

# We deliver multi-line messages by typing raw LF (``\n``, char 0x0A)
# between lines.  In claude's TUI that's the keystroke produced by
# Ctrl+J, which the user has confirmed inserts a newline; bracketed
# paste (``\x1b[200~ ... \x1b[201~``) was tried first but did not
# take in practice, so we fall back to direct Ctrl+J injection plus a
# single trailing CR (``\r``, Ctrl+M) to submit.
_CR = b"\r"   # Ctrl+M -- "send" in claude's prompt
# Esc byte -- claude's TUI treats this as "cancel current turn" /
# "stop streaming response".  Crucially it's idempotent: pressing
# Esc multiple times never quits claude (Ctrl+C does, on the second
# hit, which is why we use Esc instead).  Used by the
# ``action: interrupt`` envelope.
_ESC = b"\x1b"

# Pause inserted between "type the text" and "press Enter" when
# delivering a queued message.  Claude's TUI (Ink + readline) does
# rapid-input coalescing that looks a lot like paste detection: when a
# burst of bytes arrives in a single read, the trailing CR is absorbed
# into the buffer as content rather than being treated as the discrete
# Enter keystroke that submits.  Splitting body and CR across two
# os.write() calls with a short sleep between them breaks the coalesce
# and lets the CR fire its 'return' key event.  120 ms is empirically
# enough on Linux+xterm without feeling laggy to the user.
_PRE_SUBMIT_PAUSE_SEC = 0.12

# Pause after the erasure prefix (Ctrl+E + Ctrl+U + backspaces) and
# before we start typing the new message.  Claude's prompt repaints
# after deletions; we want it settled before the new bytes arrive so
# they don't race the redraw.
_POST_ERASE_PAUSE_SEC = 0.05

# Erasure sequence we send before injecting a message when the user
# has uncommitted text in claude's prompt and the GUI has
# authorized an erase-and-send.
_CTRL_E = b"\x05"   # readline 'end-of-line'
_CTRL_U = b"\x15"   # readline 'kill-to-start-of-line' (whole line in claude)
_CTRL_W = b"\x17"   # kill word
_BACKSPACE = b"\x7f"

# Cap the number of synthetic backspaces we send so a desync of the
# tracker can't lock the launcher into a long write loop.
_MAX_ERASE_BACKSPACES = 1000

# Polling cadence for the side channel.  200 ms is invisible to humans
# for chat-style sends and keeps wakeups low.
_TICK_SEC = 0.20

# claude session metadata file lives at ~/.claude/sessions/<pid>.json
# and looks like (excerpted)::
#
#   {"pid":701412,"sessionId":"d155f9ea-...","status":"idle", ...}
#
# We use this to discover our own session_id (we know our claude's
# pid) and to gate injection on claude actually being idle.
_SESSIONS_SUBDIR = "sessions"

# Where Claude Code stores per-conversation transcripts:
#   ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl
# The encoding is just ``cwd.replace("/", "-")`` so
# ``/home/me/code`` -> ``-home-me-code``.  This file is the
# strongest "session-identity" invariant we have: claude rewrites the
# in-memory ``sessionId`` reported in
# ``~/.claude/sessions/<pid>.json`` during a resume (transient first,
# canonical second), but only the canonical sessionId ever gets a
# transcript file.  The GUI shows tabs based on these JSONL files,
# so its envelope ``session_id`` is always the canonical UUID.  We
# use transcript existence to filter out transient sessionIds before
# they cause silent send-mismatches.
_PROJECTS_SUBDIR = "projects"

# How long after a user keystroke we hold off on injecting, to avoid
# racing into the prompt right as the user is submitting their own
# message.  The byte buffer tracker is the primary signal; this is a
# small safety net for the post-Enter window where claude hasn't yet
# flipped its status from "idle" to whatever it uses for "busy".
_QUIET_AFTER_KEYSTROKE_SEC = 0.5

# Outgoing files older than this are considered stale and rejected.
# Prevents re-delivery of messages stuck from a prior session/disconnect.
_OUTGOING_MAX_AGE_SEC = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Logging -- mirror the agent's append-only log style
# ---------------------------------------------------------------------------


_LOG_NAME = "clacogui_launcher.log"


def _log_path() -> str:
    return os.path.join(_claude_dir(), _LOG_NAME)


def _log(label: str, payload: object = None) -> None:
    """Append-only log next to the script.  Best-effort -- we never
    let logging crash the launcher, because then the user's terminal
    is left in raw mode."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} pid={os.getpid()} {label}"
        if payload is not None:
            try:
                line += " " + json.dumps(payload, default=str)
            except Exception:
                line += f" {payload!r}"
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# argv parsing -- we only intercept --resume to short-circuit session-id
# discovery; everything else passes through verbatim to claude.
# ---------------------------------------------------------------------------


def _argv_resume(argv: list[str]) -> Optional[str]:
    """Return the session_id given on the launcher command line, or None.

    Recognises the same forms ``claude`` does: ``--resume <id>``,
    ``--resume=<id>``, ``-r <id>``, ``-r=<id>``.  ``--resume`` /
    ``-r`` with no argument means "open the picker" and returns None
    (we'll fall back to /proc-style discovery in that case)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--resume", "-r"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                return argv[i + 1]
            return None
        if a.startswith("--resume="):
            return a.split("=", 1)[1] or None
        if a.startswith("-r="):
            return a.split("=", 1)[1] or None
        i += 1
    return None


def _resolve_claude_binary() -> str:
    """Find the real ``claude`` binary on PATH.

    The launcher is *not* on PATH itself (it lives in ``~/.claude/``),
    so a plain ``shutil.which("claude")`` is enough.  If the user
    later puts a ``claude`` shim in front of us, we can revisit; for
    now we keep it simple and fail loudly if claude isn't found.
    """
    for name in ("claude",):
        p = shutil.which(name)
        if p:
            return p
    sys.stderr.write(
        "clacogui_launcher: could not find 'claude' on PATH.\n"
        "Make sure Claude Code is installed in this shell/container.\n"
    )
    sys.exit(127)


# ---------------------------------------------------------------------------
# User-buffer tracker -- approximates how much the user has typed into
# claude's input prompt since the last submit/cancel.  Approximate is
# fine; this only gates whether we ask for the user's permission to
# overwrite their unfinished input.
# ---------------------------------------------------------------------------


class UserBufferTracker:
    """Estimate "what's in claude's input prompt right now" by
    watching the bytes the user types through us.

    Not a perfect terminal-state machine (we don't parse arrow keys,
    multi-line cursor movement, paste echoes), but robust enough to
    answer the only question we actually use it for: "is the user
    actively composing input?"
    """

    def __init__(self) -> None:
        self._buffer = 0
        self._last_keystroke_ts = 0.0

    def observe(self, b: bytes) -> None:
        self._last_keystroke_ts = time.monotonic()
        for c in b:
            if c in (0x0d, 0x0a):       # CR / LF -- submitted
                self._buffer = 0
            elif c == 0x03:              # Ctrl+C -- cancel
                self._buffer = 0
            elif c == 0x15:              # Ctrl+U -- kill line
                self._buffer = 0
            elif c == 0x17:              # Ctrl+W -- kill word (rough)
                self._buffer = max(0, self._buffer - 5)
            elif c in (0x7f, 0x08):      # Backspace
                self._buffer = max(0, self._buffer - 1)
            elif c >= 0x20:              # printable byte
                self._buffer += 1
            # ESC sequences, arrow keys, etc. are no-ops for this counter.

    def estimate(self) -> int:
        return self._buffer

    @property
    def last_keystroke_ts(self) -> float:
        return self._last_keystroke_ts

    def force_reset(self) -> None:
        """Clear the buffer estimate (e.g. after we ourselves typed
        an erasure sequence)."""
        self._buffer = 0


# ---------------------------------------------------------------------------
# Session-metadata helpers
# ---------------------------------------------------------------------------


def _session_metadata_path(claude_pid: int) -> str:
    return os.path.join(
        _claude_dir(), _SESSIONS_SUBDIR, f"{claude_pid}.json"
    )


def _read_session_metadata(claude_pid: int) -> Optional[dict]:
    """Read the JSON Claude Code maintains about its own running session.

    Returns ``None`` if the file isn't there yet (claude usually writes
    it within milliseconds of startup, but we may be called before that)
    or if it can't be parsed.  We never treat parse failures as fatal;
    polling will pick up the next valid version.
    """
    path = _session_metadata_path(claude_pid)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        _log("session metadata read failed", {"path": path, "err": str(e)})
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _transcript_path(meta: dict) -> Optional[str]:
    """Return ``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` or None.

    Both ``cwd`` and ``sessionId`` must be present and string-valued
    in ``meta`` for us to derive a path.  We only build the string;
    we don't stat it here -- callers do that explicitly so the
    no-meta and no-file cases stay distinguishable.
    """
    sid = meta.get("sessionId") if isinstance(meta, dict) else None
    cwd = meta.get("cwd") if isinstance(meta, dict) else None
    if not (isinstance(sid, str) and sid):
        return None
    if not (isinstance(cwd, str) and cwd):
        return None
    encoded_cwd = cwd.replace("/", "-")
    return os.path.join(
        _claude_dir(), _PROJECTS_SUBDIR, encoded_cwd, f"{sid}.jsonl",
    )


def _canonical_session_id(meta: Optional[dict]) -> Optional[str]:
    """Return ``sessionId`` from ``meta`` only if its transcript exists.

    This is the launcher's primary anti-transient guard.  When the
    user resumes an aliased session, claude writes a transient
    ``sessionId`` into the metadata file before swapping it for the
    canonical one a moment later.  Only the canonical sessionId ever
    gets a JSONL transcript on disk, so checking transcript
    existence tells us "this sessionId is the one the GUI sees" with
    no further coordination required.

    Returns ``None`` if metadata is missing, malformed, or refers to
    a sessionId without a transcript.
    """
    if not isinstance(meta, dict):
        return None
    path = _transcript_path(meta)
    if not path:
        return None
    try:
        if os.path.isfile(path):
            sid = meta.get("sessionId")
            if isinstance(sid, str):
                return sid
    except OSError:
        pass
    return None


def _wait_for_session_id(
    claude_pid: int,
    *,
    deadline_sec: float,
) -> Optional[str]:
    """Poll the session metadata file until a *canonical* sessionId appears.

    "Canonical" here means: the metadata's ``sessionId`` has a
    matching ``~/.claude/projects/<cwd>/<sessionId>.jsonl`` transcript
    on disk.  This filters out the transient sessionId claude writes
    early during a ``--resume`` of an aliased session.

    Returns the session_id as soon as we see one with a transcript,
    or ``None`` if the deadline expires (we then run in
    "session unknown" mode -- nothing will be delivered, but the
    launcher continues to byte-pump so the user can still use claude
    normally).
    """
    end = time.monotonic() + deadline_sec
    while time.monotonic() < end:
        meta = _read_session_metadata(claude_pid)
        sid = _canonical_session_id(meta)
        if sid:
            return sid
        time.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# PTY / raw-mode plumbing
# ---------------------------------------------------------------------------


def _set_winsize_from_stdin(slave_fd: int) -> None:
    """Copy the current terminal's window size onto the slave PTY."""
    try:
        # TIOCGWINSZ on stdin -> TIOCSWINSZ on slave.
        size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, size)
    except OSError as e:
        _log("winsize forward failed", {"err": str(e)})


def _spawn_claude(claude_argv: list[str]) -> tuple[int, int]:
    """Fork claude under a PTY.

    Returns ``(master_fd, child_pid)``.  The slave side lives inside
    the child process and is the controlling TTY for claude.

    We use ``pty.fork()`` rather than ``forkpty + execvp`` directly
    because it handles the "make slave the controlling tty + dup it
    onto stdin/stdout/stderr" dance for us.
    """
    pid, master_fd = pty.fork()
    if pid == 0:
        # In child.  Match parent's window size so claude's first
        # render uses the right dimensions.
        # (We don't have direct access to the slave fd here -- but
        # stdin in the child IS the slave PTY at this point.)
        try:
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(0, termios.TIOCSWINSZ, size)
        except OSError:
            pass
        try:
            os.execvp(claude_argv[0], claude_argv)
        except OSError as e:
            sys.stderr.write(
                f"clacogui_launcher: exec {claude_argv[0]!r} failed: {e}\n"
            )
            os._exit(127)
    return master_fd, pid


# ---------------------------------------------------------------------------
# Side-channel: clacogui_outgoing/
# ---------------------------------------------------------------------------


def _outgoing_dir() -> str:
    return os.path.join(_claude_dir(), OUTGOING_DIR)


def _list_outgoing() -> list[str]:
    """Return the absolute paths of pending message files.

    Pending = ``<id>.json`` (no ``.failed``/``.needs_decision`` suffix,
    no leading dot).  Sorted by name so chronological filenames
    (``<unix-millis>_...``) deliver in arrival order.
    """
    d = _outgoing_dir()
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return []
    except OSError:
        return []
    out: list[str] = []
    for name in names:
        if name.startswith("."):
            continue
        if not name.endswith(".json"):
            continue
        # Suffixed states (".json.failed", ".json.needs_decision") aren't
        # caught by endswith(".json") because the actual basename ends
        # with .failed / .needs_decision.  But be defensive in case
        # anything ever writes ".json" at a non-tail position.
        if ".json." in name:
            continue
        out.append(os.path.join(d, name))
    return out


def _read_envelope(path: str) -> Optional[dict]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _safe_size(path: str) -> int:
    """``os.path.getsize`` that returns -1 instead of raising."""
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


# Per-path state for the partial-write detector below.  Maps an
# absolute path to its (size, mtime) at the previous tick.  Cleared
# implicitly when files vanish (we just stop checking for them).
_FILE_STABILITY_STATE: dict[str, tuple[int, float]] = {}


def _file_is_stable(path: str) -> bool:
    """True if ``path``'s size + mtime have not changed since last tick.

    Used as a defence against a non-atomic FTP ``STOR``: pyftpdlib
    creates the file at byte 0 and then streams data over the data
    connection, so a launcher tick that fires mid-write sees an
    incomplete (often empty) buffer and ``json.loads`` fails.  We
    compare the current ``stat`` to the previous tick's value; if
    they differ, the file is still being written and we should not
    poison it with ``.failed`` yet.

    First-time-seen files are always treated as unstable so we never
    rename a freshly-spotted file on the same tick we discovered it.
    """
    try:
        st = os.stat(path)
    except OSError:
        # Can't stat -- treat as unstable so we don't .failed it.
        return False
    cur = (int(st.st_size), float(st.st_mtime))
    prev = _FILE_STABILITY_STATE.get(path)
    _FILE_STABILITY_STATE[path] = cur
    if prev is None:
        return False
    return prev == cur


def _rename_with_suffix(
    path: str,
    suffix: str,
    extra_fields: Optional[dict] = None,
) -> Optional[str]:
    """Rewrite ``path`` with ``extra_fields`` merged in, then rename it
    to ``path + suffix``.  Returns the new path, or None on failure
    (in which case we just log and drop the file -- next tick we'll
    see it again and try once more)."""
    envelope = _read_envelope(path) or {}
    if extra_fields:
        envelope.update(extra_fields)
    try:
        with open(path, "wb") as f:
            f.write(json.dumps(envelope, indent=2).encode("utf-8"))
        new_path = path + suffix
        os.replace(path, new_path)
        return new_path
    except OSError as e:
        _log("rename_with_suffix failed", {
            "path": path, "suffix": suffix, "err": str(e),
        })
        return None


# ---------------------------------------------------------------------------
# Tail-file writer
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Main launcher object
# ---------------------------------------------------------------------------


class Launcher:
    def __init__(self, claude_argv: list[str]) -> None:
        self._claude_argv = claude_argv
        self._master_fd: int = -1
        self._claude_pid: int = -1
        self._session_id: Optional[str] = None
        self._user = UserBufferTracker()

        # Cached value of claude's "status" field, refreshed on each
        # tick.  Anything that isn't "idle" means we don't inject.
        self._claude_status: str = "unknown"
        self._claude_status_checked_at: float = 0.0
        # The UserBufferTracker counts printable bytes typed through us,
        # but during startup/resume it also sees keystrokes and echoes
        # that never land in claude's (not-yet-ready) prompt -- leaving a
        # bogus non-zero estimate that trips ``user_has_uncommitted_text``
        # on the very first injected message.  We clear the estimate once,
        # the first time claude reports ``idle`` (TUI up, prompt empty),
        # so the tracker starts from a known-clean baseline.
        self._buffer_baselined: bool = False

        # Per-file dedup so we log "ignored" envelopes exactly once
        # even though the side-channel scan visits them every 200 ms.
        # Keyed by absolute path so re-creating a file with the same
        # id doesn't permanently mute it.  We prune entries for
        # paths that vanished from disk on each tick.
        self._logged_session_skips: set[str] = set()
        self._logged_gate_skips: dict[str, str] = {}

        # Original parent terminal attrs, captured before we go raw.
        self._original_termios: Optional[list] = None

        # Have we already restored the parent terminal?  Used by
        # signal handlers and atexit so we don't restore twice.
        self._restored: bool = False


    # -- terminal state ------------------------------------------------------

    def _enter_raw_mode(self) -> None:
        try:
            self._original_termios = termios.tcgetattr(0)
            tty.setraw(0)
        except (termios.error, OSError) as e:
            # Non-TTY stdin (piped) -- nothing to switch.  Probably
            # not a real interactive run, but don't bail.
            _log("setraw failed (no TTY?)", {"err": str(e)})
            self._original_termios = None

    def _restore_terminal(self) -> None:
        if self._restored:
            return
        self._restored = True
        if self._original_termios is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, self._original_termios)
            except (termios.error, OSError) as e:
                _log("restore termios failed", {"err": str(e)})

    # -- signal forwarding ---------------------------------------------------

    def _handle_winch(self, *_a) -> None:
        if self._master_fd >= 0:
            try:
                size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, size)
            except OSError as e:
                _log("winch forward failed", {"err": str(e)})

    def _handle_term(self, signum, _frame) -> None:
        # Pass the signal on to claude, then restore our terminal.
        # We do NOT exit here -- the select loop will see EOF on the
        # master fd and unwind cleanly, with the right exit code.
        if self._claude_pid > 0:
            try:
                os.kill(self._claude_pid, signum)
            except OSError:
                pass

    # -- session/status discovery -------------------------------------------

    def _refresh_claude_status(self) -> None:
        # Cheap; the file is tiny and we're already polling at 200 ms.
        meta = _read_session_metadata(self._claude_pid)
        self._claude_status_checked_at = time.monotonic()
        if not meta:
            return
        st = meta.get("status")
        if isinstance(st, str):
            self._claude_status = st
            # First time claude is idle: the TUI prompt is up and empty,
            # so any buffer estimate accrued during startup/resume is
            # noise.  Reset once to a clean baseline.  Subsequent
            # mid-typing detection (keystrokes after this point) still
            # works normally.
            if st == "idle" and not self._buffer_baselined:
                self._buffer_baselined = True
                if self._user.estimate() > 0:
                    _log(
                        "baselining user buffer at first idle",
                        {"prev_estimate": self._user.estimate()},
                    )
                self._user.force_reset()
        # Re-sync session_id from the metadata file every tick, not
        # just once at startup.  When the user resumes an aliased
        # session (``claude -r aaa1``), claude first writes a
        # transient ``sessionId`` and a moment later swaps it for the
        # canonical resumed UUID.  We use transcript existence
        # (``_canonical_session_id``) to filter out the transient:
        # only the canonical UUID ever gets a JSONL transcript, and
        # that's the same UUID the GUI tags envelopes with (the GUI
        # opens conversations by JSONL filename), so this puts the
        # two sides in lock-step.
        sid = _canonical_session_id(meta)
        if sid and sid != self._session_id:
            if self._session_id is None:
                _log("session_id discovered (late)", {"sid": sid})
            else:
                _log(
                    "session_id changed",
                    {"old": self._session_id, "new": sid},
                )
                # Anything we'd previously skipped because of a sid
                # mismatch should be reconsidered against the new
                # session_id, so drop the dedup so the next pass
                # logs+evaluates afresh.
                self._logged_session_skips.clear()
            self._session_id = sid

    # -- injection gate ------------------------------------------------------

    def _can_inject_now(self) -> tuple[bool, str]:
        if self._session_id is None:
            return False, "session_unknown"
        # Refresh status if it's stale (we re-check on every tick anyway,
        # so this is mostly belt-and-suspenders).
        if self._claude_status != "idle":
            return False, f"claude_{self._claude_status}"
        if self._user.estimate() > 0:
            return False, "user_has_uncommitted_text"
        if (
            time.monotonic() - self._user.last_keystroke_ts
            < _QUIET_AFTER_KEYSTROKE_SEC
        ):
            return False, "user_just_submitted"
        return True, "ok"

    # -- delivery -----------------------------------------------------------

    def _build_inject_segments(
        self, text: str, *, with_erasure: bool, prior_buffer: int
    ) -> list[tuple[bytes, float]]:
        """Plan the byte writes that deliver one queued message.

        Returns a list of ``(chunk, pause_after_seconds)`` tuples, in
        order.  The caller does ``os.write(fd, chunk)`` followed by
        ``time.sleep(pause_after_seconds)`` for each tuple (skipping
        the sleep when it's zero).

        Why segments instead of one big blob: claude's Ink/readline
        TUI does rapid-input coalescing.  When body + CR arrive in a
        single PTY read, the CR gets absorbed into the line buffer as
        content rather than being parsed as a 'return' key event, so
        the message visibly types but doesn't submit.  Splitting the
        write across two syscalls with a short sleep between them
        forces the CR to land in its own input batch, where it
        triggers submit normally.  Bracketed paste would solve the
        same problem the "right" way but did not take in practice in
        claude's prompt, so we use timing instead.

        Order, when ``with_erasure``:
          1. Ctrl+E (move cursor to end), Ctrl+U (kill line),
             N backspaces (capped at ``_MAX_ERASE_BACKSPACES``)
             -- single write, followed by a small settle pause
          2. The user's text, with CR/CRLF normalised to LF
             (``\\n``, Ctrl+J) -- claude's "insert newline"
             keystroke; followed by ``_PRE_SUBMIT_PAUSE_SEC``
          3. CR (``\\r``, Ctrl+M / Enter) -- submits the prompt

        ``prior_buffer`` is the user-buffer estimate immediately
        before we erase, used to size the backspace flood; we pad an
        extra ``+ 32`` for safety.
        """
        segments: list[tuple[bytes, float]] = []
        if with_erasure:
            erase = bytearray()
            erase += _CTRL_E
            erase += _CTRL_U
            n = min(prior_buffer * 2 + 32, _MAX_ERASE_BACKSPACES)
            erase += _BACKSPACE * n
            segments.append((bytes(erase), _POST_ERASE_PAUSE_SEC))
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")
        body = normalised.encode("utf-8", errors="replace")
        segments.append((body, _PRE_SUBMIT_PAUSE_SEC))
        segments.append((_CR, 0.0))
        return segments

    def _process_outgoing(self) -> None:
        paths = _list_outgoing()
        # Drop dedup entries for files that no longer exist so a new
        # envelope reusing the same path (very unlikely with our
        # uuid-tagged ids, but possible) starts fresh.
        live = set(paths)
        self._logged_session_skips &= live
        self._logged_gate_skips = {
            p: r for p, r in self._logged_gate_skips.items() if p in live
        }
        for path in paths:
            envelope = _read_envelope(path)
            if envelope is None:
                # Could legitimately be a partially-written file from a
                # GUI doing a non-atomic FTP STOR (the file is created
                # at 0 bytes and grows over the data connection; we
                # might be reading it mid-flight on a 200 ms tick).
                # The GUI is supposed to write to ``<id>.json.part``
                # and rename, but older GUIs don't.  Defer the failure
                # decision: try a couple of ticks before declaring it
                # corrupt, with a small sleep in between, so a
                # transient empty read doesn't doom an otherwise valid
                # envelope.
                if not _file_is_stable(path):
                    _log(
                        "envelope unparseable but file is still being "
                        "written; will retry next tick",
                        {"path": path},
                    )
                    continue
                size = _safe_size(path)
                _log(
                    "rename to .failed: could not parse JSON envelope",
                    {"path": path, "size": size},
                )
                _rename_with_suffix(
                    path,
                    ".failed",
                    {
                        "error": (
                            f"could not parse JSON envelope "
                            f"(file size {size} bytes)"
                        ),
                        "failed_at": int(time.time() * 1000),
                    },
                )
                continue

            sid = envelope.get("session_id")
            env_id = envelope.get("id") or os.path.basename(path)

            # Reject stale outgoing files.  The filename encodes the
            # creation timestamp as <unix_millis>_<uuid>.json.
            try:
                file_ts_ms = int(os.path.basename(path).split("_", 1)[0])
                age_sec = (time.time() * 1000 - file_ts_ms) / 1000
            except (ValueError, IndexError):
                age_sec = 0.0
            if age_sec > _OUTGOING_MAX_AGE_SEC:
                _log(
                    "rename to .failed: outgoing file too old",
                    {"id": env_id, "age_sec": round(age_sec, 1)},
                )
                _rename_with_suffix(
                    path,
                    ".failed",
                    {
                        "error": f"stale outgoing file (age {age_sec:.0f}s > {_OUTGOING_MAX_AGE_SEC}s)",
                        "failed_at": int(time.time() * 1000),
                    },
                )
                continue

            if not sid:
                # Nothing to match; treat as broadcast intended for a
                # single-launcher setup and accept it.  This is the
                # fallback for older GUIs that didn't tag.
                pass
            elif sid != self._session_id:
                # Belongs to a different launcher (or our own
                # session_id discovery is wrong) -- leave the file
                # alone so a matching launcher can pick it up.  Log
                # once per path so this failure mode is visible in
                # ``clacogui_launcher.log`` without spamming every
                # 200 ms tick.
                if path not in self._logged_session_skips:
                    _log(
                        "skip: session_id mismatch",
                        {
                            "path": path,
                            "id": env_id,
                            "envelope_sid": sid,
                            "our_sid": self._session_id,
                        },
                    )
                    self._logged_session_skips.add(path)
                continue

            action = envelope.get("action")
            if action == "interrupt":
                # Single Esc keystroke -- bypass all gates.  Esc
                # is the *point* of interrupt: it's only useful when
                # claude is busy, and unlike Ctrl+C it's idempotent
                # so spamming it is safe.  We do NOT touch the
                # user-buffer tracker here: Esc cancels the current
                # turn / streaming, it doesn't affect what's in the
                # input prompt.
                try:
                    os.write(self._master_fd, _ESC)
                except OSError as e:
                    _log(
                        "rename to .failed: interrupt write failed",
                        {"id": env_id, "err": str(e)},
                    )
                    _rename_with_suffix(
                        path,
                        ".failed",
                        {
                            "error": f"interrupt write failed: {e}",
                            "failed_at": int(time.time() * 1000),
                        },
                    )
                    continue
                _log(
                    "delivered interrupt",
                    {"id": env_id, "claude_status": self._claude_status},
                )
                self._logged_gate_skips.pop(path, None)
                self._logged_session_skips.discard(path)
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    _log(
                        "delete after interrupt failed",
                        {"path": path, "err": str(e)},
                    )
                continue
            elif action not in (None, "", "send"):
                _log(
                    "rename to .failed: unknown action",
                    {"id": env_id, "action": action},
                )
                _rename_with_suffix(
                    path,
                    ".failed",
                    {
                        "error": f"unknown action: {action!r}",
                        "failed_at": int(time.time() * 1000),
                    },
                )
                continue

            text = envelope.get("text")
            if not isinstance(text, str):
                _log(
                    "rename to .failed: envelope has no 'text' string",
                    {"id": env_id, "envelope_keys": sorted(envelope.keys())},
                )
                _rename_with_suffix(
                    path,
                    ".failed",
                    {
                        "error": "envelope has no 'text' string",
                        "failed_at": int(time.time() * 1000),
                    },
                )
                continue

            ok, reason = self._can_inject_now()
            force_clear = bool(envelope.get("force_clear"))

            if not ok and reason == "user_has_uncommitted_text":
                if not force_clear:
                    # Need user to make a decision.  Hand it off via the
                    # .needs_decision suffix; the GUI will show a dialog
                    # and rewrite us back as <id>.json with force_clear
                    # set if the user chose erase-and-send.
                    _rename_with_suffix(
                        path,
                        ".needs_decision",
                        {
                            "reason": "user_has_uncommitted_text",
                            "noticed_at": int(time.time() * 1000),
                        },
                    )
                    continue
                # force_clear=True: the user has authorised us to
                # wipe whatever they typed in claude's TUI before
                # our message lands.  ``_build_inject_segments``
                # will prepend Ctrl+E + Ctrl+U + a backspace flood
                # in the with_erasure path, so the buffer is cleared
                # by the same write that delivers our text.  Bypass
                # this gate.  Other gates (claude_busy,
                # user_just_submitted, session_unknown) still apply
                # below -- they're orthogonal to "what's in the
                # input box".
                ok = True
                reason = "ok (force_clear bypass)"

            if not ok:
                # claude is busy / we just submitted / session
                # unknown: leave the file alone, retry next tick.
                # Log once per (path, reason) so a stuck "queued"
                # state in the GUI is debuggable from the log
                # without spamming on every poll.
                prev_reason = self._logged_gate_skips.get(path)
                if prev_reason != reason:
                    _log(
                        "skip: gate not ready",
                        {"path": path, "id": env_id, "reason": reason},
                    )
                    self._logged_gate_skips[path] = reason
                continue

            # Past the gate: clear any stale dedup entries so the
            # next time we have to skip we'll log it again.
            self._logged_gate_skips.pop(path, None)
            self._logged_session_skips.discard(path)

            prior_buffer = self._user.estimate()
            segments = self._build_inject_segments(
                text,
                with_erasure=force_clear,
                prior_buffer=prior_buffer,
            )

            total_bytes = 0
            write_failed: OSError | None = None
            for chunk, pause in segments:
                try:
                    # We deliberately split body and submit across
                    # multiple writes (with a small sleep between)
                    # because claude's TUI coalesces a single-burst
                    # read as a paste and absorbs the trailing CR
                    # into the buffer rather than firing 'return'.
                    # See ``_build_inject_segments`` for the why.
                    os.write(self._master_fd, chunk)
                except OSError as e:
                    write_failed = e
                    break
                total_bytes += len(chunk)
                if pause > 0:
                    time.sleep(pause)

            if write_failed is not None:
                _log(
                    "rename to .failed: write to claude failed",
                    {"id": env_id, "err": str(write_failed)},
                )
                _rename_with_suffix(
                    path,
                    ".failed",
                    {
                        "error": f"write to claude failed: {write_failed}",
                        "failed_at": int(time.time() * 1000),
                    },
                )
                continue

            _log(
                "delivered",
                {
                    "id": env_id,
                    "bytes": total_bytes,
                    "segments": len(segments),
                    "force_clear": force_clear,
                },
            )

            # Successful delivery -- let the tracker know we just typed
            # something so a momentarily-fresh keystroke window doesn't
            # gate the *next* send if multiple are queued.
            if force_clear:
                self._user.force_reset()

            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                _log("delete after deliver failed", {"path": path, "err": str(e)})

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        # 1. Spawn claude under PTY first; we want it visible to the
        #    user as fast as possible.
        self._master_fd, self._claude_pid = _spawn_claude(self._claude_argv)
        _log("spawned claude", {
            "pid": self._claude_pid, "argv": self._claude_argv,
        })

        # 2. Take the parent terminal raw and arrange for cleanup.
        self._enter_raw_mode()
        signal.signal(signal.SIGWINCH, self._handle_winch)
        signal.signal(signal.SIGHUP, self._handle_term)
        signal.signal(signal.SIGTERM, self._handle_term)
        # Don't trap SIGINT -- in raw mode it's already passed through
        # to claude as ^C, which is what we want.  The OS will raise
        # KeyboardInterrupt only if claude is dead and stdin is closed.

        try:
            # 3. Discover session_id.  The session metadata file
            #    (``~/.claude/sessions/<claude_pid>.json``) is the
            #    ground truth -- it's claude itself reporting its own
            #    sessionId after parsing ``--resume``.  We do NOT
            #    treat the argv value as authoritative because
            #    ``-r``/``--resume`` accepts a session *alias*
            #    (``/rename`` target like ``aaa1``) as well as a real
            #    UUID; using the alias would cause every envelope
            #    from the GUI (which is always tagged with the real
            #    UUID from the JSONL filename) to be silently
            #    dropped on session-id mismatch.
            sid = _wait_for_session_id(
                self._claude_pid, deadline_sec=15.0
            )
            if sid:
                self._session_id = sid
                _log("session_id discovered", {"sid": sid})
            else:
                # Metadata never appeared (very old claude / weird
                # PID-namespace setup).  Fall back to argv ONLY if it
                # parses as a UUID; aliases are useless for matching.
                forced = _argv_resume(self._claude_argv[1:])
                if forced and _looks_like_uuid(forced):
                    self._session_id = forced
                    _log(
                        "session_id from argv (metadata timed out)",
                        {"sid": forced},
                    )
                else:
                    _log(
                        "session_id unknown after timeout",
                        {"argv_resume": forced},
                    )

            return self._select_loop()
        finally:
            self._restore_terminal()

    def _select_loop(self) -> int:
        next_tick = time.monotonic() + _TICK_SEC
        while True:
            now = time.monotonic()
            timeout = max(0.0, next_tick - now)
            try:
                rlist, _, _ = select.select(
                    [0, self._master_fd], [], [], timeout,
                )
            except (InterruptedError, OSError) as e:
                # EINTR happens around SIGWINCH / SIGTERM; just retry.
                if isinstance(e, OSError) and e.errno != errno.EINTR:
                    _log("select error", {"err": str(e)})
                    if e.errno == errno.EBADF:
                        break
                continue

            # User keystrokes -> claude.
            if 0 in rlist:
                try:
                    data = os.read(0, 4096)
                except OSError as e:
                    _log("stdin read failed", {"err": str(e)})
                    data = b""
                if not data:
                    # Parent stdin closed -- we're being detached.
                    # Stop reading from stdin but keep pumping claude
                    # output until claude itself exits.
                    try:
                        os.close(0)
                    except OSError:
                        pass
                else:
                    self._user.observe(data)
                    try:
                        os.write(self._master_fd, data)
                    except OSError as e:
                        _log("stdin -> claude write failed", {"err": str(e)})

            # Claude output -> stdout.
            if self._master_fd in rlist:
                try:
                    data = os.read(self._master_fd, 4096)
                except OSError as e:
                    # EIO on a PTY master typically means the slave
                    # closed -- claude has exited.
                    _log("master read EOF/err", {"err": str(e)})
                    data = b""
                if not data:
                    break
                try:
                    os.write(1, data)
                except OSError as e:
                    _log("claude -> stdout write failed", {"err": str(e)})

            # Periodic side-channel + status refresh.
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + _TICK_SEC
                # ``_refresh_claude_status`` also re-syncs
                # ``session_id`` from the metadata file each tick
                # (claude rewrites the file after a resume settles,
                # so the sessionId we saw at startup may be stale).
                self._refresh_claude_status()
                try:
                    self._process_outgoing()
                except Exception as e:
                    _log("process_outgoing crashed", {"err": str(e)})

        # Drain claude's exit code.
        return _wait_child(self._claude_pid)


def _wait_child(pid: int) -> int:
    if pid <= 0:
        return 0
    try:
        _, status = os.waitpid(pid, 0)
    except OSError:
        return 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    # Build claude argv: [<real claude binary>, <args from caller>].
    real_claude = _resolve_claude_binary()
    claude_argv = [real_claude, *argv[1:]]

    # Make sure the outgoing dir exists; if it doesn't, the install
    # step was skipped or the user is on an older agent build.  We
    # create it lazily to avoid breaking the launcher in that case.
    try:
        os.makedirs(_outgoing_dir(), exist_ok=True)
    except OSError as e:
        _log("could not ensure outgoing dir", {"err": str(e)})

    # Generate a per-process WID so we can correlate log lines with
    # rendezvous artefacts later if we ever need them.  Not used for
    # routing -- routing is by session_id from the metadata file.
    wid = uuid.uuid4().hex[:8]
    _log("starting", {"wid": wid, "claude_argv": claude_argv})

    launcher = Launcher(claude_argv)
    return launcher.run()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        # We don't normally get here -- raw mode means ^C is passed
        # to claude as a byte -- but if we do, exit cleanly.
        sys.exit(130)
