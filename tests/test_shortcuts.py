"""Keep native bindings, the actual JavaScript handler and the inventory aligned."""

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gui
import render
from shortcuts import SHORTCUTS, markdown_table, wx_accelerators


class TestShortcuts(unittest.TestCase):
    def test_no_duplicate_key_combinations_and_all_actions_have_native_commands(self):
        combinations = [(s.key, s.ctrl, s.alt, s.shift) for s in SHORTCUTS]
        self.assertEqual(len(combinations), len(set(combinations)))
        ids = gui.MainFrame._shortcut_ids(None)
        for shortcut in SHORTCUTS:
            if not shortcut.action.startswith("alt-key/"):
                self.assertIn(shortcut.action, ids)
        native = wx_accelerators(gui.wx, ids)
        self.assertEqual(len(native), len([s for s in SHORTCUTS if not s.action.startswith("alt-key/")]))

    def test_inventory_contains_the_current_registry(self):
        text = (Path(__file__).resolve().parents[1] / "KEYBOARD_SHORTCUTS.md").read_text(encoding="utf-8")
        self.assertIn(markdown_table(), text)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the WebView JavaScript")
    def test_actual_javascript_forwards_every_registered_combination(self):
        events = [dict(key=s.key.upper() if s.key.startswith("f") and len(s.key) > 1 else s.key,
                       ctrlKey=s.ctrl, shiftKey=s.shift, altKey=s.alt) for s in SHORTCUTS]
        expected = [s.action for s in SHORTCUTS]
        # Unrelated editing, AltGr, composition and extra modifiers must pass through.
        unrelated = [dict(key="c", ctrlKey=True), dict(key="v", ctrlKey=True),
                     dict(key="a", ctrlKey=True), dict(key="F4", ctrlKey=True, shiftKey=True),
                     dict(key="o", ctrlKey=True, altKey=True), dict(key="Tab"),
                     dict(key="o", ctrlKey=True, metaKey=True),
                     dict(key="o", ctrlKey=True, isComposing=True)]
        events += unrelated
        expected += [None] * len(unrelated)
        handler = render._KEY_FORWARDER_JS.replace("<script>", "").replace("</script>", "")
        script = """
const handlers = {};
const document = {addEventListener: (kind, handler) => handlers[kind] = handler};
let action = null;
const window = {location: {set href(value) {action = value.replace('clacogui-action://', '');}}};
""" + handler + "\nconst events = " + json.dumps(events) + ";\n" + """
const results = events.map(event => {
  action = null;
  let prevented = false;
  event.preventDefault = () => prevented = true;
  event.stopPropagation = () => {};
  handlers.keydown(event);
  return {action, prevented};
});
process.stdout.write(JSON.stringify(results));
"""
        result = subprocess.run(["node"], input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        results = json.loads(result.stdout)
        self.assertEqual([r["action"] for r in results], expected)
        self.assertEqual([r["prevented"] for r in results], [e is not None for e in expected])

    def test_webview_actions_post_the_same_native_command(self):
        ids = gui.MainFrame._shortcut_ids(None)
        class Frame:
            def _shortcut_ids(self):
                return ids
        frame = Frame()
        panel = SimpleNamespace(GetTopLevelParent=lambda: frame)
        with patch.object(gui, "MainFrame", Frame), patch.object(gui.wx, "PostEvent") as post:
            for action, command in ids.items():
                gui.ConversationPanel._handle_webview_action(panel, action)
                self.assertEqual(post.call_args.args[1].GetId(), command)
            self.assertEqual(post.call_count, len(ids))
