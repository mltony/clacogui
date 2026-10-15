# clacogui

A small Windows GUI for browsing **Claude Code** conversation transcripts,
and optionally **Codex CLI** ones side by side (see
[Codex CLI support](#codex-cli-support)).
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
  `ftp://ftpuser:<password>@localhost:2121/home/me/.claude`.

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

By default `run.cmd` connects to the FTP backend through WSL's localhost
forwarding (WSL exposes its listening ports on Windows `localhost`), at

    ftp://ftpuser:<CLACOGUI_FTP_PASSWORD>@localhost:2121/home/<user>/.claude

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
Content-view (WebView) health is also written to `clacogui-webview.log`
(rotated at 5 MB): every 30 seconds clacogui checks each tab's Edge child
windows and their processes, pings the current page, and logs any change;
focus moves into the content view are logged too. A view whose Edge
windows or process disappear is rebuilt automatically; Ctrl+Shift+R
rebuilds the current one by hand.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+O` | Open conversation dialog |
| `Ctrl+Shift+O` | Open Codex conversation dialog |
| `Ctrl+W` (or `Ctrl+F4`) | Close current conversation tab |
| `Ctrl+R` | Refresh current conversation from disk |
| `Ctrl+Shift+R` | Rebuild the content view (when the screen reader no longer sees inside it) |
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

NVDA's status-bar command (`NVDA+End` with the desktop keyboard layout) reads the full field text, including text that does not fit visually. Long fields are clipped on screen rather than replaced with an ellipsis, and their tooltips retain the full text.

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

Claude also reports `busy` while only a background agent is running
and its prompt is back up ("Waiting for N background agents to
finish").  The status bar shows that state as `Claude: background`, and the
launcher delivers messages in it: it treats
`busy` as sendable when the session transcript's last main-chain entry
is the `turn_duration` record that closes a turn.  Esc does nothing
there, since there is no foreground turn to cancel; to stop the
background agent, send claude a message asking it to.

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

### Uncommitted text in claude's prompt

If clacogui asks the launcher to deliver a message while claude's
input box is non-empty, **the launcher erases the prompt and delivers
anyway**.  Your message always wins; anything half-typed in claude's
TUI is discarded.

The erasure is `_ERASE_CYCLES` repetitions of Ctrl+E (end of line) +
Ctrl+U (kill line) + Backspace (join with the line above), followed by
a backspace flood of at least `_MIN_ERASE_BACKSPACES`.  The loop is
what makes **multi-line** prompts work: Ctrl+U kills only the line the
cursor is on, so a single Ctrl+E + Ctrl+U would leave the other lines
behind and your message would be appended to them.  Backspace crosses
newlines in claude's TUI, so the trailing flood keeps walking upward
through anything the cycles didn't reach.  The flood is *not* sized
from the user-buffer estimate alone -- that counter undercounts -- so
it has a generous floor; backspacing past the start of an empty prompt
is a no-op.

The launcher does still wait out `_QUIET_AFTER_KEYSTROKE_SEC` (500 ms)
since your last keystroke before erasing, so it won't yank the prompt
out from under you mid-word.  It simply retries on the next 200 ms
tick until you pause.

The protocol files mutate visibly on disk so multiple GUIs (or future
debugging) can observe the state machine:

| Filename | Meaning |
| --- | --- |
| `<id>.json` | Pending (gate not yet open) |
| (gone) | Delivered |
| `<id>.json.failed` (with `error`) | Permanent failure; clacogui surfaces the error and restores your text |

`<id>.json.needs_decision` is a **retired** state.  Older launchers
parked an envelope there and waited for clacogui to pop an
"Uncommitted prompt" dialog; if nobody answered it -- GUI restarted,
tab closed -- the file was orphaned and silently reaped an hour later,
losing the message.  Current launchers never write it.  clacogui still
recognises the suffix, so a current GUI keeps working against an old
installed launcher; it auto-authorises the erasure instead of asking.

The launcher only consumes envelopes whose `session_id` matches the
one it discovered in `<.claude>/sessions/<claude-pid>.json`, so
multiple concurrent launchers (one per claude process) are safe.

## Codex CLI support

clacogui can show Codex CLI conversations next to Claude Code ones.
The Codex home (`~/.codex`, or `$CODEX_HOME`) is a second, optional
folder; it accepts the same forms as the Claude folder (local path,
SMB-mapped drive, `ftp://` URL).

### Pointing clacogui at it

Any of:

- **File -> Change Codex folder...** (remembered as `codex_dir` in
  `%USERPROFILE%\.clacogui_config.json`);
- `python clacogui.py <claude-spec> --codex <codex-spec>`;
- with `run.cmd` and the WSL FTP setup, `setx CLACOGUI_WSL_CODEX_HOME
  /home/me/.codex` and the Codex home is served by the same FTP server.

Then **File -> Open Codex conversation...** (`Ctrl+Shift+O`).  The
dialog walks the 14 newest day directories under `sessions/` (each
listing is an FTP round trip); ticking **Show all** scans everything.
"Active" there means attached to a Codex launcher or touched in the
last hour.  Thread names come from Codex's `session_index.jsonl`.

Changing the Claude folder no longer closes Codex tabs, and vice versa.

### Hooks and launcher

```bat
install_agent.cmd --codex                                  REM prompts
install_agent.cmd --codex X:\.codex
install_agent.cmd --codex ftp://ftpuser:<pw>@host:2121/home/me/.codex
```

This copies `clacogui_agent.py` and `clacogui_codex_launcher.py` into
the Codex home, creates the same working directories plus
`clacogui_sessions/`, and appends a managed block to `config.toml`
(`Stop`, `SubagentStop` and `PermissionRequest` hooks running
`python3 .../clacogui_agent.py hook --codex`).  Re-running replaces the
block in place; your own hooks are left where they are.  The next time
Codex starts it asks you to **review the new hooks** -- pick *Trust all
and continue*, otherwise they don't run.

Run the launcher instead of `codex`; arguments pass straight through:

```bash
python3 ~/.codex/clacogui_codex_launcher.py                 # new thread
python3 ~/.codex/clacogui_codex_launcher.py resume <id|name>
```

#### Windows, and choosing how interrupts are sent

The launcher also runs on Windows (Codex in Windows Terminal).  Pick how
the GUI's interrupt reaches Codex with `--interrupt MODE` (or the
`CLACOGUI_CODEX_INTERRUPT` environment variable); the flag is the
launcher's own and is not passed to codex:

| Mode | Platforms | How it works |
| --- | --- | --- |
| `pty` | Linux, macOS (default there) | Codex runs under the launcher's PTY; interrupt types Esc into it. |
| `console` | Windows (default there) | Codex runs directly in the same console window; interrupt writes an Esc key event into the console's input queue. |
| `rpc` | any | Codex runs directly in the terminal; interrupt calls the Codex app-server's `turn/interrupt` through `codex app-server proxy`. |

```bat
python %USERPROFILE%\.codex\clacogui_codex_launcher.py resume <id|name>
python %USERPROFILE%\.codex\clacogui_codex_launcher.py --interrupt rpc resume <id|name>
```

On Windows, install the hooks with an interpreter and an absolute script
path Windows can run, e.g. `install_agent.cmd --codex
ftp://...@host:2121/Users/me/.codex --python python --remote-script
C:/Users/me/.codex/clacogui_agent.py --yes`.

What changes compared to Claude:

- **Status bar** field 0 reads `Codex: idle` / `Codex: busy` from the
  launcher's `clacogui_sessions/<pid>.json`.  Without a launcher it
  shows `Codex: idle (no launcher)` / `Codex: busy (no launcher)`,
  taken from the rollout itself; sending needs the launcher.
- **Send** uses `codex queue --thread <id> --message <text>`, so nothing
  is typed into the TUI and a message sent while Codex is busy runs
  right after the current turn.
- **Interrupt** (`Ctrl+.`) sends one Esc, and only while a turn is
  running: a second Esc at an idle Codex prompt opens "edit previous
  message".
- **Permission dialogs** come from Codex's `PermissionRequest` hook,
  which fires when Codex itself would ask for approval (it depends on
  your approval policy).  Codex sends no "always allow" suggestions.
- The launcher learns its thread from Codex's
  `tui-thread-reference-capabilities/` files, which the TUI touches when
  it opens a thread; `/new` or `/resume` inside the TUI switches it.  A
  *second* Codex TUI started without a launcher at the same moment can
  be mistaken for it -- run every Codex you want to drive from clacogui
  through a launcher.

## Files

- `clacogui.py` &mdash; entry point, logging, `sys.excepthook`.
- `gui.py` &mdash; `wx` UI: `MainFrame`, `OpenConversationDialog`,
  `ConversationPanel`, IO worker thread, agent monitor + permission
  dialog.
- `fs.py` &mdash; backend abstraction: `LocalBackend`, `FtpBackend`,
  `make_backend()`; read/write/list/delete/mkdir.
- `models.py` &mdash; JSONL parsing, session-name lookup. All IO goes
  through an `FsBackend`.
- `codex_models.py` &mdash; the same for Codex rollouts, thread names and
  Codex launcher state.
- `render.py` &mdash; Markdown -> HTML, code-block -> `<textarea>`
  rewriting, JS keystroke shim.
- `clacogui_agent.py` &mdash; hook + installer; ships standalone.
- `clacogui_launcher.py` &mdash; transparent PTY launcher for
  claude; installed by `clacogui_agent.py install`.
- `clacogui_codex_launcher.py` &mdash; the Codex counterpart; installed by
  `clacogui_agent.py install --codex`.
- `install_agent.cmd` &mdash; convenience wrapper that runs
  `clacogui_agent.py install` under the venv.
- `run.cmd` &mdash; Windows launcher: creates `.venv`, refreshes deps when
  `requirements.txt` changes, defaults the spec to the dev FTP URL.
- `requirements.txt`
