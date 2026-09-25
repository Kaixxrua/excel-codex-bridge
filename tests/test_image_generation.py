from __future__ import annotations

import base64
import email
import email.policy
import unittest

import httpx

from excel_codex_bridge import image_generation
from excel_codex_bridge.image_generation import Refused, edit_form, generation_body

from test_server import BridgeHarness

PICTURE = b"\x89PNG\r\n\x1a\n not really a picture"
PICTURE_URL = "data:image/png;base64," + base64.b64encode(PICTURE).decode()
# What Codex's image tool sends for a new picture.
CODEX_REQUEST = {"prompt": "a blue whale", "background": "opaque", "model": "gpt-image-2",
                 "quality": "auto", "size": "auto"}
# What the add-in's generate_image sends for the same.
ADDIN_REQUEST = {"background": "opaque", "model": "gpt-image-2", "output_format": "png",
                 "prompt": "a blue whale", "quality": "auto", "size": "auto"}
DRAWN = {"created": 1, "background": "opaque", "data": [{"b64_json": "iVBORw0KGgo="}]}


def form_parts(request: httpx.Request) -> list[tuple[str, str | None, bytes]]:
    """(field name, file name, content) for each part of a multipart request."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = email.message_from_bytes(head + request.content, policy=email.policy.HTTP)
    return [
        (part.get_param("name", header="content-disposition"), part.get_filename(),
         part.get_payload(decode=True))
        for part in message.iter_parts()
    ]


class RequestTests(unittest.TestCase):
    def test_new_picture_is_asked_for_as_the_add_in_does(self):
        self.assertEqual(generation_body(CODEX_REQUEST), ADDIN_REQUEST)
        self.assertEqual(
            generation_body({"prompt": "p", "n": 2, "size": "1536x1024", "quality": "low"}),
            {"background": "auto", "model": "gpt-image-2", "n": 2, "output_format": "png",
             "prompt": "p", "quality": "low", "size": "1536x1024"},
        )

    def test_what_the_add_in_does_not_offer_is_refused(self):
        for change in ({"background": "transparent"}, {"model": "gpt-image-1"}, {"size": "1792x1024"},
                       {"quality": "ultra"}, {"n": 4}, {"n": True}, {"n": "2"}, {"prompt": " "},
                       {"prompt": None}, {"output_format": "webp"}):
            with self.subTest(change=change), self.assertRaises(Refused):
                generation_body({**CODEX_REQUEST, **change})
        with self.assertRaisesRegex(Refused, "Transparent backgrounds"):
            generation_body({**CODEX_REQUEST, "background": "transparent"})

    def test_edit_sends_the_pictures_as_a_form(self):
        fields, files = edit_form({**CODEX_REQUEST, "images": [{"image_url": PICTURE_URL}]})
        self.assertEqual(fields, ADDIN_REQUEST)
        self.assertEqual(files, [("image", ("picture-1.png", PICTURE, "image/png"))])
        jpeg = "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode()
        _, files = edit_form({"prompt": "p", "images": [{"image_url": PICTURE_URL}, {"image_url": jpeg}]})
        self.assertEqual([(name, file[0]) for name, file in files],
                         [("image[]", "picture-1.png"), ("image[]", "picture-2.jpg")])

    def test_edit_needs_inline_pictures(self):
        for pictures in (None, [], [{"image_url": "https://example.com/a.png"}], [{"file_id": "file-1"}],
                         ["data:image/png;base64,AAAA"]):
            with self.subTest(pictures=pictures), self.assertRaises(Refused):
                edit_form({**CODEX_REQUEST, "images": pictures})


class RouteTests(unittest.TestCase):
    def test_new_picture_is_drawn_on_the_excel_session(self):
        harness = BridgeHarness(lambda _r: httpx.Response(200, json=DRAWN))
        response = harness.request("POST", "/v1/images/generations", json=CODEX_REQUEST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), DRAWN)
        upstream = harness.upstream_requests[0]
        self.assertEqual(str(upstream.url), image_generation.GENERATIONS_URL)
        self.assertEqual(str(upstream.url), "https://bps.openai.com/basispoints/api/images/generations")
        self.assertEqual(harness.upstream_json(), ADDIN_REQUEST)
        self.assertTrue(upstream.headers["authorization"].startswith("Bearer "))
        self.assertEqual(upstream.headers["chatgpt-account-id"], "account-id")
        self.assertEqual(upstream.headers["accept"], "application/json")

    def test_edit_is_posted_as_the_add_ins_form(self):
        harness = BridgeHarness(lambda _r: httpx.Response(200, json=DRAWN))
        response = harness.request(
            "POST", "/images/edits", json={**CODEX_REQUEST, "images": [{"image_url": PICTURE_URL}]}
        )
        self.assertEqual(response.status_code, 200)
        upstream = harness.upstream_requests[0]
        self.assertEqual(str(upstream.url), "https://bps.openai.com/basispoints/api/images/edits")
        self.assertTrue(upstream.headers["content-type"].startswith("multipart/form-data; boundary="))
        parts = form_parts(upstream)
        self.assertIn(("image", "picture-1.png", PICTURE), parts)
        fields = {name: content.decode() for name, filename, content in parts if filename is None}
        self.assertEqual(fields, ADDIN_REQUEST)

    def test_refused_request_never_leaves_the_computer(self):
        harness = BridgeHarness(lambda _r: httpx.Response(200, json=DRAWN))
        response = harness.request(
            "POST", "/v1/images/generations", json={**CODEX_REQUEST, "background": "transparent"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Transparent backgrounds", response.json()["error"]["message"])
        self.assertEqual(harness.request("POST", "/v1/images/variations", json={}).status_code, 404)
        self.assertEqual(harness.upstream_requests, [])

    def test_missing_session_is_a_401(self):
        harness = BridgeHarness(lambda _r: httpx.Response(200, json=DRAWN), configured=False)
        response = harness.request("POST", "/v1/images/generations", json=CODEX_REQUEST)
        self.assertEqual(response.status_code, 401)
        self.assertIn("ChatGPT add-in pane", response.json()["error"]["message"])
        self.assertEqual(harness.upstream_requests, [])

    def test_backend_refusals_come_back_as_they_are(self):
        harness = BridgeHarness(
            lambda _r: httpx.Response(429, headers={"retry-after": "60"},
                                      json={"error": {"message": "image limit reached"}})
        )
        response = harness.request("POST", "/v1/images/generations", json=CODEX_REQUEST)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")
        self.assertEqual(response.json()["error"]["message"], "image limit reached")

    def test_refused_drawing_is_not_blamed_on_the_session(self):
        harness = BridgeHarness(lambda _r: httpx.Response(403, json={"detail": "not enabled"}))
        response = harness.request("POST", "/v1/images/generations", json=CODEX_REQUEST)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["message"],
                         "The Excel backend refused the image request (403): not enabled")

    def test_an_answer_that_is_not_json_is_a_502(self):
        harness = BridgeHarness(lambda _r: httpx.Response(200, text="<html>"))
        response = harness.request("POST", "/v1/images/generations", json=CODEX_REQUEST)
        self.assertEqual(response.status_code, 502)

    def test_slow_drawing_gets_ten_minutes(self):
        def fail(request):
            raise httpx.ReadTimeout("", request=request)

        response = BridgeHarness(fail).request("POST", "/v1/images/generations", json=CODEX_REQUEST)
        self.assertEqual(response.status_code, 504)
        self.assertIn("within 10 minutes", response.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
