from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from excel_codex_bridge import local_host

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

# Stands in for cloudflared.  FAKE_CF_MODE: "ok" prints what a working quick
# tunnel prints; "drop" does that and exits on its first start; "never" only
# prints errors.  Each start is recorded in FAKE_CF_RECORD.
FAKE_CLOUDFLARED = r"""
import json, os, sys, time
record = os.environ["FAKE_CF_RECORD"]
with open(record, "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid(), "home": os.environ.get("HOME"),
                          "tunnel_env": sorted(k for k in os.environ if k.upper().startswith("TUNNEL_"))}) + "\n")
with open(record, encoding="utf-8") as starts:
    start = len(starts.readlines())
mode = os.environ.get("FAKE_CF_MODE", "ok")
if mode == "never":
    print("2026-09-24T00:00:00Z ERR Failed to dial a quick tunnel error=\"connection refused\"", flush=True)
    time.sleep(600)
print("INF Requesting new quick Tunnel on https://api.trycloudflare.com ...", flush=True)
print(f"INF |  https://fake-start-{start}.trycloudflare.com  |", flush=True)
time.sleep(0.05)
print("INF Registered tunnel connection connIndex=0 protocol=http2", flush=True)
if mode == "drop" and start == 1:
    time.sleep(0.3)
    sys.exit(1)
time.sleep(600)
"""


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class MemoryStoreTests(unittest.TestCase):
    def test_pictures_are_open_only_for_a_while_after_use(self):
        clock = Clock()
        store = local_host.MemoryStore(clock, window=300)
        name = store.put(PNG)
        self.assertRegex(name, r"^[A-Za-z0-9_-]{32}\.png$")
        self.assertEqual(store.get(name), (PNG, "image/png"))
        clock.now += 301
        self.assertIsNone(store.get(name))
        self.assertEqual(store.put(PNG), name, "the same picture keeps its name")
        self.assertEqual(store.get(name), (PNG, "image/png"))

    def test_names_are_random_and_only_raster_images_are_kept(self):
        first = local_host.MemoryStore().put(PNG)
        self.assertNotEqual(local_host.MemoryStore().put(PNG), first)
        store = local_host.MemoryStore()
        for data in (b"", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"%PDF-1.7", b"<html>"):
            self.assertIsNone(store.put(data))
        self.assertTrue(store.put(JPEG).endswith(".jpg"))
        for name in ("", "../x.png", "a" * 32 + ".svg", "a" * 31 + ".png", first):
            self.assertIsNone(store.get(name))

    def test_old_pictures_are_forgotten(self):
        clock = Clock()
        store = local_host.MemoryStore(clock, max_bytes=len(PNG) * 2)
        names = []
        for index in range(3):
            clock.now += 1
            names.append(store.put(PNG[:-1] + bytes([index + 1])))
        self.assertEqual([store.get(name) is not None for name in names], [False, True, True])
        self.assertEqual(len(store), 2)
        clock.now += local_host.IDLE_FORGET + 1
        store.put(JPEG)
        self.assertEqual(len(store), 1)


class PublicAppTests(unittest.TestCase):
    def request(self, store, method: str, path: str) -> httpx.Response:
        async def run():
            transport = httpx.ASGITransport(app=local_host.public_app(store))
            async with httpx.AsyncClient(transport=transport, base_url="https://x.trycloudflare.com") as client:
                return await client.request(method, path)

        return asyncio.run(run())

    def test_only_open_pictures_are_served_and_inertly(self):
        clock = Clock()
        store = local_host.MemoryStore(clock, window=300)
        name = store.put(PNG)
        response = self.request(store, "GET", f"/i/{name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-security-policy"], "default-src 'none'")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        head = self.request(store, "HEAD", f"/i/{name}")
        self.assertEqual((head.status_code, head.content), (200, b""))
        for method, path in (("GET", "/"), ("GET", "/i/"), ("GET", f"/i/{name}/"), ("POST", f"/i/{name}"),
                             ("GET", "/i/..%2F" + name), ("GET", "/healthz"), ("GET", "/v1/responses")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(store, method, path).status_code, 404)
        clock.now += 301
        self.assertEqual(self.request(store, "GET", f"/i/{name}").status_code, 404)


