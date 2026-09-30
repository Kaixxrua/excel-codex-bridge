"""HTTP/2 PINGs keep a connection to the backend from going silent while the model thinks.

Proxy nodes close a connection that carries nothing for a minute or two, and
the backend cannot pick an answer up again.
"""

from __future__ import annotations

import asyncio
import enum
import os
import unittest
from unittest import mock

import h2.config
import h2.connection
import h2.events
import httpx

from excel_codex_bridge import server, upstream_ping
from excel_codex_bridge.upstream_ping import UpstreamPings, busy_http2_connections

from test_server import StaticReader


class H2Server:
    """HTTP/2 without TLS (prior knowledge): answers each request after ``hold`` seconds, counting PINGs."""

    def __init__(self, hold: float) -> None:
        self.hold = hold
        self.pings = 0

    async def __aenter__(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/"

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        connection.initiate_connection()
        writer.write(connection.data_to_send())
        answers = set()
        try:
            while data := await reader.read(65536):
                for event in connection.receive_data(data):
                    if isinstance(event, h2.events.PingReceived):
                        self.pings += 1
                    elif isinstance(event, h2.events.RequestReceived):
                        answers.add(asyncio.create_task(self._answer(connection, writer, event.stream_id)))
                writer.write(connection.data_to_send())
                await writer.drain()
        finally:
            for task in answers:
                task.cancel()
            writer.close()

    async def _answer(self, connection, writer, stream_id: int) -> None:
        await asyncio.sleep(self.hold)
        connection.send_headers(stream_id, [(":status", "200"), ("content-type", "text/plain")])
        connection.send_data(stream_id, b"done", end_stream=True)
        writer.write(connection.data_to_send())
        await writer.drain()


def h2_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(http1=False, http2=True, timeout=10.0, trust_env=False)


class RealConnectionTest(unittest.IsolatedAsyncioTestCase):
    """Against httpx's own pool, so a change in its internals shows here."""

    async def test_pings_while_an_answer_is_awaited_and_not_after(self):
        backend = H2Server(hold=1.6)
        async with backend as url, h2_client() as client:
            pings = UpstreamPings(lambda: client, every=0.3)
            pings.start()
            try:
                response = await client.get(url)
                self.assertEqual((response.http_version, response.text), ("HTTP/2", "done"))
                during = backend.pings
                self.assertGreaterEqual(during, 3)
                # The connection stays in the pool, idle: no more PINGs.
                await asyncio.sleep(1.0)
                self.assertEqual(backend.pings, during)
            finally:
                await pings.aclose()

    async def test_finds_the_open_connection_once_for_concurrent_requests(self):
        async with H2Server(hold=0.5) as url, h2_client() as client:
            requests = [asyncio.create_task(client.get(url)) for _ in range(3)]
            await asyncio.sleep(0.2)
            self.assertEqual(len(list(busy_http2_connections(client))), 1)
            self.assertEqual(await UpstreamPings(lambda: client, every=1.0).ping_busy(), 1)
            for response in await asyncio.gather(*requests):
                self.assertEqual(response.text, "done")
            self.assertEqual(list(busy_http2_connections(client)), [])

    async def test_http1_connections_are_left_alone(self):
        async def answer(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(0.4)
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\nconnection: close\r\n\r\ndone")
            await writer.drain()
            writer.close()

        http1 = await asyncio.start_server(answer, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{http1.sockets[0].getsockname()[1]}/"
        try:
            async with httpx.AsyncClient(http2=True, timeout=10.0, trust_env=False) as client:
                request = asyncio.create_task(client.get(url))
                await asyncio.sleep(0.2)
                self.assertEqual(await UpstreamPings(lambda: client, every=1.0).ping_busy(), 0)
                self.assertEqual((await request).http_version, "HTTP/1.1")
        finally:
            http1.close()
            await http1.wait_closed()


class State(enum.Enum):
    ACTIVE = 1
    IDLE = 2


class FakeH2:
    def __init__(self, state=State.ACTIVE, *, fail: bool = False, **attrs) -> None:
        self._h2_state = mock.Mock()
        self._state = state
        self._sent_connection_init = True
        self._write_exception = None
        self._connection_terminated = None
        self.writes = []
        self.fail = fail
        self.__dict__.update(attrs)

    async def _write_outgoing_data(self, request) -> None:
        if self.fail:
            raise OSError("broken pipe")
        self.writes.append(request.extensions["timeout"]["write"])


class Wrapper:
    def __init__(self, inner) -> None:
        self._connection = inner


def fake_client(*pools, write: float = 60.0):
    client = mock.Mock(spec=["_transport", "_mounts", "timeout"])
    client.timeout = httpx.Timeout(30.0, write=write)
    transports = [mock.Mock(_pool=mock.Mock(connections=list(entries))) for entries in pools]
    client._transport = transports[0] if transports else None
    client._mounts = {f"pattern{i}": t for i, t in enumerate(transports[1:])} | {"no-proxy": None}
    return client


class PoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_pings_busy_connections_through_proxy_wrappers(self):
        direct, tunnelled, socks = FakeH2(), FakeH2(), FakeH2()
        client = fake_client([direct], [Wrapper(tunnelled), Wrapper(Wrapper(socks))], write=12.0)
        self.assertEqual(await UpstreamPings(lambda: client, every=1.0).ping_busy(), 3)
        for connection in (direct, tunnelled, socks):
            self.assertEqual(connection.writes, [12.0])
            connection._h2_state.ping.assert_called_once()
            self.assertEqual(len(connection._h2_state.ping.call_args.args[0]), 8)

    async def test_each_connection_once(self):
        shared = FakeH2()
        client = fake_client([shared, Wrapper(shared)], [shared])
        self.assertEqual(await UpstreamPings(lambda: client, every=1.0).ping_busy(), 1)
        self.assertEqual(len(shared.writes), 1)

    async def test_skips_idle_unready_and_failed_connections(self):
        skipped = [
            FakeH2(State.IDLE),
            FakeH2(_sent_connection_init=False),
            FakeH2(_write_exception=OSError()),
            FakeH2(_connection_terminated=object()),
            Wrapper(None),
            Wrapper(Wrapper(Wrapper(Wrapper(FakeH2())))),  # deeper than any pool nests it
            object(),
        ]
        client = fake_client(skipped)
        self.assertEqual(await UpstreamPings(lambda: client, every=1.0).ping_busy(), 0)

    async def test_a_failing_connection_does_not_stop_the_others(self):
        broken, fine = FakeH2(fail=True), FakeH2()
        client = fake_client([broken, fine])
        self.assertEqual(await UpstreamPings(lambda: client, every=1.0).ping_busy(), 1)
        self.assertEqual(len(fine.writes), 1)

    async def test_nothing_before_the_client_exists(self):
        self.assertEqual(await UpstreamPings(lambda: None, every=1.0).ping_busy(), 0)

    async def test_a_client_without_pools(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        self.assertEqual(await UpstreamPings(lambda: client, every=1.0).ping_busy(), 0)
        await client.aclose()

    async def test_runs_every_interval_until_closed(self):
        connection = FakeH2()
        client = fake_client([connection])
        pings = UpstreamPings(lambda: client, every=0.05)
        pings.start()
        pings.start()  # once
        await asyncio.sleep(0.28)
        await pings.aclose()
        sent = len(connection.writes)
        self.assertGreaterEqual(sent, 3)
        self.assertLessEqual(sent, 6)
        await asyncio.sleep(0.15)
        self.assertEqual(len(connection.writes), sent)

    async def test_goes_on_after_an_unexpected_pool(self):
        connection = FakeH2()
        clients = iter([object(), fake_client([connection])])
        pings = UpstreamPings(lambda: next(clients, None), every=0.05)
        pings.start()
        await asyncio.sleep(0.18)
        await pings.aclose()
        self.assertEqual(len(connection.writes), 1)

    async def test_off(self):
        pings = UpstreamPings(lambda: fake_client([FakeH2()]), every=0.0)
        pings.start()
        self.assertIsNone(pings._task)
        await pings.aclose()


class StartTest(unittest.TestCase):
    def test_start_without_an_event_loop_does_nothing(self):
        pings = UpstreamPings(lambda: None, every=1.0)
        pings.start()
        self.assertIsNone(pings._task)


class SettingTest(unittest.TestCase):
    def interval(self, value: str | None) -> float:
        env = {} if value is None else {"EXCEL_BRIDGE_UPSTREAM_PING": value}
        with mock.patch.dict(os.environ, env):
            if value is None:
                os.environ.pop("EXCEL_BRIDGE_UPSTREAM_PING", None)
            return upstream_ping.ping_every()

    def test_values(self):
        cases = {None: 10.0, "": 10.0, "25": 25.0, "0": 0.0, "-5": 0.0, "0.2": 1.0, "600": 60.0,
                 "soon": 10.0, "nan": 10.0}
        for value, seconds in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self.interval(value), seconds)

    def pool_http2(self, value: str | None, *, available: bool = True) -> bool:
        env = {"EXCEL_BRIDGE_PROXY": ""} | ({} if value is None else {"EXCEL_BRIDGE_UPSTREAM_PING": value})
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(upstream_ping, "http2_available", return_value=available):
            if value is None:
                os.environ.pop("EXCEL_BRIDGE_UPSTREAM_PING", None)
            client = server.build_upstream_client()
        try:
            return client._transport._pool._http2
        finally:
            asyncio.run(client.aclose())

    def test_upstream_client_speaks_http2_unless_pings_are_off(self):
        self.assertTrue(self.pool_http2(None))
        self.assertFalse(self.pool_http2("0"))

    def test_upstream_client_falls_back_to_http1_without_h2(self):
        with self.assertLogs("excel_codex_bridge", "WARNING"):
            self.assertFalse(self.pool_http2(None, available=False))

    def test_h2_is_installed(self):
        self.assertTrue(upstream_ping.http2_available())


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_pings_start_with_the_client_and_stop_with_the_bridge(self):
        bridge = server.Bridge(StaticReader(), client_factory=h2_client)
        self.assertIsNone(bridge.pings._task)
        bridge.client
        task = bridge.pings._task
        self.assertIsNotNone(task)
        await bridge.aclose()
        self.assertTrue(task.cancelled())
        self.assertIsNone(bridge.pings._task)


if __name__ == "__main__":
    unittest.main()
