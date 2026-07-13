"""wxPython GUI for clacogui.

Layout:
    MainFrame
      \u2514\u2500 wx.Notebook  (one page per open conversation)
            \u2514\u2500 ConversationPanel
                  \u251c\u2500 left: wx.ListBox of user-message previews
                  \u2514\u2500 right: wx.html2.WebView with rendered turn

All keyboard navigation is wired through ``wx.AcceleratorTable`` so screen
readers get the standard menu accelerators *and* the WebView cannot swallow
shortcuts.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

import wx
import wx.html2

try:
    import winsound
except ImportError:  # not on Windows; we'll fall back to wx.Bell
    winsound = None  # type: ignore[assignment]

import models
from fs import FsBackend, make_backend

# Directory names mirrored from clacogui_agent.py.  Kept inline (rather than
# imported) so we don't drag the agent's argparse dependencies into the GUI.
AGENT_NOTIFICATIONS_DIR = "clacogui_notifications"
AGENT_REQUESTS_DIR = "clacogui_requests"
AGENT_RESPONSES_DIR = "clacogui_responses"
# clacogui_launcher.py polls this directory for messages typed in the
# GUI and types them into Claude's PTY (raw Ctrl+J between lines, CR
# to submit -- see clacogui_launcher.py for why).  The launcher is the
# only consumer; this GUI is the only producer.
AGENT_OUTGOING_DIR = "clacogui_outgoing"
# Claude itself writes per-process JSON metadata files here:
#   ~/.claude/sessions/<pid>.json
# Each file is a small object that includes ``sessionId`` and ``status``
# (``idle`` / ``busy`` / ...).  We read these on every poll tick and use
# them to display "Claude: idle" / "Claude: busy" in the status bar.
# Same data the launcher uses to gate injection -- keeping the GUI in
# sync with the launcher's view of reality is what makes "stuck on
# queued" debuggable for the user.
CLAUDE_SESSIONS_DIR = "sessions"
# Session metadata files older than this are ignored when building the
# claude-status map.  Claude updates the file on every status
# transition (idle <-> busy), so a long-idle live session keeps a
# stale mtime; we therefore only use staleness to filter *very* old
# files (multi-day) which almost certainly belong to dead processes.
STALE_CLAUDE_SESSION_AGE_SEC = 7 * 24 * 3600

# A request file in ``clacogui_requests/`` older than this is guaranteed
# stale: the agent process that wrote it has long since timed out and
# emitted a deny.  Must be greater than the agent's
# ``DEFAULT_REQUEST_TIMEOUT_SEC`` (300 s as of this writing); we keep a
# small safety buffer so a slow GUI->backend round trip can't ever
# misclassify a still-pending request as stale.  Stale request files
# normally only show up when the agent process is killed mid-flight
# (e.g. you kill the wrapping ``ai-sandbox`` shell while a
# permission dialog is open) and end up resurrecting on every GUI
# launch until something deletes them.
STALE_REQUEST_AGE_SEC = 360

# Hook events that mean "Claude actually finished something the user is
# waiting on" -- ring on these.  ``Notification`` is treated specially
# (see ``_NOTIFICATION_DEDUP_SEC``).
#
# Only ``Stop`` counts as a real turn boundary.  ``SubagentStop`` fires when
# a subagent (Task/Agent tool) finishes mid-turn while the main loop is
# still working; ``PreToolUse`` fires *before* every tool call.  Ringing on
# those makes the completion sound go off during ongoing work, which is
# exactly what the user reported as a bug.
_COMPLETION_EVENTS: frozenset = frozenset({"Stop"})

# Events that should be completely silent (no sound at all).
_SILENT_EVENTS: frozenset = frozenset({"StopCompact"})

# If a "Notification" hook fires within this many seconds of the most
# recent completion event, swallow it.  Claude Code emits a "Claude is
# waiting for your input" Notification roughly 60s after Stop, which
# would otherwise produce a redundant "second ping a minute later" --
# the exact symptom the user reported.  300s gives plenty of slack
# while still letting genuine long-idle alerts through.
_NOTIFICATION_DEDUP_SEC: float = 300.0


def _event_from_notification_name(name: str) -> str:
    """Extract the hook event name from a notification filename.

    The agent encodes it as ``<unix-millis>_<rand>__<EventName>.json``.
    Older agent versions used just ``<unix-millis>_<rand>.json`` -- those
    return ``""`` and the GUI treats them as "ring to be safe".
    """
    base = name.rsplit(".", 1)[0]
    parts = base.rsplit("__", 1)
    if len(parts) != 2:
        return ""
    return parts[1]
from models import (
    ConversationData,
    ConversationInfo,
    Turn,
    extract_name_from_head,
    file_signature,
    format_timestamp,
    iter_conversation_files,
    load_session_names,
    parse_conversation,
    summarize_conversation,
)
from render import (
    content_id as _render_content_id,
    render_blank_html,
    render_turn_html,
    render_turn_inner_html,
)

log = logging.getLogger(__name__)


CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".clacogui_config.json"
)

POLL_INTERVAL_MS = 200
ACTIVE_POLL_INTERVAL_MS = 200


def _set_window_appid(hwnd: int) -> None:
    """Set per-window AppUserModelID + RelaunchDisplayName on Windows.

    NVDA reads the taskbar button's accessible name partly from the
    window's IPropertyStore.  Setting System.AppUserModel.ID and
    .RelaunchDisplayNameResource via COM makes NVDA announce "clacogui"
    instead of the process exe name ("python").

    Uses raw ctypes COM vtable calls — no comtypes dependency.
    """
    if not hwnd:
        return
    try:
        import ctypes
        import ctypes.wintypes as wt
        import struct

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def _guid(s: str) -> GUID:
            s = s.strip("{}")
            p = s.split("-")
            g = GUID()
            g.Data1 = int(p[0], 16)
            g.Data2 = int(p[1], 16)
            g.Data3 = int(p[2], 16)
            d4 = bytes.fromhex(p[3] + p[4])
            for i in range(8):
                g.Data4[i] = d4[i]
            return g

        IID_IPropertyStore = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")

        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        pstore = ctypes.c_void_p()
        hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
            hwnd, ctypes.byref(IID_IPropertyStore), ctypes.byref(pstore)
        )
        if hr != 0 or not pstore:
            return

        # IPropertyStore vtable layout (IUnknown + 5 methods):
        # 0=QueryInterface 1=AddRef 2=Release
        # 3=GetCount 4=GetAt 5=GetValue 6=SetValue 7=Commit
        vt_ptr = ctypes.cast(pstore, ctypes.POINTER(ctypes.c_void_p))[0]
        VT = ctypes.cast(vt_ptr, ctypes.POINTER(ctypes.c_void_p * 8))[0]

        SETVALUE = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(VT[6])
        COMMIT = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(VT[7])
        RELEASE = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(VT[2])

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]

        fmtid = _guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")
        PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)

        def _set_str(pid: int, value: str) -> None:
            key = PROPERTYKEY()
            key.fmtid = fmtid
            key.pid = pid
            # PROPVARIANT: vt(2) + pad(6) + pointer(PTR_SIZE)
            pv = (ctypes.c_byte * (8 + PTR_SIZE))()
            struct.pack_into("<H", pv, 0, 31)  # VT_LPWSTR
            wstr = ctypes.c_wchar_p(value)
            ptr_val = ctypes.cast(wstr, ctypes.c_void_p).value or 0
            fmt = "<Q" if PTR_SIZE == 8 else "<I"
            struct.pack_into(fmt, pv, 8, ptr_val)
            SETVALUE(pstore, ctypes.byref(key), ctypes.byref(pv))

        _set_str(5, "clacogui.app.1")  # AppUserModel.ID
        _set_str(4, "clacogui")  # RelaunchDisplayNameResource
        COMMIT(pstore)
        RELEASE(pstore)
    except Exception:
        log.debug("_set_window_appid failed", exc_info=True)


# ---------------------------------------------------------------------------
# Background IO worker thread
# ---------------------------------------------------------------------------
#
# All Claude Code data lives on a network share, so any file read can stall
# for a few hundred ms.  We keep the UI thread off the disk by handing reads
# to a single background thread and posting results back via wx.CallAfter.
#
# A *single* thread (not a pool) is intentional:
#
#   * The bottleneck is the SMB share, not CPU. Parallel reads against a
#     slow share usually make it slower, not faster, because the server has
#     to thrash between handles.
#   * Serialising IO means at most one outstanding stat()/read() at any
#     moment, which is the gentlest pattern for a flaky share.
#   * Each panel already de-duplicates its own jobs (``_polling`` /
#     ``_loading`` flags), so the queue can never grow without bound.

_io_queue: "queue.Queue[Optional[tuple[Callable, Callable]]]" = queue.Queue()
_io_write_queue: "queue.Queue[Optional[tuple[Callable, Callable]]]" = queue.Queue()


def _io_worker_loop() -> None:
    while True:
        item = _io_queue.get()
        if item is None:
            return  # shutdown sentinel
        fn, on_done = item
        try:
            result = fn()
            exc: Optional[BaseException] = None
        except BaseException as e:  # noqa: BLE001
            log.exception("Background IO failed")
            result = None
            exc = e
        try:
            wx.CallAfter(on_done, result, exc)
        except Exception:
            log.exception("wx.CallAfter dispatch failed")


def _io_write_worker_loop() -> None:
    """Dedicated thread for write operations (send messages, permission
    responses).  Runs in parallel with the read thread so writes are
    never blocked behind a large read."""
    while True:
        item = _io_write_queue.get()
        if item is None:
            return
        fn, on_done = item
        try:
            result = fn()
            exc: Optional[BaseException] = None
        except BaseException as e:  # noqa: BLE001
            log.exception("Background write IO failed")
            result = None
            exc = e
        try:
            wx.CallAfter(on_done, result, exc)
        except Exception:
            log.exception("wx.CallAfter dispatch failed")


_io_thread = threading.Thread(
    target=_io_worker_loop,
    name="clacogui-io",
    daemon=True,
)
_io_thread.start()

_io_write_thread = threading.Thread(
    target=_io_write_worker_loop,
    name="clacogui-io-write",
    daemon=True,
)
_io_write_thread.start()


@atexit.register
def _shutdown_io_thread() -> None:
    log.debug("Shutting down IO threads")
    try:
        _io_queue.put_nowait(None)
    except Exception:
        pass
    try:
        _io_write_queue.put_nowait(None)
    except Exception:
        pass


def submit_io(
    fn: Callable,
    on_done: Callable[[object, Optional[BaseException]], None],
) -> None:
    """Run ``fn()`` on the background IO thread, then call
    ``on_done(result, exc)`` on the wx main thread.

    Jobs are processed strictly in submission order on a single thread.
    Exceptions raised by ``fn`` are caught and forwarded as ``exc`` (with
    ``result`` set to ``None``).

    ``on_done`` must always start with ``if not window:`` (or equivalent)
    before touching wx state, because the window may have been destroyed
    while the read was queued.
    """
    _io_queue.put((fn, on_done))


def submit_io_write(
    fn: Callable,
    on_done: Callable[[object, Optional[BaseException]], None],
) -> None:
    """Like ``submit_io`` but runs on a dedicated write thread.

    Use for small writes (send messages, permission responses) that
    must not be blocked behind a large read in progress.
    """
    _io_write_queue.put((fn, on_done))


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        log.exception("Could not save config to %s", CONFIG_PATH)


# ---------------------------------------------------------------------------
# IO error handling (async)
# ---------------------------------------------------------------------------

_io_dialog_active = False


def submit_io_with_retry(
    parent: Optional[wx.Window],
    op_label: str,
    fn: Callable,
    on_success: Callable[[object], None],
    on_give_up: Optional[Callable[[BaseException], None]] = None,
) -> None:
    """Async ``submit_io`` that pops a Retry/Quit dialog on ``OSError``.

    On success: ``on_success(result)`` is called on the wx thread.

    On ``OSError``: a single modal Retry/Quit dialog is shown.
      * Retry  -> re-submit ``fn``.
      * Quit   -> close the top-level window (or call ``on_give_up`` if given).

    Only one dialog is on screen at a time across the whole app; subsequent
    errors fall through to ``on_give_up``/log without showing another box.
    """

    def callback(result, exc):
        global _io_dialog_active
        if parent is not None and not parent:  # wx C++ object already gone
            return
        if exc is None:
            try:
                on_success(result)
            except Exception:
                log.exception("on_success crashed for %s", op_label)
            return
        if not isinstance(exc, OSError):
            log.exception("Non-IO failure in %s: %r", op_label, exc)
            wx.MessageBox(
                f"{op_label} failed:\n{exc.__class__.__name__}: {exc}",
                "Error",
                wx.OK | wx.ICON_ERROR,
                parent,
            )
            if on_give_up is not None:
                on_give_up(exc)
            return

        if _io_dialog_active:
            log.warning("Suppressed extra IO dialog for %s: %s", op_label, exc)
            if on_give_up is not None:
                on_give_up(exc)
            return

        _io_dialog_active = True
        try:
            msg = (
                f"An I/O error occurred while {op_label}.\n\n"
                f"{exc.__class__.__name__}: {exc}\n\n"
                "This often happens with network shares (SMB).\n\n"
                "Press Retry to try again, or Quit to exit clacogui."
            )
            dlg = wx.MessageDialog(
                parent,
                msg,
                "I/O error",
                style=wx.YES_NO | wx.ICON_ERROR | wx.YES_DEFAULT,
            )
            dlg.SetYesNoLabels("&Retry", "&Quit")
            choice = dlg.ShowModal()
            dlg.Destroy()
        finally:
            _io_dialog_active = False

        if choice == wx.ID_YES:
            submit_io_with_retry(parent, op_label, fn, on_success, on_give_up)
            return

        if on_give_up is not None:
            on_give_up(exc)
        else:
            top = wx.GetApp().GetTopWindow() if wx.GetApp() else None
            if top is not None:
                top.Close(force=True)

    submit_io(fn, callback)


# ---------------------------------------------------------------------------
# Open-conversation dialog
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    info: ConversationInfo


class OpenConversationDialog(wx.Dialog):
    """Lists every conversation under ``<claude_dir>/projects``."""

    SORT_TIMESTAMP = 0
    SORT_NAME = 1

    def __init__(
        self,
        parent: wx.Window,
        backend: FsBackend,
        cached_active_sids: Optional[set[str]] = None,
    ) -> None:
        super().__init__(
            parent,
            title="Open conversation",
            size=(800, 550),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.backend = backend
        self._rows: list[_Row] = []
        self._all_rows: list[_Row] = []
        self._active_sids: set[str] = cached_active_sids or set()
        self._sort_mode = self.SORT_TIMESTAMP
        self._loading = False
        self._pulse_timer: Optional[wx.Timer] = None
        self._build_ui()
        self._populate()

    # -- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=(
                f"Conversations under: {self.backend.display_path('projects')}\n"
                "Use Up/Down to choose, Enter to open. "
                "Press Alt+T to sort by time, Alt+N to sort by name."
            ),
        )
        vbox.Add(intro, 0, wx.ALL | wx.EXPAND, 6)

        sort_box = wx.BoxSizer(wx.HORIZONTAL)
        self._btn_sort_time = wx.Button(panel, label="Sort by &time")
        self._btn_sort_name = wx.Button(panel, label="Sort by &name")
        self._btn_refresh = wx.Button(panel, label="&Refresh")
        self._chk_show_all = wx.CheckBox(panel, label="Show &all conversations")
        self._status_label = wx.StaticText(panel, label="")
        sort_box.Add(self._btn_sort_time, 0, wx.RIGHT, 6)
        sort_box.Add(self._btn_sort_name, 0, wx.RIGHT, 6)
        sort_box.Add(self._btn_refresh, 0, wx.RIGHT, 12)
        sort_box.Add(self._chk_show_all, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 12)
        sort_box.Add(self._status_label, 0, wx.ALIGN_CENTER_VERTICAL)
        vbox.Add(sort_box, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self._list = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
            name="Conversations",
        )
        self._list.InsertColumn(0, "Name", width=320)
        self._list.InsertColumn(1, "Last modified", width=160)
        self._list.InsertColumn(2, "Project", width=180)
        self._list.InsertColumn(3, "File", width=160)
        vbox.Add(self._list, 1, wx.ALL | wx.EXPAND, 6)

        btns = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK, label="&Open")
        ok_btn.SetDefault()
        btns.AddButton(ok_btn)
        btns.AddButton(wx.Button(panel, wx.ID_CANCEL, label="&Cancel"))
        btns.Realize()
        vbox.Add(btns, 0, wx.ALL | wx.EXPAND, 6)

        panel.SetSizer(vbox)

        # Bindings
        self._btn_sort_time.Bind(wx.EVT_BUTTON, lambda _e: self._sort(self.SORT_TIMESTAMP))
        self._btn_sort_name.Bind(wx.EVT_BUTTON, lambda _e: self._sort(self.SORT_NAME))
        self._btn_refresh.Bind(wx.EVT_BUTTON, lambda _e: self._populate())
        self._chk_show_all.Bind(wx.EVT_CHECKBOX, lambda _e: self._apply_filter())
        self._list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: self.EndModal(wx.ID_OK))
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        # Make Enter on the list activate the selection.
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.selected() is not None:
                self.EndModal(wx.ID_OK)
                return
        event.Skip()

    def _on_ok(self, event: wx.CommandEvent) -> None:
        if self._loading:
            wx.MessageBox(
                "Still scanning conversations, please wait...",
                "Loading",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            return
        if self.selected() is None:
            wx.MessageBox(
                "Please choose a conversation first.",
                "No conversation selected",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            return
        event.Skip()

    # -- Data ---------------------------------------------------------------

    def _populate(self) -> None:
        """Kick off a background scan of the conversation index.

        Runs sessions -> projects -> filename-based summaries on the IO
        worker thread; the JSONL bodies are *not* read here, only the
        directory listing metadata.  This keeps the dialog snappy on FTP
        and slow shares.
        """
        if self._loading:
            return
        self._loading = True
        self._set_loading(True)

        backend = self.backend

        cached_sids = self._active_sids

        def work() -> tuple[list[_Row], set[str]]:
            session_names = load_session_names(backend)
            rows: list[_Row] = []
            for rel_path, project_dir, mtime, _size in iter_conversation_files(backend):
                info = summarize_conversation(rel_path, session_names, project_dir, mtime)
                rows.append(_Row(info))
            # Reuse cached active_sids if available (from MainFrame's
            # 1s poll), saving an FTP round-trip.
            if cached_sids:
                active_sids = cached_sids
            else:
                status_map, _ = _read_claude_session_state(backend)
                active_sids = set(status_map.keys())
            # Head-read for unnamed conversations: only those that are
            # active (shown in the default view).  Inactive unnamed ones
            # get their names lazily if the user checks "Show all".
            unnamed = [
                r for r in rows
                if r.info.name == r.info.session_id
                and r.info.session_id in active_sids
            ]
            unnamed.sort(key=lambda r: -r.info.mtime)
            for r in unnamed[:20]:
                try:
                    head = backend.read_text_head(r.info.rel_path, 4096)
                    name = extract_name_from_head(head)
                    if name:
                        r.info.name = name
                except OSError:
                    pass
            return rows, active_sids

        submit_io_with_retry(
            self,
            "loading conversation index",
            work,
            self._on_populate_done,
            on_give_up=lambda _exc: self._on_populate_aborted(),
        )

    def _on_populate_done(self, result: tuple[list[_Row], set[str]]) -> None:
        if not self:
            return
        self._loading = False
        self._set_loading(False)
        rows, active_sids = result
        self._all_rows = rows or []
        self._active_sids = active_sids
        self._apply_filter()

    def _apply_filter(self) -> None:
        show_all = self._chk_show_all.IsChecked()
        if show_all:
            self._rows = list(self._all_rows)
            self._resolve_unnamed_lazy()
        else:
            self._rows = [
                r for r in self._all_rows
                if r.info.session_id in self._active_sids
            ]
        self._sort(self._sort_mode)
        total = len(self._all_rows)
        shown = len(self._rows)
        if shown == total:
            self._status_label.SetLabel(f"{total} conversation(s)")
        else:
            self._status_label.SetLabel(
                f"{shown} active of {total} conversation(s)"
            )

    def _resolve_unnamed_lazy(self) -> None:
        """Background-resolve names for unnamed rows (triggered by Show All)."""
        unnamed = [
            r for r in self._all_rows
            if r.info.name == r.info.session_id
        ]
        if not unnamed:
            return
        unnamed.sort(key=lambda r: -r.info.mtime)
        to_resolve = unnamed[:30]
        backend = self.backend

        def work():
            resolved = {}
            for r in to_resolve:
                try:
                    head = backend.read_text_head(r.info.rel_path, 4096)
                    name = extract_name_from_head(head)
                    if name:
                        resolved[r.info.rel_path] = name
                except OSError:
                    pass
            return resolved

        def callback(result, exc):
            if not self or exc is not None or not result:
                return
            for r in self._all_rows:
                if r.info.rel_path in result:
                    r.info.name = result[r.info.rel_path]
            self._sort(self._sort_mode)

        submit_io(work, callback)

    def _on_populate_aborted(self) -> None:
        if not self:
            return
        self._loading = False
        self._set_loading(False)
        self.EndModal(wx.ID_CANCEL)

    def _set_loading(self, loading: bool) -> None:
        for btn in (self._btn_sort_time, self._btn_sort_name, self._btn_refresh):
            btn.Enable(not loading)
        ok = self.FindWindow(wx.ID_OK)
        if ok is not None:
            ok.Enable(not loading)
        if loading:
            self._list.DeleteAllItems()
            self._list.InsertItem(0, "Scanning conversations, please wait...")
            self._status_label.SetLabel("Loading...")
            if self._pulse_timer is None:
                self._pulse_timer = wx.Timer(self)
                self.Bind(wx.EVT_TIMER, self._on_pulse, self._pulse_timer)
            self._pulse_timer.Start(400)
        else:
            if self._pulse_timer is not None:
                self._pulse_timer.Stop()

    def _on_pulse(self, _event: wx.TimerEvent) -> None:
        # Cycle the status text so a screen reader notices we're still alive.
        cur = self._status_label.GetLabel()
        if cur.endswith("..."):
            self._status_label.SetLabel("Loading")
        else:
            self._status_label.SetLabel(cur + ".")

    def _sort(self, mode: int) -> None:
        self._sort_mode = mode
        if mode == self.SORT_NAME:
            self._rows.sort(key=lambda r: (r.info.display_name().lower(), -r.info.mtime))
        else:
            self._rows.sort(key=lambda r: -r.info.mtime)
        self._refresh_list()

    def _refresh_list(self) -> None:
        self._list.DeleteAllItems()
        for row in self._rows:
            idx = self._list.InsertItem(self._list.GetItemCount(), row.info.display_name())
            self._list.SetItem(idx, 1, format_timestamp(row.info.mtime))
            self._list.SetItem(idx, 2, row.info.project_dir)
            self._list.SetItem(idx, 3, os.path.basename(row.info.rel_path))
        if self._list.GetItemCount() > 0:
            self._list.Select(0)
            self._list.Focus(0)
            self._list.SetFocus()

    def selected(self) -> Optional[ConversationInfo]:
        idx = self._list.GetFirstSelected()
        if idx < 0 or idx >= len(self._rows):
            return None
        return self._rows[idx].info


# ---------------------------------------------------------------------------
# Reorder-tabs dialog
# ---------------------------------------------------------------------------


class ReorderTabsDialog(wx.Dialog):
    """Modal that lets the user shuffle open notebook tabs up/down.

    Layout: a tall list of tab names + Move-up / Move-down buttons.
    Public output: ``self.new_order`` -- a list of *original* indices
    in the order they should end up after the user clicks OK.
    """

    def __init__(
        self,
        parent: wx.Window,
        names: list[str],
        current: int,
    ) -> None:
        super().__init__(
            parent,
            title="Reorder conversations",
            size=(520, 460),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        # ``self._order`` is a permutation of range(len(names)); each
        # entry is the *original* tab index.  We keep the labels in a
        # parallel list so the ListBox can rebuild from a single source
        # of truth after every move.
        self._order: list[int] = list(range(len(names)))
        self._labels: list[str] = list(names)
        self.new_order: list[int] = list(self._order)
        self._build_ui(current)

    def _build_ui(self, current: int) -> None:
        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=(
                "Use Up/Down to choose a tab, then Move &up / Move &down "
                "(Alt+Up / Alt+Down) to shuffle.\n"
                "Press OK to apply."
            ),
        )
        vbox.Add(intro, 0, wx.ALL, 6)

        body = wx.BoxSizer(wx.HORIZONTAL)
        self._list = wx.ListBox(
            panel,
            choices=self._labels,
            style=wx.LB_SINGLE,
            name="Tab order",
        )
        body.Add(self._list, 1, wx.EXPAND | wx.RIGHT, 6)

        btn_col = wx.BoxSizer(wx.VERTICAL)
        self._btn_up = wx.Button(panel, label="Move &up")
        self._btn_down = wx.Button(panel, label="Move &down")
        btn_col.Add(self._btn_up, 0, wx.BOTTOM | wx.EXPAND, 4)
        btn_col.Add(self._btn_down, 0, wx.BOTTOM | wx.EXPAND, 4)
        body.Add(btn_col, 0, wx.ALIGN_TOP)

        vbox.Add(body, 1, wx.ALL | wx.EXPAND, 6)

        btns = wx.StdDialogButtonSizer()
        ok = wx.Button(panel, wx.ID_OK, "&OK")
        ok.SetDefault()
        btns.AddButton(ok)
        btns.AddButton(wx.Button(panel, wx.ID_CANCEL, "&Cancel"))
        btns.Realize()
        vbox.Add(btns, 0, wx.ALL | wx.EXPAND, 6)

        panel.SetSizer(vbox)

        # Outer sizer ensures the panel fills the dialog.
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        self._btn_up.Bind(wx.EVT_BUTTON, lambda _e: self._move(-1))
        self._btn_down.Bind(wx.EVT_BUTTON, lambda _e: self._move(+1))
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)

        if 0 <= current < len(self._labels):
            self._list.SetSelection(current)
        elif self._labels:
            self._list.SetSelection(0)
        self._update_button_state()
        self._list.SetFocus()

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        # Alt+Up/Alt+Down provide a one-key way to nudge the current
        # entry; this matches what most file-managers do for similar
        # reorder dialogs.
        if event.AltDown() and event.GetKeyCode() == wx.WXK_UP:
            self._move(-1)
            return
        if event.AltDown() and event.GetKeyCode() == wx.WXK_DOWN:
            self._move(+1)
            return
        event.Skip()

    def _move(self, delta: int) -> None:
        sel = self._list.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        new_idx = sel + delta
        if new_idx < 0 or new_idx >= len(self._order):
            # Spec: don't wrap; first-tab Up = no-op, last-tab Down = no-op.
            return
        self._order[sel], self._order[new_idx] = (
            self._order[new_idx],
            self._order[sel],
        )
        self._labels[sel], self._labels[new_idx] = (
            self._labels[new_idx],
            self._labels[sel],
        )
        self._list.Set(self._labels)
        self._list.SetSelection(new_idx)
        self._list.SetFocus()
        self._update_button_state()

    def _update_button_state(self) -> None:
        sel = self._list.GetSelection()
        self._btn_up.Enable(sel != wx.NOT_FOUND and sel > 0)
        self._btn_down.Enable(
            sel != wx.NOT_FOUND and sel < len(self._order) - 1
        )

    def _on_ok(self, event: wx.CommandEvent) -> None:
        self.new_order = list(self._order)
        event.Skip()


# ---------------------------------------------------------------------------
# Uncommitted-prompt dialog (raised by the launcher's .needs_decision state)
# ---------------------------------------------------------------------------


class UncommittedPromptDialog(wx.Dialog):
    """Modal asking the user how to handle a "user is mid-typing" stall.

    The launcher saw that claude's TUI input buffer is non-empty when
    we asked it to deliver a message.  We don't want to silently
    overwrite whatever the user was typing, so we surface this dialog
    and let them choose:

    * **Cancel (keep my message)** -- default.  The pending file is
      removed and the GUI restores the message into the edit box so
      the user can resend later.

    * **Erase claude's prompt and send** -- the GUI rewrites the file
      with ``force_clear: true`` and renames it back to ``<id>.json``;
      the launcher then sends Ctrl+E + Ctrl+U + a backspace flood
      before injecting the message bytes.

    Public output:

    * ``self.decision``: ``wx.ID_OK`` (erase-and-send) or
      ``wx.ID_CANCEL`` (default).
    """

    def __init__(self, parent: wx.Window, reason: str) -> None:
        super().__init__(
            parent,
            title="Claude has uncommitted text",
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self.decision: int = wx.ID_CANCEL
        self._reason = reason or "user_has_uncommitted_text"
        self._build_ui()
        self.Fit()
        # Default to Cancel -- pressing Enter / Esc both keep the
        # user's message safe.
        self.SetEscapeId(wx.ID_CANCEL)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=(
                "You appear to be typing in claude's terminal.\n"
                "clacogui can't safely insert your message without\n"
                "overwriting what's already in claude's prompt."
            ),
        )
        vbox.Add(intro, 0, wx.ALL, 8)

        # Multi-line read-only ctrl so screen readers can step through
        # the explanation; same trick as PermissionRequestDialog.
        detail = wx.TextCtrl(
            panel,
            value=(
                "Reason from launcher: "
                f"{self._reason}\n\n"
                "Choose Cancel to keep what you typed in clacogui's "
                "send box (you can resend later) or Erase to clear "
                "claude's prompt and send your message anyway."
            ),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL,
            name="Details",
        )
        # 5 visible rows; small min size so it doesn't dominate.
        detail.SetMinSize((520, -1))
        vbox.Add(detail, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_cancel = wx.Button(
            panel, wx.ID_CANCEL, "&Cancel (keep my message)"
        )
        btn_erase = wx.Button(
            panel, wx.ID_OK, "&Erase claude's prompt and send"
        )
        btn_cancel.SetDefault()
        btn_row.AddStretchSpacer(1)
        btn_row.Add(btn_cancel, 0, wx.RIGHT, 6)
        btn_row.Add(btn_erase, 0)
        vbox.Add(btn_row, 0, wx.ALL | wx.EXPAND, 8)

        panel.SetSizer(vbox)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        btn_cancel.Bind(wx.EVT_BUTTON, lambda _e: self._end(wx.ID_CANCEL))
        btn_erase.Bind(wx.EVT_BUTTON, lambda _e: self._end(wx.ID_OK))

    def _end(self, code: int) -> None:
        self.decision = code
        self.EndModal(code)


# ---------------------------------------------------------------------------
# Conversation panel  (one tab in the notebook)
# ---------------------------------------------------------------------------


class ConversationPanel(wx.Panel):
    """Two-pane viewer for a single open conversation.

    Left:  wx.ListBox with cropped previews of every user prompt.
    Right: wx.html2.WebView showing the selected user prompt + reply.
    F6 / Shift+F6 swap focus between the two panes.
    """

    def __init__(
        self,
        parent: wx.Window,
        backend: FsBackend,
        rel_path: str,
    ) -> None:
        super().__init__(parent, name="ConversationPanel")
        self.backend = backend
        self.rel_path = rel_path
        self._data: ConversationData = ConversationData()
        self._signature: Optional[tuple[float, int]] = None
        self._poll_failures = 0
        self._poll_quiet_count = 0
        self._poll_started_at: Optional[float] = None
        self._loading = False
        self._polling = False
        # Assistant entry uuids already merged into a turn's response.  Used by
        # ``_apply_incremental`` to skip re-appending chunks if the byte offset
        # ever regresses and re-reads an overlapping tail (which otherwise
        # duplicates whole assistant messages in the live view).
        self._seen_assistant_uuids: set[str] = set()
        # Tracks the last URL we fed to ``WebView.SetPage`` and the last
        # inner HTML we rendered for that URL.  When the same turn ticks
        # again (assistant streamed more text), we ``RunScript`` an
        # innerHTML swap instead of reloading the whole document, which
        # would otherwise force the screen reader back to the top.
        self._last_doc_url: Optional[str] = None
        self._last_inner_html: Optional[str] = None
        # Outgoing-message state: id -> tracking dict.  See
        # ``_on_send`` for the dict shape and the state machine in
        # ``handle_outgoing_snapshot``.
        self._pending_sends: dict[str, dict] = {}
        # Monotonic tick counter, bumped at the top of every
        # ``handle_outgoing_snapshot`` call.  Used to prove that a
        # given snapshot was scheduled *after* a specific write
        # committed: because the IO worker is a single FIFO thread
        # shared between write and list-directory jobs, any snapshot
        # tick whose seq is strictly greater than the seq recorded
        # when the write's ``on_done`` fired must have been enqueued
        # after the write completed -- so if that snapshot doesn't
        # list our file, the launcher has already consumed it and we
        # can safely mark the send delivered.  Without this, a fast
        # launcher that consumes the file in <5s never lets the GUI
        # observe the file on disk, and the send sits in "submitting"
        # for 300s before the timeout guard rings the error bell.
        self._snapshot_seq: int = 0
        # Set to True by ``interrupt_claude``; consumed (and reset) by the
        # next ``_on_send`` so the outgoing envelope carries
        # ``force_clear=True``.  Rationale: an interrupt leaves whatever
        # the user had partly typed (or that this GUI had partly injected)
        # sitting in claude's TUI input buffer.  The launcher's
        # ``_can_inject_now`` gate cannot always see it because the
        # buffer estimator drifts across an Esc interrupt.  Forcing an
        # erasure on the very next send is the reliable fix; the
        # launcher already implements ``force_clear`` as Ctrl+E + Ctrl+U
        # + backspace flood before the message body.
        self._next_send_force_clear: bool = False
        # Latest session alias from ``~/.claude/sessions/*.json``
        # (the ``name`` the user set with ``/rename aaa1``).  Pushed
        # in by ``MainFrame._push_session_aliases`` on every poll
        # tick.  ``None`` means "no alias known" (either no metadata
        # file matches our session_id, or we haven't polled yet).
        # ``conversation_name`` prefers this over ``_data.name``
        # because the user's explicit ``/rename`` is a stronger
        # signal than any heuristic we can derive from the JSONL.
        self._session_alias: Optional[str] = None
        self._build_ui()
        self.reload(initial=True)

    @property
    def display_path(self) -> str:
        return self.backend.display_path(self.rel_path)

    # -- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        self.splitter = wx.SplitterWindow(
            self,
            style=wx.SP_LIVE_UPDATE | wx.SP_3D,
        )
        self.splitter.SetMinimumPaneSize(180)

        self._left_panel = wx.Panel(self.splitter)
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        left_label = wx.StaticText(
            self._left_panel,
            label=(
                "Your &messages "
                "(Up/Down to choose; F2 list / F4 content / F5 send)"
            ),
        )
        left_sizer.Add(left_label, 0, wx.ALL, 4)
        self.list_box = wx.ListBox(
            self._left_panel,
            style=wx.LB_SINGLE | wx.LB_NEEDED_SB,
            name="User messages",
        )
        # Prevent the visible flicker when ``_refresh_list`` rebuilds via
        # ``Set(previews)`` -- wx paints an intermediate empty state on
        # some Windows themes, which reads as a full-list flash to the eye.
        try:
            self.list_box.SetDoubleBuffered(True)
        except Exception:
            pass
        left_sizer.Add(self.list_box, 1, wx.EXPAND | wx.ALL, 4)
        self._left_panel.SetSizer(left_sizer)

        self._right_panel = wx.Panel(self.splitter)
        right_sizer = wx.BoxSizer(wx.VERTICAL)
        right_label = wx.StaticText(
            self._right_panel,
            label=(
                "Conversation content "
                "(F2 list / F4 content / F5 send)"
            ),
        )
        right_sizer.Add(right_label, 0, wx.ALL, 4)
        self.webview = _make_webview(self._right_panel)
        right_sizer.Add(self.webview, 1, wx.EXPAND | wx.ALL, 4)
        self._right_panel.SetSizer(right_sizer)

        self.splitter.SplitVertically(self._left_panel, self._right_panel, 320)

        # Send-to-claude composer below the splitter.  Drops a JSON
        # envelope into ``clacogui_outgoing/`` for clacogui_launcher.py
        # to type into claude's TUI (one os.write of the message
        # bytes, with embedded LF acting as Ctrl+J).
        self._send_panel = wx.Panel(self, name="Send box")
        send_sizer = wx.BoxSizer(wx.VERTICAL)
        send_label = wx.StaticText(
            self._send_panel,
            label=(
                "Send a &message to Claude "
                "(Enter sends; Shift+Enter inserts a newline)"
            ),
        )
        send_sizer.Add(send_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)

        edit_row = wx.BoxSizer(wx.HORIZONTAL)
        # ``TE_PROCESS_ENTER`` so we get ``EVT_TEXT_ENTER`` and can
        # treat plain Enter as "submit" instead of inserting a newline.
        # ``TE_MULTILINE`` keeps the field expandable; we manually
        # honour Shift+Enter in ``_on_send_keydown`` to mean "newline".
        self._send_input = wx.TextCtrl(
            self._send_panel,
            style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER,
            name="Send to Claude",
        )
        # ~3 visible rows on most platforms.
        self._send_input.SetMinSize((-1, 70))
        edit_row.Add(self._send_input, 1, wx.EXPAND | wx.RIGHT, 4)
        self._send_button = wx.Button(self._send_panel, label="&Send")
        edit_row.Add(self._send_button, 0, wx.ALIGN_CENTER_VERTICAL)
        # Interrupt button -- writes an ``action: interrupt`` envelope
        # the launcher turns into a single Esc keystroke.  Always
        # enabled (Esc is idempotent in claude's TUI -- see launcher
        # docstring), so the user doesn't have to second-guess
        # whether claude is busy enough to warrant an interrupt.
        self._interrupt_button = wx.Button(
            self._send_panel, label="&Interrupt"
        )
        self._interrupt_button.SetToolTip(
            "Send Esc to claude to cancel its current turn (Ctrl+.)."
        )
        edit_row.Add(
            self._interrupt_button, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 4
        )
        send_sizer.Add(edit_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        # Send status used to live in a wx.StaticText right under the
        # edit box, but StaticText updates aren't announced by screen
        # readers (no automation event fires when SetLabel is called),
        # which made the "Queued" / "Sent" feedback effectively
        # invisible to NVDA/JAWS users.  We now keep just a plain
        # string here, route it to status-bar field 1 (which screen
        # readers can navigate to and which most readers do
        # auto-announce), and lose the StaticText entirely.
        self._last_send_status: str = "Idle"

        self._send_panel.SetSizer(send_sizer)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.splitter, 1, wx.EXPAND)
        outer.Add(self._send_panel, 0, wx.EXPAND)
        self.SetSizer(outer)

        # Send-box bindings.  EVT_TEXT_ENTER fires for plain Enter
        # *because* TE_PROCESS_ENTER is set; we don't Skip() so the
        # control never inserts the newline.  Shift+Enter is
        # intercepted in _on_send_keydown to mean "insert newline"
        # without triggering EVT_TEXT_ENTER.
        self._send_input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
        self._send_input.Bind(wx.EVT_KEY_DOWN, self._on_send_keydown)
        # On Windows, wx.TextCtrl only recognises CRLF as a line break for
        # display purposes.  Pasted content originating from Linux (LF-only)
        # ends up on a single line in the box even though the underlying
        # buffer has the newlines -- confusing when the user expected to see
        # the paste laid out.  Intercept paste and normalise to \r\n before
        # the control ingests it.  (Enter typed in-app still inserts a
        # single \n via _on_send_keydown; that path is already fine because
        # wx accepts \n from keystroke input just not from clipboard blobs.)
        self._send_input.Bind(wx.EVT_TEXT_PASTE, self._on_send_paste)
        self._send_button.Bind(wx.EVT_BUTTON, self._on_send)
        self._interrupt_button.Bind(
            wx.EVT_BUTTON, lambda _e: self.interrupt_claude()
        )

        self.list_box.Bind(wx.EVT_LISTBOX, self._on_select)
        # F6 / Shift+F6 work even with focus inside the list.
        for ctrl in (self.list_box, self._right_panel, self._left_panel):
            ctrl.Bind(wx.EVT_KEY_DOWN, self._on_keydown)

        # The WebView's native control swallows keyboard input before wx can
        # turn it into accelerators.  We work around this with the JS in
        # render._KEY_FORWARDER_JS, which navigates to ``clacogui-action://x``;
        # we intercept that here.
        self.webview.Bind(
            wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_webview_navigating
        )

    def _on_keydown(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_F6:
            self.toggle_pane(reverse=event.ShiftDown())
            return
        event.Skip()

    # -- Public actions -----------------------------------------------------

    def toggle_pane(self, reverse: bool = False) -> None:
        """Move focus between the message list and the WebView."""
        if self.list_box.HasFocus():
            self.webview.SetFocus()
        else:
            self.list_box.SetFocus()

    def conversation_name(self) -> str:
        """Best label for the tab title and window title.

        Priority:

        1. ``_session_alias`` -- the ``/rename aaa1`` value from
           ``~/.claude/sessions/<pid>.json``.  This is the user's
           own explicit choice, so it wins over any heuristic.
        2. ``_data.name`` -- derived by ``parse_conversation`` from
           the JSONL transcript (custom-title / agent-name / first
           turn preview).
        3. JSONL filename (the bare UUID) -- last-resort fallback.
        """
        if self._session_alias:
            return self._session_alias
        return self._data.name or os.path.splitext(
            os.path.basename(self.rel_path)
        )[0]

    def claude_session_id(self) -> Optional[str]:
        """Return this conversation's claude session_id, if known.

        Used by ``MainFrame._refresh_claude_status_field`` to look up
        the live ``status`` for *this* tab's conversation in the
        per-poll session-metadata map.  Returns ``None`` until the
        first ``reload`` populates ``self._data``.
        """
        sid = getattr(self._data, "session_id", None)
        return sid if isinstance(sid, str) and sid else None

    def set_session_alias(self, alias: Optional[str]) -> None:
        """Record the rename-alias for this conversation, if any.

        Called from ``MainFrame._push_session_aliases`` once per
        poll tick.  No-ops when the alias hasn't changed (avoids
        a redundant tab-title repaint storm), otherwise stores it
        and posts an :data:`EVT_NAME_CHANGED` so the frame's tab
        label and window title pick up the new name through the
        same code path that handles JSONL-derived name changes.
        """
        if not isinstance(alias, str) or not alias:
            new = None
        else:
            new = alias
        if new == self._session_alias:
            return
        self._session_alias = new
        self._notify_name_change()

    def selected_turn(self) -> Optional[Turn]:
        idx = self.list_box.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._data.turns):
            return None
        return self._data.turns[idx]

    # -- Data / polling -----------------------------------------------------

    def reload(self, initial: bool = False, feedback: bool = False) -> None:
        """Re-read the JSONL file from disk on the IO worker thread.

        The UI thread keeps spinning while the read happens.  IO errors
        surface a Retry/Quit dialog because the user is waiting on this.

        If ``feedback`` is True, the parent frame's status bar shows
        ``Refreshing...`` before the read is submitted and a confirmation
        message once the IO callback fires.  This is what makes ``F5`` /
        ``Ctrl+R`` provably round-trip through the worker rather than just
        being fire-and-forget.
        """
        if self._loading:
            if feedback:
                self._set_status("Refresh already in progress, please wait...")
            return
        self._loading = True
        backend = self.backend
        rel_path = self.rel_path
        basename = os.path.basename(rel_path)
        op = f"reading conversation {basename}"

        if feedback:
            self._set_status(f"Refreshing {basename}...")

        def work():
            return parse_conversation(backend, rel_path), file_signature(backend, rel_path)

        def on_success(result):
            if not self:
                return
            self._loading = False
            data, sig = result
            self._data = data
            self._signature = sig
            self._seen_assistant_uuids = set(data.assistant_uuids)
            self._refresh_list(preserve_selection=not initial)
            self._refresh_html()
            self._notify_name_change()
            if feedback:
                stamp = time.strftime("%H:%M:%S")
                self._set_status(
                    f"Refreshed at {stamp} - {len(data.turns)} message(s)",
                    revert_after_ms=5000,
                )

        def on_give_up(_exc):
            if not self:
                return
            self._loading = False
            if feedback:
                self._set_status(
                    "Refresh failed; existing data unchanged.",
                    revert_after_ms=5000,
                )
            top = wx.GetApp().GetTopWindow() if wx.GetApp() else None
            if top is not None and not initial:
                # User-initiated reload that they declined to retry: just
                # leave the existing data in place rather than tearing down.
                return
            if top is not None:
                top.Close(force=True)

        submit_io_with_retry(self, op, work, on_success, on_give_up=on_give_up)

    def _set_status(self, message: str, revert_after_ms: int = 0) -> None:
        """Write a transient message to the parent frame's status bar.

        Screen readers re-announce the bar when its text changes, so this
        is also how we make a successful refresh audible.
        """
        frame = self.GetTopLevelParent()
        if not isinstance(frame, MainFrame):
            return
        frame.show_transient_status(message, revert_after_ms=revert_after_ms)

    def poll(self) -> None:
        """Tick called every second by ``MainFrame``.

        Cheap-checks ``stat()`` first, only re-parses if size or mtime
        changed.  Skips entirely if a previous tick is still running, so a
        slow share never piles up jobs.  All errors are swallowed and logged.
        """
        if self._loading:
            return
        if self._polling:
            # If we've been "polling" for unreasonably long, assume the
            # callback was lost (this should never happen but guard anyway)
            # and free the flag so we don't deadlock the panel forever.
            stuck_for = time.monotonic() - (self._poll_started_at or 0.0)
            if self._poll_started_at is not None and stuck_for > 30.0:
                log.warning(
                    "Forcing polling flag clear for %s; stuck for %.1fs",
                    self.rel_path,
                    stuck_for,
                )
                self._polling = False
            else:
                return
        self._polling = True
        self._poll_started_at = time.monotonic()

        backend = self.backend
        rel_path = self.rel_path
        prev_sig = self._signature
        # For incremental reads: if we have existing data and the previous
        # size is known, we can read only the new bytes.
        prev_size = prev_sig[1] if prev_sig else 0

        def work():
            sig = file_signature(backend, rel_path)
            if sig is None or sig == prev_sig:
                return (sig, None, None)  # nothing to re-parse
            new_size = sig[1]
            # Incremental: if file grew (append-only) and we have prior data,
            # read only the tail.
            if prev_size > 0 and new_size > prev_size:
                tail_text = backend.read_text_from(rel_path, prev_size)
                return (sig, None, tail_text)
            # Full re-read (file shrank / first load / truncated).
            data = parse_conversation(backend, rel_path)
            return (sig, data, None)

        def callback(result, exc):
            if not self:
                return
            self._polling = False
            self._poll_started_at = None
            if exc is not None:
                self._poll_failures += 1
                if self._poll_failures % 30 == 1:
                    log.warning(
                        "Polling failed for %s (%d): %s",
                        self.rel_path,
                        self._poll_failures,
                        exc,
                    )
                return
            self._poll_failures = 0
            sig, data, tail_text = result
            if sig is None:
                log.debug("Poll: stat returned None for %s", self.rel_path)
                return
            if sig == prev_sig:
                self._poll_quiet_count += 1
                if self._poll_quiet_count % 30 == 0:
                    log.debug(
                        "Poll: %d quiet ticks for %s, sig=%r",
                        self._poll_quiet_count,
                        self.rel_path,
                        sig,
                    )
                return
            self._poll_quiet_count = 0

            if tail_text is not None:
                # Incremental update: parse only the new lines.
                self._apply_incremental(tail_text, sig)
                return

            if data is None:
                log.warning(
                    "Poll: sig changed but no data for %s (prev=%r new=%r)",
                    self.rel_path, prev_sig, sig,
                )
                return

            log.info(
                "Poll: %s full reload prev=%r new=%r turns=%d",
                self.rel_path, prev_sig, sig, len(data.turns),
            )

            old_count = len(self._data.turns)
            old_name = self._data.name
            old_selection = self.list_box.GetSelection()
            self._data = data
            self._signature = sig
            self._seen_assistant_uuids = set(data.assistant_uuids)

            self._refresh_list(preserve_selection=True)
            if (
                old_count > 0
                and old_selection == old_count - 1
                and len(data.turns) > old_count
            ):
                self.list_box.SetSelection(len(data.turns) - 1)

            self._refresh_html()

            if data.name != old_name:
                self._notify_name_change()

        submit_io(work, callback)

    def _apply_incremental(self, tail_text: str, sig: tuple[float, int]) -> None:
        """Parse appended JSONL lines and merge into existing conversation data."""
        from models import _extract_user_text, _extract_assistant_text

        old_count = len(self._data.turns)
        old_selection = self.list_box.GetSelection()
        last_turn = self._data.turns[-1] if self._data.turns else None

        # The byte offset we resume from next time MUST equal exactly the
        # bytes we consume here.  The stat size in ``sig`` was taken before
        # ``read_text_from`` ran, and the file is being appended to live, so
        # the tail we just read can extend *past* that stat size.  Trusting
        # ``sig[1]`` as the next offset would re-read (and re-parse) the
        # overlap on the following poll -- that is what duplicates paragraphs
        # and whole messages.  Instead we track how many bytes we actually
        # consume and store that as the signature size.
        prev_size = self._signature[1] if self._signature else 0

        # Only consume up to the last complete line.  The tail read can stop
        # mid-line while Claude is still writing a JSONL record; that partial
        # line must be left unconsumed so it is re-read and completed next
        # poll, rather than parsed-and-dropped here.
        last_nl = tail_text.rfind("\n")
        if last_nl < 0:
            # No complete line yet; consume nothing, leave offset unchanged so
            # we retry from the same place once more bytes arrive.
            self._signature = (sig[0], prev_size)
            return
        complete_text = tail_text[: last_nl + 1]
        consumed_bytes = len(complete_text.encode("utf-8"))

        # Belt-and-suspenders dedup: never create a second turn for a user
        # uuid we already have (guards against any future offset regression).
        seen_user_uuids = {t.user_uuid for t in self._data.turns if t.user_uuid}

        for line in complete_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue

            etype = entry.get("type")
            if etype == "custom-title":
                self._data.custom_title = entry.get("customTitle") or self._data.custom_title
                continue
            if etype == "agent-name":
                self._data.agent_name = entry.get("agentName") or self._data.agent_name
                continue

            if etype == "user":
                user_text = _extract_user_text(entry)
                if user_text is None:
                    continue
                user_uuid = entry.get("uuid", "")
                if user_uuid and user_uuid in seen_user_uuids:
                    # Already have this turn; don't duplicate it. Keep
                    # last_turn pointing at it so any of its assistant
                    # chunks still attach to the right place.
                    for t in self._data.turns:
                        if t.user_uuid == user_uuid:
                            last_turn = t
                            break
                    continue
                # Finalize previous turn's response if needed.
                new_turn = Turn(
                    order=len(self._data.turns),
                    user_uuid=user_uuid,
                    user_text=user_text,
                    user_timestamp=entry.get("timestamp", ""),
                )
                self._data.turns.append(new_turn)
                if user_uuid:
                    seen_user_uuids.add(user_uuid)
                last_turn = new_turn
                continue

            if etype == "assistant" and last_turn is not None:
                # Skip assistant entries we've already merged; an offset
                # regression can re-deliver the same lines, which would
                # otherwise re-append identical text to the response.
                a_uuid = entry.get("uuid", "")
                if a_uuid and a_uuid in self._seen_assistant_uuids:
                    continue
                chunk = _extract_assistant_text(entry)
                if chunk:
                    if last_turn.response:
                        last_turn.response += "\n\n" + chunk
                    else:
                        last_turn.response = chunk
                    if a_uuid:
                        self._seen_assistant_uuids.add(a_uuid)
                continue

        self._signature = (sig[0], prev_size + consumed_bytes)

        new_count = len(self._data.turns)
        if new_count != old_count:
            self._refresh_list(preserve_selection=True)
            if (
                old_count > 0
                and old_selection == old_count - 1
            ):
                self.list_box.SetSelection(new_count - 1)
        self._refresh_html()

        # Update display name if it changed.
        old_name = self._data.name
        self._data.name = (
            self._data.custom_title
            or self._data.agent_name
            or (self._data.turns[0].preview(60) if self._data.turns else "")
            or self._data.name
        )
        if self._data.name != old_name:
            self._notify_name_change()

        log.info(
            "Poll: %s incremental +%d bytes, turns %d->%d",
            self.rel_path, len(tail_text), old_count, new_count,
        )


    # -- WebView keystroke forwarding --------------------------------------

    def _on_webview_navigating(self, event) -> None:
        """Intercept WebView navigations.

        Two cases are special-cased; everything else is allowed through:

        * ``clacogui-action://`` URLs are the page's keystroke-forwarding
          shim (F6, Ctrl+Tab, ...) -- vetoed and dispatched as actions.
        * Real external links (``http(s)://``, ``mailto:`` ...) are vetoed
          and handed to the system default browser instead of navigating
          away from the rendered transcript inside the embedded WebView.
        """
        try:
            url = event.GetURL() or ""
        except Exception:
            return
        prefix = "clacogui-action://"
        if url.startswith(prefix):
            try:
                event.Veto()
            except Exception:
                log.debug("Could not Veto navigation to %s", url, exc_info=True)
            action = url[len(prefix):].rstrip("/")
            log.debug("WebView action: %s", action)
            self._handle_webview_action(action)
            return

        # Our own rendered content loads under the ``clacogui://`` scheme;
        # let those (and ``about:``/empty) proceed normally.
        low = url.lower()
        if not low or low.startswith("clacogui://") or low.startswith("about:"):
            return

        # Anything else is a user-clicked external link: open it in the
        # system browser and keep the transcript in place.
        if low.startswith(("http://", "https://", "mailto:")):
            try:
                event.Veto()
            except Exception:
                log.debug("Could not Veto external link %s", url, exc_info=True)
            try:
                webbrowser.open(url)
            except Exception:
                log.warning("Could not open external link %s", url, exc_info=True)

    def _handle_webview_action(self, action: str) -> None:
        if action == "toggle-pane":
            self.toggle_pane()
            return
        frame = self.GetTopLevelParent()
        if not isinstance(frame, MainFrame):
            return
        if action == "next-tab":
            frame._switch_tab(+1)
        elif action == "prev-tab":
            frame._switch_tab(-1)
        elif action == "move-tab-prev":
            frame._move_tab(-1)
        elif action == "move-tab-next":
            frame._move_tab(+1)
        elif action == "focus-list":
            frame._focus_pane(False)
        elif action == "focus-html":
            frame._focus_pane(True)
        elif action == "focus-send":
            frame._focus_send_box()
        elif action.startswith("tab-"):
            try:
                n = int(action[4:]) - 1
            except ValueError:
                return
            frame._goto_tab(n)
        elif action.startswith("alt-key/"):
            letter = action[len("alt-key/"):]
            frame._activate_menu_mnemonic(letter)
        elif action == "open":
            frame._on_open(None)
        elif action == "close-tab":
            frame._on_close_tab(None)
        elif action == "reload":
            frame._on_reload(None)
        elif action == "interrupt-claude":
            frame._on_interrupt_claude(None)
        else:
            log.debug("Unknown WebView action: %r", action)

    # -- Internals ----------------------------------------------------------

    def _notify_name_change(self) -> None:
        # The MainFrame listens for this and updates the notebook tab label.
        evt = _NameChangedEvent(self.GetId())
        evt.SetEventObject(self)
        wx.PostEvent(self.GetParent(), evt)

    def _refresh_list(self, preserve_selection: bool = False) -> None:
        prev_uuid: Optional[str] = None
        if preserve_selection:
            sel = self.list_box.GetSelection()
            if 0 <= sel < len(self._data.turns):
                prev_uuid = self._data.turns[sel].user_uuid

        previews = [t.preview(120) for t in self._data.turns]
        # ``Set`` rebuilds the list in one shot, which is faster and avoids
        # flicker that screen readers find irritating.
        self.list_box.Set(previews)

        if not previews:
            return

        new_idx = 0
        if prev_uuid is not None:
            for i, turn in enumerate(self._data.turns):
                if turn.user_uuid == prev_uuid:
                    new_idx = i
                    break
        self.list_box.SetSelection(new_idx)

    def _doc_url(self, turn: Optional[Turn]) -> str:
        """Stable URL for the WebView so screen readers can track location.

        wx.html2.WebView's ``baseUrl`` argument becomes the document URL
        exposed via UIA / IAccessible2.  We use a custom ``clacogui://``
        scheme so it never collides with anything the WebView would try to
        actually load.
        """
        sid = self._data.session_id or _slug_from_rel(self.rel_path)
        if turn is None:
            if not self._data.turns:
                return f"clacogui://{sid}/empty"
            return f"clacogui://{sid}/none"
        return f"clacogui://{sid}/message/{turn.order}"

    def _refresh_html(self) -> None:
        """Render the current selection into the WebView.

        Two paths:

          * **Same URL as last render** -> JS-only update: encode the
            new inner HTML as a JSON string and ``RunScript`` it into
            the existing page's content div.  No document reload, so
            the screen reader keeps its caret position.

          * **Different URL** (selection changed, or first paint):
            full ``SetPage`` with a stable URL so IAccessible2's
            documentURL stays meaningful.
        """
        turn = self.selected_turn()
        if turn is None:
            inner_html = (
                "<h1>clacogui</h1><p>"
                + (
                    "This conversation has no user messages yet."
                    if not self._data.turns
                    else "Choose a message on the left."
                )
                + "</p>"
            )
        else:
            inner_html = render_turn_inner_html(turn)
        url = self._doc_url(turn)

        if url == self._last_doc_url and inner_html == self._last_inner_html:
            return  # no change at all

        if url == self._last_doc_url and self._last_inner_html is not None:
            if self._inject_inner_html(inner_html):
                self._last_inner_html = inner_html
                return
            # JS injection failed -- fall through to a full reload.

        if turn is None:
            full = render_blank_html(
                "This conversation has no user messages yet."
                if not self._data.turns
                else "Choose a message on the left."
            )
        else:
            full = render_turn_html(turn)

        # ``SetPage`` on the Edge WebView backend reliably grabs
        # keyboard focus once the page finishes loading, even though
        # the user got here by navigating the message list (Up/Down)
        # or because of a background polling refresh.  Capture who
        # owned focus before the load and re-assert it afterwards if
        # (and only if) the WebView ended up stealing it.
        prev_focus = self._focus_holder_safely()
        prev_was_in_webview = self._is_inside_webview(prev_focus)
        try:
            self.webview.SetPage(full, url)
            self._last_doc_url = url
            self._last_inner_html = inner_html
        except Exception:
            log.exception("WebView SetPage failed")
            self._last_doc_url = None
            self._last_inner_html = None
            return

        if prev_focus is not None and not prev_was_in_webview:
            # Two-tier restore: ``CallAfter`` covers the synchronous
            # case where the WebView grabs focus inside SetPage, and
            # the deferred ``CallLater`` covers the Edge backend's
            # async navigation that completes a frame or two later.
            wx.CallAfter(
                self._restore_focus_if_webview_stole, prev_focus
            )
            wx.CallLater(
                120, self._restore_focus_if_webview_stole, prev_focus
            )

    def _inject_inner_html(self, inner_html: str) -> bool:
        """Replace ``#turn-content``'s innerHTML via ``RunScript``.

        Returns True on success.  Wrapped in a try/except because the
        Edge backend occasionally errors out before the document is
        ready to receive scripts; we then fall back to a full reload.
        """
        content_id = _render_content_id()
        # Encode the HTML payload as a JSON string so embedded quotes,
        # backslashes, and newlines round-trip safely through JS.
        encoded = json.dumps(inner_html, ensure_ascii=False)
        # </script> inside encoded would otherwise terminate any script
        # block that ever embeds this; not used here but cheap to be
        # defensive.
        encoded = encoded.replace("</", "<\\/")
        script = (
            "(function(html){"
            f"var el = document.getElementById('{content_id}');"
            "if (!el) return false;"
            "el.innerHTML = html;"
            "return true;"
            f"}})({encoded})"
        )
        try:
            ok = self.webview.RunScript(script)
        except Exception:
            log.exception("WebView RunScript failed")
            return False
        # wxPython's RunScript signature varies by backend: some return
        # ``bool`` (True on success), others ``(bool, str)``.  Treat any
        # truthy result as success.
        if isinstance(ok, tuple):
            ok = bool(ok and ok[0])
        if not ok:
            log.debug("RunScript returned falsy; will full-reload")
            return False
        return True

    def _focus_holder_safely(self) -> Optional[wx.Window]:
        """``wx.Window.FindFocus()`` but tolerant of partial teardown."""
        try:
            return wx.Window.FindFocus()
        except Exception:
            return None

    def _is_inside_webview(self, w: Optional[wx.Window]) -> bool:
        """True if ``w`` is the WebView itself or any descendant of it.

        wx.html2.WebView wraps a native control whose actual focused
        descendant may be an internal child window, not the
        WebView object we created.  Walking parents lets us answer
        "did focus end up inside the WebView?" reliably.
        """
        if w is None:
            return False
        try:
            cur = w
            for _ in range(64):  # cap walk; sane parents never get this deep
                if cur is self.webview:
                    return True
                parent = cur.GetParent()
                if parent is None or parent is cur:
                    return False
                cur = parent
        except Exception:
            return False
        return False

    def _restore_focus_if_webview_stole(
        self, target: wx.Window
    ) -> None:
        """Re-focus ``target`` iff the WebView grabbed focus from it.

        If the user has explicitly moved focus elsewhere since
        ``_refresh_html`` was called (e.g. clicked the Send button,
        pressed F5, tabbed into another control), respect that and
        do nothing.  We only act when focus is currently inside the
        WebView -- i.e. it really was stolen.
        """
        if not self or not target:
            return
        cur = self._focus_holder_safely()
        if not self._is_inside_webview(cur):
            return
        try:
            target.SetFocus()
        except Exception:
            log.debug("focus restore failed", exc_info=True)

    def _on_select(self, event: wx.CommandEvent) -> None:
        self._refresh_html()

    # -- Send box / launcher protocol --------------------------------------

    def _set_send_status(self, message: str) -> None:
        """Update this panel's send status and mirror it into the
        frame's status bar (field 1).

        Always called on the wx main thread.  If the panel was already
        destroyed (tab closed) we silently no-op.  The frame is
        responsible for ignoring updates from non-active panels --
        we always remember the latest message here so a tab switch
        can repopulate the status bar with our state.
        """
        if not self:
            return
        self._last_send_status = message
        top = wx.GetTopLevelParent(self)
        refresh = getattr(top, "refresh_send_status_field", None)
        if callable(refresh):
            try:
                refresh(self)
            except Exception:
                log.debug(
                    "refresh_send_status_field crashed", exc_info=True
                )

    def _on_send_paste(self, event: wx.CommandEvent) -> None:
        """Normalise \\n / \\r line endings to \\r\\n on paste.

        wx.TextCtrl on Windows only recognises CRLF as a display line
        break; a Linux-origin LF-only paste otherwise collapses to one
        visible line.  We read the clipboard ourselves, rewrite the
        newlines, and let the default paste run with the rewritten
        content in the clipboard.  If anything goes wrong we fall
        through to the default paste unchanged.
        """
        try:
            if not wx.TheClipboard.Open():
                event.Skip()
                return
            try:
                data = wx.TextDataObject()
                if not wx.TheClipboard.GetData(data):
                    event.Skip()
                    return
                original = data.GetText()
                # Normalise: CRLF -> CRLF (no-op), lone CR -> LF, then LF -> CRLF.
                if "\n" not in original and "\r" not in original:
                    event.Skip()
                    return
                normalised = (
                    original.replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .replace("\n", "\r\n")
                )
                if normalised == original:
                    event.Skip()
                    return
                wx.TheClipboard.SetData(wx.TextDataObject(normalised))
            finally:
                wx.TheClipboard.Close()
        except Exception:
            log.debug("paste normalisation failed; falling back", exc_info=True)
        event.Skip()

    def _on_send_keydown(self, event: wx.KeyEvent) -> None:
        """Honour Shift+Enter as 'insert newline' inside the send box.

        Plain Enter is routed via ``TE_PROCESS_ENTER`` to
        ``EVT_TEXT_ENTER`` -> ``_on_send`` (i.e. Enter sends).  When
        Shift is held we intercept the keydown here, call
        ``WriteText('\\n')`` ourselves, and *do not* call
        ``event.Skip()``: that prevents the underlying control from
        ever firing ``EVT_TEXT_ENTER``, so Shift+Enter never sends.

        Note: claude's TUI uses Ctrl+J for newline; Shift+Enter is
        only the GUI-side choice (per user request).  The launcher
        translates this newline into Ctrl+J when injecting into
        claude's prompt -- see ``_build_inject_segments`` in
        ``clacogui_launcher.py``.
        """
        kc = event.GetKeyCode()
        if (
            event.ShiftDown()
            and not event.ControlDown()
            and not event.AltDown()
            and kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
        ):
            self._send_input.WriteText("\n")
            return
        event.Skip()

    def _on_send(self, _event) -> None:
        text = self._send_input.GetValue().rstrip("\r\n")
        if not text.strip():
            self._set_send_status("Type a message first.")
            return
        sid = self._data.session_id
        if not sid:
            self._set_send_status(
                "Cannot send: this conversation has no session id yet."
            )
            return
        backend = self.backend
        if backend is None:
            self._set_send_status("Cannot send: no backend.")
            return

        # Filename pattern matches the agent's notification/request
        # naming so things sort the same way under listings.  uuid4
        # gives us enough entropy that two GUIs hammering the same
        # share won't collide within a millisecond.
        pending_id = (
            f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        )
        rel = f"{AGENT_OUTGOING_DIR}/{pending_id}.json"
        # Consume the "post-interrupt clear" flag exactly once: if the user
        # just interrupted claude, whatever was in its TUI input box before
        # is stale and would otherwise be prepended to our new message.
        force_clear = self._next_send_force_clear
        self._next_send_force_clear = False
        envelope = {
            "id": pending_id,
            "session_id": sid,
            "text": text,
            "force_clear": force_clear,
        }
        body = json.dumps(envelope, indent=2).encode("utf-8")

        # Reserve the tracking slot before the IO so the very next
        # poll tick (which may happen on an unlucky schedule before
        # write_bytes returns) can already see "queued" and not
        # mistakenly mark the send "delivered".
        #
        # ``write_committed`` is the key bit for avoiding a false-
        # positive "Sent" race: the IO worker is a single FIFO thread
        # shared with the directory-listing poll, and an
        # already-running listing job that started just before
        # ``submit_io(work, ...)`` returns names that pre-date our
        # write.  If the snapshot handler treated that as "file
        # missing -> delivered", every send raced into "Sent" without
        # ever hitting the launcher.  We now refuse to flip a pending
        # send out of "queued" until ``write_committed`` is True
        # (i.e. the on_done callback has run, which can only happen
        # after the worker actually wrote the file -- worker is
        # serialised, so any subsequent listing must reflect the
        # post-write state).
        self._pending_sends[pending_id] = {
            "text": text,
            "queued_at": time.monotonic(),
            "last_state": "submitting",
            "dialog_open": False,
            "write_committed": False,
            "saw_visible": False,
        }
        short = pending_id.split("_", 1)[-1]
        self._set_send_status(f"Submitting ({short})...")
        # Empty the input box and put the user back in it so they can
        # keep composing while the launcher delivers this one.
        self._send_input.SetValue("")
        self._send_input.SetFocus()

        # Atomic-write trick: write to ``<id>.json.part`` first, then
        # rename to ``<id>.json``.  Without this, the launcher's 200 ms
        # poll on the *local* FS can catch the file between the FTP
        # server's ``STOR`` opening it (size 0) and the data
        # connection finishing -- ``json.loads`` then sees an empty
        # buffer, the launcher writes ``.failed: could not parse
        # JSON envelope``, and the user sees "Send failed: unknown
        # error".  The launcher already filters its outgoing listing
        # to ``*.json`` (no ``.``-suffixed states), so a ``.part``
        # leaf is invisible to it.
        part_rel = rel + ".part"

        def work():
            # mkdir first; the launcher install creates this directory
            # but a user with an older agent install may not have it.
            try:
                backend.mkdir(AGENT_OUTGOING_DIR, exist_ok=True)
            except OSError as e:
                log.debug("mkdir %s failed: %s", AGENT_OUTGOING_DIR, e)
            backend.write_bytes(part_rel, body)
            backend.rename(part_rel, rel)

        def on_done(_result, exc):
            if not self:
                return
            info = self._pending_sends.get(pending_id)
            if exc is not None:
                log.warning("Send write failed: %s", exc)
                self._pending_sends.pop(pending_id, None)
                self._set_send_status(f"Send failed: {exc}")
                _play_error_sound(source="send_write")
                # Restore the user's text so they can fix and retry.
                self._send_input.SetValue(text)
                return
            log.info(
                "Send write committed: %s (%d bytes) -> %s",
                pending_id, len(body), rel,
            )
            # The file is now on disk and visible to any subsequent
            # backend listing.  Unlock the state machine so the next
            # snapshot can transition queued -> sent.
            if info is not None:
                info["write_committed"] = True
                # Any snapshot with seq strictly greater than this
                # value must have been enqueued after the write
                # committed (FIFO IO worker), so its listing is
                # authoritative.  See the docstring of
                # ``handle_outgoing_snapshot`` for the full argument.
                info["commit_seq"] = self._snapshot_seq

        submit_io_write(work, on_done)

    def interrupt_claude(self) -> None:
        """Drop an ``action: interrupt`` envelope into the outgoing dir.

        The launcher picks the file up on its next 200 ms tick and
        writes a single Esc to claude's PTY master, bypassing all
        gates -- Esc is exactly what you want when claude is busy
        and is idempotent (unlike Ctrl+C, which would exit on the
        second hit).  Fire-and-forget: we don't track this through
        ``_pending_sends`` because there's no useful "delivered"
        feedback beyond "the file vanished from the listing"; we
        immediately tell the user via the send-status field that
        the request was queued, and the launcher's clacogui_launcher.log
        carries the actual delivery confirmation if they need it.
        """
        sid = self._data.session_id
        if not sid:
            self._set_send_status(
                "Cannot interrupt: this conversation has no session id yet."
            )
            return
        backend = self.backend
        if backend is None:
            self._set_send_status("Cannot interrupt: no backend.")
            return

        pending_id = (
            f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        )
        rel = f"{AGENT_OUTGOING_DIR}/{pending_id}.json"
        part_rel = rel + ".part"
        envelope = {
            "id": pending_id,
            "session_id": sid,
            "action": "interrupt",
        }
        body = json.dumps(envelope, indent=2).encode("utf-8")

        short = pending_id.split("_", 1)[-1]
        self._set_send_status(f"Interrupting ({short})...")
        # The user is likely to want to type something else right
        # after interrupting (rephrase, follow up, etc.), so move
        # focus back to the input box.  The Interrupt button itself
        # is reachable via Tab from there.
        try:
            self._send_input.SetFocus()
        except Exception:
            log.debug("focus send box after interrupt failed", exc_info=True)
        # Arm the "wipe claude's TUI input buffer" flag so the next
        # ``_on_send`` writes an envelope with ``force_clear=True``.
        # Without this, whatever the user had partly typed (or that
        # the GUI had partly injected) before the interrupt stays in
        # claude's readline buffer, and gets concatenated in front of
        # the next message we send.
        self._next_send_force_clear = True

        def work():
            try:
                backend.mkdir(AGENT_OUTGOING_DIR, exist_ok=True)
            except OSError as e:
                log.debug("mkdir %s failed: %s", AGENT_OUTGOING_DIR, e)
            # Atomic write -- the launcher filters out ``.part`` files,
            # so it never sees a half-written envelope.
            backend.write_bytes(part_rel, body)
            backend.rename(part_rel, rel)

        def on_done(_result, exc):
            if not self:
                return
            if exc is not None:
                log.warning("Interrupt write failed: %s", exc)
                self._set_send_status(f"Interrupt failed: {exc}")
                _play_error_sound(source="interrupt_write")
                return
            stamp = time.strftime("%H:%M:%S")
            self._set_send_status(f"Interrupt sent at {stamp}")

        submit_io_write(work, on_done)

    def handle_outgoing_snapshot(self, basenames: list[str]) -> None:
        """Run one tick of the per-pending-send state machine.

        ``basenames`` is whatever was in ``clacogui_outgoing/`` at the
        most recent poll, *unfiltered* (so ``<id>.json``,
        ``<id>.json.failed`` and ``<id>.json.needs_decision`` all show
        up).  We only react on transitions, so a long-running
        ``queued`` state doesn't keep re-flashing the status label.

        Race guards:

        1. Ignore snapshots until ``write_committed`` is True (the
           write callback has fired, so the worker has already
           committed our bytes).
        2. Treat "file not present" as delivery in either of two
           sub-cases:
             (a) ``saw_visible`` -- we saw the file on disk at some
                 earlier tick and now it's gone.  This is the classic
                 slow-launcher case.
             (b) ``snapshot_seq > commit_seq`` -- this listing tick
                 was scheduled after the write committed.  The IO
                 worker is a single FIFO thread, so a listing whose
                 seq is strictly greater than the seq we saw at
                 write-commit time is guaranteed to reflect
                 post-write state.  This is the fast-launcher case:
                 the launcher consumed the file between the write
                 landing and the very first listing tick after it,
                 so we never observe the file at all.  Before this
                 branch existed, fast-launcher sends would sit in
                 "submitting" for 300s and get killed by the timeout
                 guard, ringing a false-alarm error bell even though
                 the message went through fine.
        """
        # Bump before any early return so successive callers agree
        # on ordering (write callbacks read the seq at their own
        # commit moment).
        self._snapshot_seq += 1
        seq = self._snapshot_seq
        if not self._pending_sends:
            return
        # Timeout sends stuck in "submitting" for over 5 minutes.
        # This prevents stale outgoing files from persisting forever
        # after a disconnect/reconnect cycle.
        now = time.monotonic()
        for pid in list(self._pending_sends.keys()):
            info = self._pending_sends[pid]
            if info["last_state"] == "submitting" and info.get("write_committed"):
                age = now - info["queued_at"]
                if age > 300:
                    log.warning(
                        "Send %s timed out after %.0fs in submitting state; "
                        "deleting outgoing file", pid, age,
                    )
                    self._pending_sends.pop(pid)
                    self._set_send_status("Send timed out (stale)")
                    _play_error_sound(source="send_timeout")
                    backend = self.backend
                    if backend is not None:
                        rel = f"{AGENT_OUTGOING_DIR}/{pid}.json"
                        part_rel = f"{AGENT_OUTGOING_DIR}/{pid}.json.part"
                        def _cleanup(r=rel, pr=part_rel, b=backend):
                            try:
                                b.delete(r)
                            except Exception:
                                pass
                            try:
                                b.delete(pr)
                            except Exception:
                                pass
                        submit_io_write(_cleanup, lambda _r, _e: None)
        if not self._pending_sends:
            return
        names = set(basenames)
        for pid in list(self._pending_sends.keys()):
            info = self._pending_sends.get(pid)
            if info is None:
                continue
            if info.get("dialog_open"):
                # Currently asking the user; ignore further snapshots
                # until they answer.
                continue
            if not info.get("write_committed", False):
                # Write hasn't returned yet; this snapshot may be
                # stale relative to it.  Skip.
                continue

            json_name = f"{pid}.json"
            nd_name = f"{pid}.json.needs_decision"
            failed_name = f"{pid}.json.failed"

            if json_name in names:
                new_state = "queued"
                info["saw_visible"] = True
            elif nd_name in names:
                new_state = "needs_decision"
                info["saw_visible"] = True
            elif failed_name in names:
                new_state = "failed"
                info["saw_visible"] = True
            else:
                # File-not-on-disk counts as "delivered" if either:
                #   (a) we saw it on disk earlier (``saw_visible``),
                #       so it definitely existed and is now gone; or
                #   (b) this listing was scheduled after the write
                #       committed (``seq > commit_seq``), meaning the
                #       FIFO IO worker processed the write before it
                #       processed this listing -- so the listing is
                #       authoritative that the file is not on disk
                #       any more.  This is the fast-launcher case.
                saw_visible = info.get("saw_visible", False)
                commit_seq = info.get("commit_seq")
                snapshot_is_post_commit = (
                    commit_seq is not None and seq > commit_seq
                )
                if not saw_visible and not snapshot_is_post_commit:
                    log.info(
                        "Send %s: post-commit snapshot still does not "
                        "list the file; staying in 'submitting' until "
                        "we observe it.  seq=%d commit_seq=%s "
                        "names_count=%d",
                        pid, seq, commit_seq, len(names),
                    )
                    continue
                new_state = "sent"

            if new_state == info["last_state"]:
                continue

            log.info(
                "Send %s state %s -> %s (snapshot has %d files)",
                pid, info["last_state"], new_state, len(names),
            )
            info["last_state"] = new_state

            if new_state == "queued":
                short = pid.split("_", 1)[-1]
                self._set_send_status(f"Queued ({short})...")
            elif new_state == "needs_decision":
                self._handle_needs_decision(pid)
            elif new_state == "failed":
                self._handle_failed(pid)
            elif new_state == "sent":
                stamp = time.strftime("%H:%M:%S")
                self._set_send_status(f"Sent at {stamp}")
                self._pending_sends.pop(pid, None)

    def _handle_needs_decision(self, pid: str) -> None:
        info = self._pending_sends.get(pid)
        if info is None:
            return
        info["dialog_open"] = True
        backend = self.backend
        rel_nd = f"{AGENT_OUTGOING_DIR}/{pid}.json.needs_decision"

        def work():
            return backend.read_text(rel_nd)

        def on_done(result, exc):
            if not self:
                return
            current = self._pending_sends.get(pid)
            if current is None:
                return
            if exc is not None:
                log.warning(
                    "Could not read .needs_decision for %s: %s", pid, exc
                )
                current["dialog_open"] = False
                # Allow the next snapshot to retry; reset last_state
                # so a transition is detected again if the file is
                # still there.
                current["last_state"] = "unknown"
                return
            try:
                envelope = json.loads(result)
            except (ValueError, TypeError):
                log.warning("Bad JSON in .needs_decision for %s", pid)
                envelope = {}
            reason = envelope.get("reason") or "user_has_uncommitted_text"
            self._show_uncommitted_dialog(pid, envelope, reason)

        submit_io(work, on_done)

    def _show_uncommitted_dialog(
        self, pid: str, envelope: dict, reason: str
    ) -> None:
        info = self._pending_sends.get(pid)
        if info is None:
            return
        backend = self.backend
        if backend is None:
            info["dialog_open"] = False
            return
        rel_nd = f"{AGENT_OUTGOING_DIR}/{pid}.json.needs_decision"
        rel_json = f"{AGENT_OUTGOING_DIR}/{pid}.json"
        try:
            top = self.GetTopLevelParent()
            dlg = UncommittedPromptDialog(top, reason)
            try:
                dlg.ShowModal()
                choice = dlg.decision
            finally:
                dlg.Destroy()
        finally:
            info_again = self._pending_sends.get(pid)
            if info_again is not None:
                info_again["dialog_open"] = False

        if choice == wx.ID_OK:
            # Erase-and-send: re-emit the envelope with force_clear and
            # rename the file back to <id>.json so the launcher picks
            # it up on its next 200 ms tick.
            new_envelope = dict(envelope)
            new_envelope["force_clear"] = True
            new_envelope.pop("reason", None)
            new_envelope.pop("noticed_at", None)
            body = json.dumps(new_envelope, indent=2).encode("utf-8")

            def rewrite_work():
                backend.write_bytes(rel_nd, body)
                backend.rename(rel_nd, rel_json)

            def rewrite_done(_r, exc):
                if not self:
                    return
                cur = self._pending_sends.get(pid)
                if exc is not None:
                    log.warning("Rewrite for erase-and-send failed: %s", exc)
                    self._set_send_status(f"Resend failed: {exc}")
                    _play_error_sound(source="resend_rewrite")
                    if cur is not None:
                        cur["last_state"] = "failed"
                    return
                if cur is not None:
                    cur["last_state"] = "queued"
                short = pid.split("_", 1)[-1]
                self._set_send_status(
                    f"Authorised erase-and-send ({short})..."
                )

            submit_io_write(rewrite_work, rewrite_done)
            return

        # Cancel: nuke the .needs_decision file and put the message
        # text back into the edit box for the user to retry.
        text = info.get("text", "")

        def cancel_work():
            backend.delete(rel_nd)

        submit_io_write(cancel_work, lambda _r, _e: None)
        # Splice the text back; if the user already started a fresh
        # message we prepend our restored text on a new line so they
        # don't lose either.
        cur_text = self._send_input.GetValue()
        if cur_text and cur_text.strip():
            self._send_input.SetValue(text + "\n\n" + cur_text)
        else:
            self._send_input.SetValue(text)
        self._send_input.SetInsertionPointEnd()
        self._set_send_status("Send cancelled; message restored.")
        self._pending_sends.pop(pid, None)

    def _handle_failed(self, pid: str) -> None:
        info = self._pending_sends.get(pid)
        if info is None:
            return
        backend = self.backend
        if backend is None:
            return
        rel = f"{AGENT_OUTGOING_DIR}/{pid}.json.failed"
        text = info.get("text", "")

        def work():
            return backend.read_text(rel)

        def on_done(result, exc):
            if not self:
                return
            # Preserve the raw JSON text from the .failed file so that
            # if the launcher's ``error`` field isn't structured the
            # way we expect, the user (and the log) still sees the
            # full payload instead of a useless "unknown error".
            raw_body = result if isinstance(result, str) else ""
            err = ""
            parsed: Optional[dict] = None
            if exc is None:
                try:
                    parsed = json.loads(raw_body)
                    if isinstance(parsed, dict):
                        err = str(parsed.get("error") or "").strip()
                except (ValueError, TypeError) as e:
                    log.warning(
                        "Could not parse .failed body for %s: %s",
                        pid, e,
                    )
            else:
                log.warning("Could not read .failed for %s: %s", pid, exc)

            # Log the full body so a future debugging session has
            # everything (we delete the file right after this).
            log.info(
                "Send %s .failed contents (%d bytes): %s",
                pid, len(raw_body or ""), raw_body or "<unreadable>",
            )

            if not err:
                # Surface *something* the user can act on.  Prefer a
                # short head of the raw payload over the literal
                # "unknown error" string.
                if raw_body:
                    head = raw_body.strip().splitlines()[0][:200]
                    err = f"no 'error' field; raw: {head}"
                else:
                    err = "unknown error (.failed file could not be read)"
            log.warning(
                "Send %s surfaced as .failed (rel=%s): %s",
                pid, rel, err,
            )
            self._set_send_status(f"Send failed: {err}")
            _play_error_sound(source="failed_file")

            def cleanup():
                try:
                    backend.delete(rel)
                except OSError:
                    log.debug("Cleanup of %s failed", rel, exc_info=True)

            submit_io_write(cleanup, lambda _r, _e: None)
            # Put the message back into the edit box so the user can
            # tweak and retry without re-typing it.
            cur_text = self._send_input.GetValue()
            if cur_text and cur_text.strip():
                self._send_input.SetValue(text + "\n\n" + cur_text)
            else:
                self._send_input.SetValue(text)
            self._send_input.SetInsertionPointEnd()
            self._pending_sends.pop(pid, None)

        submit_io(work, on_done)


def _make_webview(parent: wx.Window) -> "wx.html2.WebView":
    """Pick the most accessible WebView backend (Edge on Windows)."""
    backend = wx.html2.WebViewBackendDefault
    try:
        if wx.html2.WebView.IsBackendAvailable(wx.html2.WebViewBackendEdge):
            backend = wx.html2.WebViewBackendEdge
    except Exception:
        log.exception("WebView backend probe failed")
    log.info("Creating WebView with backend %s", backend)
    view = wx.html2.WebView.New(parent, backend=backend)
    view.SetName("Conversation content")
    view.SetPage(render_blank_html("Loading..."), "clacogui://loading")
    return view


def _slug_from_rel(rel_path: str) -> str:
    """Stable, URL-safe identifier for a conversation file with no sessionId."""
    base = os.path.splitext(os.path.basename(rel_path))[0]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in base)
    return safe or "unknown"


# Custom event for tab name changes ------------------------------------------------

_EVT_NAME_CHANGED_TYPE = wx.NewEventType()
EVT_NAME_CHANGED = wx.PyEventBinder(_EVT_NAME_CHANGED_TYPE, 1)


class _NameChangedEvent(wx.PyCommandEvent):
    def __init__(self, source_id: int) -> None:
        super().__init__(_EVT_NAME_CHANGED_TYPE, source_id)


# ---------------------------------------------------------------------------
# Main frame
# ---------------------------------------------------------------------------


# Stable IDs for accelerators / menu items.
ID_OPEN = wx.NewIdRef()
ID_CLOSE_TAB = wx.NewIdRef()
ID_CHANGE_DIR = wx.NewIdRef()
ID_NEXT_TAB = wx.NewIdRef()
ID_PREV_TAB = wx.NewIdRef()
ID_MOVE_TAB_PREV = wx.NewIdRef()
ID_MOVE_TAB_NEXT = wx.NewIdRef()
ID_REORDER_TABS = wx.NewIdRef()
ID_RELOAD = wx.NewIdRef()
ID_FOCUS_LIST = wx.NewIdRef()
ID_FOCUS_HTML = wx.NewIdRef()
ID_FOCUS_SEND = wx.NewIdRef()
ID_INTERRUPT_CLAUDE = wx.NewIdRef()
ID_TAB_1 = wx.NewIdRef()
ID_TAB_2 = wx.NewIdRef()
ID_TAB_3 = wx.NewIdRef()
ID_TAB_4 = wx.NewIdRef()
ID_TAB_5 = wx.NewIdRef()
ID_TAB_6 = wx.NewIdRef()
ID_TAB_7 = wx.NewIdRef()
ID_TAB_8 = wx.NewIdRef()
ID_TAB_9 = wx.NewIdRef()
_TAB_IDS = [ID_TAB_1, ID_TAB_2, ID_TAB_3, ID_TAB_4, ID_TAB_5,
            ID_TAB_6, ID_TAB_7, ID_TAB_8, ID_TAB_9]


class MainFrame(wx.Frame):
    """Top-level window holding a notebook of open conversations."""

    def __init__(self, backend_spec: Optional[str]) -> None:
        # The conversation name is prepended in ``_update_window_title``
        # whenever a tab is open; this is just the fallback "no
        # conversation" title.  Screen readers / the OS taskbar both
        # read the title left-to-right, and the user wants the
        # conversation name first.
        super().__init__(
            None,
            title="clacogui - Claude Code conversation viewer",
            size=(1200, 800),
            name="ClacoGuiMain",
        )
        if sys.platform == "win32":
            _set_window_appid(self.GetHandle())
        self.backend_spec: Optional[str] = backend_spec
        self.backend: Optional[FsBackend] = None
        if backend_spec:
            try:
                self.backend = make_backend(backend_spec)
            except Exception:
                log.exception("Could not build backend from %r", backend_spec)
                self.backend_spec = None
        self._build_menu()
        self._build_ui()
        self._build_accelerators()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(EVT_NAME_CHANGED, self._on_name_changed)

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._timer.Start(POLL_INTERVAL_MS)

        self._active_poll_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_active_poll_timer, self._active_poll_timer)
        self._active_poll_timer.Start(ACTIVE_POLL_INTERVAL_MS)

        # Agent (hook) monitoring state.
        self._agent_polling = False
        self._seen_notifications: set[str] = set()
        self._seen_requests: set[str] = set()
        # Permission-request dialogs are queued and shown one at a time
        # to prevent nested event loops from making the UI unresponsive.
        self._open_requests: set[str] = set()
        self._permission_queue: list[tuple[str, dict]] = []
        self._permission_dialog_active: bool = False
        self._current_permission_dialog: Optional[wx.Dialog] = None
        # Track when we last rang for a "completion" event (Stop /
        # SubagentStop / PreToolUse).  Claude Code fires a follow-up
        # ``Notification`` ~60s after Stop ("waiting for your input"),
        # which sounds redundant -- suppress those if a completion was
        # recent.  See ``_handle_new_notifications``.
        self._last_completion_ts: float = 0.0

        # Map of {session_id: status} populated from
        # ``~/.claude/sessions/*.json`` on each agent poll tick (see
        # ``_poll_agent``).  Used by ``_refresh_claude_status_field``
        # to write ``Claude: idle`` / ``Claude: busy`` into status-bar
        # field 0 for the currently selected conversation.  Empty
        # until the first poll completes; field 0 then stays in sync
        # with claude's own metadata.
        self._claude_status_map: dict[str, str] = {}
        # Map of {session_id: name} from the same metadata files.
        # The ``name`` field is what the user set with ``/rename
        # aaa1`` inside claude's TUI.  The JSONL transcript never
        # sees this string, so without this lookup the tab title
        # would fall back to "first turn preview" / "UUID" -- which
        # is exactly the bug the user reported when they saw a UUID
        # in the GUI for a session they had renamed.
        self._claude_session_names: dict[str, str] = {}

        self._update_status()
        self._gc_stale_agent_requests()
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)

    # -- Construction -------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        file_menu.Append(ID_OPEN, "&Open conversation...\tCtrl+O")
        file_menu.Append(ID_CLOSE_TAB, "&Close conversation\tCtrl+W")
        # F5 is the send-box jump now (see View menu); refresh keeps Ctrl+R.
        file_menu.Append(ID_RELOAD, "&Refresh current\tCtrl+R")
        file_menu.AppendSeparator()
        file_menu.Append(ID_CHANGE_DIR, "Change Claude Code &folder...")
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_EXIT, "E&xit\tAlt+F4")
        menubar.Append(file_menu, "&File")

        conv_menu = wx.Menu()
        # Esc-into-claude (cancel current turn).  Idempotent in
        # claude's TUI -- safe to mash, unlike Ctrl+C which would
        # exit on the second hit.  Routed through the launcher's
        # "action: interrupt" envelope; see clacogui_launcher.py.
        conv_menu.Append(
            ID_INTERRUPT_CLAUDE,
            "&Interrupt claude (send Esc)\tCtrl+.",
        )
        menubar.Append(conv_menu, "&Conversation")

        view_menu = wx.Menu()
        view_menu.Append(ID_NEXT_TAB, "&Next conversation\tCtrl+Tab")
        view_menu.Append(ID_PREV_TAB, "&Previous conversation\tCtrl+Shift+Tab")
        view_menu.AppendSeparator()
        view_menu.Append(
            ID_MOVE_TAB_PREV,
            "Move tab &left\tCtrl+Shift+PgUp",
        )
        view_menu.Append(
            ID_MOVE_TAB_NEXT,
            "Move tab &right\tCtrl+Shift+PgDn",
        )
        view_menu.Append(ID_REORDER_TABS, "Reorder &tabs...")
        view_menu.AppendSeparator()
        # F2 / F4 / F5 are dedicated pane-jump keys (per user request);
        # F6 still cycles between list and content for backward compat.
        view_menu.Append(ID_FOCUS_LIST, "Focus &message list\tF2")
        view_menu.Append(ID_FOCUS_HTML, "Focus &content\tF4")
        view_menu.Append(ID_FOCUS_SEND, "Focus &send box\tF5")
        menubar.Append(view_menu, "&View")

        self.SetMenuBar(menubar)

        self.Bind(wx.EVT_MENU, self._on_open, id=ID_OPEN)
        self.Bind(wx.EVT_MENU, self._on_close_tab, id=ID_CLOSE_TAB)
        self.Bind(wx.EVT_MENU, self._on_reload, id=ID_RELOAD)
        self.Bind(wx.EVT_MENU, self._on_change_dir, id=ID_CHANGE_DIR)
        self.Bind(wx.EVT_MENU, lambda _e: self.Close(), id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, lambda _e: self._switch_tab(+1), id=ID_NEXT_TAB)
        self.Bind(wx.EVT_MENU, lambda _e: self._switch_tab(-1), id=ID_PREV_TAB)
        self.Bind(wx.EVT_MENU, lambda _e: self._move_tab(-1), id=ID_MOVE_TAB_PREV)
        self.Bind(wx.EVT_MENU, lambda _e: self._move_tab(+1), id=ID_MOVE_TAB_NEXT)
        self.Bind(wx.EVT_MENU, self._on_reorder_tabs, id=ID_REORDER_TABS)
        self.Bind(wx.EVT_MENU, lambda _e: self._focus_pane(False), id=ID_FOCUS_LIST)
        self.Bind(wx.EVT_MENU, lambda _e: self._focus_pane(True), id=ID_FOCUS_HTML)
        self.Bind(wx.EVT_MENU, lambda _e: self._focus_send_box(), id=ID_FOCUS_SEND)
        self.Bind(wx.EVT_MENU, self._on_interrupt_claude, id=ID_INTERRUPT_CLAUDE)
        for i, tid in enumerate(_TAB_IDS):
            self.Bind(wx.EVT_MENU, lambda _e, n=i: self._goto_tab(n), id=tid)

    def _build_ui(self) -> None:
        self.notebook = wx.Notebook(self, name="Conversations")
        self.notebook.Bind(
            wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab_changed
        )
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.notebook, 1, wx.EXPAND)
        self.SetSizer(sizer)
        # Status bar layout (3 fields):
        #   field 0 -- claude busy/idle/unknown for the active tab.
        #              Leftmost so screen readers reach it first when
        #              the user wants the most-asked question
        #              answered ("is claude actually running?").
        #              Source: ``self._claude_status_map`` looked up
        #              by the active tab's session_id.
        #   field 1 -- send-message status of the active conversation
        #              (e.g. "Queued (...)", "Sent at HH:MM:SS",
        #              "Send failed: ..."), or a transient message
        #              shown via ``show_transient_status``.  Transient
        #              messages overlay this field, not field 0,
        #              because they're about app/conversation state
        #              and shouldn't squelch the always-on claude
        #              status indicator.
        #   field 2 -- folder / backend description
        self.CreateStatusBar(3)
        # Give the claude-status field a fixed width so the indicator
        # text ("Claude: busy") sits in a predictable place; the send
        # field gets a small share, and the folder gets the rest.
        try:
            self.SetStatusWidths([160, -1, -2])
        except Exception:
            log.debug("SetStatusWidths failed", exc_info=True)
        self._status_revert_timer: Optional[wx.Timer] = None
        self._base_title = "clacogui - Claude Code conversation viewer"

    def _build_accelerators(self) -> None:
        accels = [
            (wx.ACCEL_CTRL, ord("O"), ID_OPEN),
            (wx.ACCEL_CTRL, ord("W"), ID_CLOSE_TAB),
            (wx.ACCEL_CTRL, wx.WXK_F4, ID_CLOSE_TAB),
            (wx.ACCEL_CTRL, ord("R"), ID_RELOAD),
            (wx.ACCEL_CTRL, wx.WXK_TAB, ID_NEXT_TAB),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_TAB, ID_PREV_TAB),
            # Move-current-tab without wrapping.
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_PAGEUP, ID_MOVE_TAB_PREV),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_PAGEDOWN, ID_MOVE_TAB_NEXT),
            # Pane-jump function keys (per user request).  F5 used to be
            # Refresh; that's now Ctrl+R-only.
            (wx.ACCEL_NORMAL, wx.WXK_F2, ID_FOCUS_LIST),
            (wx.ACCEL_NORMAL, wx.WXK_F4, ID_FOCUS_HTML),
            (wx.ACCEL_NORMAL, wx.WXK_F5, ID_FOCUS_SEND),
            # F6 cycle kept for backward compat with previous builds.
            (wx.ACCEL_NORMAL, wx.WXK_F6, ID_FOCUS_HTML),
            (wx.ACCEL_SHIFT, wx.WXK_F6, ID_FOCUS_LIST),
            (wx.ACCEL_ALT, wx.WXK_F4, wx.ID_EXIT),
            # Interrupt claude (Esc-into-claude); ``ord('.')`` is the
            # period key, so this is Ctrl+. on every layout where
            # period is unmodified.
            (wx.ACCEL_CTRL, ord('.'), ID_INTERRUPT_CLAUDE),
        ]
        for i, tid in enumerate(_TAB_IDS):
            accels.append((wx.ACCEL_CTRL, ord(str(i + 1)), tid))
        self.SetAcceleratorTable(wx.AcceleratorTable(accels))

    # -- Helpers ------------------------------------------------------------

    def _current_panel(self) -> Optional[ConversationPanel]:
        idx = self.notebook.GetSelection()
        if idx == wx.NOT_FOUND:
            return None
        return self.notebook.GetPage(idx)  # type: ignore[return-value]

    def _switch_tab(self, delta: int) -> None:
        n = self.notebook.GetPageCount()
        if n == 0:
            return
        cur = self.notebook.GetSelection()
        if cur == wx.NOT_FOUND:
            cur = 0
        self.notebook.SetSelection((cur + delta) % n)

    def _goto_tab(self, n: int) -> None:
        if 0 <= n < self.notebook.GetPageCount():
            self.notebook.SetSelection(n)

    def _move_tab(self, delta: int) -> None:
        """Move the currently-selected tab by ``delta`` slots.

        Does **not** wrap around: at index 0 a ``-1`` is a no-op, at the
        last index a ``+1`` is a no-op (per the user's spec).
        """
        n = self.notebook.GetPageCount()
        cur = self.notebook.GetSelection()
        if cur == wx.NOT_FOUND or n == 0:
            return
        new_idx = cur + delta
        if new_idx < 0 or new_idx >= n:
            return
        page = self.notebook.GetPage(cur)
        text = self.notebook.GetPageText(cur)
        # ``RemovePage`` detaches without destroying; ``InsertPage`` re-uses
        # the existing window so the conversation state is preserved.
        self.notebook.RemovePage(cur)
        self.notebook.InsertPage(new_idx, page, text, select=True)

    def _activate_menu_mnemonic(self, letter: str) -> None:
        """Trigger the menu-bar mnemonic ``Alt+<letter>`` from a WebView click.

        The WebView swallows Alt+<letter> before the menubar can see it,
        so the page's JS forwards the letter via the ``clacogui-action://``
        scheme.  We then have to *re-issue* the keystroke at the OS
        level (the original was already consumed).  The order matters:

          1. Move focus off the WebView so the synthesised key event
             doesn't get re-routed back into the page.
          2. ``UIActionSimulator.KeyDown(Alt) + KeyDown(letter) + ...``
             generates a real OS-level Alt+letter, which Windows
             routes to whichever frame has focus -- the menubar's
             accelerator picks it up and opens the File / View menu.
        """
        if not letter or len(letter) != 1 or not letter.isalpha():
            return
        try:
            self.notebook.SetFocus()
        except Exception:
            log.debug("notebook.SetFocus failed", exc_info=True)

        # Defer the key-injection one tick so wx can finish processing
        # the navigation event (including the Veto we just issued)
        # before a new key event arrives.  Without this, Windows can
        # drop the Alt+letter on busy hardware.
        upper_key = ord(letter.upper())

        def _send():
            try:
                sim = wx.UIActionSimulator()
                # KeyDown/KeyUp pair generates a real OS-level Alt+key
                # which routes through Windows' menubar handling.
                sim.KeyDown(wx.WXK_ALT)
                sim.KeyDown(upper_key, wx.MOD_ALT)
                sim.KeyUp(upper_key, wx.MOD_ALT)
                sim.KeyUp(wx.WXK_ALT)
            except Exception:
                log.exception(
                    "UIActionSimulator Alt+%s failed", letter.upper()
                )

        wx.CallAfter(_send)

    def _focus_pane(self, html: bool) -> None:
        panel = self._current_panel()
        if panel is None:
            return
        if html:
            panel.webview.SetFocus()
        else:
            panel.list_box.SetFocus()

    def _focus_send_box(self) -> None:
        """Move keyboard focus into the current tab's send-message box.

        The send box is created in ``ConversationPanel._build_ui`` and
        is the third focus target (after the message list and the
        WebView).  No-op if no conversation is open.
        """
        panel = self._current_panel()
        if panel is None:
            return
        target = getattr(panel, "_send_input", None)
        if target is None:
            return
        try:
            target.SetFocus()
            target.SetInsertionPointEnd()
        except Exception:
            log.debug("focus send box failed", exc_info=True)

    def _on_interrupt_claude(self, _event: wx.CommandEvent) -> None:
        """Hand the interrupt-claude action off to the active panel.

        We delegate so each :class:`ConversationPanel` can format its
        own status feedback (and address the right session_id) -- the
        frame just routes the event.  No-op when no tab is open or
        the active panel hasn't loaded its session id yet.
        """
        panel = self._current_panel()
        if panel is None:
            self.show_transient_status(
                "No conversation open to interrupt.",
                revert_after_ms=3000,
            )
            return
        handler = getattr(panel, "interrupt_claude", None)
        if not callable(handler):
            return
        try:
            handler()
        except Exception:
            log.exception("interrupt_claude crashed")

    def _update_status(self) -> None:
        # Folder description goes into field 2 (rightmost).
        if self.backend is not None:
            self.SetStatusText(f"Folder: {self.backend.display_root()}", 2)
        elif self.backend_spec:
            self.SetStatusText(f"Folder (unavailable): {self.backend_spec}", 2)
        else:
            self.SetStatusText("No Claude Code folder set", 2)
        # Field 0: claude status (idle/busy/...).  Field 1: send
        # status of the active tab (or empty when no tab is open).
        # Don't clobber a transient message that's still showing.
        self._refresh_claude_status_field()
        self.refresh_send_status_field()

    def _current_send_status_text(self) -> str:
        panel = self._current_panel()
        if panel is None:
            return ""
        return getattr(panel, "_last_send_status", "") or ""

    def _claude_session_id_for_active_tab(self) -> Optional[str]:
        panel = self._current_panel()
        if panel is None:
            return None
        getter = getattr(panel, "claude_session_id", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            log.debug("claude_session_id() crashed", exc_info=True)
            return None

    def _claude_status_text(self) -> str:
        """Return the leftmost-status-field value for the active tab.

        Format: ``Claude: <status>`` (e.g. ``Claude: idle``,
        ``Claude: busy``).  Returns an empty string when there's no
        active tab, no session_id yet, or no metadata file matching
        the tab's session_id.  Status strings come straight from
        claude's own metadata ``status`` field; we don't translate
        them so what the launcher sees is what shows up here.
        """
        sid = self._claude_session_id_for_active_tab()
        if not sid:
            return ""
        status = self._claude_status_map.get(sid)
        if not status:
            return "Claude: not running"
        return f"Claude: {status}"

    def _refresh_claude_status_field(self) -> None:
        """Repaint status-bar field 0 with the active tab's claude status.

        Driven by the agent-poll callback (which populates
        ``_claude_status_map``) and by tab/backend changes (which
        change which session_id we're looking up).  Unlike the
        send-status field, this is never overridden by transient
        messages -- claude busy/idle is too important to squelch.
        """
        try:
            self.SetStatusText(self._claude_status_text(), 0)
        except Exception:
            log.debug("SetStatusText claude-status failed", exc_info=True)

    def _push_session_aliases(self) -> None:
        """Sync the freshly-read session-name map into open panels.

        For every open :class:`ConversationPanel`, hand it the alias
        (or ``None``) for its session_id.  Each panel decides
        whether the alias actually changed; only when it does does
        the panel post a name-change event that walks back here to
        update the tab label and window title.  This keeps the
        update path identical to the existing ``data.name``
        refresh, so we don't end up with two competing sources of
        truth for "what does this tab say in the title bar".
        """
        for i in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(i)
            if not isinstance(page, ConversationPanel):
                continue
            sid = page.claude_session_id()
            alias = (
                self._claude_session_names.get(sid) if sid else None
            )
            try:
                page.set_session_alias(alias)
            except Exception:
                log.debug(
                    "set_session_alias failed for page %d", i, exc_info=True
                )

    def refresh_send_status_field(
        self, source: Optional["ConversationPanel"] = None
    ) -> None:
        """Repaint status-bar field 1 with the active tab's send status.

        ``source`` is the panel that triggered the update (when
        called from ``ConversationPanel._set_send_status``).  We
        only push to the status bar if ``source`` is the currently
        selected tab -- background tabs updating their state must
        not override the active tab's display.

        While a transient status message is showing (timer armed),
        we leave the field alone; the timer's revert path will
        re-read the current send-status when it fires, picking up
        whatever the active tab's latest value is.
        """
        if self._status_revert_timer is not None:
            return
        if source is not None and source is not self._current_panel():
            return
        try:
            self.SetStatusText(self._current_send_status_text(), 1)
        except Exception:
            log.debug("SetStatusText send-status failed", exc_info=True)

    def _update_window_title(self) -> None:
        """Set the frame title to ``<conversation> - clacogui``.

        Putting the conversation name first means screen readers and
        the OS taskbar both read it before the app suffix, which is
        what the user asked for.  When no conversation is open we
        fall back to the plain app title.
        """
        panel = self._current_panel()
        if panel is None:
            self.SetTitle(self._base_title)
            return
        name = panel.conversation_name() or "conversation"
        self.SetTitle(f"{name} - clacogui")

    def _on_tab_changed(self, event: wx.BookCtrlEvent) -> None:
        self._update_window_title()
        # Both status fields reflect the *active* tab; resync now
        # that the active tab has changed.  refresh_send_status_field
        # is a no-op if a transient message is currently showing --
        # it'll catch up when the transient reverts.
        self._refresh_claude_status_field()
        self.refresh_send_status_field()
        event.Skip()

    def _on_reorder_tabs(self, _event: wx.CommandEvent) -> None:
        if self.notebook.GetPageCount() < 2:
            self.show_transient_status(
                "Need at least two open conversations to reorder.",
                revert_after_ms=4000,
            )
            return
        names = [
            self.notebook.GetPageText(i)
            for i in range(self.notebook.GetPageCount())
        ]
        cur = self.notebook.GetSelection()
        dlg = ReorderTabsDialog(self, names, cur if cur != wx.NOT_FOUND else 0)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            new_order = dlg.new_order
        finally:
            dlg.Destroy()
        # ``new_order`` is a list of original indices in their new order.
        if list(new_order) == list(range(self.notebook.GetPageCount())):
            return  # nothing to do
        self._apply_tab_order(new_order)

    def _apply_tab_order(self, new_order: list[int]) -> None:
        """Reorder notebook pages to match ``new_order``.

        ``new_order[i]`` is the *current* index of the page that should
        end up at position ``i``.  We pop pages in reverse old-index
        order so the indices we still hold remain valid, then re-insert
        them in the desired order.
        """
        n = self.notebook.GetPageCount()
        if sorted(new_order) != list(range(n)):
            log.warning("Refusing to reorder tabs: bad permutation %r", new_order)
            return
        prev_selection = self.notebook.GetSelection()
        prev_page = (
            self.notebook.GetPage(prev_selection)
            if prev_selection != wx.NOT_FOUND
            else None
        )
        # Snapshot page object + label for each existing tab.
        snapshot: list[tuple[wx.Window, str]] = [
            (self.notebook.GetPage(i), self.notebook.GetPageText(i))
            for i in range(n)
        ]
        # Detach all pages without destroying.  Going backwards keeps
        # the indices valid as we go.
        for i in range(n - 1, -1, -1):
            self.notebook.RemovePage(i)
        # Re-insert in the desired order.
        new_selection = 0
        for new_idx, old_idx in enumerate(new_order):
            page, text = snapshot[old_idx]
            self.notebook.InsertPage(new_idx, page, text, select=False)
            if page is prev_page:
                new_selection = new_idx
        self.notebook.SetSelection(new_selection)
        self._update_window_title()

    def show_transient_status(
        self, message: str, revert_after_ms: int = 0
    ) -> None:
        """Display ``message`` in status-bar field 1 (send-status slot).

        Transient messages overlay the send-status field, **not** the
        claude-status field (field 0) -- claude busy/idle is the most
        important always-on indicator and we don't want to squelch
        it for app-action messages like "Refreshed at HH:MM:SS".

        If ``revert_after_ms`` is > 0, the bar reverts to the active
        conversation's send-status after that many milliseconds (which
        is also what's shown when no transient is active).  Calling
        this again before the timer fires resets the timer, so the
        most recent message wins.
        """
        log.info("Status: %s", message)
        self.SetStatusText(message, 1)
        if self._status_revert_timer is not None:
            self._status_revert_timer.Stop()
            self._status_revert_timer = None
        if revert_after_ms > 0:
            self._status_revert_timer = wx.CallLater(
                revert_after_ms, self._revert_status
            )

    def _revert_status(self) -> None:
        if not self:
            return
        # Clear the timer first so refresh_send_status_field doesn't
        # short-circuit out on the "transient is showing" guard.
        self._status_revert_timer = None
        self.refresh_send_status_field()

    # -- Event handlers -----------------------------------------------------

    def _on_close(self, event: wx.CloseEvent) -> None:
        log.info("MainFrame closing")
        try:
            open_tabs = []
            for i in range(self.notebook.GetPageCount()):
                page = self.notebook.GetPage(i)
                if isinstance(page, ConversationPanel):
                    open_tabs.append(page.rel_path)
            active_idx = self.notebook.GetSelection()
            cfg = load_config()
            cfg["open_tabs"] = open_tabs
            cfg["active_tab_index"] = active_idx if active_idx != wx.NOT_FOUND else 0
            save_config(cfg)
        except Exception:
            log.debug("Failed to save open tabs", exc_info=True)
        try:
            self._timer.Stop()
        except Exception:
            pass
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                log.debug("Backend close raised", exc_info=True)
        self.Destroy()

    def _on_timer(self, _event: wx.TimerEvent) -> None:
        # Poll every page so name changes update tab labels too.
        for i in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(i)
            if isinstance(page, ConversationPanel):
                try:
                    page.poll()
                except Exception:
                    log.exception("Polling page %d crashed", i)
        try:
            self._poll_agent()
        except Exception:
            log.exception("Agent poll crashed")

    def _on_active_poll_timer(self, _event: wx.TimerEvent) -> None:
        """Fast poll: incremental read for the active tab at 500ms."""
        sel = self.notebook.GetSelection()
        if sel < 0:
            return
        page = self.notebook.GetPage(sel)
        if isinstance(page, ConversationPanel):
            try:
                page.poll()
            except Exception:
                log.debug("Active poll crashed", exc_info=True)

    # -- Agent (hook) monitoring -------------------------------------------

    def _poll_agent(self) -> None:
        """Look for new notification/request/outgoing files under the backend root.

        Runs at most one scan in flight at a time.  All directories are
        listed in a single backend hop because each FTP MLSD is round-trip
        heavy.
        """
        if self.backend is None or self._agent_polling:
            return
        self._agent_polling = True
        backend = self.backend

        def work():
            # Outgoing files include suffixed states (.failed,
            # .needs_decision) that ``_safe_list_basenames`` would
            # filter out (it only keeps ``*.json``), so list the
            # directory directly and return *all* file names.
            try:
                outgoing_entries = backend.list_dir(AGENT_OUTGOING_DIR)
            except FileNotFoundError:
                outgoing_entries = []
            except OSError:
                log.debug(
                    "list_dir(%s) failed", AGENT_OUTGOING_DIR, exc_info=True
                )
                outgoing_entries = []
            outgoing = sorted(
                e.name for e in outgoing_entries if not e.is_dir
            )
            # Claude's per-pid metadata under ``sessions/`` carries
            # both the live ``status`` (idle / busy / ...) for the
            # leftmost status-bar field AND the user-settable
            # ``name`` alias (e.g. ``aaa1`` after ``/rename aaa1``).
            # The JSONL transcript never sees the alias, so without
            # this lookup an open tab would fall back to "first
            # message preview" or the raw UUID.  One pass over the
            # directory builds both maps -- on FTP each metadata
            # file is its own round trip.
            claude_status_map, claude_names_map = (
                _read_claude_session_state(backend)
            )
            return (
                _safe_list_basenames(backend, AGENT_NOTIFICATIONS_DIR),
                _safe_list_basenames(backend, AGENT_REQUESTS_DIR),
                outgoing,
                claude_status_map,
                claude_names_map,
            )

        def callback(result, exc):
            if not self:
                return
            self._agent_polling = False
            if exc is not None:
                # Common case: directories don't exist (agent not installed).
                # Don't spam the log, just debug.
                log.debug("Agent poll IO failed: %s", exc)
                return
            (
                notifications,
                requests,
                outgoing,
                claude_status_map,
                claude_names_map,
            ) = result
            # Update claude session state first so the status bar
            # and tab titles reflect the freshest data even if the
            # other handlers below put up a transient message.
            self._claude_status_map = claude_status_map
            self._claude_session_names = claude_names_map
            self._refresh_claude_status_field()
            self._push_session_aliases()
            self._handle_new_notifications(notifications)
            self._handle_new_requests(requests)
            self._handle_outgoing_snapshot(outgoing)

        submit_io(work, callback)

    def _handle_outgoing_snapshot(self, names: list[str]) -> None:
        """Forward the ``clacogui_outgoing/`` listing to every panel.

        Each conversation panel maintains its own ``_pending_sends``
        dict and only acts on filenames it owns, so we don't have to
        match panel <-> file here.
        """
        for i in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(i)
            if isinstance(page, ConversationPanel):
                try:
                    page.handle_outgoing_snapshot(names)
                except Exception:
                    log.exception(
                        "outgoing snapshot dispatch crashed for page %d", i
                    )

    def _handle_new_notifications(self, names: list[str]) -> None:
        backend = self.backend
        if backend is None:
            return
        new_names = [n for n in names if n not in self._seen_notifications]
        if not new_names:
            return

        events = [(n, _event_from_notification_name(n)) for n in new_names]
        log.info(
            "Got %d new notification(s): %s",
            len(new_names),
            ", ".join(f"{n} [{ev or '?'}]" for n, ev in events),
        )
        for n in new_names:
            self._seen_notifications.add(n)

        now = time.time()
        ring_completion = False
        ring_default = False
        for _, ev in events:
            if ev in _SILENT_EVENTS:
                continue
            if ev in _COMPLETION_EVENTS:
                # Real "Claude finished" event -- ring (with the
                # completion-specific sound, if available) AND remember
                # when, so we can squelch the trailing idle reminder.
                self._last_completion_ts = now
                ring_completion = True
            elif ev == "Notification":
                # Idle reminder ("Claude is waiting for your input").
                # Suppress if a completion already rang recently.
                if now - self._last_completion_ts <= _NOTIFICATION_DEDUP_SEC:
                    log.info(
                        "Suppressing idle Notification %.1fs after last "
                        "completion (dedup window %.0fs)",
                        now - self._last_completion_ts,
                        _NOTIFICATION_DEDUP_SEC,
                    )
                    continue
                ring_default = True
            else:
                # Unknown / pre-tagged-filename agent => fall back to the
                # old behavior and ring; better an extra beep than silence.
                ring_default = True

        # Play exactly one beep per cycle even if many landed at once.
        # If both a completion and a non-completion event arrived in the
        # same poll cycle, the completion sound wins -- that's the
        # "Claude finished" cue the user explicitly cares about.
        if ring_completion:
            _play_notification_sound(completion=True)
        elif ring_default:
            _play_notification_sound()

        rel_paths = [f"{AGENT_NOTIFICATIONS_DIR}/{n}" for n in new_names]

        def work():
            for rp in rel_paths:
                try:
                    backend.delete(rp)
                except OSError:
                    log.debug("Could not delete notification %s", rp, exc_info=True)
            return None

        submit_io_write(work, lambda _r, _e: None)

    def _gc_stale_agent_requests(self) -> None:
        """Delete request files older than :data:`STALE_REQUEST_AGE_SEC`.

        A live agent process always cleans up its own request file in
        a ``finally``.  But if the agent (or its wrapping shell) was
        killed while a permission dialog was open, the file is left
        behind on disk -- and because the GUI scans
        ``clacogui_requests/`` on every poll, that file would
        resurrect the same prompt at every GUI launch forever.

        Anything older than the agent's request timeout is guaranteed
        not to belong to a still-waiting agent, so we sweep it.
        Backends that don't expose accurate mtimes will simply skip
        the entry.
        """
        backend = self.backend
        if backend is None:
            return
        cutoff = time.time() - STALE_REQUEST_AGE_SEC

        def work() -> list[str]:
            try:
                entries = backend.list_dir(AGENT_REQUESTS_DIR)
            except FileNotFoundError:
                return []
            except OSError:
                log.debug(
                    "GC: list_dir(%s) failed", AGENT_REQUESTS_DIR, exc_info=True
                )
                return []
            removed: list[str] = []
            for e in entries:
                if e.is_dir or not e.name.endswith(".json"):
                    continue
                if e.mtime <= 0 or e.mtime >= cutoff:
                    continue
                rel = f"{AGENT_REQUESTS_DIR}/{e.name}"
                try:
                    backend.delete(rel)
                    removed.append(e.name)
                except FileNotFoundError:
                    pass
                except OSError:
                    log.debug(
                        "GC: could not delete stale request %s", rel,
                        exc_info=True,
                    )
            return removed

        def callback(result, exc):
            if exc is not None:
                log.debug("Stale-request GC failed", exc_info=exc)
                return
            if result:
                log.info(
                    "Reaped %d stale clacogui_requests entries: %s",
                    len(result), ", ".join(result),
                )

        submit_io_write(work, callback)

    def _handle_new_requests(self, names: list[str]) -> None:
        backend = self.backend
        if backend is None:
            return
        any_new = False
        for name in names:
            if name in self._seen_requests or name in self._open_requests:
                continue
            self._seen_requests.add(name)
            self._open_requests.add(name)
            any_new = True
            log.info("New permission request: %s", name)
            self._fetch_request(name)
        if any_new:
            # Permission requests live in clacogui_requests/ (separate from
            # the notifications dir), so they don't pass through
            # _handle_new_notifications and wouldn't otherwise update the
            # idle-Notification dedup timestamp.  Claude Code fires a
            # follow-up "Claude needs your permission" Notification ~15s
            # later -- arm the dedup window now so that one ping is
            # squelched (the user is already looking at the modal).
            self._last_completion_ts = time.time()

    def _fetch_request(self, name: str) -> None:
        backend = self.backend
        if backend is None:
            return
        rel = f"{AGENT_REQUESTS_DIR}/{name}"

        def work():
            return backend.read_text(rel)

        def callback(result, exc):
            if not self:
                return
            if exc is not None:
                log.warning("Could not read request %s: %s", rel, exc)
                self._open_requests.discard(name)
                return
            try:
                event = json.loads(result)
            except (ValueError, TypeError) as e:
                log.warning("Bad JSON in request %s: %s", rel, e)
                self._open_requests.discard(name)
                return
            self._show_permission_dialog(name, event)

        submit_io(work, callback)

    def _show_permission_dialog(self, name: str, event: dict) -> None:
        self._permission_queue.append((name, event))
        self._process_next_permission()

    def _process_next_permission(self) -> None:
        if self._permission_dialog_active or not self._permission_queue:
            return
        self._permission_dialog_active = True
        name, event = self._permission_queue.pop(0)
        self._do_show_permission_dialog(name, event)

    def _do_show_permission_dialog(self, name: str, event: dict) -> None:
        rid = event.get("_clacogui_request_id") or os.path.splitext(name)[0]
        try:
            self.Raise()
            self.RequestUserAttention(wx.USER_ATTENTION_ERROR)
            dlg = PermissionRequestDialog(self, event)
            self._current_permission_dialog = dlg
            try:
                dlg.ShowModal()
                allow = dlg.allowed
                reason = dlg.reason
                updated_permissions = list(dlg.updated_permissions)
                updated_input = dict(dlg.updated_input)
            finally:
                self._current_permission_dialog = None
                dlg.Destroy()
        finally:
            self._open_requests.discard(name)
            self._permission_dialog_active = False

        backend = self.backend
        if backend is None:
            wx.CallAfter(self._process_next_permission)
            return
        response = {
            "decision": "allow" if allow else "deny",
            "reason": reason,
        }
        if allow and updated_permissions:
            response["updated_permissions"] = updated_permissions
        if allow and updated_input:
            response["updated_input"] = updated_input
        response_rel = f"{AGENT_RESPONSES_DIR}/{rid}.json"
        request_rel = f"{AGENT_REQUESTS_DIR}/{name}"
        body = json.dumps(response, indent=2).encode("utf-8")

        def work():
            backend.write_bytes(response_rel, body)
            try:
                backend.delete(request_rel)
            except FileNotFoundError:
                pass
            except OSError:
                log.debug(
                    "Could not delete request %s after responding",
                    request_rel,
                    exc_info=True,
                )
            return None

        def _after_write(_r, _e):
            wx.CallAfter(self._process_next_permission)

        submit_io_write(work, _after_write)

    def _on_activate(self, event: wx.ActivateEvent) -> None:
        event.Skip()
        if not event.GetActive():
            return
        dlg = self._current_permission_dialog
        if dlg is not None:
            # Delay 100ms to let Windows complete its activation.
            wx.CallLater(100, self._focus_permission_dialog)

    def _focus_permission_dialog(self) -> None:
        """Bring the permission dialog forward and set keyboard focus into it.

        On Windows, alt-tabbing to an app with a modal dialog often
        activates the *frame* (EVT_ACTIVATE fires here) but the dialog's
        child controls don't get keyboard focus — NVDA goes silent.

        Fix: use SetForegroundWindow on the dialog HWND, then SetFocus
        on the target widget, then fire EVENT_OBJECT_FOCUS so NVDA reacts.
        """
        dlg = self._current_permission_dialog
        if dlg is None or not dlg:
            return
        log.debug(
            "focus_permission_dialog: dlg.IsShown=%s dlg.IsActive=%s",
            dlg.IsShown(), dlg.IsActive(),
        )
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = dlg.GetHandle()
                if hwnd:
                    ctypes.windll.user32.BringWindowToTop(hwnd)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    log.debug("focus_permission_dialog: SetForegroundWindow(%s)", hwnd)
            except Exception:
                log.debug("SetForegroundWindow failed", exc_info=True)
        else:
            dlg.Raise()
        # Give WM time to bring the dialog forward, then set focus.
        wx.CallLater(80, self._set_permission_dialog_focus)

    def _set_permission_dialog_focus(self) -> None:
        dlg = self._current_permission_dialog
        if dlg is None or not dlg:
            return
        target = getattr(dlg, "_focus_target", None)
        if target is not None and target:
            target.SetFocus()
            try:
                target.SetInsertionPoint(0)
            except Exception:
                pass
        else:
            dlg.SetFocus()
            target = dlg
        # Fire EVENT_OBJECT_FOCUS so NVDA picks up the change.
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = (target.GetHandle() if target else dlg.GetHandle())
                if hwnd:
                    EVENT_OBJECT_FOCUS = 0x8005
                    OBJID_CLIENT = 0xFFFFFFFC
                    ctypes.windll.user32.NotifyWinEvent(
                        EVENT_OBJECT_FOCUS, hwnd, OBJID_CLIENT, 0
                    )
                    log.debug("NotifyWinEvent(FOCUS) on hwnd=%s", hwnd)
            except Exception:
                log.debug("NotifyWinEvent failed", exc_info=True)
        log.debug(
            "set_permission_dialog_focus: FindFocus=%r target=%r",
            wx.Window.FindFocus(), target,
        )

    def _on_change_dir(self, _event: wx.CommandEvent) -> None:
        new_spec = _ask_for_backend_spec(self, self.backend_spec)
        if new_spec:
            self._set_backend(new_spec)

    def _set_backend(self, spec: str) -> None:
        """Replace the current backend with one built from ``spec``."""
        # Refuse to swap while conversations are open -- they hold a reference
        # to the old backend.
        if self.notebook.GetPageCount() > 0:
            choice = wx.MessageBox(
                "Changing the folder will close all open conversations.\n\n"
                "Continue?",
                "Change folder",
                wx.YES_NO | wx.ICON_QUESTION,
                self,
            )
            if choice != wx.YES:
                return
            while self.notebook.GetPageCount() > 0:
                page = self.notebook.GetPage(0)
                self.notebook.RemovePage(0)
                try:
                    page.Destroy()
                except Exception:
                    pass

        try:
            new_backend = make_backend(spec)
        except Exception as exc:
            log.exception("Failed to build backend from %r", spec)
            wx.MessageBox(
                f"Could not use {spec!r}:\n{exc}",
                "Bad folder",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return

        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                pass

        self.backend_spec = spec
        self.backend = new_backend
        cfg = load_config()
        cfg["claude_dir"] = spec  # key kept for backwards compatibility
        save_config(cfg)
        # The claude-status / session-name maps are keyed by
        # session_id, but those ids only make sense relative to the
        # metadata files under the *previous* backend root.  Clear
        # them so we don't briefly show a stale status (or alias)
        # from another mount until the next poll repopulates from
        # the new sessions/ directory.
        self._claude_status_map = {}
        self._claude_session_names = {}
        self._update_status()
        # Sweep request files that can't possibly belong to a live
        # agent any more (see ``_gc_stale_agent_requests``).
        self._gc_stale_agent_requests()

    def _on_open(self, _event: wx.CommandEvent) -> None:
        if self.backend is None:
            new_spec = _ask_for_backend_spec(self, self.backend_spec)
            if not new_spec:
                return
            self._set_backend(new_spec)
            if self.backend is None:
                return

        dlg = OpenConversationDialog(
            self, self.backend,
            cached_active_sids=set(self._claude_status_map.keys()),
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            info = dlg.selected()
        finally:
            dlg.Destroy()

        if info is None:
            return

        # Don't open the same conversation twice; switch to existing tab.
        for i in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(i)
            if (
                isinstance(page, ConversationPanel)
                and page.backend is self.backend
                and page.rel_path == info.rel_path
            ):
                self.notebook.SetSelection(i)
                return

        try:
            panel = ConversationPanel(self.notebook, self.backend, info.rel_path)
        except Exception:
            log.exception("Could not create ConversationPanel for %s", info.rel_path)
            wx.MessageBox(
                f"Could not open conversation:\n{info.rel_path}",
                "Error",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return

        self.notebook.AddPage(panel, panel.conversation_name(), select=True)
        self._update_window_title()
        # Focus the message list so a screen reader starts reading there.
        panel.list_box.SetFocus()

    def _on_close_tab(self, _event: wx.CommandEvent) -> None:
        idx = self.notebook.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        page = self.notebook.GetPage(idx)
        self.notebook.RemovePage(idx)
        try:
            page.Destroy()
        except Exception:
            log.exception("Error destroying conversation page")
        self._update_window_title()
        # ``RemovePage`` doesn't reliably fire EVT_NOTEBOOK_PAGE_CHANGED
        # on every wx backend, so explicitly resync both status-bar
        # fields with the (possibly different) active tab, or clear
        # them if there are no tabs left.
        self._refresh_claude_status_field()
        self.refresh_send_status_field()

    def _on_reload(self, _event: wx.CommandEvent) -> None:
        panel = self._current_panel()
        if panel is None:
            self.show_transient_status(
                "No conversation open to refresh.", revert_after_ms=3000
            )
            return
        # ``feedback=True`` makes the panel narrate the round-trip via the
        # status bar so the user (and the screen reader) can tell the IO
        # thread actually completed and didn't just queue silently.
        panel.reload(feedback=True)

    def _on_name_changed(self, event: _NameChangedEvent) -> None:
        panel = event.GetEventObject()
        if not isinstance(panel, ConversationPanel):
            return
        for i in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(i) is panel:
                self.notebook.SetPageText(i, panel.conversation_name())
                if self.notebook.GetSelection() == i:
                    self._update_window_title()
                    # Conversation reload may have just discovered the
                    # session_id (it lives in the JSONL header); resync
                    # the claude-status field now instead of waiting up
                    # to a full poll tick.
                    self._refresh_claude_status_field()
                break


def _ask_for_backend_spec(
    parent: wx.Window, current: Optional[str]
) -> Optional[str]:
    """Prompt for either a local/SMB path or an FTP URL."""
    msg = (
        "Enter the path to your Claude Code folder, or an FTP URL.\n\n"
        "Examples:\n"
        "  X:\\.claude\n"
        "  \\\\server\\share\\.claude\n"
        "  /home/me/.claude\n"
        "  ftp://ftpuser:password@host:2121/home/me/.claude"
    )
    dlg = wx.TextEntryDialog(
        parent,
        msg,
        "Choose Claude Code folder",
        value=current or "",
    )
    try:
        if dlg.ShowModal() != wx.ID_OK:
            return None
        spec = dlg.GetValue().strip()
        return spec or None
    finally:
        dlg.Destroy()


# ---------------------------------------------------------------------------
# wx.App
# ---------------------------------------------------------------------------


class App(wx.App):
    """The wx application object.

    Resolves the backend spec in this priority order:
        CLI argument > saved config > prompt the user.

    A "spec" is either a local/SMB path or an ``ftp://`` URL.
    """

    def __init__(self, backend_spec_arg: Optional[str] = None) -> None:
        self._cli_spec = backend_spec_arg
        super().__init__(redirect=False)

    def OnInit(self) -> bool:
        log.info("clacogui starting (wxPython %s)", wx.version())
        self.SetAppName("clacogui")
        self.SetAppDisplayName("clacogui")
        self.SetClassName("clacogui")
        cfg = load_config()
        spec = self._cli_spec or cfg.get("claude_dir")

        if spec and _looks_local(spec) and not os.path.isdir(spec):
            log.warning("Configured local folder %r does not exist", spec)
            spec = None

        frame = MainFrame(spec)
        frame.Show()
        self.SetTopWindow(frame)

        if frame.backend is None:
            wx.CallAfter(self._first_run_prompt, frame)
        elif spec:
            cfg["claude_dir"] = spec
            save_config(cfg)
            wx.CallAfter(self._restore_tabs, frame, cfg)

        return True

    def _restore_tabs(self, frame: MainFrame, cfg: dict) -> None:
        open_tabs = cfg.get("open_tabs") or []
        if not open_tabs or frame.backend is None:
            return
        active_idx = cfg.get("active_tab_index", 0)
        for rel_path in open_tabs:
            try:
                if not frame.backend.exists(rel_path):
                    log.debug("Skipping missing tab: %s", rel_path)
                    continue
                panel = ConversationPanel(
                    frame.notebook, frame.backend, rel_path
                )
                frame.notebook.AddPage(
                    panel, panel.conversation_name(), select=False
                )
            except Exception:
                log.debug("Failed to restore tab %s", rel_path, exc_info=True)
        if frame.notebook.GetPageCount() > 0:
            idx = min(active_idx, frame.notebook.GetPageCount() - 1)
            frame.notebook.SetSelection(max(0, idx))
            frame._update_window_title()

    def _first_run_prompt(self, frame: MainFrame) -> None:
        new_spec = _ask_for_backend_spec(frame, None)
        if new_spec:
            frame._set_backend(new_spec)

    def OnExit(self) -> int:
        log.info("clacogui exiting")
        return 0


def _looks_local(spec: str) -> bool:
    lower = spec.strip().lower()
    return not (lower.startswith("ftp://") or lower.startswith("ftps://"))


# ---------------------------------------------------------------------------
# Agent (hook) helpers
# ---------------------------------------------------------------------------


def _safe_list_basenames(backend: FsBackend, rel: str) -> list[str]:
    """Return ``*.json`` basenames in ``rel``, or [] if the dir is missing.

    Sorted by name so the agent's ``<unix-millis>_<hex>.json`` filenames
    come out in arrival order.
    """
    try:
        entries = backend.list_dir(rel)
    except FileNotFoundError:
        return []
    except OSError:
        log.debug("list_dir(%s) failed", rel, exc_info=True)
        return []
    names = [e.name for e in entries if not e.is_dir and e.name.endswith(".json")]
    names.sort()
    return names


def _read_claude_session_state(
    backend: FsBackend,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read ``~/.claude/sessions/*.json`` and return two parallel maps.

    Returns ``(status_map, names_map)`` where:

    * ``status_map``  maps ``sessionId`` -> ``status`` ("idle" / "busy" /
      ...) for the leftmost status-bar field.
    * ``names_map``   maps ``sessionId`` -> ``name`` (the alias the
      user set with ``/rename aaa1`` -- the JSONL transcript never
      sees this string, so the tab title would otherwise fall back
      to "first turn preview" / "UUID").  Entries whose ``name`` is
      missing or empty are simply absent from the map; callers should
      treat that as "no alias".

    Each metadata file is written by claude itself and looks roughly
    like::

        {"pid": 701412, "sessionId": "d155f9ea-...",
         "status": "idle", "name": "aaa1", ...}

    More than one file may carry the same ``sessionId`` (e.g. after a
    resume), so we visit them mtime-descending and keep the freshest
    entry for each id.  Files whose mtime is older than
    :data:`STALE_CLAUDE_SESSION_AGE_SEC` are skipped -- they almost
    certainly belong to dead processes whose claude exited without
    cleaning up.

    Runs on the IO worker thread (called from ``_poll_agent.work``),
    so logs at debug only and never raises.

    A single pass over the directory listing builds both maps so we
    don't pay the ``list_dir`` + per-file ``read`` cost twice -- on
    FTP every metadata file is its own round trip.
    """
    try:
        entries = backend.list_dir(CLAUDE_SESSIONS_DIR)
    except FileNotFoundError:
        return {}, {}
    except OSError:
        log.debug("list_dir(%s) failed", CLAUDE_SESSIONS_DIR, exc_info=True)
        return {}, {}
    cutoff = time.time() - STALE_CLAUDE_SESSION_AGE_SEC
    candidates: list[tuple[float, str]] = []
    for e in entries:
        if e.is_dir or not e.name.endswith(".json"):
            continue
        if e.mtime and e.mtime < cutoff:
            continue
        candidates.append((e.mtime or 0.0, e.name))
    candidates.sort(reverse=True)
    statuses: dict[str, str] = {}
    names: dict[str, str] = {}
    for _mtime, fname in candidates:
        rel = f"{CLAUDE_SESSIONS_DIR}/{fname}"
        try:
            text = backend.read_text(rel)
        except OSError:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId") or data.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        status = data.get("status")
        if isinstance(status, str) and status:
            # First entry per id wins (we sorted mtime-desc).
            statuses.setdefault(sid, status)
        nm = data.get("name")
        if isinstance(nm, str) and nm:
            names.setdefault(sid, nm)
    return statuses, names


_NOTIFICATION_WAVS: tuple[str, ...] = (
    r"C:\Windows\Media\notify.wav",
    r"C:\Windows\Media\Windows Notify System Generic.wav",
    r"C:\Windows\Media\chord.wav",
    r"C:\Windows\Media\Alarm01.wav",
)


def _bundled_wav_path(name: str) -> Optional[str]:
    """Resolve a wav file shipped alongside ``gui.py`` itself.

    Returns ``None`` if the file isn't there (e.g. a stripped-down
    install or someone moved it).  We check ``__file__``'s directory
    rather than the cwd because ``run.cmd`` may launch us from
    elsewhere depending on how the user invokes it.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return None
    path = os.path.join(here, name)
    return path if os.path.isfile(path) else None


# Custom "Claude finished a response" sound.  Falls through to the
# system-WAV path (``_NOTIFICATION_WAVS``) if the file isn't installed
# alongside the source -- the user can drop it in or remove it without
# breaking everything.
_COMPLETION_WAV_NAME: str = "wubbalubbadubdub.wav"
# Played by ``_play_error_sound`` when a send-write fails.  Bundled next
# to ``gui.py`` -- silently no-ops if the file was removed.
_ERROR_WAV_NAME: str = "win95-error.wav"


def _play_error_sound(source: str = "unknown") -> None:
    """Best-effort error sound (used when a send fails).

    ``source`` is a short tag identifying the caller (``send_write``,
    ``interrupt_write``, ``send_timeout``, ``resend_rewrite``,
    ``failed_file``, ...).  It's logged at INFO so a user hearing a
    "false positive" ping can grep the log and see exactly which code
    path rang the bell, rather than having to guess.
    """
    log.info("Playing error sound (source=%s)", source)
    if winsound is None:
        try:
            wx.Bell()
        except Exception:
            pass
        return
    wav = _bundled_wav_path(_ERROR_WAV_NAME)
    if wav is None:
        try:
            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            log.debug("MessageBeep(MB_ICONHAND) failed", exc_info=True)
        return
    try:
        winsound.PlaySound(
            wav,
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except Exception:
        log.debug("PlaySound %s failed", wav, exc_info=True)


def _play_notification_sound(completion: bool = False) -> None:
    """Best-effort 'ping' sound for a Claude notification.

    Empirically on at least one Windows 11 setup, ``winsound.Beep`` and
    ``MessageBeep`` are silent (the underlying audio path appears to be
    routed somewhere that doesn't reach the speakers), but
    ``winsound.PlaySound`` with a WAV file works fine.

    ``completion=True`` is reserved for "Claude finished a turn"
    (Stop / SubagentStop) and prefers the bundled
    ``wubbalubbadubdub.wav`` so the user can tell that case apart from
    "needs your attention" pings.  Permission requests, idle reminders,
    and unknown events keep the previous behavior.

    The fallback chain (first hit wins):

      1. (only if ``completion``) bundled ``wubbalubbadubdub.wav``.
      2. Known system WAVs (``notify.wav`` etc.).
      3. The ``SystemAsterisk`` alias.
      4. ``MessageBeep`` for completeness.
      5. ``wx.Bell()`` as the last resort (non-Windows).

    ``SND_ASYNC`` keeps us off the audio path so the wx main thread can
    keep handling input while the sound plays.  ``SND_NODEFAULT``
    prevents Windows from substituting a "default beep" if the file we
    chose is somehow missing.
    """
    kind = "completion" if completion else "default"
    log.info("Playing notification sound (%s)", kind)
    if winsound is not None:
        flags = (
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
        candidates: list[str] = []
        if completion:
            bundled = _bundled_wav_path(_COMPLETION_WAV_NAME)
            if bundled:
                candidates.append(bundled)
            else:
                log.debug(
                    "Bundled completion wav %r not found; falling back",
                    _COMPLETION_WAV_NAME,
                )
        candidates.extend(_NOTIFICATION_WAVS)
        for wav in candidates:
            if not os.path.isfile(wav):
                continue
            try:
                winsound.PlaySound(wav, flags)
                return
            except Exception:
                log.debug("PlaySound %s failed", wav, exc_info=True)
        try:
            winsound.PlaySound(
                "SystemAsterisk",
                winsound.SND_ALIAS | winsound.SND_ASYNC,
            )
            return
        except Exception:
            log.debug("PlaySound SystemAsterisk failed", exc_info=True)
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        except Exception:
            log.debug("MessageBeep failed", exc_info=True)
    try:
        wx.Bell()
    except Exception:
        pass


# Order in which we render known tool_input fields when present.  Anything
# not in this list comes after, in dict order, with a humanized label.
# This is purely a UX nicety; renderer is value-driven, not schema-driven.
_PERMISSION_FIELD_ORDER: tuple[str, ...] = (
    "description",
    "command",
    "file_path",
    "path",
    "url",
    "pattern",
    "glob",
    "type",
    "old_string",
    "new_string",
    "replace_all",
    "content",
    "prompt",
    "offset",
    "limit",
)

# Friendlier labels for the most common Claude Code tool fields.
_PERMISSION_FIELD_LABELS: dict[str, str] = {
    "command": "Command",
    "description": "What it's trying to do",
    "file_path": "File",
    "path": "Path",
    "url": "URL",
    "pattern": "Pattern",
    "glob": "Filename glob",
    "type": "File type",
    "old_string": "Find (old text)",
    "new_string": "Replace with (new text)",
    "replace_all": "Replace all occurrences",
    "content": "Content",
    "prompt": "Prompt",
    "offset": "Start line",
    "limit": "Line count",
    # Synthesized by ``PermissionRequestDialog`` for Edit-style tools.
    "diff": "Diff (about to be applied)",
}


def _unified_diff(old: str, new: str, context: int = 3) -> str:
    """Compute a unified diff between two strings.

    Used by the permission dialog for ``Edit`` / ``MultiEdit`` tool
    requests so the user can see *exactly* what will change before
    approving.  ``context`` lines surround each hunk.

    Newlines are normalized to ``\\n`` and a trailing newline is
    stripped so the rendered text doesn't end with a phantom blank.
    """
    import difflib

    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="old",
        tofile="new",
        n=context,
        lineterm="",
    )
    return "\n".join(diff)


def _hscrollbar_h() -> int:
    """Return the OS-native height of a horizontal scrollbar, in pixels.

    Used when sizing multi-line ``wx.TextCtrl``s that have ``HSCROLL`` set:
    if we don't add this many pixels to ``MinSize.height``, the scrollbar
    paints over the first row of text and the field looks empty.
    """
    try:
        h = wx.SystemSettings.GetMetric(wx.SYS_HSCROLL_Y)
    except Exception:
        h = -1
    if h is None or h <= 0:
        # Conservative default for Win10/11 at 100% DPI; high-DPI
        # systems usually report a real value via SystemSettings, so
        # this only kicks in on platforms where the metric is broken.
        h = 18
    return int(h)


def _permission_field_label(name: str) -> str:
    if name in _PERMISSION_FIELD_LABELS:
        return _PERMISSION_FIELD_LABELS[name]
    return name.replace("_", " ").strip().capitalize() or name


def _permission_field_value(value: object) -> str:
    """Stringify a tool_input value for display.

    Strings pass through; everything else gets pretty-printed JSON so a
    screen reader can navigate structured payloads line by line.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return ""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _ordered_tool_input(tool_input: dict) -> list[tuple[str, object]]:
    """Stable display order: known important fields first, rest after."""
    seen: set[str] = set()
    out: list[tuple[str, object]] = []
    for k in _PERMISSION_FIELD_ORDER:
        if k in tool_input:
            out.append((k, tool_input[k]))
            seen.add(k)
    for k, v in tool_input.items():
        if k not in seen:
            out.append((k, v))
    return out


# Where Claude persists an "always allow" rule the user picked.  We render
# the destination in the button label so the user knows *which* settings
# file they're editing -- ``localSettings`` (.claude/settings.local.json)
# vs ``userSettings`` (~/.claude/settings.json) is a meaningful difference.
_PERMISSION_DESTINATION_LABELS: dict[str, str] = {
    "session":         "this session only",
    "localSettings":   "this project (local)",
    "projectSettings": "this project",
    "userSettings":    "all projects",
}


def _summarize_permission_suggestion(sug: dict) -> str:
    """Return a one-line, human-readable description of a Claude
    ``permission_suggestions`` entry.

    Optimized for the kinds of suggestions Claude actually emits:
    ``addRules`` with a list of ``{toolName, ruleContent}`` rules.  Other
    types (``setMode``, ``addDirectories``, ...) fall through to a more
    generic dump so the user still has *some* idea what they're approving.
    """
    if not isinstance(sug, dict):
        return str(sug)

    sug_type = sug.get("type") or ""
    dest = sug.get("destination") or ""
    dest_label = _PERMISSION_DESTINATION_LABELS.get(dest, dest)

    if sug_type == "addRules":
        rules = sug.get("rules") or []
        rule_strs: list[str] = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            tool = r.get("toolName") or ""
            content = r.get("ruleContent")
            if content:
                rule_strs.append(f"{tool}({content})")
            else:
                rule_strs.append(tool)
        joined = ", ".join(rule_strs) or "(no rules)"
        if dest_label:
            return f"{joined} \u2192 {dest_label}"
        return joined

    if sug_type == "setMode":
        mode = sug.get("mode") or ""
        return f"set mode to {mode!r}" + (
            f" \u2192 {dest_label}" if dest_label else ""
        )

    if sug_type == "addDirectories":
        dirs = ", ".join(sug.get("directories") or [])
        return f"trust dirs: {dirs}" + (
            f" \u2192 {dest_label}" if dest_label else ""
        )

    # Fallback: surface the type so the user at least sees what kind of
    # change they're approving.
    return f"{sug_type}" + (f" ({dest_label})" if dest_label else "")


class PermissionRequestDialog(wx.Dialog):
    """Modal shown when the agent receives a PermissionRequest event.

    Layout, top to bottom:

      * Tool name -- focusable read-only multi-line edit (so the
        screen reader's caret can navigate it; a StaticText is invisible
        to NVDA's tab-order).  Tool name is also baked into the dialog
        title so it's announced as soon as the dialog appears.
      * Working directory (focusable read-only edit, only if present).
      * One labeled row per ``tool_input`` field, plus a synthesized
        ``Diff`` row for ``Edit`` / ``MultiEdit`` so the user sees the
        actual unified diff before approving.  Every value is a
        multi-line read-only TextCtrl (single-line read-only edits on
        Windows have no caret you can move with arrow keys, which
        screen-reader users rely on).
      * Optional raw-JSON view (hidden by default, ``Show raw JSON``
        button toggles it).
      * Optional Reason field (forwarded to the agent on deny).
      * "Allow always" lives on its own row above Allow / Deny so a
        long suggestion label can't overlap them.
      * Allow / Deny  -- Deny is the default for safety.

    Diagnostic noise (``session_id``, ``transcript_path``, ``tool_use_id``,
    ``permission_mode``, ``hook_event_name``, ``_clacogui_request_id``) is
    not shown by default -- it's available behind the ``Show raw JSON``
    toggle button (and also in the request file on disk).
    """

    # Visual height clamp for multi-line read-only fields, in lines.  Long
    # commands (e.g. a 60-line python heredoc) still scroll inside.
    _MAX_FIELD_LINES: int = 12
    _MIN_MULTILINE_LINES: int = 3

    # Inputs longer than this with no newlines still get a multi-line edit,
    # so very long single-line file paths or grep patterns are readable.
    _MULTILINE_CHAR_THRESHOLD: int = 120

    # Fields that we always want rendered as multi-line, even if the
    # current payload happens to fit on one line.  Most Bash commands are
    # one-liners but the user has asked us to treat them as code blocks
    # so the screen reader can navigate them with arrow keys.
    _ALWAYS_MULTILINE: frozenset = frozenset({"command", "content"})

    # Toggle button labels for the hidden raw-JSON view.  Both carry an
    # ``&J`` mnemonic so the user can flip the panel from the keyboard
    # without colliding with &Allow / &Deny.
    _JSON_BTN_LABEL_SHOW: str = "Show raw &JSON"
    _JSON_BTN_LABEL_HIDE: str = "Hide raw &JSON"

    def __init__(self, parent: wx.Window, event: dict) -> None:
        tool = event.get("tool_name") or event.get("toolName") or "(unknown tool)"
        # Bake the tool name into the dialog title so a screen reader
        # announces it as soon as the dialog opens (and so it's the
        # first thing in the alt-tab / window list as well).
        super().__init__(
            parent,
            title=f"{tool} permission request - clacogui",
            size=(820, 640),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            name="PermissionRequest",
        )
        # Public API consumed by ``MainFrame._show_permission_dialog``:
        #
        #   ``allowed``               -- True if the user picked Allow or
        #                                Allow-always; False on Deny / close.
        #   ``reason``                -- contents of the optional Reason field;
        #                                forwarded to the agent on deny.
        #   ``updated_permissions``   -- list of permission_suggestion entries
        #                                Claude sent us that the user opted
        #                                into via Allow-always.  Empty for
        #                                plain Allow / Deny.  Wire-format
        #                                matches Claude Code's
        #                                ``hookSpecificOutput.decision
        #                                .updatedPermissions`` array.
        self.allowed: bool = False
        self.reason: str = ""
        self.updated_permissions: list[dict] = []
        # For AskUserQuestion: the ``updatedInput`` payload (original
        # questions + an ``answers`` map) sent back on Allow so the tool
        # resolves with the user's selection instead of prompting in a TUI.
        self.updated_input: dict = {}

        cwd = event.get("cwd") or ""
        tool_input = event.get("tool_input") or event.get("toolInput") or {}
        if not isinstance(tool_input, dict):
            tool_input = {"value": tool_input}

        # AskUserQuestion gets a purpose-built question/answer UI instead
        # of the generic field dump.  Detect it up front so the rest of
        # the constructor can branch.
        tool_name_norm = str(tool)
        self._is_ask_question = (
            tool_name_norm == "AskUserQuestion"
            and isinstance(tool_input.get("questions"), list)
            and len(tool_input.get("questions")) > 0
        )
        self._aq_questions: list[dict] = (
            list(tool_input.get("questions"))
            if self._is_ask_question
            else []
        )
        self._aq_tool_input: dict = tool_input
        # Per-question list of (option_label, control) so we can read
        # selections back on Allow.  Populated by ``_build_question_ui``.
        self._aq_controls: list[tuple[dict, list]] = []

        # ``permission_suggestions`` is what Claude Code would have offered
        # the user as "always allow" choices in its built-in dialog.  Each
        # entry is a dict with ``type`` ``addRules`` (typically) plus a
        # ``destination`` like ``localSettings`` / ``userSettings``.  We
        # render one button per suggestion so the user can pick which scope
        # to persist at.
        self._suggestions: list[dict] = list(
            event.get("permission_suggestions") or []
        )

        panel = wx.Panel(self)
        self._panel = panel
        vbox = wx.BoxSizer(wx.VERTICAL)

        # Tool name as a focusable read-only TextCtrl.  StaticText is
        # not in the tab order on Windows, so a screen-reader user
        # could not arrow / tab to it -- which the user reported as
        # "tool name is not readable".  A read-only TextCtrl IS
        # focusable; a bold label sits above it.
        tool_lbl = wx.StaticText(panel, label="Tool:")
        tf = tool_lbl.GetFont()
        tf.MakeBold()
        tool_lbl.SetFont(tf)
        vbox.Add(tool_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        # Multi-line read-only so the caret can move and SR can read
        # each character (single-line TE_READONLY has no caret on
        # Windows, which was the original "tool name not readable"
        # symptom).  We deliberately do NOT add TE_DONTWRAP/HSCROLL
        # here: a single-row multiline edit with a horizontal
        # scrollbar reserves ~17 px of its visible height for the
        # bar, which covered the actual text on Windows (the user's
        # "fields blocked by horizontal scroller" report).  Tool
        # names are short, so wrapping is harmless.
        self._tool_ctrl = wx.TextCtrl(
            panel,
            value=str(tool),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            name="Tool",
        )
        big = self._tool_ctrl.GetFont()
        big.SetPointSize(big.GetPointSize() + 2)
        self._tool_ctrl.SetFont(big)
        line_h = self._tool_ctrl.GetCharHeight() or 16
        self._tool_ctrl.SetMinSize((-1, line_h + 14))
        vbox.Add(self._tool_ctrl, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        if cwd:
            cwd_lbl = wx.StaticText(panel, label="Working directory:")
            vbox.Add(cwd_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            # Working directory fits on one line in normal cases; if
            # it doesn't, soft-wrap is preferable to a horizontal
            # scrollbar that hides the whole path.
            cwd_ctrl = wx.TextCtrl(
                panel,
                value=cwd,
                style=wx.TE_MULTILINE | wx.TE_READONLY,
                name="Working directory",
            )
            line_h = cwd_ctrl.GetCharHeight() or 16
            cwd_ctrl.SetMinSize((-1, line_h + 12))
            vbox.Add(cwd_ctrl, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        # First focus target: the most-likely-relevant content field.
        # We *prefer* the description ("What it's trying to do") since
        # that's the human-readable summary; otherwise fall back to
        # whatever is first.
        first_focus: Optional[wx.Window] = None
        description_focus: Optional[wx.Window] = None

        # Synthesize a "Diff" entry for Edit-style tool calls.  Claude
        # sends old_string + new_string; the user wants to see the
        # actual unified diff before approving.  We render it as a
        # multi-line monospace edit so the screen reader can navigate
        # by line.
        rendered_input = (
            [] if self._is_ask_question
            else self._maybe_inject_diff(tool, tool_input)
        )

        if self._is_ask_question:
            first_focus = self._build_question_ui(panel, vbox)

        for key, value in rendered_input:
            label = _permission_field_label(key) + ":"
            text_value = _permission_field_value(value)

            vbox.Add(
                wx.StaticText(panel, label=label),
                0,
                wx.LEFT | wx.RIGHT | wx.TOP,
                8,
            )

            # *Every* value control is a multi-line read-only TextCtrl,
            # not just the obviously-multi-line ones.  A single-line
            # TE_READONLY edit on Windows has no caret you can move
            # with the arrow keys, which is what makes NVDA report
            # "What it's trying to do" / "Tool" as not navigable -- the
            # user reported exactly that.  A multi-line read-only
            # TextCtrl *does* have a caret you can step through with
            # arrow keys, so a screen reader can read the value
            # character-by-character.
            #
            # Style picks:
            #   * Big code-shaped fields (commands, file content,
            #     diffs, anything multi-line) keep ``TE_DONTWRAP |
            #     HSCROLL`` so columns line up; we also reserve
            #     vertical space for the horizontal scrollbar so it
            #     doesn't paint over the first line of text.
            #   * Short fields wrap softly (no DONTWRAP/HSCROLL): the
            #     previous code drew a ~17 px horizontal scrollbar
            #     inside a 1-row-tall control, which covered the
            #     entire text region on Windows -- the user's
            #     "fields not visible because blocked by the
            #     horizontal scroller" report.
            lines = text_value.count("\n") + 1
            wants_big = (
                key in self._ALWAYS_MULTILINE
                or key == "diff"
                or lines > 1
                or len(text_value) > self._MULTILINE_CHAR_THRESHOLD
            )
            if wants_big:
                style = (
                    wx.TE_MULTILINE
                    | wx.TE_READONLY
                    | wx.TE_DONTWRAP
                    | wx.HSCROLL
                )
            else:
                style = wx.TE_MULTILINE | wx.TE_READONLY
            ctrl = wx.TextCtrl(
                panel,
                value=text_value,
                style=style,
                name=_permission_field_label(key),
            )
            if wants_big:
                rows = max(
                    self._MIN_MULTILINE_LINES,
                    min(self._MAX_FIELD_LINES, lines),
                )
                proportion = 1
                # Reserve room for the bottom horizontal scrollbar so
                # it doesn't hide the first row of text.
                vpad = 12 + _hscrollbar_h()
            else:
                rows = 1
                proportion = 0
                vpad = 12
            line_h = ctrl.GetCharHeight() or 16
            ctrl.SetMinSize((-1, rows * line_h + vpad))
            # Monospace for code-shaped fields so columns line up.
            if key in ("command", "content", "old_string", "new_string", "diff"):
                ctrl.SetFont(
                    wx.Font(
                        wx.FontInfo().Family(wx.FONTFAMILY_TELETYPE)
                    )
                )
            vbox.Add(
                ctrl,
                proportion,
                wx.LEFT | wx.RIGHT | wx.EXPAND,
                8,
            )
            if first_focus is None:
                first_focus = ctrl
            if key == "description" and description_focus is None:
                description_focus = ctrl

        # ---- raw JSON (hidden by default) -------------------------------
        # We keep a verbatim view of the entire event payload available
        # on demand for power users / debugging.  Both the label and the
        # text widget start hidden; the toggle button below flips them.
        self._json_label = wx.StaticText(panel, label="Raw JSON (full event):")
        self._json_text = wx.TextCtrl(
            panel,
            value=json.dumps(event, indent=2, ensure_ascii=False),
            style=(
                wx.TE_MULTILINE
                | wx.TE_READONLY
                | wx.TE_DONTWRAP
                | wx.HSCROLL
            ),
            name="RawJSON",
        )
        self._json_text.SetFont(
            wx.Font(wx.FontInfo().Family(wx.FONTFAMILY_TELETYPE))
        )
        # We deliberately do NOT pin a tall MinSize on the JSON pane:
        # the previous version would push other widgets off the dialog
        # when the user toggled it on.  Letting it be small-by-default
        # and grow with the sizer's free space is much friendlier.
        self._json_label.Show(False)
        self._json_text.Show(False)
        vbox.Add(self._json_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        vbox.Add(self._json_text, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        # ---- reason ------------------------------------------------------
        vbox.Add(
            wx.StaticText(panel, label="Reason (optional, sent on Deny):"),
            0,
            wx.LEFT | wx.RIGHT | wx.TOP,
            8,
        )
        self._reason = wx.TextCtrl(panel, name="Reason")
        vbox.Add(self._reason, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        # ---- "Allow always" row (its own row -- avoids long labels
        # ---- overlapping the Allow / Deny row when the suggestion text
        # ---- is wider than the dialog can fit on one line).
        self._allow_always_btn: Optional[wx.Button] = None
        if self._suggestions:
            allow_always_row = wx.BoxSizer(wx.HORIZONTAL)
            allow_always_row.AddStretchSpacer(1)
            self._allow_always_btn = wx.Button(
                panel,
                wx.ID_ANY,
                self._allow_always_label(),
                name="AllowAlways",
            )
            allow_always_row.Add(self._allow_always_btn, 0)
            vbox.Add(
                allow_always_row,
                0,
                wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND,
                8,
            )
            self.Bind(
                wx.EVT_BUTTON, self._on_allow_always, self._allow_always_btn
            )

        # ---- buttons -----------------------------------------------------
        btns = wx.BoxSizer(wx.HORIZONTAL)
        # Left side: power-user toggle.  Mnemonic on 'J' so it's reachable
        # without conflicting with Allow / Deny.
        self._json_btn = wx.Button(
            panel,
            wx.ID_ANY,
            self._JSON_BTN_LABEL_SHOW,
            name="ToggleRawJson",
        )
        btns.Add(self._json_btn, 0, wx.RIGHT, 8)
        btns.AddStretchSpacer(1)
        # For AskUserQuestion the affirmative action is "submit my
        # answer", not "allow a tool", so relabel.  The default button is
        # the affirmative one here (Submit) since picking an answer is the
        # expected action; for ordinary tools Deny stays the safe default.
        if self._is_ask_question:
            allow_btn = wx.Button(panel, wx.ID_YES, "&Submit")
            deny_btn = wx.Button(panel, wx.ID_NO, "&Cancel")
            allow_btn.SetDefault()
        else:
            allow_btn = wx.Button(panel, wx.ID_YES, "&Allow")
            deny_btn = wx.Button(panel, wx.ID_NO, "&Deny")
            deny_btn.SetDefault()  # safer default if user just hits Enter
        btns.Add(allow_btn, 0, wx.RIGHT, 8)
        btns.Add(deny_btn, 0)
        vbox.Add(btns, 0, wx.ALL | wx.EXPAND, 8)

        panel.SetSizer(vbox)

        # Top-level sizer on the dialog itself, so that ``self.Layout()``
        # actually re-flows the inner panel when widgets are shown /
        # hidden (which is what fixes the "raw JSON appears on top of
        # other widgets" symptom -- the panel's sizer never re-ran
        # before).
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        self.Bind(wx.EVT_BUTTON, self._on_allow, id=wx.ID_YES)
        self.Bind(wx.EVT_BUTTON, self._on_deny, id=wx.ID_NO)
        self.Bind(wx.EVT_BUTTON, self._on_toggle_json, self._json_btn)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_ACTIVATE, self._on_dlg_activate)

        # Land focus on the description field (the human summary) when
        # present, falling back to the first content field, so a screen
        # reader immediately announces what Claude is asking for rather
        # than parking on Deny.
        focus_target = description_focus or first_focus
        self._focus_target: Optional[wx.Window] = focus_target
        if focus_target is not None:
            focus_target.SetFocus()
            try:
                focus_target.SetInsertionPoint(0)
            except Exception:
                pass

        # Focus watchdog: on Windows, alt-tabbing to an app with a modal
        # dialog often leaves focus in limbo.  This timer fires every
        # 200ms and forces focus onto _focus_target if the dialog is the
        # foreground window but no child currently has keyboard focus.
        self._focus_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_focus_check, self._focus_timer)
        self._focus_timer.Start(200)

        # Audible cue when a request appears.
        _play_notification_sound()

    # -- AskUserQuestion UI ------------------------------------------------

    def _build_question_ui(
        self, panel: wx.Panel, vbox: wx.BoxSizer
    ) -> Optional[wx.Window]:
        """Render one labelled group per question with selectable options.

        Single-select questions use radio buttons; multi-select use
        checkboxes.  Records ``(question, [(label, ctrl), ...])`` in
        ``self._aq_controls`` so ``_collect_answers`` can read selections
        back.  Returns the first option control to receive focus.
        """
        first_ctrl: Optional[wx.Window] = None

        for q in self._aq_questions:
            if not isinstance(q, dict):
                continue
            q_text = str(q.get("question") or "")
            header = str(q.get("header") or "")
            multi = bool(q.get("multiSelect"))
            options = q.get("options") or []
            if not isinstance(options, list):
                options = []

            # Question prompt as a focusable read-only edit so screen
            # readers can navigate it (StaticText isn't in tab order on
            # Windows -- same rationale as the rest of this dialog).
            hdr = header or "Question"
            lbl = wx.StaticText(panel, label=f"{hdr}:")
            f = lbl.GetFont()
            f.MakeBold()
            lbl.SetFont(f)
            vbox.Add(lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

            q_ctrl = wx.TextCtrl(
                panel,
                value=q_text,
                style=wx.TE_MULTILINE | wx.TE_READONLY,
                name="Question",
            )
            line_h = q_ctrl.GetCharHeight() or 16
            q_lines = max(1, q_text.count("\n") + 1)
            q_ctrl.SetMinSize((-1, min(4, q_lines) * line_h + 12))
            vbox.Add(q_ctrl, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

            pairs: list[tuple[str, wx.Window]] = []
            first_in_group = True
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or "")
                if not label:
                    continue
                desc = str(opt.get("description") or "")
                shown = f"{label} - {desc}" if desc else label
                if multi:
                    ctrl = wx.CheckBox(panel, label=shown, name=label)
                else:
                    style = wx.RB_GROUP if first_in_group else 0
                    ctrl = wx.RadioButton(
                        panel, label=shown, style=style, name=label
                    )
                first_in_group = False
                vbox.Add(ctrl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
                pairs.append((label, ctrl))
                if first_ctrl is None:
                    first_ctrl = ctrl

            self._aq_controls.append((q, pairs))

        return first_ctrl

    def _collect_answers(self) -> Optional[dict]:
        """Read selected option label(s) per question into an answers map.

        Returns ``{question_text: label}`` for single-select and
        ``{question_text: "labelA, labelB"}`` for multi-select.  Returns
        ``None`` if any question has no selection (caller keeps the dialog
        open).  Single-select RadioButtons always have a default selection
        (the first), so only multi-select can be empty.
        """
        answers: dict = {}
        for q, pairs in self._aq_controls:
            q_text = str(q.get("question") or "")
            multi = bool(q.get("multiSelect"))
            selected = [lbl for (lbl, ctrl) in pairs if ctrl.GetValue()]
            if not selected:
                return None
            answers[q_text] = ", ".join(selected) if multi else selected[0]
        return answers

    # -- Edit-tool diff synthesis ------------------------------------------

    def _maybe_inject_diff(
        self,
        tool: str,
        tool_input: dict,
    ) -> list[tuple[str, object]]:
        """Augment ``tool_input`` with a synthesized ``diff`` field for
        ``Edit``-style tool calls.

        Claude Code sends ``old_string`` + ``new_string`` (and optionally
        ``replace_all``) for the ``Edit`` tool.  Reading those side by
        side is annoying; the user wants a unified diff inline.  We
        compute it on the wx side rather than serverside so older agent
        installs still benefit immediately after a GUI upgrade.

        We don't *replace* old_string / new_string -- the diff is added
        as an additional field so power users still see the raw inputs.
        """
        ordered = _ordered_tool_input(tool_input)
        if tool not in ("Edit", "MultiEdit"):
            return ordered
        # MultiEdit has an "edits" list rather than top-level old/new.
        if tool == "MultiEdit":
            edits = tool_input.get("edits") or []
            if not isinstance(edits, list):
                return ordered
            diff_chunks: list[str] = []
            for i, e in enumerate(edits):
                if not isinstance(e, dict):
                    continue
                old = e.get("old_string") or ""
                new = e.get("new_string") or ""
                if not old and not new:
                    continue
                diff_chunks.append(f"--- edit {i + 1} ---")
                diff_chunks.append(_unified_diff(str(old), str(new)))
            if not diff_chunks:
                return ordered
            return [("diff", "\n".join(diff_chunks).rstrip())] + ordered
        # Edit:
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return ordered
        if old == new:
            return ordered
        diff = _unified_diff(old, new)
        if not diff.strip():
            return ordered
        # Place diff right after description (or first if no description)
        # so the user sees it before the raw old/new dumps.
        out: list[tuple[str, object]] = []
        injected = False
        for k, v in ordered:
            out.append((k, v))
            if k == "description" and not injected:
                out.append(("diff", diff))
                injected = True
        if not injected:
            out.insert(0, ("diff", diff))
        return out

    def _stop_focus_timer(self) -> None:
        if hasattr(self, "_focus_timer") and self._focus_timer.IsRunning():
            self._focus_timer.Stop()

    def _on_allow(self, _event: wx.CommandEvent) -> None:
        self._stop_focus_timer()
        # AskUserQuestion: collect the chosen option(s) and build the
        # ``updatedInput`` payload (original questions + answers map).
        if self._is_ask_question:
            answers = self._collect_answers()
            if answers is None:
                # A required question is unanswered; keep the dialog open
                # and resume the focus watchdog we just stopped.
                if hasattr(self, "_focus_timer") and not self._focus_timer.IsRunning():
                    self._focus_timer.Start(200)
                wx.MessageBox(
                    "Please choose an answer for every question before "
                    "submitting.",
                    "Answer required",
                    wx.OK | wx.ICON_INFORMATION,
                    self,
                )
                return
            self.updated_input = {
                "questions": self._aq_questions,
                "answers": answers,
            }
        self.allowed = True
        self.reason = self._reason.GetValue().strip()
        # No "always" rule -- this is a one-shot allow.
        self.updated_permissions = []
        self.EndModal(wx.ID_YES)

    # -- Allow-always helpers ----------------------------------------------

    def _allow_always_label(self) -> str:
        """Label for the Allow-always button.

        With one suggestion: ``Allow al&ways for: <summary>``
        With several:        ``Allow al&ways...`` (clicks open a chooser)
        Empty suggestions:   button isn't shown at all.

        Mnemonic ``&w`` on the word "always" so Alt-W triggers it without
        colliding with Alt-A (Allow) or Alt-D (Deny).
        """
        if len(self._suggestions) == 1:
            summary = _summarize_permission_suggestion(self._suggestions[0])
            return f"Allow al&ways for: {summary}"
        return "Allow al&ways..."

    def _on_allow_always(self, _event: wx.CommandEvent) -> None:
        if not self._suggestions:
            return
        if len(self._suggestions) == 1:
            self._commit_allow_always([self._suggestions[0]])
            return
        # Multiple suggestions -- pop a menu so the user picks the scope
        # / specificity they actually want.  Each item carries its index
        # in self._suggestions as the menu id.
        menu = wx.Menu()
        for i, sug in enumerate(self._suggestions):
            menu.Append(wx.ID_HIGHEST + 1 + i,
                        _summarize_permission_suggestion(sug))
        # Bind once for the whole range.
        def on_pick(evt: wx.CommandEvent) -> None:
            idx = evt.GetId() - (wx.ID_HIGHEST + 1)
            if 0 <= idx < len(self._suggestions):
                self._commit_allow_always([self._suggestions[idx]])
        self.Bind(wx.EVT_MENU, on_pick)
        # PopupMenu blocks until the user picks; on_pick fires inline.
        if self._allow_always_btn is not None:
            self._allow_always_btn.PopupMenu(menu)
        menu.Destroy()

    def _commit_allow_always(self, chosen: list[dict]) -> None:
        self._stop_focus_timer()
        self.allowed = True
        self.reason = self._reason.GetValue().strip()
        self.updated_permissions = list(chosen)
        self.EndModal(wx.ID_YES)

    def _on_deny(self, _event: wx.CommandEvent) -> None:
        self._stop_focus_timer()
        self.allowed = False
        self.reason = self._reason.GetValue().strip()
        self.updated_permissions = []
        self.EndModal(wx.ID_NO)

    def _on_close(self, _event: wx.CloseEvent) -> None:
        self._stop_focus_timer()
        self.allowed = False
        self.reason = self._reason.GetValue().strip() or "Dialog closed without choosing"
        self.updated_permissions = []
        self.EndModal(wx.ID_NO)

    def _on_dlg_activate(self, event: wx.ActivateEvent) -> None:
        event.Skip()
        if not event.GetActive():
            return
        wx.CallLater(50, self._ensure_child_focus)

    def _on_focus_check(self, _event: wx.TimerEvent) -> None:
        """Watchdog: if the app is foregrounded but focus is not inside this
        dialog, force it.  On Windows, alt-tabbing to an app with a modal
        dialog activates the frame, not the dialog — so we check if either
        the dialog OR its parent frame is the foreground window."""
        if not self:
            return
        # Check if either this dialog or the parent frame is foreground.
        parent_active = False
        parent = self.GetParent()
        if parent:
            parent_active = parent.IsActive()
        is_active = self.IsActive() or parent_active
        if sys.platform == "win32":
            try:
                import ctypes
                fg = ctypes.windll.user32.GetForegroundWindow()
                my_hwnd = self.GetHandle()
                parent_hwnd = parent.GetHandle() if parent else 0
                if fg == my_hwnd or fg == parent_hwnd:
                    is_active = True
            except Exception:
                pass
        if not is_active:
            return
        # Check if focus is inside us.
        focused = self.FindFocus()
        if focused is not None and focused != self:
            win = focused
            while win is not None:
                if win == self:
                    return  # focus is inside us
                win = win.GetParent()
        # Focus is NOT inside this dialog but we are foreground.
        log.debug(
            "Focus watchdog: forcing focus (FindFocus=%r, IsActive=%s, parent_active=%s)",
            focused, self.IsActive(), parent_active,
        )
        self._force_focus()

    def _ensure_child_focus(self) -> None:
        if not self:
            return
        focused = self.FindFocus()
        if focused is not None and focused != self:
            win = focused
            while win is not None:
                if win == self:
                    return
                win = win.GetParent()
        self._force_focus()

    def _force_focus(self) -> None:
        """Force a focus change that NVDA will announce.

        Simply calling SetFocus() on the target isn't enough when NVDA
        thinks that control already has focus (it won't re-announce).
        The fix: move focus to a *different* widget first, then back to
        the target after a short delay.  This creates a genuine focus
        transition that NVDA always tracks.
        """
        target = self._focus_target
        if target is None or not target:
            self.SetFocus()
            return
        # Step 1: move focus to the dialog itself (or Deny button).
        self.SetFocus()
        # Step 2: after a short delay, move to the real target.
        wx.CallLater(30, self._focus_bounce_step2)

    def _focus_bounce_step2(self) -> None:
        if not self:
            return
        target = self._focus_target
        if target is None or not target:
            return
        target.SetFocus()
        try:
            target.SetInsertionPoint(0)
        except Exception:
            pass

    def _on_toggle_json(self, _event: wx.CommandEvent) -> None:
        """Show/hide the raw event JSON in place.

        Critical: we have to re-flow BOTH the inner panel's sizer and
        the dialog's outer sizer for show/hide to take effect.  The
        previous implementation only called ``self.Layout()`` (the
        dialog), so the panel's children kept their old positions and
        the JSON edit was painted on top of the Reason field / button
        row.
        """
        showing = not self._json_text.IsShown()
        self._json_label.Show(showing)
        self._json_text.Show(showing)
        self._json_btn.SetLabel(
            self._JSON_BTN_LABEL_HIDE if showing else self._JSON_BTN_LABEL_SHOW
        )
        if showing:
            # Move focus into the JSON edit so a screen-reader user
            # immediately knows the toggle revealed something to read.
            self._json_text.SetFocus()
            try:
                self._json_text.SetInsertionPoint(0)
            except Exception:
                pass
            # Grow the dialog (only if it isn't already big enough) so
            # the JSON pane has somewhere to live.  We don't shrink on
            # hide -- the user may have manually resized for some other
            # reason.
            cur_w, cur_h = self.GetSize()
            best_h = self.GetBestSize().GetHeight()
            if best_h > cur_h:
                self.SetSize((cur_w, best_h))
        # Re-run the inner panel's layout so the show/hide actually
        # changes the sized positions of its children, then the outer
        # sizer so the panel itself fills any new dialog height.
        self._panel.Layout()
        self.Layout()