class FakeCloudflared:
    """Runs FAKE_CLOUDFLARED in place of the real binary."""

    def __init__(self, test: unittest.TestCase, mode: str = "ok") -> None:
        root = Path(tempfile.mkdtemp())
        self.script = root / "fake-cloudflared.py"
        self.script.write_text(FAKE_CLOUDFLARED, encoding="utf-8")
        self.record = root / "starts.jsonl"
        self.home = root / "home"
        env = mock.patch.dict(os.environ, {
            "FAKE_CF_RECORD": str(self.record), "FAKE_CF_MODE": mode, "TUNNEL_TOKEN": "someone-elses-tunnel",
        })
        env.start()
        test.addCleanup(env.stop)
        fake = self

        class Tunnel(local_host.Tunnel):
            def __init__(self, binary, origin_port, *, home, delays=(0.05,)):
                super().__init__(binary, origin_port, home=home, delays=delays)

            def _command(self):
                return [sys.executable, str(fake.script), *super()._command()[1:]]

        self.Tunnel = Tunnel

    def starts(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def tunnel(self, test: unittest.TestCase, port: int = 9) -> local_host.Tunnel:
        tunnel = self.Tunnel("cloudflared", port, home=self.home)
        test.addCleanup(tunnel.stop)
        return tunnel


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class TunnelTests(unittest.TestCase):
    def test_the_link_is_ready_once_cloudflared_registers(self):
        fake = FakeCloudflared(self)
        tunnel = fake.tunnel(self, port=4321)
        self.assertEqual(tunnel.url(timeout=20), "https://fake-start-1.trycloudflare.com")
        self.assertIsNotNone(tunnel.ready_since)
        [start] = fake.starts()
        self.assertEqual(start["argv"], ["tunnel", "--no-autoupdate", "--protocol", "http2",
                                         "--url", "http://127.0.0.1:4321"])
        # The user's own cloudflared settings stay out.
        self.assertEqual(start["tunnel_env"], [])
        self.assertEqual(Path(start["home"]), fake.home)
        tunnel.stop()
        self.assertTrue(gone(start["pid"]))

    def test_a_dropped_tunnel_comes_back_with_a_new_link(self):
        fake = FakeCloudflared(self, mode="drop")
        tunnel = fake.tunnel(self)
        self.assertEqual(tunnel.url(timeout=20), "https://fake-start-1.trycloudflare.com")
        deadline = time.monotonic() + 20
        while len(fake.starts()) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(tunnel.url(timeout=20), "https://fake-start-2.trycloudflare.com")

    def test_a_tunnel_that_never_comes_up_says_why(self):
        fake = FakeCloudflared(self, mode="never")
        tunnel = fake.tunnel(self)
        self.assertIsNone(tunnel.url(timeout=1))
        self.assertIn("Failed to dial a quick tunnel", tunnel.problem())

    def test_a_missing_binary_is_reported(self):
        tunnel = local_host.Tunnel(str(Path(tempfile.mkdtemp()) / "no-cloudflared"), 9,
                                   home=Path(tempfile.mkdtemp()), delays=(0.05,))
        self.addCleanup(tunnel.stop)
        self.assertIsNone(tunnel.url(timeout=0.5))
        self.assertIn("could not start cloudflared", tunnel.problem())

    def test_stop_all_stops_every_tunnel(self):
        fake = FakeCloudflared(self)
        tunnel = fake.tunnel(self)
        self.assertIsNotNone(tunnel.url(timeout=20))
        local_host.stop_all()
        self.assertTrue(gone(fake.starts()[0]["pid"]))


class LocalPicturesTests(unittest.TestCase):
    def test_a_picture_link_serves_the_picture_through_the_tunnel(self):
        fake = FakeCloudflared(self)
        pictures = local_host.LocalPictures("cloudflared", fake.home, tunnel_factory=fake.Tunnel)
        self.addCleanup(pictures.stop)
        self.assertEqual(fake.starts(), [], "nothing starts before the first picture")
        link = pictures.link(PNG, timeout=20)
        self.assertRegex(link, r"^https://fake-start-1\.trycloudflare\.com/i/[A-Za-z0-9_-]{32}\.png$")
        self.assertGreater(pictures.settling(), 0, "a brand-new link may not resolve yet")
        origin = fake.starts()[0]["argv"][-1]
        path = link.removeprefix("https://fake-start-1.trycloudflare.com")
        with httpx.Client(trust_env=False) as client:
            self.assertEqual(client.get(origin + path).content, PNG)
            self.assertEqual(client.get(origin + "/").status_code, 404)
        self.assertEqual(pictures.link(PNG, timeout=20), link)
        self.assertEqual(len(fake.starts()), 1)
        with self.assertRaises(ValueError):
            pictures.link(b"<svg/>")
        pictures.stop()
        self.assertTrue(gone(fake.starts()[0]["pid"]))

    def test_no_tunnel_is_an_error(self):
        fake = FakeCloudflared(self, mode="never")
        pictures = local_host.LocalPictures("cloudflared", fake.home, tunnel_factory=fake.Tunnel)
        self.addCleanup(pictures.stop)
        with self.assertRaisesRegex(ConnectionError, "Failed to dial"):
            pictures.link(PNG, timeout=1)
        self.assertEqual(pictures.settling(), 0)


class FindCloudflaredTests(unittest.TestCase):
    def test_lookup_order(self):
        state = Path(tempfile.mkdtemp())
        name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("shutil.which", return_value="/usr/bin/cf"):
            os.environ.pop(local_host.CLOUDFLARED_ENV, None)
            self.assertEqual(local_host.find_cloudflared(state), "/usr/bin/cf")
            (state / "bin").mkdir()
            (state / "bin" / name).write_bytes(b"")
            self.assertEqual(local_host.find_cloudflared(state), str(state / "bin" / name))
            os.environ[local_host.CLOUDFLARED_ENV] = str(state / "missing")
            self.assertIsNone(local_host.find_cloudflared(state))


if __name__ == "__main__":
    unittest.main()
