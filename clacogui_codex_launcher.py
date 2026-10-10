#!/usr/bin/env python3
"""clacogui_codex_launcher - transparent PTY parent for the Codex CLI.

Run *instead of* ``codex``::

    python3 ~/.codex/clacogui_codex_launcher.py [args passed to codex...]
    python3 ~/.codex/clacogui_codex_launcher.py resume <thread-id-or-name>

Your terminal renders the Codex TUI exactly as before; keystrokes pass
straight through.  On a 200 ms side channel the launcher also:

* publishes this TUI's thread and busy/idle state to
  ``$CODEX_HOME/clacogui_sessions/<pid>.json`` for the clacogui status bar;
* delivers GUI messages from ``$CODEX_HOME/clacogui_outgoing/<id>.json``
  with ``codex queue --thread <id> --message <text>`` -- Codex queues it
  behind a running turn by itself, so nothing is typed into the TUI;
* turns an ``action: interrupt`` envelope into one Esc on the PTY, only
  while a turn is running (a second Esc at an idle Codex prompt opens
  "edit previous message").

Envelope format and outcome signalling match ``clacogui_launcher.py``:
the file is deleted once delivered, or renamed to ``<id>.json.failed``
with an ``error`` field.

Stdlib only, so it can be dropped into any container.
"""

from __future__ import annotations

import errno
import fcntl
import glob
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
from typing import Optional

OUTGOING_DIR = "clacogui_outgoing"
STATE_DIR = "clacogui_sessions"
SESSIONS_DIR = "sessions"
SESSION_INDEX = "session_index.jsonl"
# Codex touches ``<thread-id>`` in here whenever a TUI opens a thread
# (new, resumed or forked), which is how we learn which thread is ours.
THREAD_REFS_DIR = "tui-thread-reference-capabilities"

_TICK_SEC = 0.20
_DISCOVERY_EVERY_SEC = 1.0
_HEARTBEAT_SEC = 30.0
_OUTGOING_MAX_AGE_SEC = 300
_QUEUE_TIMEOUT_SEC = 60.0
_ESC = b"\x1b"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _log(label: str, payload: object = None) -> None:
    """Append-only log; never raises (the terminal is in raw mode)."""
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {label}"
        if payload is not None:
            try:
                line += " " + json.dumps(payload, default=str)
            except Exception:
                line += f" {payload!r}"
        with open(
            os.path.join(_codex_home(), "clacogui_codex_launcher.log"), "a", encoding="utf-8"
        ) as f:
            f.write(line + "\n")
    except Exception:
        pass


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Thread discovery
# ---------------------------------------------------------------------------


def _argv_thread_hint(argv: list[str]) -> Optional[str]:
    """``codex resume <id-or-name>`` -> ``<id-or-name>``; otherwise None."""
    if len(argv) >= 2 and argv[0] == "resume" and not argv[1].startswith("-"):
        return argv[1]
    return None


