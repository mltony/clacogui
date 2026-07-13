"""clacogui - a wxPython GUI for browsing Claude Code conversations.

Run with:
    python clacogui.py [path-or-ftp-url]

Examples::

    python clacogui.py X:\\.claude
    python clacogui.py /home/me/.claude
    python clacogui.py ftp://ftpuser:<pw>@host:2121/home/me/.claude

Logs go to ``clacogui.log`` next to this script.  The chosen spec is
remembered in ``~/.clacogui_config.json`` (under the ``claude_dir`` key for
backwards compatibility).
"""

from __future__ import annotations

import logging
import os
import sys
import traceback

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("clacogui.app.1")
    except Exception:
        pass


LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "clacogui.log"
)


def _configure_logging() -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
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


def main() -> None:
    _install_excepthooks()
    log.info("=" * 60)
    log.info("Starting clacogui; logging to %s", LOG_PATH)

    cli_spec = sys.argv[1] if len(sys.argv) > 1 else None

    # Import wx-dependent modules AFTER logging is set up so any import errors
    # land in clacogui.log too.
    try:
        from gui import App
    except Exception:
        log.exception("Failed to import GUI; is wxPython installed?")
        raise

    try:
        app = App(backend_spec_arg=cli_spec)
        app.MainLoop()
    except Exception:
        log.exception("Fatal error in main loop")
        raise
    finally:
        logging.shutdown()


if __name__ == "__main__":
    main()
