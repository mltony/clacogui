"""Markdown -> HTML rendering for clacogui.

Produces a self-contained HTML document for one ``Turn`` (user prompt +
assistant response).  Fenced code blocks (``` ... ```) become ``<textarea>``
form controls so they expose as multi-line edit boxes to screen readers in
``wx.html2.WebView``.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Tuple

import markdown as md_lib

from models import Turn

log = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_PLACEHOLDER_FMT = "\x00CLACOCODEBLOCK{}\x00"


# Injected into every page so that keystrokes the WebView would otherwise
# swallow (F2, F4, F5, F6, Ctrl+Tab, Ctrl+1..9, Ctrl+O, Ctrl+W, Ctrl+R,
# Ctrl+Shift+PgUp/Dn, Alt+<letter>) are forwarded to the wx side via a
# custom-scheme navigation.  ``ConversationPanel`` listens for
# EVT_WEBVIEW_NAVIGATING with this prefix, vetoes the navigation, and
# dispatches the action.
#
# Note: F5 in a normal browser would reload the page.  Here we hijack it
# so the wx accelerator (which moves focus into the send box) can fire.
_KEY_FORWARDER_JS = """
<script>
(function() {
  function act(name) {
    try { window.location.href = 'clacogui-action://' + name; } catch (e) {}
  }
  document.addEventListener('keydown', function(e) {
    var k = e.key;
    if (k === 'F2') {
      e.preventDefault(); e.stopPropagation();
      act('focus-list');
      return;
    }
    if (k === 'F4') {
      e.preventDefault(); e.stopPropagation();
      act('focus-html');
      return;
    }
    if (k === 'F5') {
      e.preventDefault(); e.stopPropagation();
      act('focus-send');
      return;
    }
    if (k === 'F6') {
      e.preventDefault(); e.stopPropagation();
      act('toggle-pane');
      return;
    }
    // Alt+<letter> -- give it back to wx so the menu bar's mnemonic
    // (Alt+F = File, Alt+V = View, ...) actually triggers.  Without this
    // the WebView swallows the keystroke before the menubar sees it.
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      var ak = (k || '').toLowerCase();
      if (ak.length === 1 && ak >= 'a' && ak <= 'z') {
        e.preventDefault(); e.stopPropagation();
        act('alt-key/' + ak);
        return;
      }
    }
    if (e.ctrlKey && !e.altKey) {
      if (k === 'Tab') {
        e.preventDefault(); e.stopPropagation();
        act(e.shiftKey ? 'prev-tab' : 'next-tab');
        return;
      }
      if (e.shiftKey) {
        // Ctrl+Shift+PageUp/PageDown -- move the current tab without
        // wrapping.  wx side decides whether the move is legal.
        if (k === 'PageUp') {
          e.preventDefault(); e.stopPropagation();
          act('move-tab-prev');
          return;
        }
        if (k === 'PageDown') {
          e.preventDefault(); e.stopPropagation();
          act('move-tab-next');
          return;
        }
      } else {
        if (/^[1-9]$/.test(k)) {
          e.preventDefault(); e.stopPropagation();
          act('tab-' + k);
          return;
        }
        // Ctrl+. -- "Interrupt claude".  e.key is literally '.'
        // on every layout I checked; keep the explicit string so we
        // don't trip on non-US layouts where keyCode==190 isn't
        // the period.
        if (k === '.') {
          e.preventDefault(); e.stopPropagation();
          act('interrupt-claude');
          return;
        }
        var lower = (k || '').toLowerCase();
        if (lower === 'o') { e.preventDefault(); e.stopPropagation(); act('open'); return; }
        if (lower === 'w') { e.preventDefault(); e.stopPropagation(); act('close-tab'); return; }
        if (lower === 'r') { e.preventDefault(); e.stopPropagation(); act('reload'); return; }
      }
    }
  }, true);
})();
</script>
"""


_PAGE_CSS = """
body {
  font-family: Segoe UI, Arial, sans-serif;
  font-size: 14px;
  padding: 1em;
  max-width: 950px;
  color: #111;
}
h1.user-msg {
  background: #e8eef7;
  border-left: 5px solid #335599;
  padding: 0.6em 0.8em;
  margin: 0 0 1em 0;
  font-size: 1.4em;
}
h1.response-heading {
  border-bottom: 1px solid #ccc;
  padding-bottom: 0.2em;
  margin-top: 0.5em;
  font-size: 1.3em;
}
pre.user-msg-body {
  background: transparent;
  border: none;
  padding: 0 0.8em 0.6em 0.8em;
  margin: -0.6em 0 1em 0;
  white-space: pre-wrap;
  font-family: inherit;
  font-size: 1em;
  color: #111;
}
pre {
  background: #f3f3f3;
  border: 1px solid #ddd;
  padding: 0.5em;
  overflow-x: auto;
}
code {
  font-family: Consolas, Menlo, monospace;
  font-size: 0.95em;
}
table {
  border-collapse: collapse;
  margin: 0.5em 0;
}
table, th, td {
  border: 1px solid #bbb;
}
th, td {
  padding: 0.3em 0.6em;
}
textarea.code-block {
  font-family: Consolas, Menlo, monospace;
  font-size: 0.95em;
  width: 95%;
  background: #f8f8f8;
  border: 1px solid #bbb;
  padding: 0.4em;
  white-space: pre;
  display: block;
  margin: 0.5em 0;
}
.code-lang {
  font-size: 0.85em;
  color: #555;
  margin: 0.5em 0 0 0;
}
"""


_CONTENT_ID = "turn-content"


def render_turn_inner_html(turn: Turn) -> str:
    """Render only the inner HTML for a turn -- no <html>/<head>/<body>.

    Used by ``ConversationPanel`` for *incremental* WebView updates: when
    the same turn is being polled and only its content grew, we
    JSON-encode this string and ``RunScript`` it into the existing page's
    ``#turn-content`` div, so the document is never reloaded and the
    screen reader keeps its cursor position.
    """
    user_text = turn.cropped_user_text(10000)
    # Preserve leading indentation for pasted code snippets: HTML normally
    # collapses runs of ASCII space, which strips indentation to the eye.
    # Wrap the escaped text in a <pre> so ``white-space: pre-wrap`` retains
    # every space *and* honours \n line breaks -- no <br> substitution
    # needed.  <pre> here inherits the .user-msg-body CSS class defined in
    # _PAGE_CSS below.
    user_html = html.escape(user_text)

    response_md = (turn.response or "").strip()
    if not response_md:
        response_md = "_(no response yet)_"
    # Ensure response has an h1 at the top so screen readers can jump to it.
    if not _starts_with_h1(response_md):
        response_md = "# Response\n\n" + response_md

    body_html = _markdown_to_html(response_md)

    # Tag the first <h1> in the response so we can style it distinctly.
    body_html = re.sub(
        r"<h1>", '<h1 class="response-heading">', body_html, count=1
    )

    return (
        '<h1 class="user-msg" lang="en">User message</h1>\n'
        f'<pre class="user-msg-body">{user_html}</pre>\n'
        f"{body_html}"
    )


def render_turn_html(turn: Turn) -> str:
    """Render a single turn as a complete HTML document."""
    inner = render_turn_inner_html(turn)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Conversation turn</title>
<style>{_PAGE_CSS}</style>
{_KEY_FORWARDER_JS}
</head>
<body>
<div id="{_CONTENT_ID}" aria-live="polite">
{inner}
</div>
</body>
</html>
"""


