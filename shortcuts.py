"""Application shortcuts shared by wx accelerators, WebView and documentation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Shortcut:
    key: str
    action: str
    label: str
    description: str
    ctrl: bool = False
    shift: bool = False
    alt: bool = False


SHORTCUTS = (
    Shortcut("o", "open", "Ctrl+O", "Open Claude conversation", ctrl=True),
    Shortcut("o", "open-codex", "Ctrl+Shift+O", "Open Codex conversation", ctrl=True, shift=True),
    Shortcut("w", "close-tab", "Ctrl+W", "Close conversation", ctrl=True),
    Shortcut("f4", "close-tab", "Ctrl+F4", "Close conversation", ctrl=True),
    Shortcut("r", "reload", "Ctrl+R", "Refresh conversation", ctrl=True),
    Shortcut("r", "rebuild-webview", "Ctrl+Shift+R", "Rebuild content view", ctrl=True, shift=True),
    Shortcut("tab", "next-tab", "Ctrl+Tab", "Next conversation", ctrl=True),
    Shortcut("tab", "prev-tab", "Ctrl+Shift+Tab", "Previous conversation", ctrl=True, shift=True),
    Shortcut("pageup", "move-tab-prev", "Ctrl+Shift+PgUp", "Move tab left", ctrl=True, shift=True),
    Shortcut("pagedown", "move-tab-next", "Ctrl+Shift+PgDn", "Move tab right", ctrl=True, shift=True),
    Shortcut("f2", "focus-list", "F2", "Focus message list"),
    Shortcut("f4", "focus-html", "F4", "Focus content"),
    Shortcut("f5", "focus-send", "F5", "Focus send box"),
    Shortcut("f6", "toggle-pane", "F6", "Switch between message list and content"),
    Shortcut("f6", "toggle-pane", "Shift+F6", "Switch between message list and content", shift=True),
    Shortcut("f4", "quit", "Alt+F4", "Close window", alt=True),
    Shortcut(".", "interrupt-claude", "Ctrl+.", "Interrupt current Claude/Codex turn", ctrl=True),
    Shortcut("s", "send", "Alt+S", "Send composed message", alt=True),
    Shortcut("i", "interrupt-claude", "Alt+I", "Interrupt current Claude/Codex turn", alt=True),
) + tuple(
    Shortcut(str(i), f"tab-{i}", f"Ctrl+{i}", f"Select conversation {i}", ctrl=True)
    for i in range(1, 10)
) + tuple(
    Shortcut(key, f"alt-key/{key}", f"Alt+{key.upper()}", f"Open {name} menu", alt=True)
    for key, name in (("f", "File"), ("c", "Conversation"), ("v", "View"))
)


def wx_accelerators(wx, action_ids):
    entries = []
    for shortcut in SHORTCUTS:
        if shortcut.action.startswith("alt-key/"):
            continue  # wx's menu mnemonics handle these outside the WebView.
        flags = wx.ACCEL_NORMAL
        for enabled, flag in ((shortcut.ctrl, wx.ACCEL_CTRL),
                              (shortcut.shift, wx.ACCEL_SHIFT),
                              (shortcut.alt, wx.ACCEL_ALT)):
            if enabled:
                flags |= flag
        key = (ord(shortcut.key.upper()) if len(shortcut.key) == 1
               else getattr(wx, "WXK_" + shortcut.key.upper()))
        entries.append((flags, key, action_ids[shortcut.action]))
    return entries


def markdown_table():
    lines = ["| Shortcut | Action |", "|---|---|"]
    lines.extend(f"| `{s.label}` | {s.description} |" for s in SHORTCUTS)
    return "\n".join(lines)
