"""SFTP filesystem access using verified host keys and an explicit SSH key."""

from __future__ import annotations

import getpass
import logging
import os
from pathlib import Path
import posixpath
import socket
import stat
import threading
import time
from urllib.parse import quote, unquote, urlparse
import uuid

import paramiko

from diagnostics import ReadTrace
from fs import FsBackend, FsEntry

log = logging.getLogger(__name__)
TIMEOUT_SEC = 30
READ_BLOCK_BYTES = 32768
READ_BATCH_REQUESTS = 16


class SftpBackend(FsBackend):
    """Persistent, independent SSH/SFTP connections for each IO worker.

    Connections are lazy: constructing a backend never blocks the GUI.
    Host keys must already be trusted. Authentication matches the explicit
    Ed25519-key setup, with agent and automatic key discovery disabled.
    """

    cache_transcripts = True

    def __init__(self, host, port, user, root, *, key_filename=None, known_hosts=None):
        self.host = host
        self.port = port
        self.user = user
        self.root = posixpath.normpath("/" + (root or "").lstrip("/"))
        self.key_filename = os.path.expanduser(
            key_filename or os.environ.get("CLACOGUI_SSH_KEY")
            or str(Path.home() / ".ssh" / "id_ed25519")
        )
        self.known_hosts = os.path.expanduser(
            known_hosts or os.environ.get("CLACOGUI_SSH_KNOWN_HOSTS")
            or str(Path.home() / ".ssh" / "known_hosts")
        )
        self._tls = threading.local()
        self._connections = []
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def from_url(cls, spec):
        url = urlparse(spec)
        if url.scheme.lower() != "sftp" or not url.hostname:
            raise ValueError("Expected sftp://user@host:22/absolute/directory")
        if url.password is not None:
            raise ValueError("SFTP uses an SSH key; omit the password from the URL")
        if url.query or url.fragment:
            raise ValueError("SFTP directory URLs do not use query strings or fragments")
        return cls(url.hostname, url.port or 22,
                   unquote(url.username) if url.username else getpass.getuser(),
                   unquote(url.path or "/"))

    def _full(self, rel):
        return posixpath.normpath(posixpath.join(self.root, (rel or "").lstrip("/")))

    @staticmethod
    def _close_pair(pair):
        ssh, sftp = pair
        try:
            sftp.close()
        finally:
            ssh.close()

    def _connect(self):
        log.info("SFTP connect start: host=%s port=%d user=%s root=%s",
                 self.host, self.port, self.user, self.root)
        if not os.path.isfile(self.key_filename):
            raise OSError(f"SSH key file not found: {self.key_filename}")
        ssh = paramiko.SSHClient()
        try:
            ssh.load_system_host_keys()
            # Explicitly load the Windows user's known_hosts as well.
            ssh.load_host_keys(self.known_hosts)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            ssh.connect(
                hostname=self.host, username=self.user, port=self.port,
                key_filename=self.key_filename, allow_agent=False,
                look_for_keys=False, timeout=TIMEOUT_SEC,
                banner_timeout=TIMEOUT_SEC, auth_timeout=TIMEOUT_SEC,
            )
            ssh.get_transport().set_keepalive(30)
            log.info("SFTP authenticated: host=%s; opening SFTP subsystem", self.host)
            sftp = ssh.open_sftp()
            sftp.get_channel().settimeout(TIMEOUT_SEC)
        except (paramiko.SSHException, OSError) as error:
            ssh.close()
            raise OSError(f"SFTP connection to {self.host}:{self.port}: {error}") from error
        pair = (ssh, sftp)
        with self._lock:
            if self._closed:
                self._close_pair(pair)
                raise OSError("SFTP backend is closed")
            self._connections.append(pair)
        self._tls.pair = pair
        log.info("SFTP connected: %s:%d as %s, root=%s",
                 self.host, self.port, self.user, self.root)
        return sftp

    def _session(self):
        if self._closed:
            raise OSError("SFTP backend is closed")
        pair = getattr(self._tls, "pair", None)
        if pair is not None:
            transport = pair[0].get_transport()
            if transport is not None and transport.is_active():
                return pair[1]
            self._reset()
        return self._connect()

    def _reset(self):
        pair = getattr(self._tls, "pair", None)
        self._tls.pair = None
        if pair is None:
            return
        with self._lock:
            if pair in self._connections:
                self._connections.remove(pair)
        try:
            self._close_pair(pair)
        except Exception:
            log.debug("SFTP connection cleanup failed", exc_info=True)

    def _run(self, operation):
        started = time.monotonic()
        try:
            for attempt in range(2):
                try:
                    return operation(self._session())
                except (EOFError, paramiko.SSHException, ConnectionError, socket.timeout) as error:
                    self._reset()
                    if attempt:
                        raise OSError(f"SFTP transport failed: {error}") from error
                    log.warning("SFTP transport interrupted; reconnecting")
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 0.1:
                log.info("SFTP timing: %s %.1fms", operation.__qualname__, elapsed * 1000)

    def list_dir(self, rel):
        def work(sftp):
            return [FsEntry(e.filename, stat.S_ISDIR(e.st_mode or 0),
                            int(e.st_size or 0), float(e.st_mtime or 0))
                    for e in sftp.listdir_attr(self._full(rel))]
        return self._run(work)

    def is_dir(self, rel):
        try:
            return self._run(lambda sftp: stat.S_ISDIR(sftp.stat(self._full(rel)).st_mode))
        except FileNotFoundError:
            return False

    def stat(self, rel):
        def work(sftp):
            attrs = sftp.stat(self._full(rel))
            return float(attrs.st_mtime or 0), int(attrs.st_size or 0)
        return self._run(work)

    def read_text(self, rel, encoding="utf-8"):
        return self.read_bytes_from(rel, 0).decode(encoding, errors="replace")

    def read_text_head(self, rel, max_bytes=4096, encoding="utf-8"):
        def work(sftp):
            with sftp.open(self._full(rel), "rb") as stream:
                return stream.read(max_bytes)
        return self._run(work).decode(encoding, errors="replace")

    def read_bytes_from(self, rel, offset):
        def work(sftp):
            with ReadTrace(self.display_path(rel), offset) as trace:
                with sftp.open(self._full(rel), "rb") as stream:
                    trace.phase("stat")
                    stream.seek(max(0, offset))
                    size = stream.stat().st_size
                    trace.expected = max(0, size - max(0, offset)) if size is not None else None
                    trace.extra = lambda: {
                        "prefetch_done": getattr(stream, "_prefetch_done", None),
                        "pending_requests": len(getattr(stream, "_prefetch_extents", {})),
                        "buffered_blocks": len(getattr(stream, "_prefetch_data", {})),
                    }
                    log.log(trace.level, "Read size: id=%s file_size=%s offset=%d expected=%s",
                            trace.id, size, offset, trace.expected)
                    data = bytearray()
                    if trace.expected is not None and trace.expected > 65536:
                        log.info("Read strategy: id=%s bounded batches requests=%d block_bytes=%d",
                                 trace.id, READ_BATCH_REQUESTS, READ_BLOCK_BYTES)
                        position = max(0, offset)
                        short_read = False
                        while position < size:
                            trace.phase("reading batches")
                            chunks = [(start, min(READ_BLOCK_BYTES, size - start))
                                      for start in range(position, min(size, position + READ_BLOCK_BYTES * READ_BATCH_REQUESTS), READ_BLOCK_BYTES)]
                            # Schedule only one small batch. A full-file
                            # prefetch can mark itself done between request
                            # batches and silently abandon its buffered data.
                            blocks = stream.readv(chunks, max_concurrent_prefetch_requests=READ_BATCH_REQUESTS)
                            for (_, requested), chunk in zip(chunks, blocks):
                                data.extend(chunk)
                                trace.advance(len(chunk))
                                position += len(chunk)
                                if len(chunk) != requested:
                                    short_read = True  # File shrank while reading.
                                    break
                            if short_read:
                                break
                            if position >= size:
                                # Preserve read-to-EOF behavior for transcripts
                                # that were appended during the download.
                                trace.phase("checking appended data")
                                size = stream.stat().st_size or 0
                                trace.expected = max(0, size - max(0, offset))
                    else:
                        trace.phase("reading")
                        while True:
                            chunk = stream.read(65536)
                            if not chunk:
                                break
                            data.extend(chunk)
                            trace.advance(len(chunk))
                    trace.phase("closing")
                trace.phase("assembling")
                return bytes(data)
        return self._run(work)

    def mkdir(self, rel, exist_ok=True):
        full = self._full(rel)
        def work(sftp):
            def ensure(path):
                try:
                    attrs = sftp.stat(path)
                except FileNotFoundError:
                    parent = posixpath.dirname(path)
                    if parent != path:
                        ensure(parent)
                    try:
                        sftp.mkdir(path)
                    except OSError:
                        if not stat.S_ISDIR(sftp.stat(path).st_mode):
                            raise
                    return
                if not stat.S_ISDIR(attrs.st_mode):
                    raise NotADirectoryError(path)
                if path == full and not exist_ok:
                    raise FileExistsError(full)
            ensure(full)
        self._run(work)

    @staticmethod
    def _replace(sftp, source, target):
        try:
            sftp.posix_rename(source, target)
        except OSError as error:
            # OpenSSH supports atomic replacement. Other servers may only
            # implement standard SFTP rename (which requires a new target).
            if error.errno is not None or "unsupported" not in str(error).lower():
                raise
            sftp.rename(source, target)

    def write_bytes(self, rel, data):
        self.mkdir(posixpath.dirname(rel), exist_ok=True)
        target = self._full(rel)
        temporary = target + "." + uuid.uuid4().hex + ".tmp"
        def work(sftp):
            committed = False
            try:
                with sftp.open(temporary, "wb") as stream:
                    stream.write(data)
                self._replace(sftp, temporary, target)
                committed = True
            finally:
                if not committed:
                    try:
                        sftp.remove(temporary)
                    except OSError:
                        pass
        self._run(work)

    def delete(self, rel):
        try:
            self._run(lambda sftp: sftp.remove(self._full(rel)))
        except FileNotFoundError:
            pass

    def rename(self, src, dst):
        self.mkdir(posixpath.dirname(dst), exist_ok=True)
        self._run(lambda sftp: self._replace(sftp, self._full(src), self._full(dst)))

    def display_root(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"sftp://{quote(self.user, safe='')}@{host}:{self.port}{self.root}"

    def display_path(self, rel):
        return self.display_root().rstrip("/") + "/" + (rel or "").lstrip("/")

    def close(self):
        with self._lock:
            self._closed = True
            pairs, self._connections = self._connections, []
        for pair in pairs:
            try:
                self._close_pair(pair)
            except Exception:
                log.debug("SFTP connection cleanup failed", exc_info=True)
