"""Real loopback SSH/SFTP checks: authentication, offsets and file protocol."""

import errno
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import paramiko

import codex_models
import clacogui_agent
import diagnostics
from fs import make_backend
import gui
import models
from sftp_backend import SftpBackend
import transcript_cache


class TestSshServer(paramiko.ServerInterface):
    def __init__(self, key):
        self.key = key

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == "probe" and key == self.key else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, channel_id):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class LocalSftpServer(paramiko.SFTPServerInterface):
    """Test-only server rooted in a temporary directory."""

    def __init__(self, server, *args, root, reads, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = root.resolve()
        self.reads = reads

    def path(self, remote):
        path = (self.root / remote.lstrip("/")).resolve()
        if os.path.commonpath((str(self.root), str(path))) != str(self.root):
            raise PermissionError(errno.EACCES, "Outside test root")
        return path

    @staticmethod
    def error(error):
        return paramiko.SFTPServer.convert_errno(error.errno or errno.EIO)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as error:
            return self.error(error)

    lstat = stat

    def list_folder(self, path):
        try:
            return [paramiko.SFTPAttributes.from_stat(item.stat(), filename=item.name)
                    for item in self.path(path).iterdir()]
        except OSError as error:
            return self.error(error)

    def open(self, path, flags, attrs):
        try:
            descriptor = os.open(self.path(path), flags | getattr(os, "O_BINARY", 0), 0o600)
            stream = os.fdopen(descriptor, "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb")
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = handle.writefile = stream
            handle.stat = lambda: paramiko.SFTPAttributes.from_stat(os.fstat(stream.fileno()))
            original_read = handle.read
            def read(offset, length):
                self.reads.append((path, offset, length))
                return original_read(offset, length)
            handle.read = read
            return handle
        except OSError as error:
            return self.error(error)

    def mkdir(self, path, attrs):
        try:
            self.path(path).mkdir()
            return paramiko.SFTP_OK
        except OSError as error:
            return self.error(error)

    def remove(self, path):
        try:
            self.path(path).unlink()
            return paramiko.SFTP_OK
        except OSError as error:
            return self.error(error)

    def rename(self, source, target):
        try:
            os.rename(self.path(source), self.path(target))
            return paramiko.SFTP_OK
        except OSError as error:
            return self.error(error)

    def posix_rename(self, source, target):
        try:
            os.replace(self.path(source), self.path(target))
            return paramiko.SFTP_OK
        except OSError as error:
            return self.error(error)


class LoopbackSftp:
    def __init__(self, root):
        self.root = root
        self.host_key = paramiko.RSAKey.generate(2048)
        self.client_key = paramiko.RSAKey.generate(2048)
        self.key_file = root / "client-key"
        self.client_key.write_private_key_file(str(self.key_file))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.known_hosts = root / "known_hosts"
        self.known_hosts.write_text(f"[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n")
        self.transports = []
        self.reads = []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.accept, args=(connection,), daemon=True).start()

    def accept(self, connection):
        transport = paramiko.Transport(connection)
        self.transports.append(transport)
        transport.add_server_key(self.host_key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, LocalSftpServer,
                                        root=self.root, reads=self.reads)
        try:
            transport.start_server(server=TestSshServer(self.client_key))
            while transport.is_active() and not self.stopping.wait(0.02):
                pass
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def backend(self, root="/"):
        return SftpBackend("127.0.0.1", self.port, "probe", root,
                           key_filename=str(self.key_file), known_hosts=str(self.known_hosts))

    def close(self):
        self.stopping.set()
        self.listener.close()
        self.thread.join(2)
        for transport in self.transports:
            transport.close()