def render_blank_html(message: str = "") -> str:
    """Render an empty placeholder document."""
    msg = html.escape(message) if message else "Select a message on the left."
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>clacogui</title><style>{_PAGE_CSS}</style>
{_KEY_FORWARDER_JS}
</head>
<body>
<div id="{_CONTENT_ID}" aria-live="polite">
<h1>clacogui</h1><p>{msg}</p>
</div>
</body></html>
"""


def content_id() -> str:
    """The ``id`` of the wrapper div used for incremental updates.

    Exposed so ``gui.py`` can build the ``RunScript`` payload that
    swaps just the inner content (rather than reloading the whole
    document and resetting screen-reader position).
    """
    return _CONTENT_ID


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _starts_with_h1(md: str) -> bool:
    stripped = md.lstrip()
    if stripped.startswith("# "):
        return True
    # Setext-style:  Title\n=====
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].strip() and re.fullmatch(r"=+\s*", lines[1]):
        return True
    return False


def _markdown_to_html(md: str) -> str:
    """Run python-markdown on ``md`` after pulling fenced code blocks out.

    The fenced blocks are replaced with placeholders, converted to
    ``<textarea>`` elements after the markdown pass, then spliced back in.
    """
    blocks: list[Tuple[str, str]] = []

    def grab(match: re.Match) -> str:
        lang = (match.group(1) or "").strip()
        code = match.group(2)
        idx = len(blocks)
        blocks.append((lang, code))
        # Surround with blank lines so markdown treats the placeholder as its
        # own paragraph and does NOT wrap it in `<p>` tags incorrectly.
        return f"\n\n{_PLACEHOLDER_FMT.format(idx)}\n\n"

    pre = _FENCE_RE.sub(grab, md)

    try:
        rendered = md_lib.markdown(
            pre,
            extensions=["tables", "sane_lists", "nl2br"],
            output_format="html",
        )
    except Exception:
        log.exception("markdown conversion failed; falling back to plain")
        rendered = "<pre>" + html.escape(pre) + "</pre>"

    for idx, (lang, code) in enumerate(blocks):
        textarea = _code_block_html(lang, code)
        placeholder = _PLACEHOLDER_FMT.format(idx)
        # Markdown often wraps the lone placeholder in <p>...</p>.
        rendered = rendered.replace(f"<p>{placeholder}</p>", textarea, 1)
        rendered = rendered.replace(placeholder, textarea, 1)

    return rendered


def _code_block_html(lang: str, code: str) -> str:
    """Render one fenced code block as a read-only multi-line text box.

    The textarea is ``readonly`` so it can't be edited, but the caret still
    lands in it and arrow-key navigation still works -- exactly what a
    screen-reader user needs to read the block character by character.
    The visible "Code block (lang):" caption above the box was removed on
    request; the ``aria-label`` still carries the language for
    accessibility.
    """
    code = code.rstrip("\n")
    rows = max(3, min(40, code.count("\n") + 2))
    label = f"Code block ({lang})" if lang else "Code block"
    label_esc = html.escape(label)
    # Width in cols based on the longest line, capped.
    longest = max((len(line) for line in code.splitlines()), default=20)
    cols = max(40, min(100, longest + 4))
    safe = html.escape(code)
    return (
        f'<textarea class="code-block" aria-label="{label_esc}" '
        f'rows="{rows}" cols="{cols}" spellcheck="false" '
        f'wrap="off" readonly>{safe}</textarea>'
    )
