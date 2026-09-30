"""HTTP/2 PINGs on the connections to the Excel backend while answers are coming.

The model can think for minutes before it sends a byte of its answer.  Proxy
nodes and NAT boxes on the way close a connection that stays silent that long,
some after a minute, some after two, and Codex then shows "The Excel backend
closed the connection N minutes into the answer".  The backend cannot pick an
answer up again, so the bridge keeps the connection from going silent instead:
every ``EXCEL_BRIDGE_UPSTREAM_PING`` seconds each HTTP/2 connection with a
request open sends a PING, which the backend answers and every hop on the way
sees as traffic.

httpx has no API for this.  The connection pool's own objects are reached
through private attributes; anything unexpected there just means no PINGs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import types
from typing import Callable, Iterator

import httpx

log = logging.getLogger("excel_codex_bridge")

PING_EVERY = 10.0
MIN_PING_EVERY = 1.0
MAX_PING_EVERY = 60.0
# How many wrappers (proxy tunnel, SOCKS, plain connection) can sit around the HTTP/2 one.
_MAX_WRAPPERS = 4


def ping_every() -> float:
    """Seconds between PINGs (``EXCEL_BRIDGE_UPSTREAM_PING``); 0 means none, and HTTP/1.1 as before."""
    value = os.environ.get("EXCEL_BRIDGE_UPSTREAM_PING", "").strip()
    try:
        seconds = float(value) if value else PING_EVERY
    except ValueError:
        return PING_EVERY
    if math.isnan(seconds):
        return PING_EVERY
    if seconds <= 0:
        return 0.0
    return min(max(seconds, MIN_PING_EVERY), MAX_PING_EVERY)


def http2_available() -> bool:
    try:
        import h2  # noqa: F401
    except ImportError:
        return False
    return True


def _h2_connection(entry):
    """The HTTP/2 connection inside a pool entry (direct, through a proxy tunnel or SOCKS), else None."""
    for _ in range(_MAX_WRAPPERS):
        if entry is None or hasattr(entry, "_h2_state"):
            return entry
        entry = getattr(entry, "_connection", None)
    return None


def _busy(connection) -> bool:
    """A request is open on it, and it is still usable."""
    return (
        getattr(getattr(connection, "_state", None), "name", None) == "ACTIVE"
        and getattr(connection, "_sent_connection_init", False)
        and getattr(connection, "_write_exception", None) is None
        and getattr(connection, "_connection_terminated", None) is None
    )


def busy_http2_connections(client: httpx.AsyncClient) -> Iterator[object]:
    """Each HTTP/2 connection of ``client``'s pools that has a request open, once."""
    transports = [getattr(client, "_transport", None), *(getattr(client, "_mounts", None) or {}).values()]
    seen: set[int] = set()
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        try:
            entries = list(getattr(pool, "connections", None) or ())
        except TypeError:
            continue
        for entry in entries:
            connection = _h2_connection(entry)
            if connection is None or id(connection) in seen or not _busy(connection):
                continue
            seen.add(id(connection))
            yield connection


async def ping(connection, write_timeout: float | None) -> bool:
    """Send one PING on ``connection``; False when it could not be sent."""
    try:
        connection._h2_state.ping(os.urandom(8))
        # Only the write timeout is read from the request.
        request = types.SimpleNamespace(extensions={"timeout": {"write": write_timeout}})
        await connection._write_outgoing_data(request)
    except Exception as exc:  # the connection is failing; its own request finds out
        log.debug("upstream PING not sent: %s", type(exc).__name__)
        return False
    return True


class UpstreamPings:
    """Every ``every`` seconds, a PING on each busy HTTP/2 connection of ``client()``."""

    def __init__(self, client: Callable[[], httpx.AsyncClient | None], every: float | None = None) -> None:
        self._client = client
        self.every = ping_every() if every is None else every
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start pinging, if on and not yet started; needs a running event loop."""
        if self.every <= 0 or self._task is not None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            try:
                await self.ping_busy()
            except Exception as exc:  # the pool looked different from what this expects
                log.debug("upstream PINGs skipped: %s", type(exc).__name__)

    async def ping_busy(self) -> int:
        """One round of PINGs; how many were sent."""
        client = self._client()
        if client is None:
            return 0
        write_timeout = client.timeout.write
        sent = 0
        for connection in list(busy_http2_connections(client)):
            sent += await ping(connection, write_timeout)
        return sent

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
