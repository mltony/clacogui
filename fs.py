"""Filesystem backends for clacogui.

Two transports are supported:

* :class:`LocalBackend` -- any local path, including drives mapped to an
  SMB share (e.g. ``X:\\.claude`` on Windows or ``/home/me/.claude`` on
  Unix).
* :class:`FtpBackend`   -- a plain FTP URL such as
  ``ftp://ftpuser:<pw>@host:2121/home/me/.claude``.

The FTP path is the recommended workaround for aggressive Windows SMB
client caching: ``ftplib`` issues a fresh transfer for every read, and
the small pyftpdlib server we target uses MLSD for directory listings so
metadata comes back fresh on every poll.

Backend objects expose forward-slash, root-relative paths to the rest of
the program.  Callers never see absolute Windows paths or ``ftp://`` URLs
except through :meth:`FsBackend.display_path`.
"""

from __future__ import annotations

import ftplib
import logging
import os
import posixpath
import threading
from calendar import timegm
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class FsEntry:
    """One entry in a directory listing."""

    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


class FsBackend:
    """Abstract filesystem backend.

    All ``rel`` arguments are forward-slash paths relative to the backend's
    root.  An empty string or ``"/"`` refers to the root itself.
    """

    def list_dir(self, rel: str) -> list[FsEntry]:
        raise NotImplementedError

    def is_dir(self, rel: str) -> bool:
        raise NotImplementedError

    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        raise NotImplementedError

    def read_text_head(self, rel: str, max_bytes: int = 4096, encoding: str = "utf-8") -> str:
        """Read at most ``max_bytes`` from the start of ``rel``."""
        return self.read_text(rel, encoding)[:max_bytes]

    def read_text_from(self, rel: str, offset: int, encoding: str = "utf-8") -> str:
        """Read from byte ``offset`` to end of file."""
        text = self.read_text(rel, encoding)
        # Default: read all then slice (subclasses override for efficiency).
        return text[offset:] if offset > 0 else text

    def stat(self, rel: str) -> tuple[float, int]:
        """Return ``(mtime, size)`` or raise :class:`OSError`."""
        raise NotImplementedError

    def write_bytes(self, rel: str, data: bytes) -> None:
        """Atomically (best-effort) write ``data`` to ``rel``."""
        raise NotImplementedError

    def write_text(self, rel: str, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(rel, text.encode(encoding))

    def delete(self, rel: str) -> None:
        """Delete a regular file. Missing file is treated as success."""
        raise NotImplementedError

    def mkdir(self, rel: str, exist_ok: bool = True) -> None:
        """Create a directory.  Parents are created as needed."""
        raise NotImplementedError

    def rename(self, src: str, dst: str) -> None:
        """Rename ``src`` -> ``dst`` (both root-relative).

        The default implementation is a non-atomic copy + delete,
        which is fine for the small JSON envelopes the launcher
        protocol uses (< 1 KB).  Backends that have a real atomic
        rename (``os.replace``, FTP ``RNFR``/``RNTO``) override it.
        """
        data = self.read_text(src).encode("utf-8")
        self.write_bytes(dst, data)
        self.delete(src)

    def exists(self, rel: str) -> bool:
        try:
            self.stat(rel)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def display_root(self) -> str:
        """Human-readable form of the backend's root."""
        raise NotImplementedError

    def display_path(self, rel: str) -> str:
        """Human-readable form of ``rel`` (e.g. for log messages)."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any underlying resources (FTP control connection, ...)."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_backend(spec: str) -> FsBackend:
    """Build a backend from a spec string.

    ``ftp://...`` (or ``ftps://...``) URLs become :class:`FtpBackend`;
    anything else is treated as a local path.
    """
    spec = (spec or "").strip()
    lower = spec.lower()
    if lower.startswith("ftp://") or lower.startswith("ftps://"):
        return FtpBackend.from_url(spec)
    if not spec:
        raise ValueError("Empty backend spec")
    return LocalBackend(spec)


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------


class LocalBackend(FsBackend):
    """Plain filesystem access; works for SMB-mapped drives too."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root.rstrip("/\\"))

    def _full(self, rel: str) -> str:
        rel = (rel or "").lstrip("/\\")
        if not rel:
            return self.root
        # Forward-slash inputs are accepted on Windows too, but converting
        # to native separators keeps tracebacks readable.
        return os.path.join(self.root, rel.replace("/", os.sep))

    def list_dir(self, rel: str) -> list[FsEntry]:
        full = self._full(rel)
        out: list[FsEntry] = []
        for name in os.listdir(full):
            sub = os.path.join(full, name)
            try:
                st = os.stat(sub)
            except OSError:
                log.debug("Skipping unreadable %s", sub, exc_info=True)
                continue
            out.append(
                FsEntry(
                    name=name,
                    is_dir=os.path.isdir(sub),
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                )
            )
        return out

    def is_dir(self, rel: str) -> bool:
        return os.path.isdir(self._full(rel))

    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        with open(self._full(rel), "r", encoding=encoding, errors="replace") as f:
            return f.read()

    def read_text_head(self, rel: str, max_bytes: int = 4096, encoding: str = "utf-8") -> str:
        with open(self._full(rel), "rb") as f:
            raw = f.read(max_bytes)
        return raw.decode(encoding, errors="replace")

    def read_text_from(self, rel: str, offset: int, encoding: str = "utf-8") -> str:
        with open(self._full(rel), "rb") as f:
            if offset > 0:
                f.seek(offset)
            raw = f.read()
        return raw.decode(encoding, errors="replace")

    def stat(self, rel: str) -> tuple[float, int]:
        st = os.stat(self._full(rel))
        return (float(st.st_mtime), int(st.st_size))

    def write_bytes(self, rel: str, data: bytes) -> None:
        full = self._full(rel)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        # Best-effort atomic write: tmp file in same dir, then rename.
        tmp = full + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)

    def delete(self, rel: str) -> None:
        try:
            os.remove(self._full(rel))
        except FileNotFoundError:
            pass

    def mkdir(self, rel: str, exist_ok: bool = True) -> None:
        os.makedirs(self._full(rel), exist_ok=exist_ok)

    def rename(self, src: str, dst: str) -> None:
        s = self._full(src)
        d = self._full(dst)
        os.makedirs(os.path.dirname(d) or self.root, exist_ok=True)
        os.replace(s, d)

    def display_root(self) -> str:
        return self.root

    def display_path(self, rel: str) -> str:
        return self._full(rel)


