"""clacogui - a wxPython GUI for browsing Claude Code conversations.

Run with:
    python clacogui.py [path-or-ftp-url] [--codex path-or-ftp-url]

Examples::

    python clacogui.py X:\\.claude
    python clacogui.py /home/me/.claude
    python clacogui.py ftp://ftpuser:<pw>@host:2121/home/me/.claude
    python clacogui.py /home/me/.claude --codex /home/me/.codex

Each process writes its own logs under the local Clacogui log directory.
The chosen specs are
remembered in ``~/.clacogui_config.json`` (``claude_dir`` and
``codex_dir``).
"""

from __future__ import annotations

import logging
from datetime import datetime
import logging.handlers
import os
import re
import socket
import sys
import traceback

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("clacogui.app.1")
    except Exception:
        pass


def _log_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value)[:48].strip("-.") or "default"


LOG_DIR = os.environ.get("CLACOGUI_LOG_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache"),
    "clacogui", "logs",
)
SESSION_LOG_STEM = "clacogui-{}-{}-{}-{}".format(
    _log_component(os.environ.get("CLACOGUI_LOG_PROFILE", "direct")),
    _log_component(socket.gethostname()), datetime.now().strftime("%Y%m%d-%H%M%S-%f"),
    os.getpid(),
)
LOG_PATH = os.path.join(LOG_DIR, SESSION_LOG_STEM + ".log")
WEBVIEW_LOG_PATH = os.path.join(LOG_DIR, SESSION_LOG_STEM + "-webview.log")


def _configure_logging() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s pid=%(process)d thread=%(threadName)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    # WebView health (gui.wvlog) also goes to its own small file, which is
    # easy to copy independently of this process's main log.
    webview_logger = logging.getLogger("webview")
    for handler in list(webview_logger.handlers):
        webview_logger.removeHandler(handler)
        handler.close()
    webview_handler = logging.handlers.RotatingFileHandler(
        WEBVIEW_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    webview_handler.setFormatter(file_handler.formatter)
    webview_logger.addHandler(webview_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter("pid=%(process)d [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(stderr_handler)

    return logging.getLogger("clacogui")


log = _configure_logging()


class _StreamToLogger:
    """File-like object that forwards writes to a logger."""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, message) -> int:
        if not isinstance(message, str):
            message = str(message)
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                self._logger.log(self._level, line)
        return len(message)

    def flush(self) -> None:
        if self._buffer:
            self._logger.log(self._level, self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:
        return False


def _install_excepthooks() -> None:
    def _excepthook(exc_type, exc_value, exc_tb):
        log.error(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    if hasattr(sys, "unraisablehook"):
        def _unraisable(args):
            log.error(
                "Unraisable exception in %r: %s",
                getattr(args, "object", None),
                getattr(args, "exc_value", None),
            )

        sys.unraisablehook = _unraisable

    sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.ERROR)


def _parse_args(args: list[str]) -> tuple[str | None, str | None]:
    """``[claude-spec] [--codex codex-spec]`` in either order."""
    args = list(args)
    codex_spec = None
    if "--codex" in args:
        i = args.index("--codex")
        codex_spec = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    return (args[0] if args else None), codex_spec


def main() -> None:
    _install_excepthooks()
    log.info("=" * 60)
    log.info("Starting clacogui; logging to %s", LOG_PATH)
    log.info("WebView log: %s", WEBVIEW_LOG_PATH)
    if sys.__stderr__ is not None:
        sys.__stderr__.write(f"[clacogui] Runtime log: {LOG_PATH}\n")
        sys.__stderr__.flush()

    cli_spec, codex_spec = _parse_args(sys.argv[1:])

    # Import wx-dependent modules AFTER logging is set up so any import errors
    # land in this process's runtime log too.
    try:
        from gui import App
    except Exception:
        log.exception("Failed to import GUI; is wxPython installed?")
        raise

    try:
        app = App(backend_spec_arg=cli_spec, codex_spec_arg=codex_spec)
        app.MainLoop()
    except Exception:
        log.exception("Fatal error in main loop")
        raise
    finally:
        logging.shutdown()


if __name__ == "__main__":
    main()
