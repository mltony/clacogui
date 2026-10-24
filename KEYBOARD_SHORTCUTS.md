# Keyboard shortcuts

The application-wide table is generated from `shortcuts.SHORTCUTS`, which
also drives wx accelerators and the WebView JavaScript. Update that registry
and this table together; a test checks that they match. Modifier combinations
are exact, so F4, Ctrl+F4 and Alt+F4 invoke different actions.

## Main window, including transcript content and code blocks

| Shortcut | Action |
|---|---|
| `Ctrl+O` | Open Claude conversation |
| `Ctrl+Shift+O` | Open Codex conversation |
| `Ctrl+W` | Close conversation |
| `Ctrl+F4` | Close conversation |
| `Ctrl+R` | Refresh conversation |
| `Ctrl+Shift+R` | Rebuild content view |
| `Ctrl+Tab` | Next conversation |
| `Ctrl+Shift+Tab` | Previous conversation |
| `Ctrl+Shift+PgUp` | Move tab left |
| `Ctrl+Shift+PgDn` | Move tab right |
| `F2` | Focus message list |
| `F4` | Focus content |
| `F5` | Focus send box |
| `F6` | Switch between message list and content |
| `Shift+F6` | Switch between message list and content |
| `Alt+F4` | Close window |
| `Ctrl+.` | Interrupt current Claude/Codex turn |
| `Alt+S` | Send composed message |
| `Alt+I` | Interrupt current Claude/Codex turn |
| `Ctrl+1` | Select conversation 1 |
| `Ctrl+2` | Select conversation 2 |
| `Ctrl+3` | Select conversation 3 |
| `Ctrl+4` | Select conversation 4 |
| `Ctrl+5` | Select conversation 5 |
| `Ctrl+6` | Select conversation 6 |
| `Ctrl+7` | Select conversation 7 |
| `Ctrl+8` | Select conversation 8 |
| `Ctrl+9` | Select conversation 9 |
| `Alt+F` | Open File menu |
| `Alt+C` | Open Conversation menu |
| `Alt+V` | Open View menu |

## Context-specific keys

| Context | Shortcut | Action |
|---|---|---|
| Send box | Enter / numpad Enter | Send message |
| Send box | Shift+Enter / Shift+numpad Enter | Insert newline |
| Message list / Open dialog | Up / Down | Select message / conversation |
| Open dialog | Enter / numpad Enter / Alt+O | Open selection |
| Open dialog | Alt+T / Alt+N | Sort by time / name |
| Open dialog | Alt+R | Refresh, bypassing cached directory listings |
| Open dialog | Alt+A | Toggle Show all conversations |
| Open dialog | Alt+C / Escape | Cancel |
| Reorder tabs dialog | Alt+Up / Alt+U | Move selected tab up |
| Reorder tabs dialog | Alt+Down / Alt+D | Move selected tab down |
| Reorder tabs dialog | Enter / Alt+O | Apply order |
| Reorder tabs dialog | Escape / Alt+C | Cancel |
| Prompt-conflict dialog | Alt+E | Erase remote prompt and send |
| Prompt-conflict dialog | Escape / Alt+C | Keep message and cancel |
| Permission dialog | Alt+A / Alt+D | Allow / deny |
| Question dialog | Alt+S / Alt+C | Submit / cancel |
| Permission dialog | Alt+W / Alt+J / Alt+X / Alt+H | Optional allow-always, JSON, and turn-grant buttons as labelled |
| Native controls and WebView | Tab / Shift+Tab | Move between focusable elements |
| Native controls and WebView | Ctrl+C / Ctrl+A and normal selection/navigation keys | Standard text operations |
| WebView | Enter on a link | Open link in the system browser |

Context-specific dialog mnemonics use native wx controls; no WebView is
present in those dialogs. Permission and question defaults remain determined
by their dialogs. Code blocks allow selection and copying while blocking
text changes. Unregistered key combinations, AltGr, composition and screen
reader commands are left to the focused control, Windows or the screen reader.