class TestSftpBackend(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="clacogui-sftp-")))
        self.server = LoopbackSftp(self.root)
        self.addCleanup(self.server.close)
        self.backend = self.server.backend()
        self.addCleanup(self.backend.close)

    def test_metadata_head_and_incremental_reads(self):
        prefix, tail = b"x" * (1024 * 1024), "hello world\n".encode()
        (self.root / "session.jsonl").write_bytes(prefix + tail)
        self.assertEqual(self.backend.stat("session.jsonl")[1], len(prefix) + len(tail))
        self.assertEqual(self.backend.read_text_head("session.jsonl", 3), "xxx")
        self.server.reads.clear()
        self.assertEqual(self.backend.read_bytes_from("session.jsonl", len(prefix)), tail)
        self.assertTrue(self.server.reads)
        self.assertTrue(all(offset >= len(prefix) for _, offset, _ in self.server.reads))
        self.assertEqual(self.backend.read_bytes_from("session.jsonl", 0), prefix + tail)
        entries = {entry.name: entry for entry in self.backend.list_dir("")}
        self.assertEqual(entries["session.jsonl"].size, len(prefix) + len(tail))

    def test_large_download_does_not_abandon_prefetched_data_between_batches(self):
        payload = b"0123456789abcdef" * (3 * 1024 * 1024 // 16) + b"last record\n"
        (self.root / "large.jsonl").write_bytes(payload)
        original = paramiko.SFTPFile._prefetch_thread
        def delayed_batches(stream, chunks, maximum):
            # Reproduce a temporarily empty request queue before a full-file
            # prefetch has finished scheduling, as seen in the user's log.
            for index in range(0, len(chunks), 16):
                original(stream, chunks[index:index + 16], None)
                deadline = time.monotonic() + 5
                while stream._prefetch_extents and time.monotonic() < deadline:
                    time.sleep(0.001)
                time.sleep(0.02)
        with patch.object(paramiko.SFTPFile, "_prefetch_thread", delayed_batches):
            self.assertEqual(self.backend.read_bytes_from("large.jsonl", 0), payload)
        offsets = [offset for path, offset, _ in self.server.reads
                   if path == "/large.jsonl" and offset < len(payload)]
        self.assertEqual(len(offsets), len(set(offsets)), "Already prefetched blocks were downloaded again")
        self.assertEqual(len(offsets), (len(payload) + 32767) // 32768)

    def test_large_download_includes_data_appended_during_transfer(self):
        payload = b"x" * (1024 * 1024)
        path = self.root / "growing.jsonl"
        path.write_bytes(payload)
        original = LocalSftpServer.open
        appended = threading.Event()
        def open_growing(server, remote, flags, attrs):
            handle = original(server, remote, flags, attrs)
            if remote == "/growing.jsonl":
                read = handle.read
                def append_then_read(offset, size):
                    if not appended.is_set():
                        appended.set()
                        with path.open("ab") as stream:
                            stream.write(b"new record\n")
                    return read(offset, size)
                handle.read = append_then_read
            return handle
        with patch.object(LocalSftpServer, "open", open_growing):
            self.assertEqual(self.backend.read_bytes_from("growing.jsonl", 0), payload + b"new record\n")

    def test_atomic_replacement_rename_and_directory_creation(self):
        self.backend.write_bytes("notifications/event.json", b"first")
        self.backend.write_bytes("notifications/event.json", b"second")
        self.assertEqual(self.backend.read_text("notifications/event.json"), "second")
        self.backend.rename("notifications/event.json", "outgoing/event.json")
        self.assertFalse(self.backend.exists("notifications/event.json"))
        self.assertEqual(self.backend.read_text("outgoing/event.json"), "second")
        self.assertEqual([entry.name for entry in self.backend.list_dir("outgoing")], ["event.json"])
        self.backend.delete("outgoing/event.json")
        self.backend.delete("outgoing/event.json")
        self.assertFalse(self.backend.is_dir("missing"))
        with self.assertRaises(FileExistsError):
            self.backend.mkdir("outgoing", exist_ok=False)

    def test_missing_files_keep_file_not_found_semantics(self):
        for operation in (lambda: self.backend.stat("missing"),
                          lambda: self.backend.read_text("missing"),
                          lambda: self.backend.list_dir("missing")):
            with self.assertRaises(FileNotFoundError):
                operation()

    def test_workers_have_independent_connections(self):
        (self.root / "session.jsonl").write_text("hello")
        barrier = threading.Barrier(3)
        pairs, errors = [], []
        def read():
            try:
                self.assertEqual(self.backend.read_text("session.jsonl"), "hello")
                pairs.append(self.backend._tls.pair)
                barrier.wait(5)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=read) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        for thread in threads:
            thread.join(5)
        self.assertFalse(errors, errors)
        self.assertEqual(len(pairs), 2)
        self.assertIsNot(pairs[0][0], pairs[1][0])
        self.assertIsNot(pairs[0][1], pairs[1][1])

    def test_unknown_and_changed_host_keys_are_rejected(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                other = self.root / "untrusted-hosts"
                other.write_text(self.server.known_hosts.read_text().replace(
                    self.server.host_key.get_base64(), self.server.client_key.get_base64()
                ) if changed else "")
                backend = self.server.backend()
                backend.known_hosts = str(other)
                try:
                    with self.assertRaises(OSError):
                        backend.stat("session.jsonl")
                finally:
                    backend.close()

    def test_close_prevents_late_jobs_from_reconnecting(self):
        self.backend.close()
        with self.assertRaisesRegex(OSError, "closed"):
            self.backend.list_dir("")
        self.assertEqual(self.backend._connections, [])

    def test_blocked_sftp_read_reports_its_worker_stack_before_it_returns(self):
        (self.root / "blocked.jsonl").write_bytes(b"test")
        blocked, release = threading.Event(), threading.Event()
        result, errors = [], []
        original = LocalSftpServer.open
        def open_blocked(server, path, flags, attrs):
            handle = original(server, path, flags, attrs)
            if path == "/blocked.jsonl":
                read = handle.read
                def wait_read(offset, size):
                    blocked.set()
                    release.wait(5)
                    return read(offset, size)
                handle.read = wait_read
            return handle
        def read():
            try:
                result.append(self.backend.read_bytes_from("blocked.jsonl", 0))
            except Exception as error:
                errors.append(error)
        with patch.object(LocalSftpServer, "open", open_blocked):
            worker = threading.Thread(target=read, name="blocked-read-test")
            worker.start()
            try:
                self.assertTrue(blocked.wait(5))
                with patch.object(diagnostics, "REPORT_SEC", 0), patch.object(diagnostics, "STALL_SEC", 0):
                    with self.assertLogs("diagnostics", level="INFO") as logs:
                        diagnostics.report_pending()
                output = "\n".join(logs.output)
                self.assertIn("Read stalled:", output)
                self.assertIn("phase=reading", output)
                self.assertIn("blocked-read-test", output)
                self.assertIn("sftp_backend.py", output)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(errors, errors)
        self.assertEqual(result, [b"test"])

    def test_transport_failure_retries_once_but_permissions_do_not(self):
        operation = Mock(side_effect=[EOFError(), "recovered"])
        with patch.object(self.backend, "_session", return_value=object()), patch.object(self.backend, "_reset") as reset:
            with self.assertLogs("sftp_backend", level="WARNING"):
                self.assertEqual(self.backend._run(operation), "recovered")
            self.assertEqual(operation.call_count, 2)
            reset.assert_called_once()
        operation = Mock(side_effect=PermissionError(errno.EACCES, "Denied"))
        with patch.object(self.backend, "_session", return_value=object()), patch.object(self.backend, "_reset") as reset:
            with self.assertRaises(PermissionError):
                self.backend._run(operation)
            self.assertEqual(operation.call_count, 1)
            reset.assert_not_called()

    def test_both_providers_parse_and_reopen_with_cached_tail(self):
        records = {
            "claude.jsonl": [{"type": "user", "uuid": "u", "promptId": "p", "message": {"role": "user", "content": "Hello"}},
                              {"type": "assistant", "uuid": "a", "message": {"role": "assistant", "content": [{"type": "text", "text": "World"}]}}],
            "codex.jsonl": [{"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}},
                             {"type": "response_item", "payload": {"type": "message", "role": "assistant", "id": "a", "content": [{"type": "output_text", "text": "World"}]}}],
        }
        with patch.object(transcript_cache, "CACHE_DIR", str(self.root / "cache")):
            for name, entries in records.items():
                text = "".join(json.dumps(entry) + "\n" for entry in entries)
                path = self.root / name
                path.write_bytes(text.encode("utf-8"))
                parser = models.parse_conversation if name.startswith("claude") else codex_models.parse_conversation
                data = parser(self.backend, name)
                self.assertEqual(data.turns[0].response, "World")
                with path.open("ab") as stream:
                    stream.write(b"{}\n")
                self.assertEqual(transcript_cache.read_text_from(self.backend, name, len(text.encode())), "{}\n")
                self.assertEqual(parser(self.backend, name).turns[0].response, "World")

    def test_installer_uploads_both_providers_and_preserves_settings(self):
        for codex in (False, True):
            with self.subTest(codex=codex):
                name = ".codex" if codex else ".claude"
                folder = self.root / name
                folder.mkdir()
                if codex:
                    (folder / "config.toml").write_text('model = "example"\n')
                else:
                    (folder / "settings.json").write_text('{"customSetting": true}')
                backend = self.server.backend("/" + name)
                args = SimpleNamespace(remote_script=f"/{name}/clacogui_agent.py", python="python3", yes=True)
                with patch("fs.make_backend", return_value=backend), contextlib.redirect_stdout(io.StringIO()):
                    clacogui_agent._install_sftp("sftp://probe@host/" + name, b"agent", b"launcher", args, codex=codex)
                self.assertEqual((folder / "clacogui_agent.py").read_bytes(), b"agent")
                launcher = clacogui_agent.CODEX_LAUNCHER_NAME if codex else clacogui_agent.LAUNCHER_NAME
                self.assertEqual((folder / launcher).read_bytes(), b"launcher")
                if codex:
                    config = (folder / "config.toml").read_text()
                    self.assertIn('model = "example"', config)
                    self.assertIn("hook --codex", config)
                else:
                    config = json.loads((folder / "settings.json").read_text())
                    self.assertTrue(config["customSetting"])
                    self.assertIn("Stop", config["hooks"])


class TestSftpUrls(unittest.TestCase):
    def test_factory_and_gui_recognize_sftp_without_connecting(self):
        backend = make_backend("sftp://probe@host/home/probe/.claude")
        self.addCleanup(backend.close)
        self.assertIsInstance(backend, SftpBackend)
        self.assertFalse(gui._looks_local(backend.display_root()))
        self.assertEqual(gui.folder_key(backend), "sftp://probe@host:22/home/probe/.claude")
        self.assertEqual(gui._lock_key("sftp://probe@HOST/home/probe/.claude/"), gui._lock_key(backend.display_root()))
        self.assertNotEqual(gui._lock_key(backend.display_root()), gui._lock_key("sftp://other@host/home/probe/.claude"))
        self.assertNotEqual(gui._lock_key(backend.display_root()), gui._lock_key("sftp://probe@other/home/probe/.claude"))

    def test_authentication_matches_explicit_key_setup(self):
        with patch("sftp_backend.os.path.isfile", return_value=True), patch("sftp_backend.paramiko.SSHClient") as client:
            backend = SftpBackend("host", 22, "probe", "/", key_filename="identity", known_hosts="trusted")
            self.addCleanup(backend.close)
            backend._connect()
            client.return_value.load_system_host_keys.assert_called_once()
            client.return_value.load_host_keys.assert_called_once_with("trusted")
            options = client.return_value.connect.call_args.kwargs
            self.assertEqual(options["key_filename"], "identity")
            self.assertFalse(options["allow_agent"])
            self.assertFalse(options["look_for_keys"])
            self.assertIsInstance(client.return_value.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)

    def test_password_urls_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "omit the password"):
            make_backend("sftp://probe:password@host/home/probe")
