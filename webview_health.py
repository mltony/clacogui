"""What is really inside a wx WebView on Windows (Edge / WebView2 backend).

The wx window is only a host.  The page lives in ``Chrome_*`` child windows
owned by ``msedgewebview2.exe`` processes; if those die or detach, NVDA sees
an empty "wxWebView" pane (or calls the old document "defunct").  These
helpers report the child windows, their processes and the focused window so
the log shows which of those happened.  Everything returns empty results
off Windows.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

_WIN = sys.platform == "win32"

if _WIN:
    import ctypes
    import ctypes.wintypes as wt

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    _ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _user32.EnumChildWindows.argtypes = [wt.HWND, _ENUMPROC, wt.LPARAM]
    _user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    _user32.IsWindowVisible.argtypes = [wt.HWND]
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
            ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
            ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010
_STILL_ACTIVE = 259


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd) -> int:
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_info(pid: int) -> tuple:
    """(alive, working set in MB or None) for a process id."""
    if not _WIN or not pid:
        return False, None
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False, None
    try:
        code = wt.DWORD()
        alive = bool(_kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        mb = None
        if _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            mb = counters.WorkingSetSize // (1024 * 1024)
        return alive, mb
    finally:
        _kernel32.CloseHandle(handle)


@dataclass(frozen=True)
class NativeState:
    windows: tuple  # ((class name, pid, visible), ...) for every descendant window
    processes: tuple  # ((pid, alive, working set MB), ...) for the Chrome windows' processes

    @property
    def chrome_windows(self) -> tuple:
        return tuple(w for w in self.windows if w[0].startswith("Chrome_"))

    @property
    def dead_pids(self) -> tuple:
        return tuple(pid for pid, alive, _mb in self.processes if not alive)

    @property
    def healthy(self) -> bool:
        return bool(self.chrome_windows) and not self.dead_pids

    def shape(self) -> tuple:
        """Everything except memory, to spot changes worth logging."""
        return self.windows, tuple((pid, alive) for pid, alive, _mb in self.processes)

    def summary(self) -> str:
        wins = ", ".join(f"{cls}[pid {pid}{'' if vis else ' hidden'}]" for cls, pid, vis in self.windows) or "none"
        procs = ", ".join(f"{pid} {'alive' if alive else 'DEAD'}{f' {mb}MB' if mb is not None else ''}"
                          for pid, alive, mb in self.processes) or "none"
        return f"child windows: {wins}; processes: {procs}"


def native_state(hwnd: int) -> NativeState:
    if not _WIN or not hwnd:
        return NativeState((), ())
    found: list = []

    def visit(child, _lparam):
        found.append((_class_name(child), _pid(child), bool(_user32.IsWindowVisible(child))))
        return True

    _user32.EnumChildWindows(hwnd, _ENUMPROC(visit), 0)
    own = os.getpid()
    pids = sorted({pid for cls, pid, _vis in found if cls.startswith("Chrome_") and pid != own})
    return NativeState(tuple(found), tuple((pid, *process_info(pid)) for pid in pids))


def focused_window() -> str:
    """Class, process and handle of the window that has keyboard focus in the foreground app."""
    if not _WIN:
        return "n/a"
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndFocus:
        return "none"
    hwnd = info.hwndFocus
    pid = _pid(hwnd)
    owner = "clacogui" if pid == os.getpid() else f"pid {pid}"
    return f"{_class_name(hwnd)} ({owner}, hwnd {int(hwnd)})"
