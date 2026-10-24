"""Concurrent GUI processes must not share runtime or WebView log files."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import clacogui, json, logging, os, sys
clacogui.log.info('runtime marker ' + sys.argv[1])
logging.getLogger('webview').info('webview marker ' + sys.argv[1])
logging.shutdown()
print(json.dumps({'log': clacogui.LOG_PATH, 'webview': clacogui.WEBVIEW_LOG_PATH, 'pid': os.getpid()}))
"""


class TestAppLogging(unittest.TestCase):
    def test_two_processes_with_same_profile_have_independent_logs(self):
        with tempfile.TemporaryDirectory(prefix="clacogui-logs-") as folder:
            env = dict(os.environ, CLACOGUI_LOG_DIR=folder, CLACOGUI_LOG_PROFILE="run_hidden_sftp")
            processes = [subprocess.Popen([sys.executable, "-c", PROBE, marker], cwd=ROOT,
                                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for marker in ("FIRST", "SECOND")]
            results = []
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, stderr)
                    results.append(json.loads(stdout))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            paths = [result[key] for result in results for key in ("log", "webview")]
            self.assertEqual(len(set(paths)), 4)
            for result, marker, other in zip(results, ("FIRST", "SECOND"), ("SECOND", "FIRST")):
                content = Path(result["log"]).read_text(encoding="utf-8")
                self.assertIn("runtime marker " + marker, content)
                self.assertNotIn(other, content)
                self.assertIn("thread=MainThread", content)
                self.assertIn("pid=" + str(result["pid"]), content)
                self.assertIn("run_hidden_sftp", Path(result["log"]).name)
                webview = Path(result["webview"]).read_text(encoding="utf-8")
                self.assertIn("webview marker " + marker, webview)
                self.assertNotIn(other, webview)

    def test_default_logs_use_local_appdata_instead_of_repository(self):
        with tempfile.TemporaryDirectory(prefix="clacogui-local-logs-") as folder:
            env = os.environ.copy()
            env.pop("CLACOGUI_LOG_DIR", None)
            env["LOCALAPPDATA"] = folder
            result = subprocess.run([sys.executable, "-c", PROBE, "LOCAL"], cwd=ROOT,
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = json.loads(result.stdout)
            self.assertEqual(Path(paths["log"]).parent, Path(folder) / "clacogui" / "logs")
