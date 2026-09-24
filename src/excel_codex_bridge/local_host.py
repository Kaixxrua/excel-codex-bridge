"""Serve pictures from this computer, through a temporary Cloudflare link.

The Excel backend only takes pictures as https links that OpenAI can fetch.
Instead of uploading them anywhere, the bridge keeps them in memory and
serves them on a second loopback port, which ``cloudflared`` exposes as a
random ``https://<words>.trycloudflare.com`` address (a Cloudflare "quick
tunnel": no account, nothing to configure).

What stays private:

- pictures live only in this process's memory: nothing is written to disk,
  and everything is gone when the bridge stops;
- a picture is fetchable only for ``FETCH_WINDOW`` seconds after a request
  that contains it was sent upstream, by an unguessable name; the public side
  serves nothing else (no listing, no upload, no API);
- the tunnel is only opened the first time a picture is used.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("excel_codex_bridge.local_host")

FETCH_WINDOW = 300.0
MAX_TOTAL_BYTES = 256 * 1024 * 1024
IDLE_FORGET = 24 * 3600.0
TUNNEL_START_TIMEOUT = 45.0
# A new trycloudflare name can take a few seconds to resolve everywhere.
SETTLE_WINDOW = 60.0
SETTLE_DELAY = 5.0
_RESTART_DELAYS = (2.0, 5.0, 10.0, 30.0, 60.0)
_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_NAME = re.compile(r"^[A-Za-z0-9_-]{32}\.(png|jpg|gif|webp)$")
# Only raster formats; SVG could carry script.
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
_SERVE_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-robots-tag", b"noindex, nofollow"),
    (b"content-security-policy", b"default-src 'none'"),
    (b"referrer-policy", b"no-referrer"),
]


def sniff(data: bytes) -> str | None:
    """The file extension for supported image bytes, going by the magic number."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@dataclass
class _Picture:
    digest: bytes
    data: bytes
    media_type: str
    open_until: float
    last_used: float


class MemoryStore:
    """Pictures by unguessable name, each open for fetching only briefly after use."""

    def __init__(self, clock=time.monotonic, window: float = FETCH_WINDOW, max_bytes: int = MAX_TOTAL_BYTES):
        self._clock = clock
        self._window = window
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._pictures: OrderedDict[str, _Picture] = OrderedDict()
        self._names: dict[bytes, str] = {}  # content digest -> name
        self._total = 0

    def put(self, data: bytes) -> str | None:
        """Store (or find) a picture, open it for the next fetch window, return its name."""
        ext = sniff(data)
        if ext is None:
            return None
        digest = hashlib.sha256(data).digest()
        now = self._clock()
        with self._lock:
            name = self._names.get(digest)
            picture = self._pictures.get(name) if name else None
            if picture is None:
                name = f"{secrets.token_urlsafe(24)}.{ext}"
                picture = _Picture(digest, data, MEDIA_TYPES[ext], now, now)
                self._pictures[name] = picture
                self._names[digest] = name
                self._total += len(data)
            picture.open_until = now + self._window
            picture.last_used = now
            self._pictures.move_to_end(name)
            self._evict(now)
            return name

    def get(self, name: str) -> tuple[bytes, str] | None:
        if not _NAME.match(name):
            return None
        with self._lock:
            picture = self._pictures.get(name)
            if picture is None or self._clock() > picture.open_until:
                return None
            return picture.data, picture.media_type

    def _evict(self, now: float) -> None:
        for name in list(self._pictures):
            picture = self._pictures[name]
            if self._total <= self._max_bytes and now - picture.last_used < IDLE_FORGET:
                break
            del self._pictures[name]
            self._names.pop(picture.digest, None)
            self._total -= len(picture.data)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pictures)


def public_app(store: MemoryStore):
    """The only thing the tunnel reaches: GET/HEAD /i/<name> while that picture is open."""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        found = None
        if scope.get("method") in {"GET", "HEAD"} and path.startswith("/i/"):
            found = store.get(path[3:])
        if found is None:
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-length", b"0"), *_SERVE_HEADERS]})
            await send({"type": "http.response.body", "body": b""})
            return
        data, media_type = found
        log.info("a picture was fetched through the Cloudflare link")
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", media_type.encode()), (b"content-length", str(len(data)).encode()), *_SERVE_HEADERS,
        ]})
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else data})

    return app


class PublicServer:
    """``public_app`` on a free loopback port, in its own thread."""

    def __init__(self, store: MemoryStore) -> None:
        import uvicorn

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(
            public_app(store), host="127.0.0.1", port=self.port, log_level="warning", log_config=None,
            access_log=False, lifespan="off", timeout_graceful_shutdown=2,
        ))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        with contextlib.suppress(OSError):
            self._sock.close()


# ─── cloudflared ──────────────────────────────────────────────────────────────

