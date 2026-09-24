from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from excel_codex_bridge import cli, codex_config, image_host, images
from excel_codex_bridge.server import create_app

from helpers import session_headers
from test_server import StaticReader, ok_stream

TOKEN = "group-token-0123456789"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
OPENAI = {"user-agent": "OpenAI File Downloader"}
HAS_PILLOW = importlib.util.find_spec("PIL") is not None


def data_url(data: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class HostHarness:
    """The image host app on a temp directory, driven in-process."""

    def __init__(self, directory: Path | None = None, clock: Clock | None = None, **settings) -> None:
        self.directory = directory or Path(tempfile.mkdtemp())
        self.clock = clock or Clock()
        settings.setdefault("tokens", (TOKEN,))
        self.settings = image_host.Settings(
            public_url="https://img.example.com", directory=self.directory, **settings
        )
        self.app = image_host.create_app(self.settings, clock=self.clock)

    def client(self, address: str = "127.0.0.1") -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(address, 50000))
        return httpx.AsyncClient(transport=transport, base_url="https://img.example.com")

    def request(self, method: str, path: str, address: str = "127.0.0.1", **kwargs) -> httpx.Response:
        async def run():
            async with self.client(address) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def upload(self, data: bytes, token: str | None = TOKEN, address: str = "127.0.0.1") -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return self.request("POST", "/upload", address=address, content=data, headers=headers)

    def get(self, url: str, headers: dict | None = None) -> httpx.Response:
        return self.request("GET", url.removeprefix("https://img.example.com"), headers=headers)


