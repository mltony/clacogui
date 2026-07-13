# clacogui

A small Windows GUI for browsing **Claude Code** conversation transcripts.
Built with `wxPython` + `wx.html2.WebView` so it works well with screen
readers (NVDA, JAWS).

## What it does

- Reads `*.jsonl` conversation files under
  `<root>/projects/<project>/<sessionId>.jsonl`.
- Reads matching session names from `<root>/sessions/*.json`.
- Lets you keep multiple conversations open as tabs.
- Polls every second so new messages and renamed sessions show up live.

`<root>` can be either:

- a **local or SMB-mapped path**, e.g. `X:\.claude`, `\\server\share\.claude`,
  `/home/me/.claude`; or
- an **FTP URL**, e.g.
  `ftp://ftpuser:<password>@<wsl-hostname>:2121/home/me/.claude`.

The FTP path is recommended when the Linux side runs Claude Code and you
view from Windows, because Windows aggressively caches SMB metadata
(see *SMB caching on Windows* below). FTP doesn't cache anything in the
client, so polling and `F5` always see fresh data.

> **Security note.** The FTP bridge is intended only for a loopback / LAN
> connection between your Windows box and its WSL distro (or a private
> devbox reached via SSH port forwarding). Pick your own password (any
> value will do -- `pyftpdlib`'s built-in `-P` is what enforces it) and
> keep the server bound to a network only you can reach.

## Install + run (recommended)

Pick a password and export it once so both `run.cmd` and the WSL-side
FTP server agree on it. Any string is fine; it just has to match.

```bat
setx CLACOGUI_FTP_PASSWORD <your-password>
```

(You'll need to open a *new* cmd window for `setx` to take effect.)

Then double-click **`run.cmd`** (or run it from a shell):

```bat
run.cmd
```

By default `run.cmd` figures out the IP of your WSL distro
(`wsl -- hostname -I`) and connects to the FTP backend at

    ftp://ftpuser:<CLACOGUI_FTP_PASSWORD>@<wsl-ip>:2121/home/<user>/.claude

The matching pyftpdlib server, launched **inside WSL** with the same
password, is:

```bash
python -m pyftpdlib -u ftpuser -P "$CLACOGUI_FTP_PASSWORD" \
  --range 60000-60009 -d / -w
```

If your WSL account doesn't match your Windows `%USERNAME%`, override the
remote home with `CLACOGUI_WSL_HOME`, e.g.
`setx CLACOGUI_WSL_HOME /home/me/.claude`.

If you'd rather use a direct path, an SMB share, or a different FTP host
(e.g. when reaching a remote box through SSH port forwarding), just pass
it on the command line; that overrides the default:

```bat
run.cmd X:\.claude
run.cmd \\server\share\.claude
run.cmd ftp://ftpuser:<pw>@localhost:2121/home/me/.claude
run.cmd ftp://ftpuser:<pw>@some-host:2121/home/me/.claude
```

What `run.cmd` does:

1. Creates `.venv\` next to the script if it doesn't exist.
2. Hashes `requirements.txt` with SHA-256 and stores the hash in
   `.venv\.req-stamp`.
3. Re-runs `pip install -r requirements.txt` **only** when the hash
   changes (i.e. you edited `requirements.txt`). Otherwise it skips
   straight to launching the app, so subsequent runs start in well under
   a second.
4. Launches `clacogui.py` using the venv's Python and passes through any
   extra arguments.

The folder you pick the first time is remembered in
`%USERPROFILE%\.clacogui_config.json`, so the path argument is optional
from then on.

## Manual install (if you don't want to use `run.cmd`)

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python clacogui.py
```

(`wxPython` and `markdown`. Python 3.10+ recommended.)

Diagnostics go to `clacogui.log` in the same directory as `clacogui.py`.
The log file is appended to on every run.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+O` | Open conversation dialog |
| `Ctrl+W` (or `Ctrl+F4`) | Close current conversation tab |
| `Ctrl+R` | Refresh current conversation from disk |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Next / previous conversation |
| `Ctrl+1` ... `Ctrl+9` | Switch directly to tab N |
| `Ctrl+Shift+PgUp` / `Ctrl+Shift+PgDn` | Move current tab left / right |
| `F2` | Focus the message list |
| `F4` | Focus the conversation content (WebView) |
| `F5` | Focus the send-message box |
| `F6` / `Shift+F6` | Cycle focus between message list and content (legacy) |
| `Enter` (in send box) | Send message to claude (via the launcher) |
| `Shift+Enter` (in send box) | Insert a newline without sending |
| `Ctrl+.` | Interrupt claude (send Esc; cancels the current turn) |
| `Alt+F4` | Quit |

In the **Open conversation** dialog:

- `Alt+T` sorts by last-modified time (default).
- `Alt+N` sorts by conversation name.
- `Up`/`Down` chooses, `Enter` opens.

## Layout of an open conversation

Each tab is split vertically:

- **Left**: a list of *your* messages (cropped to 10000 characters in the
  HTML view, previews truncated to ~120 characters in the list itself).
- **Right**: a `WebView` showing the selected user message followed by the
  assistant's reply. The reply is rendered from Markdown to HTML; if the
  assistant didn't start with an `# h1`, one is added so screen readers can
  jump headings consistently. Fenced ```` ``` ```` code blocks become
  multi-line `<textarea>` form controls so they expose as edit boxes to
  screen readers.

## Status bar layout

The window has a 3-field status bar.  Screen readers read fields
left-to-right when navigating, so the most important indicator
(*"is claude actually running?"*) sits in field 0:

| Field | Contents |
| --- | --- |
| 0 (leftmost) | `Claude: <status>` for the active tab &mdash; e.g. `Claude: idle`, `Claude: busy`, `Claude: not running`.  Source: `~/.claude/sessions/*.json` (the same files the launcher reads).  Empty when no tab is open or the conversation has no `session_id` yet. |
| 1 (middle) | Send-message status of the active tab: `Idle`, `Queued (...)`, `Sent at HH:MM:SS`, `Interrupting (...)`, `Interrupt sent at HH:MM:SS`, `Send failed: <reason>`.  Transient app messages (e.g. `Refreshed at HH:MM:SS - N message(s)`) overlay this slot, then revert. |
| 2 (rightmost) | Folder / backend description, e.g. `Folder: ftp://172.x.x.x:2121/home/me/.claude` or `Folder: X:\.claude`. |

Reading field 0 is the fastest way to tell from the GUI whether
your message is going to land (`Claude: idle` &rarr; the launcher
will inject right away) or is going to sit in the queue
(`Claude: busy` &rarr; the launcher is gating until claude
finishes).  When in doubt, `Ctrl+.` sends Esc to cancel.

## Screen-reader URL

`wx.html2.WebView` exposes its document URL via UIA / IAccessible2, which is
how screen readers tell pages apart. clacogui sets this URL to a stable,
deterministic value of the form

    clacogui://<sessionId>/message/<index>

(or `clacogui://<sessionId>/empty`, `clacogui://<sessionId>/none`, or
`clacogui://loading` for transient states). The `clacogui://` scheme is
custom so the WebView never tries to actually navigate it; it is purely an
identifier the screen reader can hash on. Re-rendering the same message
always yields the same URL, so screen readers do not lose their place when
the file is re-polled and the page is rebuilt.

## Threading

All filesystem access (the open-dialog scan, the initial read of an opened
conversation, every poll tick) goes through a **single** background thread
fed by a FIFO queue. Choosing one thread instead of a pool is deliberate:

- The bottleneck is the SMB share, not CPU. Issuing several reads in
  parallel against a slow share tends to make it slower, not faster.
- Serialising guarantees at most one outstanding `stat()`/`read()` at any
  moment, which is the gentlest pattern for a flaky network filesystem.
- Each `ConversationPanel` de-duplicates its own jobs with `_polling` and
  `_loading` flags, so the queue can never grow without bound: if the
  previous poll is still in flight when the 1 s timer fires again, the new
  tick is simply dropped.

When the worker finishes, the result is posted back to the wx main thread
with `wx.CallAfter`. Every callback short-circuits with `if not self:` so a
tab that was closed mid-IO doesn't crash on a destroyed wx object.

## F5 / Ctrl+R is *not* fire-and-forget

Pressing `F5` (or `Ctrl+R`) submits a refresh job to the IO worker thread
and then waits for the worker to actually complete the read. The status
bar narrates each phase:

1. `Refreshing <session>.jsonl...` is shown immediately.
2. When the worker callback fires on the wx main thread (i.e. the bytes
   really came back from the share), it updates the status bar to
   `Refreshed at HH:MM:SS - N message(s)`.
3. After 5 seconds the bar reverts to the default shortcut hint.

If a refresh is already running, an extra `F5` does **not** stack a
duplicate request; it just shows `Refresh already in progress, please
wait...` until the current one finishes. Screen readers re-announce the
status bar whenever its text changes, so this round-trip is audible.

## SMB caching on Windows

Yes, this is mostly a **Windows** problem. The Linux SMB client
(`cifs.ko` / `cifs-utils`) caches metadata for ~1 second by default; the
Windows SMB client (`mrxsmb`) caches directory listings, file metadata,
and "file not found" answers for ~10 seconds. Even a freshly opened file
handle can re-use cached metadata if the cache hasn't expired, which can
make `F5` look like it didn't pick up a remote change.

Three registry values control this on Windows. They live under

    HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters

and all are `DWORD`, in seconds. The defaults:

| Value | Default | What it caches |
| --- | --- | --- |
| `DirectoryCacheLifetime` | 10 | Directory listings (`os.listdir`) |
| `FileInfoCacheLifetime` | 10 | File size, mtime, attributes (`os.stat`) |
| `FileNotFoundCacheLifetime` | 5 | Negative lookups for missing files |

Setting all three to `0` disables those caches entirely. From an elevated
Command Prompt or PowerShell:

```powershell
$key = "HKLM:\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters"
New-ItemProperty -Path $key -Name DirectoryCacheLifetime    -Value 0 -PropertyType DWord -Force
New-ItemProperty -Path $key -Name FileInfoCacheLifetime     -Value 0 -PropertyType DWord -Force
New-ItemProperty -Path $key -Name FileNotFoundCacheLifetime -Value 0 -PropertyType DWord -Force
```

You'll need to log off and back on (or reboot) before the change takes
effect for new SMB connections.

There is also a separate **opportunistic locking / leasing** layer at the
SMB protocol level which can cache file *contents* on the client. That
one is best disabled on the **server** side (or by mounting the share
with leasing turned off in `Set-SmbClientConfiguration -OplocksDisabled
$true` on Windows 10/11). Leasing is a perf win for typical workloads, so
turn it off only if the client cache disable above isn't enough.

If a different host is the one writing the JSONL files (e.g. Claude Code
running in a Linux VM/container while you read from Windows), and the
server is a Linux Samba host, the cache settings above plus a 1 s
clacogui poll loop will catch every change in well under 2 s.

## Resilience

The Claude Code folder is typically on a network share (SMB), so I/O can
fail intermittently. clacogui handles that as follows:

- **User-initiated reads** (open dialog, opening a conversation, manual
  reload) pop a single Retry/Quit dialog when an `OSError` happens. The
  dialog never stacks: only one is on screen at a time. Retry re-submits
  the same job to the worker pool.
- **Background polling** swallows the failure, logs it (rate-limited), and
  tries again on the next tick. It does **not** open a dialog so a flaky
  share won't spam you.

## Why some keys are forwarded through the page

`wx.html2.WebView` (Edge / WebView2 on Windows) is a full out-of-process
browser control. It captures keystrokes natively before wx can map them to
accelerators, so menu shortcuts like `F6`, `Ctrl+Tab`, `Ctrl+1..9`,
`Ctrl+O`, `Ctrl+W`, `Ctrl+R` would otherwise be silently swallowed when
focus is in the WebView.

Workaround: every rendered page contains a tiny JavaScript `keydown`
listener that, on those keys, navigates to a `clacogui-action://...` URL
(e.g. `clacogui-action://toggle-pane`). `ConversationPanel` listens for
`EVT_WEBVIEW_NAVIGATING`, vetoes the navigation, and dispatches the action
to the right method. From a screen reader's point of view the document URL
never changes, because the navigation is vetoed before it commits.

## Hook agent (`clacogui_agent.py`)

`clacogui_agent.py` is a single-file companion to the GUI.  It does two
things:

1. **`install`** runs on Windows and configures Claude Code on the Linux
   side (local/SMB or via FTP) to call back into clacogui for
   `Notification` and `PreToolUse` events.  It writes
   `<.claude>/settings.json`, copies itself to the Linux side, and
   creates these directories:

       clacogui_notifications/    fire-and-forget sound notifications
       clacogui_requests/         pending PreToolUse permission requests
       clacogui_responses/        the user's allow/deny answers

2. **`hook`** runs on Linux, invoked by Claude Code via `settings.json`.
   For `Notification` events it drops a tiny JSON file under
   `clacogui_notifications/` and exits.  For `PreToolUse` events it
   writes a request file and waits (default 5 minutes) for the matching
   response file to appear, then exits with the corresponding allow/block
   decision.  If nothing answers, it denies by default.

To install:

```bat
install_agent.cmd                                          REM prompts
install_agent.cmd X:\.claude
install_agent.cmd ftp://ftpuser:<pw>@host:2121/home/me/.claude
```

The installer is interactive: it asks for the Linux-side absolute path of
the script and the Linux-side Python interpreter.  Re-running it is
idempotent &mdash; it replaces any existing clacogui hook entry in
`settings.json` rather than duplicating it.

When clacogui is running and the agent is installed:

- A new file in `clacogui_notifications/` plays a Windows notification
  sound and is then deleted.  (Sound only; today nothing else is shown.)
- A new file in `clacogui_requests/` opens a modal dialog that displays
  the full event JSON with **Allow** / **Deny** buttons and an optional
  reason field.  Clicking writes a small JSON answer to
  `clacogui_responses/`; the agent picks it up and tells Claude to either
  proceed or block the tool call.

The permission UI is intentionally raw &mdash; we don't yet know the
shape of every `PreToolUse` payload, so the dialog just shows the JSON.
Once we've seen a few real ones we can build a smarter view.

## Send messages from clacogui (`clacogui_launcher.py`)

Each open conversation tab has a multi-line **Send** edit box below
the splitter.  When you type a message and press `Enter`, clacogui
drops a small JSON envelope into `<.claude>/clacogui_outgoing/`.  A
companion launcher inside the sandbox &mdash; `clacogui_launcher.py`,
installed alongside `clacogui_agent.py` &mdash; runs claude under a
PTY and types your message into claude's TUI.  Multi-line messages
are emitted with `Ctrl+J` between lines (claude's "insert newline"
keystroke) and a final `Enter` to submit; bracketed paste was tried
first but did not take in claude's prompt.

### Wiring it up

Run `clacogui_launcher.py` in place of `claude` &mdash; however your
environment normally launches Claude Code.  The typical invocation is:

```bash
python3 ~/.claude/clacogui_launcher.py                     # fresh session
python3 ~/.claude/clacogui_launcher.py --resume <session>  # resume one
```

Any additional arguments are passed straight through to claude.

The launcher is byte-transparent: your terminal still renders claude's
TUI exactly as before, and your keystrokes still flow through to
claude unchanged.  All it adds is a 200 ms-cadence side channel that
watches `clacogui_outgoing/`.

### Send-box keybindings

| Shortcut | Action |
| --- | --- |
| `Enter` | Send the message |
| `Shift+Enter` | Insert a newline (compose multi-line messages without sending) |
| `Alt+S` | Click the **Send** button |

Note: `Shift+Enter` is the *clacogui* convention.  Inside claude's
TUI itself the equivalent keystroke is `Ctrl+J`; the launcher
translates GUI newlines into `Ctrl+J` when injecting into claude's
prompt.

Send-message status appears in **field 1** of the window's status
bar (the middle slot): `Idle`, `Queued (...)`, `Sent at HH:MM:SS`,
`Interrupting (...)`, `Interrupt sent at HH:MM:SS`, or `Send failed:
<reason>`.  The bar mirrors the *active* tab's send-state; switching
tabs updates it to that tab's latest value.  Field 0 (leftmost) is
**claude's own busy/idle status** for the active tab -- see
[Status bar layout](#status-bar-layout) below.  Field 2 carries the
current folder / backend description.

### Interrupt claude

When claude is busy streaming a response or running a tool, you can
cancel the current turn from clacogui by:

- pressing `Ctrl+.` from anywhere in the window (works from the
  message list, content view, and send box);
- choosing **Conversation -> Interrupt claude** from the menu bar; or
- clicking the **Interrupt** button next to **Send** in the
  conversation pane.

clacogui drops an `action: interrupt` envelope into
`clacogui_outgoing/`; the launcher writes a single Esc (`\x1b`) byte
to claude's PTY master, bypassing every gate (claude_busy in
particular -- interrupt is *meant* for busy claude).  Esc is
idempotent in claude's TUI so this is safe to spam: unlike Ctrl+C,
which exits claude on the second hit, repeated Esc just cancels the
current turn each time.

### Uncommitted prompt dialog

The launcher refuses to overwrite text the user is already typing
into claude's TUI.  If clacogui asks the launcher to deliver a
message while claude's input box is non-empty, the launcher renames
your envelope to `<id>.json.needs_decision` and clacogui pops an
**Uncommitted prompt** dialog with two buttons:

- **Cancel (keep my message)** &ndash; the default.  The pending file
  is removed and your message is restored to clacogui's send box so
  you can retry later.
- **Erase claude's prompt and send** &ndash; clacogui rewrites the
  envelope with `force_clear: true` and renames it back to
  `<id>.json`.  The launcher then sends a Ctrl+E + Ctrl+U + backspace
  flood to clear claude's prompt before pasting your message.

The protocol files mutate visibly on disk so multiple GUIs (or future
debugging) can observe the state machine:

| Filename | Meaning |
| --- | --- |
| `<id>.json` | Pending (gate not yet open) |
| (gone) | Delivered |
| `<id>.json.needs_decision` (with `reason`) | Launcher needs the user to decide |
| `<id>.json.failed` (with `error`) | Permanent failure; clacogui surfaces the error and restores your text |

The launcher only consumes envelopes whose `session_id` matches the
one it discovered in `<.claude>/sessions/<claude-pid>.json`, so
multiple concurrent launchers (one per claude process) are safe.

## Files

- `clacogui.py` &mdash; entry point, logging, `sys.excepthook`.
- `gui.py` &mdash; `wx` UI: `MainFrame`, `OpenConversationDialog`,
  `ConversationPanel`, IO worker thread, agent monitor + permission
  dialog.
- `fs.py` &mdash; backend abstraction: `LocalBackend`, `FtpBackend`,
  `make_backend()`; read/write/list/delete/mkdir.
- `models.py` &mdash; JSONL parsing, session-name lookup. All IO goes
  through an `FsBackend`.
- `render.py` &mdash; Markdown -> HTML, code-block -> `<textarea>`
  rewriting, JS keystroke shim.
- `clacogui_agent.py` &mdash; hook + installer; ships standalone.
- `clacogui_launcher.py` &mdash; transparent PTY launcher for
  claude; installed by `clacogui_agent.py install`.
- `install_agent.cmd` &mdash; convenience wrapper that runs
  `clacogui_agent.py install` under the venv.
- `run.cmd` &mdash; Windows launcher: creates `.venv`, refreshes deps when
  `requirements.txt` changes, defaults the spec to the dev FTP URL.
- `requirements.txt`