# ---------------------------------------------------------------------------
# FtpBackend
# ---------------------------------------------------------------------------


_FTP_TIMEOUT_SEC = 30


class FtpBackend(FsBackend):
    """Plain FTP backend (RFC 959 + MLSD for directory listings).

    Per-thread connection pool: each calling thread gets its own
    long-lived FTP control connection, lazily opened on first use
    and stored in ``self._tls.ftp``.  This is required for
    correctness, not just throughput: clacogui has more than one IO
    worker thread (a read queue and a write queue, see
    ``gui.submit_io`` / ``gui.submit_io_write``) and FTP's control
    channel is strictly request-response, so two threads sharing
    one connection inevitably corrupt each other's responses.
    Symptoms of the old single-connection setup were
    ``error_reply('350 Ready for destination name')`` (one thread's
    ``RNTO voidcmd`` reading another thread's stale ``RNFR``
    response) and 0-byte ``STOR`` deliveries (the ``PASV`` data
    port allocation getting stolen by a concurrent ``mlsd`` from
    the other thread).  Giving each thread its own control channel
    sidesteps the entire class of races without sacrificing
    parallelism: the read-queue thread can be in the middle of a
    slow ``mlsd`` while the write-queue thread does a ``STOR`` for
    a queued send -- they're literally talking to different sockets
    on the server.

    On any transport error we close the calling thread's connection
    and the next call from that same thread reconnects.  Other
    threads' connections are unaffected.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        root: str,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        # Always store root as a posix-style absolute path.
        self.root = "/" + (root or "/").strip("/")
        if self.root == "/":
            self.root = ""
        # Per-thread connection pool.  ``self._tls.ftp`` is created
        # lazily on first use from each thread (see ``_conn``).
        self._tls = threading.local()
        # Bookkeeping for ``close()`` -- we need to be able to tear
        # down connections that belong to *other* threads (e.g. on
        # backend swap) since each thread can only see its own TLS.
        # The lock only protects this list; it does NOT serialise
        # any FTP command, so reads and writes still run in
        # parallel on their respective control channels.
        self._all_conns: list[ftplib.FTP] = []
        self._all_conns_lock = threading.Lock()

    # ---- construction -----------------------------------------------------

    @classmethod
    def from_url(cls, url: str) -> "FtpBackend":
        u = urlparse(url)
        scheme = u.scheme.lower()
        if scheme not in ("ftp", "ftps"):
            raise ValueError(f"Not an FTP URL: {url!r}")
        if scheme == "ftps":
            log.warning(
                "FTPS not implemented; falling back to plain FTP for %s",
                url,
            )
        host = u.hostname or ""
        if not host:
            raise ValueError(f"FTP URL has no host: {url!r}")
        port = u.port or 21  # pyftpdlib's default control port is 2121, give it explicitly in the URL
        user = unquote(u.username) if u.username else "anonymous"
        password = unquote(u.password) if u.password else ""
        path = unquote(u.path or "/")
        return cls(host, port, user, password, path)

    # ---- connection management -------------------------------------------

    def _connect(self) -> ftplib.FTP:
        ftp = ftplib.FTP(timeout=_FTP_TIMEOUT_SEC)
        ftp.connect(self.host, self.port)
        ftp.login(self.user, self.password)
        # Ask the server to interpret filenames as UTF-8 if it supports it.
        try:
            ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.all_errors:
            pass
        ftp.set_pasv(True)
        # Force binary mode for the lifetime of this connection.  pyftpdlib
        # (and most servers) deliberately reject SIZE in ASCII mode -- the
        # default -- so polling's stat() would otherwise fail with 550 every
        # tick.  TYPE I is also fine for retrbinary and our delete/mkdir.
        # See ``_force_type_i`` for the full story (ftplib.retrlines flips
        # back to TYPE A on every listing, so we re-assert in several spots).
        self._force_type_i(ftp)
        log.info(
            "FTP connected: %s:%d as %s, root=%s, type=I",
            self.host, self.port, self.user, self.root or "/",
        )
        return ftp

    def _conn(self) -> ftplib.FTP:
        """Return *this thread's* FTP control connection, opening it lazily.

        Each calling thread gets its own connection (see class
        docstring for why FTP forces this).  We also remember every
        connection ever opened in ``self._all_conns`` so ``close()``
        can tear them all down at backend swap / app exit, even
        connections that belong to threads other than the caller.
        """
        ftp = getattr(self._tls, "ftp", None)
        if ftp is None:
            ftp = self._connect()
            self._tls.ftp = ftp
            with self._all_conns_lock:
                self._all_conns.append(ftp)
        return ftp

    @staticmethod
    def _force_type_i(ftp: ftplib.FTP) -> None:
        """Re-assert TYPE I on the control connection.

        ``ftplib.retrlines`` -- and therefore ``mlsd``/``nlst``/``LIST`` --
        unconditionally sends ``TYPE A`` before its transfer and never
        restores the previous mode.  pyftpdlib correctly rejects ``SIZE``
        in ASCII mode (RFC 3659), so any SIZE issued after a directory
        listing fails with 550.  We compensate by flipping back to TYPE I
        after every listing and (defensively) before every stat.
        """
        try:
            ftp.voidcmd("TYPE I")
        except ftplib.all_errors as e:
            log.debug("voidcmd('TYPE I') failed: %r", e)

    def _reset(self) -> None:
        """Drop *this thread's* connection (e.g. after a transport error).

        Other threads keep their own connections -- a transient
        error on one channel doesn't kick everyone else off.
        """
        ftp = getattr(self._tls, "ftp", None)
        if ftp is None:
            return
        try:
            ftp.close()
        except Exception:
            pass
        self._tls.ftp = None
        with self._all_conns_lock:
            try:
                self._all_conns.remove(ftp)
            except ValueError:
                pass

    def close(self) -> None:
        """Tear down every connection across every thread.

        Called on backend swap and at app shutdown.  We close
        connections that belong to other threads too -- they're
        idle by definition (their owning thread isn't currently
        running an IO job, since we hold their reference here),
        and leaking them on backend swap would otherwise stack up
        for the lifetime of the process.
        """
        with self._all_conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for ftp in conns:
            try:
                ftp.close()
            except Exception:
                pass
        # Also clear the calling thread's TLS slot so a subsequent
        # ``_conn()`` from this thread reconnects fresh.  Other
        # threads' TLS slots still point at now-closed sockets;
        # their next operation will hit a transport error, fall
        # into ``_retry``'s reconnect path, and end up at
        # ``_reset`` which will quietly drop the dead reference.
        try:
            self._tls.ftp = None
        except AttributeError:
            pass

    def _retry(self, fn):
        """Run ``fn()``; on a transport error, reconnect once and retry.

        ``error_perm`` (5xx) is treated as an application-level failure --
        ``550 No such file`` becomes :class:`FileNotFoundError`, anything
        else becomes :class:`OSError`.  The caller's normal error handling
        applies (Retry/Quit dialog for user-initiated reads, silent log
        for the polling thread).

        No locking here: each thread has its own FTP control
        connection (see class docstring + ``_conn``), so concurrent
        ``_retry`` calls from the read and write queues use
        independent sockets and can't interleave.
        """
        try:
            return fn()
        except ftplib.error_perm as e:
            msg = str(e)
            if msg.startswith("550"):
                raise FileNotFoundError(msg)
            raise OSError(msg) from e
        # ``ftplib.all_errors`` is already a tuple ``(Error,
        # OSError, EOFError)`` -- it must NOT be nested inside
        # another tuple, or ``except`` raises ``TypeError:
        # catching classes that do not inherit from
        # BaseException``.
        except ftplib.all_errors as e:
            log.warning("FTP error %r, reconnecting", e)
            self._reset()
            try:
                return fn()
            except ftplib.error_perm as e2:
                raise OSError(str(e2)) from e2
            except ftplib.all_errors as e2:
                self._reset()
                raise OSError(f"FTP failure: {e2}") from e2

    # ---- path helpers -----------------------------------------------------

    def _full(self, rel: str) -> str:
        rel = (rel or "").lstrip("/")
        if not rel:
            return self.root or "/"
        return posixpath.join(self.root or "/", rel)

    # ---- FsBackend implementation -----------------------------------------

    def list_dir(self, rel: str) -> list[FsEntry]:
        full = self._full(rel)

        def work() -> list[FsEntry]:
            ftp = self._conn()
            try:
                entries = list(ftp.mlsd(full))
            finally:
                # mlsd -> retrlines unconditionally sends "TYPE A".  Restore
                # binary mode immediately so a subsequent stat() doesn't get
                # a 550 back from pyftpdlib.
                self._force_type_i(ftp)
            out: list[FsEntry] = []
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                etype = (facts.get("type") or "file").lower()
                if etype in ("cdir", "pdir"):
                    continue
                is_dir = etype == "dir"
                size = 0 if is_dir else int(facts.get("size", "0") or 0)
                mtime = _parse_mdtm(facts.get("modify", ""))
                out.append(FsEntry(name=name, is_dir=is_dir, size=size, mtime=mtime))
            return out

        return self._retry(work)

    def is_dir(self, rel: str) -> bool:
        full = self._full(rel)

        def work() -> bool:
            ftp = self._conn()
            try:
                cur = ftp.pwd()
            except ftplib.all_errors:
                cur = ""
            try:
                ftp.cwd(full)
                return True
            except ftplib.error_perm:
                return False
            finally:
                if cur:
                    try:
                        ftp.cwd(cur)
                    except ftplib.all_errors:
                        pass

        try:
            return self._retry(work)
        except (FileNotFoundError, OSError):
            return False

    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        full = self._full(rel)

        def work() -> str:
            ftp = self._conn()
            buf = bytearray()
            ftp.retrbinary("RETR " + full, buf.extend)
            return buf.decode(encoding, errors="replace")

        return self._retry(work)

    def read_text_head(self, rel: str, max_bytes: int = 4096, encoding: str = "utf-8") -> str:
        full = self._full(rel)

        def work() -> str:
            ftp = self._conn()
            self._force_type_i(ftp)
            # Use REST (restart) to request only the first max_bytes.
            # Open a data connection manually so we can close it cleanly
            # after reading enough, without corrupting the control channel.
            with ftp.transfercmd("RETR " + full) as conn:
                buf = bytearray()
                while len(buf) < max_bytes:
                    chunk = conn.recv(min(4096, max_bytes - len(buf)))
                    if not chunk:
                        break
                    buf.extend(chunk)
            # After closing the data socket, consume the server's
            # transfer-complete response so the control channel is clean.
            try:
                ftp.voidresp()
            except ftplib.all_errors:
                pass
            return bytes(buf[:max_bytes]).decode(encoding, errors="replace")

        return self._retry(work)

    def read_text_from(self, rel: str, offset: int, encoding: str = "utf-8") -> str:
        full = self._full(rel)

        def work() -> str:
            ftp = self._conn()
            self._force_type_i(ftp)
            # Use REST to resume from offset, avoiding full file download.
            with ftp.transfercmd("RETR " + full, rest=offset if offset > 0 else None) as conn:
                buf = bytearray()
                while True:
                    chunk = conn.recv(8192)
                    if not chunk:
                        break
                    buf.extend(chunk)
            try:
                ftp.voidresp()
            except ftplib.all_errors:
                pass
            return bytes(buf).decode(encoding, errors="replace")

        return self._retry(work)

    def stat(self, rel: str) -> tuple[float, int]:
        full = self._full(rel)

        def work() -> tuple[float, int]:
            ftp = self._conn()
            # Belt-and-suspenders: pyftpdlib refuses SIZE in ASCII mode, and
            # we can't be sure some other ftplib call hasn't switched the
            # type since we last looked.  An extra round-trip on the polling
            # hot path is cheap; a 550 here would silently break polling.
            self._force_type_i(ftp)
            size = ftp.size(full)
            mdtm = ftp.voidcmd("MDTM " + full)
            # Response is e.g. "213 20231201123456" -- voidcmd has already
            # checked the 213 and returned the whole line.
            ts = mdtm.split(maxsplit=1)[-1] if " " in mdtm else mdtm
            mtime = _parse_mdtm(ts)
            return (mtime, int(size or 0))

        return self._retry(work)

    def write_bytes(self, rel: str, data: bytes) -> None:
        import io as _io

        full = self._full(rel)

        def work() -> None:
            ftp = self._conn()
            ftp.storbinary("STOR " + full, _io.BytesIO(data))

        self._retry(work)

    def delete(self, rel: str) -> None:
        full = self._full(rel)

        def work() -> None:
            ftp = self._conn()
            try:
                ftp.delete(full)
            except ftplib.error_perm as e:
                if str(e).startswith("550"):
                    return  # missing -> idempotent
                raise

        self._retry(work)

    def mkdir(self, rel: str, exist_ok: bool = True) -> None:
        full = self._full(rel)

        def work() -> None:
            ftp = self._conn()
            # Walk parent dirs, MKD-ing each in turn.  Cheap because we
            # don't care about errors on existing dirs.
            parts = full.strip("/").split("/")
            cur = ""
            for p in parts:
                cur = (cur + "/" + p) if cur else "/" + p
                try:
                    ftp.mkd(cur)
                except ftplib.error_perm as e:
                    msg = str(e)
                    if msg.startswith("550") and exist_ok:
                        continue
                    raise

        self._retry(work)

    def rename(self, src: str, dst: str) -> None:
        src_full = self._full(src)
        dst_full = self._full(dst)

        def work() -> None:
            ftp = self._conn()
            # ``rename`` issues RNFR + RNTO under the hood.  pyftpdlib
            # supports both; we still fall back to read+write+delete in
            # the except branch below for servers that refuse it.
            ftp.rename(src_full, dst_full)

        try:
            self._retry(work)
        except OSError:
            log.warning(
                "FTP rename %s -> %s failed; falling back to copy+delete",
                src_full, dst_full,
            )
            # Each helper here uses *this thread's* connection
            # (see ``_conn``), so the fallback sequence runs on a
            # single channel and is wire-atomic against any other
            # thread's traffic.  We do *not* try to make the
            # fallback observably atomic across threads: at most
            # the launcher could see ``src`` then ``dst`` then
            # ``src`` gone, which it already handles via
            # ``_file_is_stable`` and the ``.part`` extension
            # filter.
            data = self.read_text(src).encode("utf-8")
            self.write_bytes(dst, data)
            self.delete(src)

    def display_root(self) -> str:
        return f"ftp://{self.user}@{self.host}:{self.port}{self.root or '/'}"

    def display_path(self, rel: str) -> str:
        rel = (rel or "").lstrip("/")
        if not rel:
            return self.display_root()
        return self.display_root().rstrip("/") + "/" + rel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_mdtm(s: str) -> float:
    """Parse a ``YYYYMMDDHHMMSS[.fff]`` UTC timestamp into a Unix epoch.

    Returns ``0.0`` on any parse error -- callers treat that as "unknown
    timestamp" and the file is still readable.
    """
    s = (s or "").strip()
    if not s:
        return 0.0
    if "." in s:
        s, frac = s.split(".", 1)
    else:
        frac = "0"
    if len(s) < 14:
        return 0.0
    try:
        y = int(s[0:4]); mo = int(s[4:6]); d = int(s[6:8])
        h = int(s[8:10]); mi = int(s[10:12]); se = int(s[12:14])
    except ValueError:
        return 0.0
    try:
        base = timegm((y, mo, d, h, mi, se, 0, 0, 0))
    except (OverflowError, ValueError):
        return 0.0
    return float(base) + float("0." + (frac or "0"))
