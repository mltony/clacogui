#!/usr/bin/env python3
"""clacogui_codex_launcher - clacogui side channel for the Codex CLI.

Run *instead of* ``codex``::

    python3 ~/.codex/clacogui_codex_launcher.py [args passed to codex...]
    python3 ~/.codex/clacogui_codex_launcher.py resume <thread-id-or-name>
    python clacogui_codex_launcher.py --interrupt rpc [args passed to codex...]

``--interrupt MODE`` (or ``$CLACOGUI_CODEX_INTERRUPT``) picks how the GUI's
interrupt reaches Codex; the flag is consumed, not passed to codex:

* ``pty`` (default on Linux/macOS) -- Codex runs under our PTY and gets
  an Esc typed into it.
* ``console`` (default on Windows) -- Codex runs directly in this
  console window and gets an Esc key event written into the console's
  input queue.
* ``rpc`` (any OS) -- Codex runs directly in this terminal and the turn is
  stopped with the app-server ``turn/interrupt`` request, sent through
  ``codex app-server proxy``.

In ``console`` and ``rpc`` mode nothing sits between your keyboard and
Codex; the launcher only runs the side channel next to it.

Your terminal renders the Codex TUI exactly as before; keystrokes pass
straight through.  On a 200 ms side channel the launcher also:

* publishes this TUI's thread and busy/idle state to
  ``$CODEX_HOME/clacogui_sessions/<pid>.json`` for the clacogui status bar;
* delivers GUI messages from ``$CODEX_HOME/clacogui_outgoing/<id>.json``
  without typing into the TUI: while Codex is idle it starts the turn with
  the app-server's ``turn/start`` (``codex queue`` would sit unprocessed
  after an interrupt -- Codex pauses its queue then); while a turn runs it
  uses ``codex queue --thread <id> --message <text>`` so the message
  follows that turn;
* turns an ``action: interrupt`` envelope into an interrupt (see above),
  only while a turn is running (a second Esc at an idle Codex prompt
  opens "edit previous message").

Envelope format and outcome signalling match ``clacogui_launcher.py``:
the file is deleted once delivered, or renamed to ``<id>.json.failed``
with an ``error`` field.

Stdlib only, so it can be dropped into any container.
"""

from __future__ import annotations

import base64
import errno
import glob
import json
import os
import queue
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from typing import Optional

_IS_WINDOWS = sys.platform == "win32"
try:
    import fcntl
    import pty
    import termios
    import tty
except ImportError:  # Windows: only the console / rpc modes are available.
    fcntl = pty = termios = tty = None  # type: ignore[assignment]

