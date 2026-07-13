"""clacogui_agent - Claude Code hook bridge.

Two subcommands:

* ``hook``     -- invoked **on the Linux box** by Claude Code.  Reads the
                  hook event JSON from stdin, then either writes a
                  notification file (and exits immediately) or writes a
                  permission-request file and waits for a response.

* ``install``  -- runs **on Windows** (or anywhere with Python).  Configures
                  ``settings.json`` and the three working directories on a
                  local/SMB path or via FTP.  Idempotent; safe to re-run.

Layout under ``<.claude>/``::

    clacogui_agent.py          (the script, copied here by ``install``)
    clacogui_notifications/    fire-and-forget notifications
    clacogui_requests/         pending permission requests awaiting a user click
    clacogui_responses/        user replies the hook is waiting for

Files use ``<unix-millis>_<hex>.json`` names so they sort by time and are
cheap to dedupe by basename.

We listen for the ``PermissionRequest`` hook event (not ``PreToolUse``):
PermissionRequest only fires when Claude Code would otherwise show the
user its built-in permission dialog, so already-allowed tools (e.g. the
ones in ``permissions.allow``) skip our pipeline entirely instead of
spawning a modal for every Read/Grep/Glob.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

NOTIFICATIONS_DIR = "clacogui_notifications"
REQUESTS_DIR = "clacogui_requests"
RESPONSES_DIR = "clacogui_responses"
# Side channel used by clacogui_launcher.py to inject messages typed in
# the GUI back into claude's TUI.  The launcher (not this script) is
# the one that polls the directory; the only thing the agent needs to
# do is create the directory and drop the launcher source next to its
# own at install time.
OUTGOING_DIR = "clacogui_outgoing"
LAUNCHER_NAME = "clacogui_launcher.py"

# Permission requests block Claude until the user answers.  After this many
# seconds we give up and deny by default so a forgotten window can't wedge a
# Claude session forever.
DEFAULT_REQUEST_TIMEOUT_SEC = 300


# ---------------------------------------------------------------------------
# hook subcommand (runs on Linux, invoked by Claude Code)
# ---------------------------------------------------------------------------


def cmd_hook() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        _hook_log("bad JSON on stdin", {"error": str(e), "raw_head": raw[:200]})
        print(f"clacogui_agent: bad JSON on stdin: {e}", file=sys.stderr)
        # Don't break Claude over our own bug -- exit clean.
        sys.exit(0)

    name = event.get("hook_event_name") or event.get("hookEventName") or ""
    claude_dir = _claude_dir_from_env()
    _hook_log(
        "received",
        {
            "event": name,
            "tool": event.get("tool_name"),
            "session": event.get("session_id"),
            "pid": os.getpid(),
        },
    )

    # ``Notification``      - Claude wants the user's attention (idle /
    #                         waiting for input).
    # ``Stop``              - Claude finished its top-level turn.
    # ``SubagentStop``      - a subagent (Task tool) finished.
    # ``PermissionRequest`` - Claude is about to show its built-in
    #                         permission dialog.  We hijack it: write a
    #                         request file, wait for the user to click
    #                         in clacogui, then emit allow/deny on
    #                         their behalf.  Tools the user has already
    #                         allowed via ``permissions.allow`` skip
    #                         this hook entirely.
    if name in ("Notification", "Stop", "SubagentStop"):
        _handle_notification(claude_dir, event)
    elif name == "PermissionRequest":
        _handle_permission_request(claude_dir, event)
    else:
        # Anything else: don't get in Claude's way.
        _hook_log("ignored", {"event": name})
        sys.exit(0)


def _claude_dir_from_env() -> Path:
    return Path(
        os.environ.get("CLAUDE_CONFIG_DIR")
        or os.path.expanduser("~/.claude")
    )


# clacogui_agent.log lives next to the script so we can debug hook decisions
# without being able to attach to the process Claude Code spawns.  Best-effort:
# any failure here is silently dropped (we'd rather have a working hook than
# a crashed one).
_HOOK_LOG_NAME = "clacogui_agent.log"


def _hook_log_path() -> Path:
    return _claude_dir_from_env() / _HOOK_LOG_NAME


def _hook_log(label: str, payload: object = None) -> None:
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} pid={os.getpid()} {label}"
        if payload is not None:
            try:
                line += " " + json.dumps(payload, default=str)
            except (TypeError, ValueError):
                line += f" {payload!r}"
        with _hook_log_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _new_id() -> str:
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def _safe_event_tag(name: str) -> str:
    """Restrict the event-name suffix to ``[A-Za-z0-9]`` so it's filename-safe.

    Empty / unknown becomes ``Unknown`` (the GUI treats unknown as a real
    notification and rings).
    """
    out = "".join(c for c in (name or "") if c.isalnum())
    return out or "Unknown"


def _is_compaction_event(event: dict) -> bool:
    """Detect if a Stop event is due to context compaction/truncation."""
    stop_reason = event.get("stopReason") or ""
    if "context_window" in stop_reason.lower():
        return True
    message = event.get("message") or {}
    if isinstance(message, dict):
        content = message.get("content") or ""
        if isinstance(content, str):
            low = content.lower()
            if "compacted" in low or "truncated" in low or "summarized" in low:
                return True
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = (item.get("text") or "").lower()
                    if "compacted" in text or "truncated" in text or "summarized" in text:
                        return True
    return False


def _handle_notification(claude_dir: Path, event: dict) -> None:
    """Write a notification file.

    The basename is ``<unix-millis>_<rand>__<EventName>.json``.  Encoding
    the event name in the filename lets the GUI dedupe ``Notification``
    (idle reminders that fire ~60s after Stop) without having to download
    each tiny JSON over FTP.
    """
    out_dir = claude_dir / NOTIFICATIONS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _safe_event_tag(event.get("hook_event_name") or "")
    if tag == "Stop" and _is_compaction_event(event):
        tag = "StopCompact"
    _atomic_write(out_dir / f"{_new_id()}__{tag}.json", event)
    sys.exit(0)


def _handle_permission_request(claude_dir: Path, event: dict) -> None:
    """Bridge a Claude Code ``PermissionRequest`` event to clacogui.

    Workflow:

    1. Write the request payload (event JSON, plus our request id) to
       ``clacogui_requests/<rid>.json``.
    2. Block until ``clacogui_responses/<rid>.json`` appears (the user
       clicked Allow / Allow-always / Deny in clacogui), or until
       :data:`DEFAULT_REQUEST_TIMEOUT_SEC` elapses.
    3. Translate the response to a ``hookSpecificOutput.decision``
       payload (per the docs, ``behavior`` is ``"allow"`` or ``"deny"``)
       and print it to stdout, where Claude Code is waiting to read it.

    We stash any ``permission_suggestions`` Claude sent us into the
    request file unchanged so the GUI can offer "Allow always" without
    re-deriving the rule.
    """
    requests_dir = claude_dir / REQUESTS_DIR
    responses_dir = claude_dir / RESPONSES_DIR
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)

    rid = _new_id()
    request_path = requests_dir / f"{rid}.json"
    response_path = responses_dir / f"{rid}.json"

    payload = dict(event)
    payload["_clacogui_request_id"] = rid
    _atomic_write(request_path, payload)
    _hook_log(
        "permission_request wrote request",
        {
            "rid": rid,
            "tool": event.get("tool_name"),
            "suggestions": len(event.get("permission_suggestions") or []),
        },
    )

    deadline = time.time() + DEFAULT_REQUEST_TIMEOUT_SEC
    poll_interval = 0.5
    started = time.time()

    try:
        while time.time() < deadline:
            if response_path.exists():
                try:
                    resp = json.loads(response_path.read_text())
                except (OSError, ValueError) as e:
                    _hook_log(
                        "permission_request partial response, retrying",
                        {"rid": rid, "error": str(e)},
                    )
                    time.sleep(poll_interval)
                    continue
                _hook_log(
                    "permission_request got response",
                    {
                        "rid": rid,
                        "waited_s": round(time.time() - started, 2),
                        "decision": resp.get("decision"),
                        "updated_permissions": len(
                            resp.get("updated_permissions") or []
                        ),
                    },
                )
                _emit_permission_decision(resp)
                return
            time.sleep(poll_interval)

        _hook_log(
            "permission_request timed out",
            {"rid": rid, "waited_s": round(time.time() - started, 2)},
        )
        _emit_permission_decision({
            "decision": "deny",
            "reason": "Permission request timed out (no answer in clacogui).",
        })
    finally:
        # Clean up our scratch files so the dirs don't grow forever.
        for p in (request_path, response_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _emit_permission_decision(resp: dict) -> None:
    """Translate a clacogui response to a ``PermissionRequest`` hook output.

    Per the Claude Code docs (Hooks reference -> PermissionRequest
    decision control), the hook returns its decision under
    ``hookSpecificOutput.decision`` with a ``behavior`` of
    ``"allow"`` or ``"deny"``::

        {
          "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": { "behavior": "allow",
                          "updatedPermissions": [...] }
          }
        }

    A response of ``decision: "allow"`` may also include
    ``updated_permissions`` -- a list of suggestion entries Claude
    handed us in the original ``permission_suggestions`` array, which
    the user opted into (e.g. picked "Allow always for Bash(grep *)").
    We pass those through verbatim so Claude persists the rule for us.

    Anything else (``"deny"``, missing, unparseable) falls through to a
    deny -- safer default for a hook with no clear answer.
    """
    decision = (resp.get("decision") or "").lower()
    reason = resp.get("reason") or ""

    if decision == "allow":
        decision_obj: dict = {"behavior": "allow"}
        ups = resp.get("updated_permissions") or []
        if ups:
            decision_obj["updatedPermissions"] = ups
        # AskUserQuestion (and any tool the GUI answers programmatically)
        # rides back on the allow decision's ``updatedInput`` field: the
        # GUI echoes the original questions plus an ``answers`` map so the
        # tool resolves without a TUI prompt.
        ui = resp.get("updated_input")
        if isinstance(ui, dict) and ui:
            decision_obj["updatedInput"] = ui
        if reason:
            # `permissionDecisionReason` is the field shown to the
            # user; PermissionRequest doesn't define a reason on
            # allow, but including a top-level "reason" doesn't hurt
            # and shows up in transcripts.
            decision_obj["reason"] = reason
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision_obj,
            }
        }
        _hook_log("emit allow", out)
        print(json.dumps(out))
        sys.exit(0)

    deny_msg = reason or "Denied by clacogui user"
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": deny_msg,
            },
        }
    }
    _hook_log("emit deny", out)
    print(json.dumps(out))
    sys.exit(0)


# ---------------------------------------------------------------------------
# install subcommand (runs on Windows or wherever)
# ---------------------------------------------------------------------------


def cmd_install(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog="clacogui_agent install",
        description=(
            "Install the clacogui hook into a Claude Code config directory. "
            "Target may be a local path (incl. an SMB-mapped drive) or an "
            "ftp:// URL."
        ),
    )
    p.add_argument(
        "target",
        nargs="?",
        help="Local path or ftp:// URL to the .claude directory",
    )
    p.add_argument(
        "--remote-script",
        help=(
            "Absolute path on the Linux box where the hook script will live. "
            "Default: <claude-dir>/clacogui_agent.py."
        ),
    )
    p.add_argument(
        "--python",
        help=(
            "Python interpreter to use when Claude invokes the hook on "
            "Linux.  Default: python3."
        ),
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Don't ask for confirmation, accept defaults for prompts.",
    )
    args = p.parse_args(argv)

    target = args.target or _ask(
        "Path or ftp:// URL to your Claude Code .claude folder",
        default=None,
        require=True,
    )

    src_bytes = Path(__file__).resolve().read_bytes()
    # The launcher ships next to this script in the source tree; if a
    # user is running an older bundle that lacks it, we'll still install
    # the agent and just skip the launcher with a warning.
    launcher_path = Path(__file__).resolve().parent / LAUNCHER_NAME
    try:
        launcher_bytes = launcher_path.read_bytes()
    except OSError:
        launcher_bytes = None
        print(
            f"[clacogui_agent] WARNING: {LAUNCHER_NAME} not found next to "
            f"clacogui_agent.py; the GUI 'send to claude' feature will be "
            f"unavailable until you reinstall from a complete bundle."
        )

    if target.lower().startswith(("ftp://", "ftps://")):
        _install_ftp(target, src_bytes, launcher_bytes, args)
    else:
        _install_local(target, src_bytes, launcher_bytes, args)


def _install_local(
    target: str,
    src_bytes: bytes,
    launcher_bytes: bytes | None,
    args,
) -> None:
    claude_dir = Path(target).expanduser()
    if not claude_dir.exists():
        sys.exit(f"clacogui_agent: not a directory: {claude_dir}")
    claude_dir = claude_dir.resolve()
    print(f"[clacogui_agent] Local install into {claude_dir}")

    # Where will the script live on the *Linux* box?  When the .claude dir
    # is mounted via SMB the local path is meaningless to Claude, so we ask.
    default_remote = _guess_remote_default(claude_dir)
    remote_script = (
        args.remote_script
        or _ask(
            "Absolute path on the Linux box for the hook script",
            default=default_remote,
            require=True,
            silent=args.yes,
        )
    )

    interpreter = args.python or _ask(
        "Python interpreter on the Linux box",
        default="python3",
        require=True,
        silent=args.yes,
    )

    # Compute the *local* path that corresponds to remote_script if possible.
    # Heuristic: if remote_script ends with the .claude leaf we mounted as
    # claude_dir, write to claude_dir / basename(remote_script).
    local_target = claude_dir / Path(remote_script).name
    local_target.write_bytes(src_bytes)
    print(f"[clacogui_agent] Wrote {local_target}")

    # The launcher always lands next to the agent on the remote, so its
    # local sibling is in the same directory whatever path the user
    # pointed --remote-script at.
    if launcher_bytes is not None:
        launcher_target = claude_dir / LAUNCHER_NAME
        launcher_target.write_bytes(launcher_bytes)
        print(f"[clacogui_agent] Wrote {launcher_target}")

    for d in (NOTIFICATIONS_DIR, REQUESTS_DIR, RESPONSES_DIR, OUTGOING_DIR):
        (claude_dir / d).mkdir(exist_ok=True)
        print(f"[clacogui_agent] Ensured {claude_dir / d}")

    settings_path = claude_dir / "settings.json"
    settings = _read_settings_local(settings_path, args.yes)
    cmd = f"{interpreter} {remote_script} hook"
    settings = _merge_hook_config(settings, cmd)
    settings_path.write_text(json.dumps(settings, indent=2))
    print(f"[clacogui_agent] Updated {settings_path}")
    print()
    print("Done. The hook command Claude will run is:")
    print(f"    {cmd}")


def _install_ftp(
    url: str,
    src_bytes: bytes,
    launcher_bytes: bytes | None,
    args,
) -> None:
    import ftplib
    import io
    from urllib.parse import unquote, urlparse

    u = urlparse(url)
    if not u.hostname:
        sys.exit(f"clacogui_agent: bad FTP URL: {url}")

    host = u.hostname
    port = u.port or 21
    user = unquote(u.username) if u.username else "anonymous"
    password = unquote(u.password) if u.password else ""
    root = unquote(u.path or "/").rstrip("/") or "/"
    print(f"[clacogui_agent] FTP install {user}@{host}:{port} root={root}")

    remote_script = (
        args.remote_script
        or _ask(
            "Absolute path on the Linux box for the hook script",
            default=f"{root}/clacogui_agent.py",
            require=True,
            silent=args.yes,
        )
    )
    interpreter = args.python or _ask(
        "Python interpreter on the Linux box",
        default="python3",
        require=True,
        silent=args.yes,
    )

    ftp = ftplib.FTP(timeout=30)
    ftp.connect(host, port)
    ftp.login(user, password)
    try:
        ftp.sendcmd("OPTS UTF8 ON")
    except ftplib.all_errors:
        pass
    ftp.set_pasv(True)

    try:
        # Upload the script.
        print(f"[clacogui_agent] Uploading hook script to {remote_script}")
        ftp.storbinary(f"STOR {remote_script}", io.BytesIO(src_bytes))
        # Best effort -- ignore if server doesn't support SITE CHMOD.
        try:
            ftp.sendcmd(f"SITE CHMOD 755 {remote_script}")
        except ftplib.all_errors:
            pass

        # Drop the launcher next to the agent, using the same directory
        # the agent landed in.  If the user gave a custom --remote-script
        # we honour the directory but always use LAUNCHER_NAME for the
        # leaf so the docs and GUI can find it.
        if launcher_bytes is not None:
            remote_dir = remote_script.rsplit("/", 1)[0] or root
            launcher_remote = f"{remote_dir}/{LAUNCHER_NAME}"
            print(f"[clacogui_agent] Uploading launcher to {launcher_remote}")
            ftp.storbinary(
                f"STOR {launcher_remote}", io.BytesIO(launcher_bytes)
            )
            try:
                ftp.sendcmd(f"SITE CHMOD 755 {launcher_remote}")
            except ftplib.all_errors:
                pass

        for d in (NOTIFICATIONS_DIR, REQUESTS_DIR, RESPONSES_DIR, OUTGOING_DIR):
            full = f"{root}/{d}"
            try:
                ftp.mkd(full)
                print(f"[clacogui_agent] Created {full}")
            except ftplib.error_perm as e:
                if str(e).startswith("550"):
                    print(f"[clacogui_agent] Already present: {full}")
                else:
                    raise

        settings_path = f"{root}/settings.json"
        settings = _read_settings_ftp(ftp, settings_path, args.yes)
        cmd = f"{interpreter} {remote_script} hook"
        settings = _merge_hook_config(settings, cmd)
        body = json.dumps(settings, indent=2).encode("utf-8")
        ftp.storbinary(f"STOR {settings_path}", io.BytesIO(body))
        print(f"[clacogui_agent] Updated {settings_path}")
        print()
        print("Done. The hook command Claude will run is:")
        print(f"    {cmd}")
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:
            pass


# ---------------------------------------------------------------------------
# Helpers shared by both install paths
# ---------------------------------------------------------------------------


def _read_settings_local(path: Path, yes: bool) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        msg = f"Existing {path} is unreadable JSON ({e})."
        if yes or _confirm(msg + " Overwrite?"):
            return {}
        sys.exit(1)


def _read_settings_ftp(ftp, remote_path: str, yes: bool) -> dict:
    import ftplib
    import io

    buf = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {remote_path}", buf.write)
    except ftplib.error_perm as e:
        if str(e).startswith("550"):
            return {}
        raise

    try:
        return json.loads(buf.getvalue().decode("utf-8"))
    except ValueError as e:
        msg = f"Existing {remote_path} is unreadable JSON ({e})."
        if yes or _confirm(msg + " Overwrite?"):
            return {}
        sys.exit(1)


_HOOK_EVENTS: tuple[str, ...] = (
    "Notification",
    "Stop",
    "SubagentStop",
    "PermissionRequest",
)

# Events we used to install but no longer want.  On re-install we strip
# any clacogui hook entries we left behind there so users don't keep
# getting a modal for every Read/Grep after the upgrade.
_HOOK_EVENTS_REMOVE: tuple[str, ...] = ("PreToolUse",)


def _merge_hook_config(settings: dict, cmd: str) -> dict:
    """Idempotent: replaces our existing hook entry, leaves others alone.

    For every event in :data:`_HOOK_EVENTS` we install with
    ``"matcher": ""``.  Per the Claude Code hook docs, an empty matcher
    means "fire on every invocation"; we previously used ``"*"``, which
    is *not* a valid regex (no preceding atom), and at least one user
    reported rare codepaths slipping past it silently.  Empty string is
    the safer documented form.

    For every event in :data:`_HOOK_EVENTS_REMOVE`, any clacogui hook we
    installed in a previous version is cleaned up (we ship a different
    set of events now -- ``PermissionRequest`` instead of
    ``PreToolUse`` -- and stale entries would re-trigger the
    "modal-for-every-tool" behavior we just got rid of).
    """
    hooks = settings.setdefault("hooks", {})

    # 1. Strip stale clacogui entries from events we no longer use.
    for event in _HOOK_EVENTS_REMOVE:
        bucket = hooks.get(event)
        if not isinstance(bucket, list):
            continue
        cleaned: list[dict] = []
        for entry in bucket:
            entry_hooks = entry.get("hooks") or []
            kept = [
                h for h in entry_hooks
                if not (
                    h.get("type") == "command"
                    and "clacogui_agent" in (h.get("command") or "")
                )
            ]
            if kept:
                # The user had other hooks alongside ours; preserve them.
                entry = dict(entry, hooks=kept)
                cleaned.append(entry)
            # If kept is empty, drop the whole entry.
        if cleaned:
            hooks[event] = cleaned
        else:
            # No surviving entries -- remove the event key entirely so
            # we don't leave an empty list lying around.
            hooks.pop(event, None)

    # 2. Install / refresh the active set of events.
    for event in _HOOK_EVENTS:
        bucket = hooks.setdefault(event, [])
        replaced = False
        for entry in bucket:
            for h in entry.get("hooks", []):
                if (
                    h.get("type") == "command"
                    and "clacogui_agent" in (h.get("command") or "")
                ):
                    h["command"] = cmd
                    # Heal old installs that wrote ``"*"`` -- replace
                    # the matcher in place, regardless of which event
                    # bucket we're in.
                    if entry.get("matcher") == "*":
                        entry["matcher"] = ""
                    replaced = True
        if not replaced:
            bucket.append({
                "matcher": "",
                "hooks": [{"type": "command", "command": cmd}],
            })
    return settings


def _guess_remote_default(claude_dir: Path) -> str:
    """For SMB-mounted .claude dirs the local path is bogus on the server.

    We can only guess: stick the script in the directory leaf.  The user is
    expected to confirm or edit.
    """
    return f"~/.claude/{Path(__file__).name}"


def _ask(
    prompt: str,
    default: object = None,
    require: bool = False,
    silent: bool = False,
) -> str:
    if silent:
        if default is None and require:
            sys.exit(f"clacogui_agent: {prompt!r} required and no default given")
        return str(default or "")
    if default:
        full = f"{prompt} [{default}]: "
    else:
        full = f"{prompt}: "
    try:
        ans = input(full).strip()
    except EOFError:
        ans = ""
    if not ans and default is not None:
        return str(default)
    if not ans and require:
        sys.exit(f"clacogui_agent: {prompt} is required")
    return ans


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        _usage_and_exit()

    sub = sys.argv[1]
    if sub == "hook":
        cmd_hook()
    elif sub == "install":
        cmd_install(sys.argv[2:])
    elif sub in ("-h", "--help"):
        _usage_and_exit(code=0)
    else:
        _usage_and_exit()


def _usage_and_exit(code: int = 2) -> None:
    sys.stderr.write(
        "Usage:\n"
        "  clacogui_agent install [target] [--remote-script PATH] "
        "[--python BIN] [--yes]\n"
        "      Configure a Claude Code .claude directory (local path or\n"
        "      ftp:// URL) to use the clacogui hook.\n"
        "\n"
        "  clacogui_agent hook\n"
        "      Hook entry point.  Reads JSON from stdin; invoked by\n"
        "      Claude Code on the Linux side.  Don't run by hand.\n"
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
