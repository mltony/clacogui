"""Exercise the real VBS -> CMD -> Python launch path on Windows."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import venv


@unittest.skipUnless(os.name == "nt", "Windows launcher")
class TestHiddenLauncher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="clacogui launch ")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        source = Path(__file__).resolve().parents[1]
        for name in ("launch_hidden.vbs", "run.cmd"):
            shutil.copyfile(source / name, cls.root / name)
        venv.create(cls.root / ".venv", with_pip=False)
        requirements = b"# Launcher test: no third-party imports\n"
        (cls.root / "requirements.txt").write_bytes(requirements)
        (cls.root / ".venv/.req-stamp").write_text(hashlib.sha256(requirements).hexdigest())
        (cls.root / "clacogui.py").write_text(
            "import json, os, sys\n"
            "print('PROBE:' + json.dumps({'args': sys.argv[1:], "
            "'host': os.environ.get('CLACOGUI_HOST'), "
            "'key': os.environ.get('CLACOGUI_SSH_KEY'), "
            "'known_hosts': os.environ.get('CLACOGUI_SSH_KNOWN_HOSTS'), "
            "'password': os.environ.get('CLACOGUI_FTP_PASSWORD'), "
            "'legacy': os.environ.get('CLACOGUI_WSL_CODEX_HOME')}))\n"
            "sys.exit(int(os.environ.get('CLACOGUI_TEST_EXIT', '0')))\n",
            encoding="utf-8",
        )

    def launch(self, name, host, claude, codex, *arguments):
        log_name = name + ".log"
        profile = self.root / (name + ".vbs")
        profile.write_text(
            'Option Explicit\nDim fso, helper\n'
            'Set fso = CreateObject("Scripting.FileSystemObject")\n'
            'Set helper = fso.OpenTextFile(fso.BuildPath( _\n'
            'fso.GetParentFolderName(WScript.ScriptFullName), "launch_hidden.vbs"), 1)\n'
            'ExecuteGlobal helper.ReadAll\nhelper.Close\n'
            f'LaunchHidden "{host}", 2121, "ftpuser", "test-value", '
            f'"{claude}", "{codex}", "{log_name}"\n', encoding="utf-8",
        )
        env = os.environ.copy()
        env["CLACOGUI_WSL_CODEX_HOME"] = "/wrong/inherited/folder"
        env["CLACOGUI_CODEX_DIR"] = "/also/wrong"
        result = subprocess.run(
            ["cscript.exe", "//nologo", str(profile), *arguments],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 10
        log = self.root / log_name
        while time.monotonic() < deadline:
            text = log.read_text(errors="replace") if log.exists() else ""
            for line in text.splitlines():
                if line.startswith("PROBE:"):
                    return json.loads(line[len("PROBE:"):])
            time.sleep(0.05)
        self.fail("Launcher did not reach Python: " + text)

    def test_profiles_pass_host_and_both_directories(self):
        for name, host, home in (
            ("work", "localhost", "/home/example"),
            ("personal", "192.0.2.10", "/Users/example"),
        ):
            with self.subTest(profile=name):
                output = self.launch(name, host, home + "/.claude", home + "/.codex")
                base = f"ftp://ftpuser:test-value@{host}:2121"
                self.assertEqual(output["args"], [base + home + "/.claude", "--codex", base + home + "/.codex"])
                self.assertEqual(output["host"], host)
                self.assertFalse(output["legacy"])

    def test_explicit_arguments_with_spaces_override_profile(self):
        output = self.launch("explicit", "localhost", "/profile/.claude", "/profile/.codex",
                             "C:\\folder with spaces\\.claude", "--codex", "C:\\other folder\\.codex")
        self.assertEqual(output["args"], ["C:\\folder with spaces\\.claude", "--codex", "C:\\other folder\\.codex"])

    def test_empty_codex_directory_does_not_inherit_another_profile(self):
        output = self.launch("claude-only", "localhost", "/profile/.claude", "")
        self.assertEqual(output["args"], ["ftp://ftpuser:test-value@localhost:2121/profile/.claude"])

    def test_run_cmd_preserves_python_exit_code_with_both_providers(self):
        env = os.environ.copy()
        env.update({
            "CLACOGUI_HOST": "localhost", "CLACOGUI_PORT": "2121", "CLACOGUI_PROTOCOL": "ftp",
            "CLACOGUI_USER": "ftpuser", "CLACOGUI_FTP_PASSWORD": "test-value",
            "CLACOGUI_CLAUDE_DIR": "/profile/.claude",
            "CLACOGUI_CODEX_DIR": "/profile/.codex", "CLACOGUI_TEST_EXIT": "7",
        })
        result = subprocess.run(
            f'cmd /d /s /c ""{self.root / "run.cmd"}""', cwd=self.root,
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)

    def test_sftp_profile_uses_key_and_both_remote_directories(self):
        profile = self.root / "sftp.vbs"
        profile.write_text(
            'Option Explicit\nDim fso, helper\n'
            'Set fso = CreateObject("Scripting.FileSystemObject")\n'
            'Set helper = fso.OpenTextFile(fso.BuildPath( _\n'
            'fso.GetParentFolderName(WScript.ScriptFullName), "launch_hidden.vbs"), 1)\n'
            'ExecuteGlobal helper.ReadAll\nhelper.Close\n'
            'LaunchHiddenSftp "sftp.example", 2222, "probe", "C:\\key folder\\id_ed25519", '
            '"C:\\key folder\\known_hosts", "/home/probe/.claude", "/home/probe/.codex", "sftp.log"\n',
            encoding="utf-8",
        )
        result = subprocess.run(["cscript.exe", "//nologo", str(profile)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            log = self.root / "sftp.log"
            text = log.read_text(errors="replace") if log.exists() else ""
            for line in text.splitlines():
                if not line.startswith("PROBE:"):
                    continue
                output = json.loads(line[6:])
                self.assertEqual(output["args"], ["sftp://probe@sftp.example:2222/home/probe/.claude", "--codex", "sftp://probe@sftp.example:2222/home/probe/.codex"])
                self.assertEqual(output["key"], "C:\\key folder\\id_ed25519")
                self.assertEqual(output["known_hosts"], "C:\\key folder\\known_hosts")
                self.assertFalse(output["password"])
                return
            time.sleep(0.05)
        self.fail("SFTP launcher did not reach Python: " + text)