def _thread_id_for_name(name: str) -> Optional[str]:
    found = None
    try:
        with open(os.path.join(_codex_home(), SESSION_INDEX), encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("thread_name") == name:
                    found = entry.get("id") or found
    except OSError:
        return None
    return found if isinstance(found, str) else None


def _find_rollout(thread_id: str) -> Optional[str]:
    """Absolute path of ``thread_id``'s rollout, newest day directory first."""
    pattern = os.path.join(_codex_home(), SESSIONS_DIR, "*", "*", "*", f"rollout-*-{thread_id}.jsonl")
    hits = sorted(glob.glob(pattern), reverse=True)
    return hits[0] if hits else None


def _rollout_paths() -> set[str]:
    return set(glob.glob(os.path.join(_codex_home(), SESSIONS_DIR, "*", "*", "*", "rollout-*.jsonl")))


def _claimed_by_others() -> set[str]:
    """Thread ids published by *other* live launchers."""
    out: set[str] = set()
    d = os.path.join(_codex_home(), STATE_DIR)
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name == f"{os.getpid()}.json":
            continue
        try:
            pid = int(name[:-5])
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            continue
        except PermissionError:
            pass
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                sid = json.load(f).get("sessionId")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(sid, str):
            out.add(sid)
    return out


# ---------------------------------------------------------------------------
# Rollout tail -> busy / idle
# ---------------------------------------------------------------------------


class TurnTracker:
    """Follows a rollout and reports whether a turn is running."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.offset = 0
        self.open_turns: set[str] = set()

    @property
    def busy(self) -> bool:
        return bool(self.open_turns)

    def poll(self) -> None:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for raw in chunk[: end + 1].splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if entry.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if ptype == "task_started":
                self.open_turns.add(payload.get("turn_id") or "")
            elif ptype in ("task_complete", "turn_aborted"):
                self.open_turns.discard(payload.get("turn_id") or "")


# ---------------------------------------------------------------------------
# Outgoing envelopes
# ---------------------------------------------------------------------------


def _outgoing_dir() -> str:
    return os.path.join(_codex_home(), OUTGOING_DIR)


def _list_outgoing() -> list[str]:
    try:
        names = sorted(os.listdir(_outgoing_dir()))
    except OSError:
        return []
    return [
        os.path.join(_outgoing_dir(), n)
        for n in names
        if not n.startswith(".") and n.endswith(".json") and ".json." not in n
    ]


def _read_envelope(path: str) -> Optional[dict]:
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fail(path: str, error: str) -> None:
    envelope = _read_envelope(path) or {}
    envelope.update({"error": error, "failed_at": int(time.time() * 1000)})
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(envelope, f, indent=2)
        os.replace(path, path + ".failed")
    except OSError as e:
        _log("could not mark envelope failed", {"path": path, "err": str(e)})


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        _log("delete envelope failed", {"path": path, "err": str(e)})


def _envelope_age_sec(path: str) -> float:
    try:
        return time.time() - int(os.path.basename(path).split("_", 1)[0]) / 1000
    except (ValueError, IndexError):
        return 0.0


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


class CodexLauncher:
    def __init__(self, codex_argv: list[str]) -> None:
        self._argv = codex_argv
        self._master_fd = -1
        self._pid = -1
        self._started_wall = time.time()
        self._baseline_rollouts: set[str] = set()
        self._session_id: Optional[str] = None
        self._rollout: Optional[str] = None
        self._tracker: Optional[TurnTracker] = None
        self._hint_id: Optional[str] = None
        self._published: Optional[dict] = None
        self._published_at = 0.0
        self._next_discovery = 0.0
        self._sizes: dict[str, int] = {}
        self._inflight: dict[str, tuple[subprocess.Popen, float]] = {}
        self._skips: set[str] = set()
        self._original_termios = None
        self._restored = False

    # -- thread / status ----------------------------------------------------

    def _set_thread(self, sid: str, rollout: str, why: str) -> None:
        if sid == self._session_id and rollout == self._rollout:
            return
        _log("thread", {"sid": sid, "rollout": rollout, "why": why, "prev": self._session_id})
        self._session_id = sid
        self._rollout = rollout
        self._tracker = TurnTracker(rollout)
        self._tracker.poll()
        self._skips.clear()

    def _discover(self) -> None:
        now = time.monotonic()
        if now < self._next_discovery:
            return
        self._next_discovery = now + _DISCOVERY_EVERY_SEC

        if self._hint_id and self._session_id is None:
            rollout = _find_rollout(self._hint_id)
            if rollout:
                self._set_thread(self._hint_id, rollout, "argv")
                return

        others = _claimed_by_others()
        candidates: list[tuple[float, str]] = []
        refs = os.path.join(_codex_home(), THREAD_REFS_DIR)
        try:
            for name in os.listdir(refs):
                if not _UUID_RE.match(name) or name in others:
                    continue
                try:
                    mtime = os.stat(os.path.join(refs, name)).st_mtime
                except OSError:
                    continue
                if mtime >= self._started_wall - 2:
                    candidates.append((mtime, name.lower()))
        except OSError:
            pass
        if not candidates:
            for path in _rollout_paths() - self._baseline_rollouts:
                m = _ROLLOUT_RE.search(path)
                if m and m.group(1).lower() not in others:
                    try:
                        candidates.append((os.stat(path).st_mtime, m.group(1).lower()))
                    except OSError:
                        pass
        for _mtime, tid in sorted(candidates, reverse=True):
            if tid == self._session_id:
                return
            rollout = _find_rollout(tid)
            if rollout:
                self._set_thread(tid, rollout, "opened by this TUI")
                return

    def _status(self) -> str:
        return "busy" if self._tracker is not None and self._tracker.busy else "idle"

    def _publish(self) -> None:
        if not self._session_id:
            return
        state = {
            "pid": os.getpid(),
            "codexPid": self._pid,
            "sessionId": self._session_id,
            "status": self._status(),
            "rollout": os.path.relpath(self._rollout, _codex_home()).replace(os.sep, "/")
            if self._rollout
            else "",
            "cwd": os.getcwd(),
        }
        now = time.monotonic()
        if state == self._published and now - self._published_at < _HEARTBEAT_SEC:
            return
        state["updatedAt"] = int(time.time() * 1000)
        try:
            os.makedirs(os.path.join(_codex_home(), STATE_DIR), exist_ok=True)
            _atomic_write_json(self._state_path(), state)
        except OSError as e:
            _log("publish failed", {"err": str(e)})
            return
        if state.get("status") != (self._published or {}).get("status"):
            _log("status", {"sid": self._session_id, "status": state["status"]})
        state.pop("updatedAt")
        self._published = state
        self._published_at = now

    def _state_path(self) -> str:
        return os.path.join(_codex_home(), STATE_DIR, f"{os.getpid()}.json")

    # -- outgoing -----------------------------------------------------------

    def _reap_inflight(self) -> None:
        for path, (proc, started) in list(self._inflight.items()):
            rc = proc.poll()
            if rc is None:
                if time.monotonic() - started > _QUEUE_TIMEOUT_SEC:
                    proc.kill()
                    proc.wait()
                    del self._inflight[path]
                    _log("codex queue timed out", {"path": path})
                    _fail(path, f"codex queue timed out after {_QUEUE_TIMEOUT_SEC:.0f}s")
                continue
            del self._inflight[path]
            out, err = proc.communicate()
            if rc == 0:
                _log("delivered", {"path": path, "out": out.strip()[-200:]})
                _unlink(path)
            else:
                msg = (err or out).strip().splitlines()
                _log("codex queue failed", {"path": path, "rc": rc, "err": msg[-3:]})
                _fail(path, f"codex queue exited {rc}: {msg[-1] if msg else ''}")

    def _process_outgoing(self) -> None:
        self._reap_inflight()
        paths = _list_outgoing()
        live = set(paths)
        self._skips &= live
        self._sizes = {p: s for p, s in self._sizes.items() if p in live}
        for path in paths:
            if path in self._inflight:
                continue
            envelope = _read_envelope(path)
            if envelope is None:
                # Possibly a non-atomic FTP STOR still streaming; only give up
                # once the size has stopped changing between ticks.
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if self._sizes.get(path) != size:
                    self._sizes[path] = size
                    continue
                _fail(path, f"could not parse JSON envelope (file size {size} bytes)")
                continue
            if _envelope_age_sec(path) > _OUTGOING_MAX_AGE_SEC:
                _fail(path, f"stale outgoing file (older than {_OUTGOING_MAX_AGE_SEC}s)")
                continue
            sid = envelope.get("session_id")
            if not sid:
                _fail(path, "envelope has no session_id")
                continue
            if sid != self._session_id:
                if path not in self._skips:
                    _log("skip: session_id mismatch", {"path": path, "envelope_sid": sid, "our_sid": self._session_id})
                    self._skips.add(path)
                continue

            action = envelope.get("action")
            if action == "interrupt":
                if self._status() == "busy":
                    try:
                        os.write(self._master_fd, _ESC)
                    except OSError as e:
                        _fail(path, f"interrupt write failed: {e}")
                        continue
                    _log("delivered interrupt", {"path": path})
                else:
                    _log("interrupt ignored: no turn running", {"path": path})
                _unlink(path)
                continue
            if action not in (None, "", "send"):
                _fail(path, f"unknown action: {action!r}")
                continue
            text = envelope.get("text")
            if not isinstance(text, str) or not text.strip():
                _fail(path, "envelope has no 'text' string")
                continue
            # The Windows GUI sends CRLF line breaks.
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            codex = shutil.which("codex") or self._argv[0]
            try:
                proc = subprocess.Popen(
                    [codex, "queue", "--thread", sid, "--message", text],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as e:
                _fail(path, f"could not run codex queue: {e}")
                continue
            self._inflight[path] = (proc, time.monotonic())
            _log("queued via codex queue", {"path": path, "chars": len(text)})

    # -- terminal plumbing --------------------------------------------------

    def _enter_raw_mode(self) -> None:
        try:
            self._original_termios = termios.tcgetattr(0)
            tty.setraw(0)
        except (termios.error, OSError) as e:
            _log("setraw failed (no TTY?)", {"err": str(e)})

    def _restore_terminal(self) -> None:
        if self._restored:
            return
        self._restored = True
        if self._original_termios is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, self._original_termios)
            except (termios.error, OSError):
                pass

    def _handle_winch(self, *_a) -> None:
        if self._master_fd >= 0:
            try:
                size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, size)
            except OSError:
                pass

    def _handle_term(self, signum, _frame) -> None:
        if self._pid > 0:
            try:
                os.kill(self._pid, signum)
            except OSError:
                pass

    def _spawn(self) -> None:
        pid, fd = pty.fork()
        if pid == 0:
            try:
                size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(0, termios.TIOCSWINSZ, size)
            except OSError:
                pass
            try:
                os.execvp(self._argv[0], self._argv)
            except OSError as e:
                sys.stderr.write(f"clacogui_codex_launcher: exec {self._argv[0]!r} failed: {e}\n")
                os._exit(127)
        self._pid, self._master_fd = pid, fd

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        hint = _argv_thread_hint(self._argv[1:])
        if hint:
            self._hint_id = hint.lower() if _UUID_RE.match(hint) else _thread_id_for_name(hint)
        self._baseline_rollouts = _rollout_paths()
        self._started_wall = time.time()
        self._spawn()
        _log("spawned codex", {"pid": self._pid, "argv": self._argv, "hint": hint})
        self._enter_raw_mode()
        signal.signal(signal.SIGWINCH, self._handle_winch)
        signal.signal(signal.SIGHUP, self._handle_term)
        signal.signal(signal.SIGTERM, self._handle_term)
        try:
            return self._select_loop()
        finally:
            self._restore_terminal()
            for proc, _started in self._inflight.values():
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                os.unlink(self._state_path())
            except OSError:
                pass

    def _tick(self) -> None:
        self._discover()
        if self._tracker is not None:
            self._tracker.poll()
        self._publish()
        self._process_outgoing()

    def _select_loop(self) -> int:
        next_tick = time.monotonic() + _TICK_SEC
        while True:
            timeout = max(0.0, next_tick - time.monotonic())
            try:
                rlist, _, _ = select.select([0, self._master_fd], [], [], timeout)
            except (InterruptedError, OSError) as e:
                if isinstance(e, OSError) and e.errno == errno.EBADF:
                    break
                continue
            if 0 in rlist:
                try:
                    data = os.read(0, 4096)
                except OSError:
                    data = b""
                if not data:
                    try:
                        os.close(0)
                    except OSError:
                        pass
                else:
                    try:
                        os.write(self._master_fd, data)
                    except OSError:
                        pass
            if self._master_fd in rlist:
                try:
                    data = os.read(self._master_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                try:
                    os.write(1, data)
                except OSError:
                    pass
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + _TICK_SEC
                try:
                    self._tick()
                except Exception as e:
                    _log("tick crashed", {"err": repr(e)})
        try:
            _, status = os.waitpid(self._pid, 0)
        except OSError:
            return 0
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 0


def main(argv: list[str]) -> int:
    codex = shutil.which("codex")
    if not codex:
        sys.stderr.write("clacogui_codex_launcher: could not find 'codex' on PATH.\n")
        return 127
    for d in (OUTGOING_DIR, STATE_DIR):
        try:
            os.makedirs(os.path.join(_codex_home(), d), exist_ok=True)
        except OSError as e:
            _log("could not create dir", {"dir": d, "err": str(e)})
    return CodexLauncher([codex, *argv[1:]]).run()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