class ImageHostTests(unittest.TestCase):
    def test_uploads_need_a_valid_token(self):
        host = HostHarness()
        self.assertEqual(host.upload(PNG, token=None).status_code, 401)
        self.assertEqual(host.upload(PNG, token="wrong-token-0123456789").status_code, 401)
        self.assertEqual(list((host.directory / "images").iterdir()), [])

    def test_only_raster_images_are_accepted(self):
        host = HostHarness()
        for data in (b"", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"<html>", b"%PDF-1.7"):
            with self.subTest(data=data[:8]):
                self.assertEqual(host.upload(data).status_code, 415)
        for data, ext in ((PNG, "png"), (JPEG, "jpg"), (b"GIF89a" + b"\x00" * 8, "gif"),
                          (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp")):
            with self.subTest(ext=ext):
                self.assertTrue(host.upload(data).json()["url"].endswith("." + ext))

    def test_large_uploads_are_refused(self):
        host = HostHarness(max_bytes=100)
        self.assertEqual(host.upload(PNG + b"\x00" * 100).status_code, 413)

    def test_the_same_picture_keeps_its_unguessable_url(self):
        host = HostHarness()
        first = host.upload(PNG).json()
        self.assertEqual(first["expires_in"], 24 * 3600)
        self.assertRegex(first["url"], r"^https://img\.example\.com/i/[0-9a-f]{32}\.png$")
        self.assertEqual(host.upload(PNG).json()["url"], first["url"])
        self.assertNotEqual(host.upload(OTHER_PNG).json()["url"], first["url"])
        # The name is keyed with a secret kept across restarts, not a plain hash.
        self.assertNotIn(__import__("hashlib").sha256(PNG).hexdigest()[:32], first["url"])
        self.assertEqual(HostHarness(host.directory).upload(PNG).json()["url"], first["url"])

    def test_pictures_are_served_inertly(self):
        host = HostHarness()
        response = host.get(host.upload(PNG).json()["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("noindex", response.headers["x-robots-tag"])
        self.assertEqual(response.headers["content-security-policy"], "default-src 'none'")

    def test_there_is_nothing_else_to_fetch(self):
        host = HostHarness()
        host.upload(PNG)
        for path in ("/i/", "/i/name-key", "/i/..%2Fname-key", "/i/../name-key", "/images", "/docs", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(host.request("GET", path).status_code, 404)

    def test_pictures_expire_a_day_after_their_last_upload(self):
        clock = Clock()
        host = HostHarness(clock=clock)
        url = host.upload(PNG).json()["url"]
        clock.now += 20 * 3600
        host.upload(PNG)  # used again: the day starts over
        clock.now += 20 * 3600
        self.assertEqual(host.get(url).status_code, 200)
        self.assertEqual(host.app.state.store.expire(), 0)
        clock.now += 5 * 3600
        self.assertEqual(host.get(url).status_code, 404)
        self.assertEqual(host.app.state.store.expire(), 1)
        self.assertEqual(list((host.directory / "images").iterdir()), [])

    def test_the_oldest_pictures_go_when_the_store_is_full(self):
        clock = Clock()
        host = HostHarness(clock=clock, max_total_bytes=len(PNG) * 2)
        urls = []
        for index in range(3):
            clock.now += 1
            urls.append(host.upload(PNG[:-1] + bytes([index + 2])).json()["url"])
        self.assertEqual([host.get(url).status_code for url in urls], [404, 200, 200])

    def test_settings_refuse_to_start_without_tokens(self):
        base = {"IMAGE_HOST_PUBLIC_URL": "https://img.example.com"}
        for extra in ({}, {"IMAGE_HOST_TOKENS": " , "}, {"IMAGE_HOST_TOKENS": "short"}):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, {**base, **extra}, clear=True):
                with self.assertRaises(SystemExit):
                    image_host.Settings.from_env()
        env = {**base, "IMAGE_HOST_TOKENS": f"{TOKEN}, other-token-0123456789", "IMAGE_HOST_TTL_HOURS": "2"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = image_host.Settings.from_env()
        self.assertEqual(settings.tokens, (TOKEN, "other-token-0123456789"))
        self.assertEqual(settings.ttl_seconds, 7200)
        self.assertEqual((settings.open_uploads, settings.fetchers, settings.reencode), (False, (), False))
        self.assertEqual((settings.uploads_per_hour, settings.upload_bytes_per_day), (0, 0))


class RelayTests(unittest.TestCase):
    """``IMAGE_HOST_OPEN=1``: anyone may upload, so the relay guards itself instead."""

    def relay(self, **settings) -> HostHarness:
        return HostHarness(tokens=(), open_uploads=True, **settings)

    def test_uploads_need_no_token(self):
        host = self.relay()
        response = host.upload(PNG, token=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(host.get(response.json()["url"]).content, PNG)

    def test_each_address_has_an_hourly_quota(self):
        clock = Clock()
        host = self.relay(clock=clock, uploads_per_hour=2)
        self.assertEqual([host.upload(PNG, None).status_code for _ in range(3)], [200, 200, 429])
        self.assertIn("uploads in an hour", host.upload(PNG, None).json()["error"]["message"])
        self.assertEqual(host.upload(PNG, None, address="198.51.100.7").status_code, 200)
        clock.now += 3601
        self.assertEqual(host.upload(PNG, None).status_code, 200)

    def test_each_address_has_a_daily_byte_quota(self):
        clock = Clock()
        host = self.relay(clock=clock, upload_bytes_per_day=len(PNG) * 2 + 1)
        self.assertEqual([host.upload(PNG, None).status_code for _ in range(3)], [200, 200, 429])
        clock.now += 86401
        self.assertEqual(host.upload(PNG, None).status_code, 200)
        host.app.state.quota.forget_idle()
        self.assertEqual(list(host.app.state.quota._seen), ["127.0.0.1"])
        clock.now += 86401
        host.app.state.quota.forget_idle()
        self.assertEqual(host.app.state.quota._seen, {})

    def test_refused_files_do_not_use_up_the_quota(self):
        host = self.relay(uploads_per_hour=1)
        self.assertEqual(host.upload(b"<html>", None).status_code, 415)
        self.assertEqual(host.upload(PNG, None).status_code, 200)

    def test_pictures_are_only_served_to_the_fetchers(self):
        host = self.relay(fetchers=("OpenAI",))
        url = host.upload(PNG, None).json()["url"]
        browser = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0"}
        for headers in (browser, {"user-agent": "curl/8.5.0"}, None):
            with self.subTest(headers=headers):
                self.assertEqual(host.get(url, headers).status_code, 403)
        response = host.get(url, OPENAI)
        self.assertEqual((response.status_code, response.content), (200, PNG))
        self.assertEqual(host.get(url.replace(".png", ".jpg"), OPENAI).status_code, 404)

    def test_settings_for_a_public_relay(self):
        env = {"IMAGE_HOST_PUBLIC_URL": "https://img.example.com", "IMAGE_HOST_OPEN": "1"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict("sys.modules", {"PIL": mock.Mock()}):
            settings = image_host.Settings.from_env()
        self.assertEqual(settings.tokens, ())
        self.assertTrue(settings.open_uploads and settings.reencode)
        self.assertEqual(settings.fetchers, ("OpenAI",))
        self.assertEqual(settings.ttl_seconds, 3600)
        self.assertEqual(settings.max_bytes, 5 * 1024 * 1024)
        self.assertEqual((settings.uploads_per_hour, settings.upload_bytes_per_day), (240, 300 * 1024 * 1024))
        env.update(IMAGE_HOST_FETCHERS="*", IMAGE_HOST_REENCODE="0", IMAGE_HOST_UPLOADS_PER_HOUR="10")
        with mock.patch.dict(os.environ, env, clear=True):
            settings = image_host.Settings.from_env()
        self.assertEqual((settings.fetchers, settings.reencode, settings.uploads_per_hour), ((), False, 10))
        with mock.patch.dict(os.environ, {**env, "IMAGE_HOST_REENCODE": "1"}, clear=True), \
                mock.patch.dict("sys.modules", {"PIL": None}):
            with self.assertRaises(SystemExit):
                image_host.Settings.from_env()


@unittest.skipUnless(HAS_PILLOW, "needs Pillow")
class ReencodeTests(unittest.TestCase):
    def setUp(self):
        self.host = HostHarness(tokens=(), open_uploads=True, reencode=True)

    def stored(self, data: bytes):
        from PIL import Image

        response = self.host.upload(data, None)
        self.assertEqual(response.status_code, 200, response.text)
        content = self.host.get(response.json()["url"]).content
        return response.json()["url"], content, Image.open(io.BytesIO(content))

    def test_png_metadata_and_trailing_bytes_are_dropped(self):
        from PIL import Image, PngImagePlugin

        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "secret-author")
        out = io.BytesIO()
        Image.new("RGB", (4, 3), (10, 20, 30)).save(out, "PNG", pnginfo=info)
        url, content, picture = self.stored(out.getvalue() + b"<script>hidden</script>")
        self.assertTrue(url.endswith(".png"))
        self.assertNotIn(b"secret-author", content)
        self.assertNotIn(b"hidden", content)
        self.assertEqual((picture.size, picture.getpixel((0, 0))), ((4, 3), (10, 20, 30)))

    def test_jpegs_lose_exif_but_stay_upright(self):
        from PIL import Image

        exif = Image.Exif()
        exif[0x0112] = 6  # stored sideways
        exif[0x010E] = "secret-place"
        out = io.BytesIO()
        Image.new("RGB", (40, 20), (200, 100, 50)).save(out, "JPEG", exif=exif.tobytes())
        url, content, picture = self.stored(out.getvalue())
        self.assertTrue(url.endswith(".jpg"))
        self.assertNotIn(b"secret-place", content)
        self.assertNotIn(b"Exif", content)
        self.assertEqual(picture.size, (20, 40))

    def test_animations_keep_their_first_frame(self):
        from PIL import Image

        frames = [Image.new("P", (5, 5), index) for index in (1, 2)]
        out = io.BytesIO()
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
        url, _content, picture = self.stored(out.getvalue())
        self.assertTrue(url.endswith(".png"))
        self.assertFalse(getattr(picture, "is_animated", False))

    def test_broken_or_huge_pictures_are_refused(self):
        from PIL import Image

        self.assertEqual(self.host.upload(PNG, None).status_code, 415)
        out = io.BytesIO()
        Image.new("L", (10, 10)).save(out, "PNG")
        with mock.patch.object(image_host, "MAX_PIXELS", 99):
            response = self.host.upload(out.getvalue(), None)
        self.assertEqual(response.status_code, 415)
        self.assertIn("megapixels", response.json()["error"]["message"])


def codex_body(*pictures: str) -> dict:
    """Shaped like Codex 0.156: `-i` pictures in the user message, view_image in a tool output."""
    return {
        "model": "gpt-5.6-sol-excel",
        "instructions": "data: in the instructions is left alone",
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<image name=[Image #1]>"},
                {"type": "input_image", "image_url": pictures[0], "detail": "high"},
                {"type": "input_text", "text": "</image>"},
                {"type": "input_text", "text": "What number is this?"},
            ]},
            {"type": "function_call", "call_id": "call_1", "name": "view_image", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [
                {"type": "input_image", "image_url": pictures[-1], "detail": "high"},
            ]},
        ],
    }


def pictures_in(body: dict) -> list[dict]:
    return [body["input"][0]["content"][1], body["input"][2]["output"][0]]


REMOTE = images.Setting(images.REMOTE, "https://img.example.com", TOKEN)


def rewrite(uploader: images.ImageUploader, body: dict, **kwargs) -> tuple[dict, int]:
    async def run():
        try:
            return await uploader.rewrite(body, **kwargs)
        finally:
            await uploader.aclose()

    return asyncio.run(run())


class UploaderTests(unittest.TestCase):
    def setUp(self):
        self.host = HostHarness()
        self.uploads: list[httpx.Request] = []

    def uploader(self, setting=REMOTE, handler=None, clock=None) -> images.ImageUploader:
        async def forward(request: httpx.Request) -> httpx.Response:
            self.uploads.append(request)
            if handler is not None:
                return handler(request)
            async with self.host.client() as client:
                return await client.send(request)

        return images.ImageUploader(images.RemoteHost(
            setting,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(forward)),
            clock=clock or Clock(),
        ))

    def test_inline_pictures_become_hosted_urls(self):
        body = codex_body(data_url(PNG), data_url(JPEG, "image/jpeg"))
        rewritten, linked = rewrite(self.uploader(), body)
        self.assertEqual(linked, 2)
        first, second = pictures_in(rewritten)
        self.assertEqual(first["detail"], "high")
        self.assertEqual(self.host.get(first["image_url"]).content, PNG)
        self.assertEqual(self.host.get(second["image_url"]).content, JPEG)
        self.assertEqual(rewritten["instructions"], body["instructions"])
        self.assertEqual(rewritten["input"][0]["content"][3], body["input"][0]["content"][3])
        self.assertTrue(pictures_in(body)[0]["image_url"].startswith("data:"), "the request body is not mutated")
        self.assertEqual(self.uploads[0].headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(self.uploads[0].headers["content-type"], "image/png")
        self.assertEqual(self.uploads[0].url, "https://img.example.com/upload")

    def test_each_picture_is_uploaded_once_until_half_its_lifetime(self):
        clock = Clock()
        uploader = self.uploader(clock=clock)
        body = codex_body(data_url(PNG), data_url(PNG))

        async def run():
            results = [await uploader.rewrite(body)]
            clock.now += 11 * 3600
            results.append(await uploader.rewrite(body))
            clock.now += 2 * 3600
            results.append(await uploader.rewrite(body))
            await uploader.aclose()
            return results

        results = asyncio.run(run())
        self.assertEqual(len(self.uploads), 2)
        # Same URL every time, so the upstream prompt cache keeps matching.
        self.assertEqual(len({picture["image_url"] for body, _ in results for picture in pictures_in(body)}), 1)

    def test_bodies_without_pictures_pass_through_untouched(self):
        body = {"model": "gpt-5.6-sol-excel", "input": [{"type": "message", "role": "user", "content": "hi"}]}
        self.assertEqual(rewrite(self.uploader(), body), (body, 0))
        self.assertIs(rewrite(self.uploader(), body)[0], body)
        self.assertEqual(self.uploads, [])

    def test_with_pictures_off_they_become_a_note(self):
        uploader = images.ImageUploader(off_reason="pictures are turned off")
        rewritten, linked = rewrite(uploader, codex_body(data_url(PNG)))
        self.assertEqual(linked, 0)
        for picture in pictures_in(rewritten):
            self.assertEqual(picture, {"type": "input_text", "text": "[image content omitted: pictures are turned off]"})

    def test_without_links_pictures_become_a_note(self):
        rewritten, linked = rewrite(self.uploader(), codex_body(data_url(PNG)), links=False)
        self.assertEqual(linked, 0)
        for picture in pictures_in(rewritten):
            self.assertIn(images.FETCH_FAILED, picture["text"])
        self.assertEqual(self.uploads, [])

    def test_upload_failures_become_a_note(self):
        failures = [
            lambda request: httpx.Response(401, json={"error": {"message": "missing or wrong upload token"}}),
            lambda request: httpx.Response(200, text="<html>not an image host</html>"),
        ]
        for handler in failures:
            with self.subTest(handler=handler):
                uploader = self.uploader(handler=handler)
                rewritten, linked = rewrite(uploader, codex_body(data_url(PNG)))
                self.assertEqual(linked, 0)
                self.assertEqual(pictures_in(rewritten)[0]["type"], "input_text")
                self.assertIn("could not be passed on", pictures_in(rewritten)[0]["text"])

    def test_an_unreachable_host_is_tried_once_per_request_and_not_remembered(self):
        def unreachable(request):
            raise httpx.ConnectError("refused", request=request)

        uploader = self.uploader(handler=unreachable)

        async def run():
            results = [await uploader.rewrite(codex_body(data_url(PNG), data_url(JPEG, "image/jpeg")))
                       for _ in range(2)]
            await uploader.aclose()
            return results

        for rewritten, linked in asyncio.run(run()):
            self.assertEqual(linked, 0)
            for picture in pictures_in(rewritten):
                self.assertIn("ConnectError", picture["text"])
        self.assertEqual(len(self.uploads), 2)

    def test_when_cloudflare_is_out_of_reach_the_relay_is_suggested(self):
        class Unreachable:
            def link(self, data):
                raise ConnectionError("the Cloudflare link did not come up (no connection)")

            def settling(self):
                return 0.0

            def stop(self):
                pass

        uploader = images.ImageUploader(images.LocalHost(Unreachable()))
        rewritten, linked = rewrite(uploader, codex_body(data_url(PNG)))
        self.assertEqual(linked, 0)
        self.assertIn("did not come up", pictures_in(rewritten)[0]["text"])
        self.assertIn("excel-codex image-host relay", pictures_in(rewritten)[0]["text"])
        uploader = images.ImageUploader(images.LocalHost(Unreachable()))
        rewritten, _ = rewrite(uploader, codex_body(data_url(PNG)), links=False)
        self.assertIn(images.FETCH_FAILED, pictures_in(rewritten)[0]["text"])
        self.assertIn("excel-codex image-host relay", pictures_in(rewritten)[0]["text"])
        rewritten, _ = rewrite(self.uploader(), codex_body(data_url(PNG)), links=False)
        self.assertNotIn("relay", pictures_in(rewritten)[0]["text"], "only local mode suggests it")

    def test_undecodable_pictures_become_a_note(self):
        rewritten, _ = rewrite(self.uploader(), codex_body("data:image/png,not-base64"))
        self.assertIn("could not be decoded", pictures_in(rewritten)[0]["text"])
        self.assertEqual(self.uploads, [])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"EXCEL_BRIDGE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (images.ENV_HOST, images.ENV_TOKEN, images.ENV_RELAY, "EXCEL_BRIDGE_CLOUDFLARED"):
            os.environ.pop(name, None)
        # Only a cloudflared the test puts there counts.
        which = mock.patch("shutil.which", return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def fake_cloudflared(self) -> Path:
        binary = self.home / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"")
        return binary

    def test_local_is_the_default_and_settings_are_kept(self):
        self.assertEqual(images.load_setting(), images.Setting(images.LOCAL))
        images.save_setting(images.Setting(images.REMOTE, "https://img.example.com/upload/", TOKEN))
        setting = images.load_setting()
        self.assertEqual((setting.mode, setting.url, setting.token), (images.REMOTE, "https://img.example.com", TOKEN))
        self.assertEqual(setting.upload_url, "https://img.example.com/upload")
        images.save_setting(images.Setting(images.OFF))
        self.assertEqual(images.load_setting().mode, images.OFF)
        self.assertEqual(json.loads(images.config_path().read_text()), {"mode": "off"})
        for value, mode in (("https://other.example.com", images.REMOTE), ("local", images.LOCAL), ("off", images.OFF)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {images.ENV_HOST: value, images.ENV_TOKEN: "t"}):
                self.assertEqual(images.load_setting().mode, mode)
                self.assertEqual(images.load_setting().source, images.ENV_HOST)
        images.config_path().write_text("[not a setting]")
        self.assertEqual(images.load_setting(), images.DEFAULT_SETTING)

    def test_the_relay_is_this_projects_host_without_a_token(self):
        images.save_setting(images.relay_setting())
        self.assertEqual(json.loads(images.config_path().read_text()), {"mode": "relay"})
        setting = images.load_setting()
        self.assertEqual((setting.mode, setting.url, setting.token), (images.RELAY, images.RELAY_URL, ""))
        self.assertEqual(setting.upload_url, "https://img.aigcnews.cn/upload")
        self.assertIn("an hour after its last use", images.describe(setting))
        with mock.patch.dict(os.environ, {images.ENV_HOST: "relay", images.ENV_RELAY: "https://mirror.example.com/"}):
            setting = images.load_setting()
        self.assertEqual((setting.mode, setting.url, setting.source), (images.RELAY, "https://mirror.example.com",
                                                                       images.ENV_HOST))
        uploader = images.build_uploader(setting)
        self.assertIsInstance(uploader.host, images.RemoteHost)
        self.assertEqual(uploader.host.setting.upload_url, "https://mirror.example.com/upload")

    def test_check_without_a_token_sends_none(self):
        def host(request: httpx.Request) -> httpx.Response:
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(415, json={"error": {"message": "only images"}})

        client = httpx.Client(transport=httpx.MockTransport(host))
        self.assertEqual(images.check(images.relay_setting(), client), (True, f"{images.RELAY_URL} is up."))

    def test_catalog_offers_pictures_only_when_they_can_be_passed_on(self):
        def modalities():
            catalog = cli._write_catalog(self.home)
            return {tuple(model["input_modalities"]) for model in json.loads(catalog.read_text())["models"]}

        self.assertEqual(modalities(), {("text",)}, "local, but no cloudflared")
        self.fake_cloudflared()
        self.assertEqual(modalities(), {("text", "image")})
        images.save_setting(images.Setting(images.OFF))
        self.assertEqual(modalities(), {("text",)})
        images.save_setting(REMOTE)
        self.assertEqual(modalities(), {("text", "image")})
        images.save_setting(images.relay_setting())
        self.assertEqual(modalities(), {("text", "image")})
        self.assertEqual(codex_config.catalog_payload(images=True)["models"][0]["input_modalities"], ["text", "image"])

    def test_build_uploader_follows_the_setting(self):
        self.assertIsNone(images.build_uploader(images.Setting(images.LOCAL)).host)
        self.fake_cloudflared()
        local = images.build_uploader(images.Setting(images.LOCAL))
        self.assertIsInstance(local.host, images.LocalHost)
        self.assertIsInstance(images.build_uploader(REMOTE).host, images.RemoteHost)
        self.assertIsNone(images.build_uploader(images.Setting(images.OFF)).host)

    def test_check_tells_a_good_token_from_a_bad_one(self):
        def host(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.content, b"")
            if request.headers.get("authorization") == f"Bearer {TOKEN}":
                return httpx.Response(415, json={"error": {"message": "only images"}})
            return httpx.Response(401, json={"error": {"message": "wrong token"}})

        client = httpx.Client(transport=httpx.MockTransport(host))
        self.assertTrue(images.check(REMOTE, client)[0])
        bad = images.Setting(images.REMOTE, "https://img.example.com", "nope")
        self.assertFalse(images.check(bad, client)[0])
        missing = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        self.assertFalse(images.check(REMOTE, missing)[0])

    def test_image_host_command(self):
        with mock.patch.object(images, "check", return_value=(False, "rejected")):
            self.assertEqual(cli.main(["image-host", "set", "https://img.example.com", "bad"]), 1)
        self.assertEqual(images.load_setting().mode, images.LOCAL, "nothing was saved")
        self.assertEqual(cli.main(["image-host", "set", "ftp://img.example.com", TOKEN]), 2)
        with mock.patch.object(images, "check", return_value=(True, "ok")):
            self.assertEqual(cli.main(["image-host", "set", "https://img.example.com", TOKEN]), 0)
            self.assertEqual(cli.main(["image-host"]), 0)
        self.assertEqual(images.load_setting().token, TOKEN)
        self.assertEqual(cli.main(["image-host", "off"]), 0)
        self.assertEqual(images.load_setting().mode, images.OFF)
        self.assertEqual(cli.main(["image-host", "show"]), 0)
        self.assertEqual(cli.main(["image-host", "local"]), 0)
        self.assertEqual(images.load_setting().mode, images.LOCAL)
        self.assertEqual(cli.main(["image-host"]), 1, "local, but no cloudflared")
        self.fake_cloudflared()
        self.assertEqual(cli.main(["image-host"]), 0)

    def test_image_host_relay_command(self):
        with mock.patch.object(images, "check", return_value=(False, "cannot reach")) as check:
            self.assertEqual(cli.main(["image-host", "relay"]), 1)
        self.assertEqual(check.call_args.args[0].url, images.RELAY_URL)
        self.assertEqual(images.load_setting().mode, images.LOCAL, "nothing was saved")
        with mock.patch.object(images, "check", return_value=(True, "up")), \
                mock.patch.object(cli, "_print") as printed:
            self.assertEqual(cli.main(["image-host", "relay"]), 0)
            self.assertEqual(cli.main(["image-host"]), 0)
        self.assertEqual(images.load_setting().mode, images.RELAY)
        shown = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        for promise in (images.RELAY_URL, "not to browsers", "deleted an hour after its last use", "metadata"):
            self.assertIn(promise, shown)


class FakeHost:
    """Links pictures under a made-up address, and can pretend its link is brand new."""

    def __init__(self, settle: float = 0.0) -> None:
        self.settle = settle
        self.linked: list[bytes] = []

    async def aclose(self) -> None:
        pass

    def settling(self) -> float:
        return self.settle

    async def link(self, media_type: str, data: bytes) -> str:
        self.linked.append(data)
        return f"https://pictures.example.com/i/{len(self.linked)}.png"


class BridgeImageTests(unittest.TestCase):
    def run_bridge(self, backend, uploader: images.ImageUploader) -> tuple[httpx.Response, list[dict]]:
        upstream: list[dict] = []

        def record(request: httpx.Request) -> httpx.Response:
            upstream.append(json.loads(request.content))
            return backend(upstream[-1], request)

        reader = StaticReader()
        reader.store.configure(session_headers(), persist=False, allow_expired=True)
        app = create_app(
            reader,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(record)),
            uploader=uploader,
        )

        async def run():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
                return await client.post("/v1/responses", json={**codex_body(data_url(PNG)), "stream": True})

        return asyncio.run(run()), upstream

    @staticmethod
    def urls(body: dict) -> list[str]:
        return [
            part["image_url"]
            for item in body["input"] if item.get("type") == "message" and isinstance(item["content"], list)
            for part in item["content"] if part.get("type") == "input_image"
        ]

    def test_upstream_only_ever_sees_hosted_urls(self):
        host = HostHarness()

        async def to_host(request: httpx.Request) -> httpx.Response:
            async with host.client() as client:
                return await client.send(request)

        uploader = images.ImageUploader(images.RemoteHost(
            REMOTE, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(to_host))
        ))
        response, upstream = self.run_bridge(lambda body, request: ok_stream(request), uploader)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("data:image", json.dumps(upstream[0]))
        urls = self.urls(upstream[0])
        self.assertEqual(len(urls), 1)
        self.assertEqual(host.get(urls[0]).content, PNG)

    def test_pictures_openai_cannot_fetch_are_dropped_and_the_request_sent_again(self):
        def backend(body, request):
            if self.urls(body):
                return httpx.Response(400, json={"error": {"message": "Error while downloading the image."}})
            return ok_stream(request)

        response, upstream = self.run_bridge(backend, images.ImageUploader(FakeHost()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(upstream), 2)
        self.assertEqual(self.urls(upstream[1]), [])
        self.assertIn(images.FETCH_FAILED, json.dumps(upstream[1]))
        # Both are the same turn to the backend.
        self.assertEqual(upstream[0]["metadata"], upstream[1]["metadata"])

    def test_a_brand_new_link_gets_a_second_chance(self):
        def backend(body, request):
            if len(upstream_calls) == 0:
                upstream_calls.append(1)
                return httpx.Response(400, json={"error": {"message": "Error while downloading the image."}})
            return ok_stream(request)

        upstream_calls: list[int] = []
        response, upstream = self.run_bridge(backend, images.ImageUploader(FakeHost(settle=0.01)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(upstream), 2)
        self.assertEqual(self.urls(upstream[1]), self.urls(upstream[0]))
        self.assertEqual(len(self.urls(upstream[1])), 1)

    def test_other_refusals_are_passed_on(self):
        def backend(body, request):
            return httpx.Response(429, json={"error": {"message": "slow down"}})

        response, upstream = self.run_bridge(backend, images.ImageUploader(FakeHost(settle=0.01)))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(upstream), 1)


if __name__ == "__main__":
    unittest.main()