INTERRUPT_MODES = ("pty", "console", "rpc")

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


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is running.  Never signals it: on Windows
    ``os.kill(pid, 0)`` would terminate the process."""
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _codex_command() -> Optional[list[str]]:
    """How to run ``codex``, as an argv prefix.

    On Windows npm installs a ``codex.cmd`` shim; running it goes through
    cmd.exe, which re-parses the arguments (``& | % ^ "``) and would mangle
    message text.  Call node on the package's own script instead.
    """
    path = shutil.which("codex")
    if not path:
        return None
    if _IS_WINDOWS and path.lower().endswith((".cmd", ".bat", ".ps1")):
        base = os.path.dirname(path)
        script = os.path.join(base, "node_modules", "@openai", "codex", "bin", "codex.js")
        node = shutil.which("node") or os.path.join(base, "node.exe")
        if os.path.isfile(script) and os.path.isfile(node):
            return [node, script]
        _log("using the codex.cmd shim; message text may be re-parsed by cmd.exe", {"path": path})
    return [path]


def _quiet_popen_kwargs() -> dict:
    """Keep helper processes off our terminal / console."""
    if _IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    # Windows refuses to replace a file another process (e.g. the FTP server
    # serving it to the GUI) has open at that moment; retry briefly.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


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
        except ValueError:
            continue
        if not _pid_alive(pid):
            continue
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
        # Insertion-ordered, so the newest open turn is the last key.
        self.open_turns: dict[str, None] = {}

    @property
    def busy(self) -> bool:
        return bool(self.open_turns)

    @property
    def current_turn(self) -> Optional[str]:
        return next(reversed(self.open_turns), None) if self.open_turns else None

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
                self.open_turns[payload.get("turn_id") or ""] = None
            elif ptype in ("task_complete", "turn_aborted"):
                self.open_turns.pop(payload.get("turn_id") or "", None)


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


def _claim(path: str) -> bool:
    """Take ``<envelope>.lock`` so only one launcher delivers an envelope.

    Several TUIs (each under its own launcher) can be attached to the same
    thread; without this every one of them would send the message.  The
    lock name doesn't end in ``.json``, so the GUI ignores it and still
    reads "envelope gone" as delivered.  A lock left by a launcher that
    died is broken so the envelope isn't stranded.
    """
    lock = path + ".lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                with open(lock, encoding="utf-8") as f:
                    owner = int(f.read().strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner == os.getpid():
                return True
            if owner == 0:
                # Created but its pid not written yet -- another launcher is
                # mid-claim.  Only an old empty lock counts as abandoned.
                try:
                    if time.time() - os.stat(lock).st_mtime < 10:
                        return False
                except OSError:
                    return False
            elif _pid_alive(owner):
                return False
            _log("breaking stale envelope lock", {"path": path, "owner": owner})
            try:
                os.unlink(lock)
            except OSError:
                return False
            continue
        except OSError as e:
            _log("could not create envelope lock", {"path": path, "err": str(e)})
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def _release(path: str) -> None:
    try:
        os.unlink(path + ".lock")
    except OSError:
        pass


def _envelope_age_sec(path: str) -> float:
    try:
        return time.time() - int(os.path.basename(path).split("_", 1)[0]) / 1000
    except (ValueError, IndexError):
        return 0.0


# ---------------------------------------------------------------------------
# Interrupt transports for the non-PTY modes
# ---------------------------------------------------------------------------


def _console_send_escape() -> None:
    """Write an Esc key press into this console's input queue (Windows).

    Codex shares our console in ``console`` mode and reads key events from
    the same queue, so it sees this exactly as if Esc had been pressed.
    Fixed-width field types keep the structs at their Windows sizes
    (KEY_EVENT_RECORD 16 bytes, INPUT_RECORD 20).
    """
    import ctypes

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", ctypes.c_int32),
            ("wRepeatCount", ctypes.c_uint16),
            ("wVirtualKeyCode", ctypes.c_uint16),
            ("wVirtualScanCode", ctypes.c_uint16),
            ("UnicodeChar", ctypes.c_uint16),
            ("dwControlKeyState", ctypes.c_uint32),
        ]

    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", ctypes.c_uint16), ("Event", _EVENT)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    k32.WriteConsoleInputW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(INPUT_RECORD), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    # CONIN$ is this console's input even if our stdin was redirected.
    handle = k32.CreateFileW(
        "CONIN$", 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0, None
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "could not open the console input (CONIN$)")
    try:
        records = (INPUT_RECORD * 2)()
        for rec, down in zip(records, (1, 0)):
            rec.EventType = 0x0001  # KEY_EVENT
            key = rec.Event.KeyEvent
            key.bKeyDown = down
            key.wRepeatCount = 1
            key.wVirtualKeyCode = 0x1B  # VK_ESCAPE
            key.wVirtualScanCode = 0x01
            key.UnicodeChar = 0x1B
        written = ctypes.c_uint32()
        if not k32.WriteConsoleInputW(handle, records, 2, ctypes.byref(written)):
            raise OSError(ctypes.get_last_error(), "WriteConsoleInputW failed")
    finally:
        k32.CloseHandle(handle)


class _AppServerRpc:
    """JSON-RPC to the running Codex app-server through ``codex app-server proxy``.

    The proxy relays raw bytes to the app-server's control socket, which
    speaks WebSocket, so this does the upgrade handshake and text framing
    itself.  A reader thread (rather than ``select``) keeps it working on
    Windows pipes.
    """

    def __init__(self, codex_cmd: list[str], timeout: float = 10.0) -> None:
        self._proc = subprocess.Popen(
            [*codex_cmd, "app-server", "proxy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **_quiet_popen_kwargs(),
        )
        self._buf = b""
        self._chunks: "queue.Queue[bytes]" = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._next_id = 1
        key = base64.b64encode(os.urandom(16)).decode()
        self._write(
            (
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self._buf:
            self._fill(deadline)
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise OSError(f"app-server refused the WebSocket upgrade: {head[:120]!r}")

    def _read_loop(self) -> None:
        while True:
            try:
                data = self._proc.stdout.read1(65536)
            except (OSError, ValueError):
                data = b""
            self._chunks.put(data)
            if not data:
                return

    def _fill(self, deadline: float) -> None:
        left = deadline - time.monotonic()
        try:
            data = self._chunks.get(timeout=max(left, 0.0))
        except queue.Empty:
            raise TimeoutError("Codex app-server did not answer in time") from None
        if not data:
            raise OSError("codex app-server proxy closed the connection")
        self._buf += data

    def _write(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        self._write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self, deadline: float):
        while True:
            while len(self._buf) < 2:
                self._fill(deadline)
            n, off = self._buf[1] & 0x7F, 2
            if n == 126:
                while len(self._buf) < 4:
                    self._fill(deadline)
                n, off = struct.unpack(">H", self._buf[2:4])[0], 4
            elif n == 127:
                while len(self._buf) < 10:
                    self._fill(deadline)
                n, off = struct.unpack(">Q", self._buf[2:10])[0], 10
            while len(self._buf) < off + n:
                self._fill(deadline)
            opcode = self._buf[0] & 0x0F
            payload, self._buf = self._buf[off:off + n], self._buf[off + n:]
            if opcode == 0x8:
                raise OSError("Codex app-server closed the connection")
            if opcode in (0x1, 0x2):
                return json.loads(payload)

    def call(self, method: str, params: dict, timeout: float = 10.0) -> dict:
        rid = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            msg = self._recv(deadline)
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise OSError(f"{method} failed: {msg['error']}")
                return msg.get("result") or {}

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        try:
            self._proc.kill()
            self._proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _rpc_turn_start(codex_cmd: list[str], thread_id: str, text: str) -> str:
    rpc = _AppServerRpc(codex_cmd)
    try:
        rpc.call("initialize", {"clientInfo": {"name": "clacogui", "version": "1"}})
        rpc.notify("initialized")
        result = rpc.call(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
            timeout=30,
        )
    finally:
        rpc.close()
    turn = result.get("turn") if isinstance(result, dict) else None
    return f"started turn {turn.get('id') if isinstance(turn, dict) else '?'}"


def _codex_queue(codex_cmd: list[str], thread_id: str, text: str) -> str:
    proc = subprocess.run(
        [*codex_cmd, "queue", "--thread", thread_id, "--message", text],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_QUEUE_TIMEOUT_SEC,
        **_quiet_popen_kwargs(),
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise OSError(f"codex queue exited {proc.returncode}: {lines[-1] if lines else ''}")
    return proc.stdout.strip()[-200:]


class _SendJob:
    """One delivery running on a worker thread, so the terminal never stalls."""

    def __init__(self, codex_cmd: list[str], thread_id: str, text: str, idle: bool) -> None:
        self.started = time.monotonic()
        self.how = "turn/start" if idle else "codex queue"
        self.result: Optional[tuple[bool, str]] = None
        args = (codex_cmd, thread_id, text, idle)
        threading.Thread(target=self._run, args=args, daemon=True).start()

    def _run(self, codex_cmd, thread_id, text, idle) -> None:
        try:
            if idle:
                try:
                    self.result = (True, _rpc_turn_start(codex_cmd, thread_id, text))
                    return
                except (OSError, TimeoutError, ValueError) as e:
                    _log("turn/start failed; falling back to codex queue", {"err": str(e)})
                    self.how = "codex queue (fallback)"
            self.result = (True, _codex_queue(codex_cmd, thread_id, text))
        except Exception as e:
            self.result = (False, str(e))


def _rpc_interrupt(codex_cmd: list[str], thread_id: str, turn_id: str) -> None:
    rpc = _AppServerRpc(codex_cmd)
    try:
        rpc.call("initialize", {"clientInfo": {"name": "clacogui", "version": "1"}})
        rpc.notify("initialized")
        rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
    finally:
        rpc.close()


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


class CodexLauncher:
    def __init__(
        self, codex_cmd: list[str], codex_args: list[str], mode: str = "pty"
    ) -> None:
        self._codex_cmd = codex_cmd
        self._codex_args = codex_args
        self._argv = [*codex_cmd, *codex_args]
        self._mode = mode
        self._child: Optional[subprocess.Popen] = None
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
        self._inflight: dict[str, _SendJob] = {}
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
        for path, job in list(self._inflight.items()):
            if job.result is None:
                if time.monotonic() - job.started > 2 * _QUEUE_TIMEOUT_SEC:
                    del self._inflight[path]
                    _log("send timed out", {"path": path, "via": job.how})
                    _fail(path, f"{job.how} timed out")
                    _release(path)
                continue
            del self._inflight[path]
            ok, msg = job.result
            if ok:
                _log("delivered", {"path": path, "via": job.how, "out": msg})
                _unlink(path)
            else:
                _log("send failed", {"path": path, "via": job.how, "err": msg})
                _fail(path, f"{job.how} failed: {msg}")
            _release(path)

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
                self._fail_claimed(path, f"could not parse JSON envelope (file size {size} bytes)")
                continue
            if _envelope_age_sec(path) > _OUTGOING_MAX_AGE_SEC:
                self._fail_claimed(path, f"stale outgoing file (older than {_OUTGOING_MAX_AGE_SEC}s)")
                continue
            sid = envelope.get("session_id")
            if not sid:
                self._fail_claimed(path, "envelope has no session_id")
                continue
            if sid != self._session_id:
                if path not in self._skips:
                    _log("skip: session_id mismatch", {"path": path, "envelope_sid": sid, "our_sid": self._session_id})
                    self._skips.add(path)
                continue
            if not _claim(path):
                # Another launcher on this thread is delivering it.
                continue
            if not os.path.exists(path):
                # It finished delivering between our read and our claim (it
                # deletes the envelope before releasing the lock).
                _release(path)
                continue
            try:
                self._deliver(path, envelope, sid)
            except Exception as e:
                _log("deliver crashed", {"path": path, "err": repr(e)})
                _release(path)

    def _fail_claimed(self, path: str, error: str) -> None:
        if _claim(path):
            if os.path.exists(path):
                _fail(path, error)
            _release(path)

    def _deliver(self, path: str, envelope: dict, sid: str) -> None:
        """Act on one claimed envelope.  The lock is released here, except
        for a send, where it stays held until ``_reap_inflight`` sees
        ``codex queue`` finish."""
        action = envelope.get("action")
        if action == "interrupt":
            if self._status() == "busy":
                try:
                    self._interrupt(sid)
                    _log("delivered interrupt", {"path": path, "via": self._mode})
                    _unlink(path)
                except (OSError, TimeoutError) as e:
                    _log("interrupt failed", {"path": path, "via": self._mode, "err": str(e)})
                    _fail(path, f"interrupt via {self._mode} failed: {e}")
            else:
                _log("interrupt ignored: no turn running", {"path": path})
                _unlink(path)
            _release(path)
            return
        if action not in (None, "", "send"):
            _fail(path, f"unknown action: {action!r}")
            _release(path)
            return
        text = envelope.get("text")
        if not isinstance(text, str) or not text.strip():
            _fail(path, "envelope has no 'text' string")
            _release(path)
            return
        # The Windows GUI sends CRLF line breaks.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        job = _SendJob(self._codex_cmd, sid, text, idle=self._status() == "idle")
        self._inflight[path] = job
        _log("sending", {"path": path, "via": job.how, "chars": len(text)})

    def _interrupt(self, sid: str) -> None:
        if self._mode == "pty":
            os.write(self._master_fd, _ESC)
        elif self._mode == "console":
            _console_send_escape()
        else:
            turn = self._tracker.current_turn if self._tracker is not None else None
            if not turn:
                raise OSError("no running turn id in the rollout")
            _rpc_interrupt(self._codex_cmd, sid, turn)

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
        # Size the PTY from our terminal *before* Codex starts: a TUI that
        # reads its size at startup and sees 0x0 draws nothing at all.
        master, slave = pty.openpty()
        try:
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        except OSError as e:
            _log("could not copy terminal size", {"err": str(e)})
        pid = os.fork()
        if pid == 0:
            try:
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                for fd in (0, 1, 2):
                    os.dup2(slave, fd)
                if slave > 2:
                    os.close(slave)
                os.execvp(self._argv[0], self._argv)
            except OSError as e:
                sys.stderr.write(f"clacogui_codex_launcher: exec {self._argv[0]!r} failed: {e}\n")
            os._exit(127)
        os.close(slave)
        self._pid, self._master_fd = pid, master

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        hint = _argv_thread_hint(self._codex_args)
        if hint:
            self._hint_id = hint.lower() if _UUID_RE.match(hint) else _thread_id_for_name(hint)
        self._baseline_rollouts = _rollout_paths()
        self._started_wall = time.time()
        if self._mode != "pty":
            return self._run_direct(hint)
        self._spawn()
        _log("spawned codex", {"pid": self._pid, "argv": self._argv, "hint": hint, "mode": "pty"})
        self._enter_raw_mode()
        signal.signal(signal.SIGWINCH, self._handle_winch)
        signal.signal(signal.SIGHUP, self._handle_term)
        signal.signal(signal.SIGTERM, self._handle_term)
        try:
            return self._select_loop()
        finally:
            self._restore_terminal()
            self._cleanup()

    def _cleanup(self) -> None:
        try:
            os.unlink(self._state_path())
        except OSError:
            pass

    def _run_direct(self, hint: Optional[str]) -> int:
        """``console`` / ``rpc``: Codex owns this terminal; we only watch.

        Ctrl+C reaches every process attached to the terminal, so ignore it
        here -- it is Codex's to handle.
        """
        self._child = subprocess.Popen(self._argv)
        self._pid = self._child.pid
        _log("spawned codex", {"pid": self._pid, "argv": self._argv, "hint": hint, "mode": self._mode})
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if not _IS_WINDOWS:
            signal.signal(signal.SIGHUP, self._handle_term)
            signal.signal(signal.SIGTERM, self._handle_term)
        try:
            while self._child.poll() is None:
                try:
                    self._tick()
                except Exception as e:
                    _log("tick crashed", {"err": repr(e)})
                time.sleep(_TICK_SEC)
        finally:
            self._cleanup()
        rc = self._child.returncode
        return 128 - rc if rc < 0 else rc

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


def _split_args(args: list[str]) -> tuple[str, list[str]]:
    """Take ``--interrupt MODE`` / ``--interrupt=MODE`` out of the codex args."""
    mode = os.environ.get("CLACOGUI_CODEX_INTERRUPT") or ("console" if _IS_WINDOWS else "pty")
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--interrupt" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
            continue
        if a.startswith("--interrupt="):
            mode = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return mode, rest


def main(argv: list[str]) -> int:
    mode, codex_args = _split_args(argv[1:])
    if mode not in INTERRUPT_MODES:
        sys.stderr.write(
            f"clacogui_codex_launcher: --interrupt must be one of {', '.join(INTERRUPT_MODES)}\n"
        )
        return 2
    if mode == "pty" and pty is None:
        sys.stderr.write(
            "clacogui_codex_launcher: --interrupt pty needs a Unix terminal; "
            "on Windows use --interrupt console or --interrupt rpc.\n"
        )
        return 2
    if mode == "console" and not _IS_WINDOWS:
        sys.stderr.write(
            "clacogui_codex_launcher: --interrupt console is Windows-only; "
            "use pty or rpc.\n"
        )
        return 2
    codex = _codex_command()
    if not codex:
        sys.stderr.write("clacogui_codex_launcher: could not find 'codex' on PATH.\n")
        return 127
    for d in (OUTGOING_DIR, STATE_DIR):
        try:
            os.makedirs(os.path.join(_codex_home(), d), exist_ok=True)
        except OSError as e:
            _log("could not create dir", {"dir": d, "err": str(e)})
    return CodexLauncher(codex, codex_args, mode).run()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