CLOUDFLARED_ENV = "EXCEL_BRIDGE_CLOUDFLARED"


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def find_cloudflared(state_dir: Path | None = None) -> str | None:
    """cloudflared from $EXCEL_BRIDGE_CLOUDFLARED, next to the release build, the state dir, or PATH."""
    explicit = os.environ.get(CLOUDFLARED_ENV, "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / _exe("cloudflared"))
    if state_dir is not None:
        candidates.append(state_dir / "bin" / _exe("cloudflared"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("cloudflared")


def _kill_with_parent(process: subprocess.Popen) -> None:
    """On Windows, tie the child to a job object so it dies with this process, however that ends."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(field, ctypes.c_uint64) for field in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        global _JOB
        if _JOB is None:
            job = kernel32.CreateJobObjectW(None, None)
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                return
            _JOB = job  # kept open for the life of this process; closing it kills the children
        kernel32.AssignProcessToJobObject(_JOB, int(process._handle))  # type: ignore[attr-defined]
    except (OSError, AttributeError, ValueError) as exc:  # pragma: no cover - Windows only
        log.debug("could not tie cloudflared to this process: %s", exc)


_JOB = None
_live_tunnels: weakref.WeakSet[Tunnel] = weakref.WeakSet()


def stop_all() -> None:
    """Stop every tunnel now, for exits that skip the normal shutdown."""
    for tunnel in list(_live_tunnels):
        tunnel.stop(wait=False)


atexit.register(stop_all)


class Tunnel:
    """Keeps one cloudflared quick tunnel to ``origin_port`` running, restarting it if it drops."""

    def __init__(self, binary: str, origin_port: int, *, home: Path, delays=_RESTART_DELAYS) -> None:
        self._binary = binary
        self._origin_port = origin_port
        self._home = home
        self._delays = delays
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._url: str | None = None
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self.ready_since: float | None = None
        self.recent = deque(maxlen=12)  # last lines cloudflared printed, for error messages

    def start(self) -> None:
        with self._lock:
            if self._thread is None:
                _live_tunnels.add(self)
                self._thread = threading.Thread(target=self._supervise, name="cloudflared", daemon=True)
                self._thread.start()

    def url(self, timeout: float = TUNNEL_START_TIMEOUT) -> str | None:
        self.start()
        self._ready.wait(timeout)
        with self._lock:
            return self._url if self._ready.is_set() else None

    def problem(self) -> str:
        lines = [line for line in self.recent if " ERR " in line or "error" in line.lower()]
        return (lines or list(self.recent) or ["cloudflared printed nothing"])[-1][-300:]

    def stop(self, wait: bool = True) -> None:
        self._stopped.set()
        _live_tunnels.discard(self)
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5 if wait else 1)
            except subprocess.TimeoutExpired:
                process.kill()
                if wait:
                    process.wait()
        if wait and self._thread is not None:
            self._thread.join(timeout=5)

    def _command(self) -> list[str]:
        return [
            self._binary, "tunnel", "--no-autoupdate",
            # TCP; the QUIC default is often throttled on home networks.
            "--protocol", "http2",
            "--url", f"http://127.0.0.1:{self._origin_port}",
        ]

    def _env(self) -> dict[str, str]:
        # A user's own cloudflared config would turn the quick tunnel off; keep it out.
        self._home.mkdir(parents=True, exist_ok=True)
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("TUNNEL_")}
        env.update(HOME=str(self._home), USERPROFILE=str(self._home))
        return env

    def _supervise(self) -> None:
        attempt = 0
        while not self._stopped.is_set():
            started = time.monotonic()
            self._run_once()
            if self._stopped.is_set():
                break
            if time.monotonic() - started > 300:
                attempt = 0
            delay = self._delays[min(attempt, len(self._delays) - 1)]
            attempt += 1
            log.warning("the Cloudflare link for pictures dropped (%s); retrying in %.0fs", self.problem(), delay)
            self._stopped.wait(delay)

    def _run_once(self) -> None:
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                self._command(), env=self._env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kwargs,
            )
        except OSError as exc:
            self.recent.append(f"could not start cloudflared: {exc}")
            return
        _kill_with_parent(process)
        with self._lock:
            self._process = process
        if self._stopped.is_set():
            process.terminate()
        candidate = None
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    self.recent.append(line)
                match = _TUNNEL_URL.search(line)
                if match and match.group(0) != "https://api.trycloudflare.com":
                    candidate = match.group(0)
                if candidate and "Registered tunnel connection" in line and not self._ready.is_set():
                    with self._lock:
                        self._url = candidate
                        self.ready_since = time.monotonic()
                    self._ready.set()
                    log.info("pictures are reachable through %s while the bridge runs", candidate)
        finally:
            self._ready.clear()
            with self._lock:
                self._url = None
                self.ready_since = None
            process.wait()


class LocalPictures:
    """The memory store, its loopback server and the tunnel, started on first use."""

    def __init__(self, binary: str, home: Path, *, store: MemoryStore | None = None, tunnel_factory=None) -> None:
        self.store = store or MemoryStore()
        self._binary = binary
        self._home = home
        self._tunnel_factory = tunnel_factory or Tunnel
        self._lock = threading.Lock()
        self._server: PublicServer | None = None
        self.tunnel: Tunnel | None = None

    def _ensure_started(self) -> Tunnel:
        with self._lock:
            if self.tunnel is None:
                self._server = PublicServer(self.store)
                self._server.start()
                self.tunnel = self._tunnel_factory(self._binary, self._server.port, home=self._home)
                self.tunnel.start()
            return self.tunnel

    def link(self, data: bytes, timeout: float = TUNNEL_START_TIMEOUT) -> str:
        """The public URL for ``data``, opened for the next fetch window.  Blocks while the tunnel starts."""
        name = self.store.put(data)
        if name is None:
            raise ValueError("only PNG, JPEG, GIF and WebP pictures can be passed on")
        tunnel = self._ensure_started()
        base = tunnel.url(timeout)
        if base is None:
            raise ConnectionError(f"the Cloudflare link did not come up ({tunnel.problem()})")
        return f"{base}/i/{name}"

    def settling(self) -> float:
        """How long to wait before trying a refused request again, if the link is brand new."""
        tunnel = self.tunnel
        since = tunnel.ready_since if tunnel is not None else None
        if since is not None and time.monotonic() - since < SETTLE_WINDOW:
            return SETTLE_DELAY
        return 0.0

    def stop(self) -> None:
        with self._lock:
            tunnel, server = self.tunnel, self._server
            self.tunnel = self._server = None
        if tunnel is not None:
            tunnel.stop()
        if server is not None:
            server.stop()
