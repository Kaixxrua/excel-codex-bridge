from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest import mock

import httpx

from excel_codex_bridge import images
from excel_codex_bridge.server import create_app

from helpers import session_headers
from test_server import StaticReader, ok_stream

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def data_url(data: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


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
    """The user message's picture and the tool output's, in Codex's body or the upstream one."""
    message = next(item for item in body["input"] if item.get("role") == "user")
    tool_output = next(item for item in body["input"] if item.get("type") == "function_call_output")
    return [message["content"][1], tool_output["output"][0]]


class Attachments:
    """The add-in's attachments endpoint: hands out file ids, or fails as told."""

    def __init__(self, fail: httpx.Response | Exception | None = None) -> None:
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail is not None:
            return self.fail
        return httpx.Response(200, json={
            "openai_file_id": f"file-{len(self.requests)}", "filename": "picture.png",
            "content_type": "image/png", "size": 72, "input_tokens": 85,
        })


def rewrite(pictures: images.Pictures, body: dict, attachments: Attachments | None = None,
            headers: dict | None = None, **kwargs) -> images.Sent:
    attachments = attachments or Attachments()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(attachments)) as client:
            return await pictures.rewrite(body, client, headers or session_headers(), **kwargs)

    return asyncio.run(run())


def refusing(*kinds: str) -> images.Pictures:
    pictures = images.Pictures()
    for kind in kinds:
        pictures.refuse_inline(kind)
    return pictures


class PicturesTests(unittest.TestCase):
    def test_bodies_without_pictures_pass_through_untouched(self):
        body = {"model": "gpt-5.6-sol-excel", "input": [{"type": "message", "role": "user", "content": "hi"}]}
        attachments = Attachments()
        sent = rewrite(images.Pictures(), body, attachments)
        self.assertIs(sent.body, body)
        self.assertFalse(sent.pictures)
        self.assertEqual(attachments.requests, [])

    def test_pictures_go_inline_until_the_backend_refuses_them(self):
        attachments = Attachments()
        body = codex_body(data_url(PNG), data_url(JPEG, "image/jpeg"))
        sent = rewrite(images.Pictures(), body, attachments)
        self.assertEqual(pictures_in(sent.body), pictures_in(body))
        self.assertEqual(sent.inline, {"message", "function_call_output"})
        self.assertEqual(attachments.requests, [])

    def test_pictures_where_inline_is_refused_are_uploaded(self):
        attachments = Attachments()
        body = codex_body(data_url(PNG), data_url(JPEG, "image/jpeg"))
        sent = rewrite(refusing("message"), body, attachments)
        message, tool_output = pictures_in(sent.body)
        self.assertEqual(message, {"type": "input_image", "detail": "high", "file_id": "file-1"})
        self.assertEqual(tool_output, pictures_in(body)[1])
        self.assertEqual(sent.uploaded, {"file-1"})
        self.assertEqual(sent.inline, {"function_call_output"})
        self.assertEqual(len(attachments.requests), 1)
        self.assertEqual(sent.body["instructions"], body["instructions"])
        # Codex's own body is left as it was.
        self.assertTrue(pictures_in(body)[0]["image_url"].startswith("data:"))

    def test_a_picture_without_a_detail_gets_auto(self):
        body = codex_body(data_url(PNG))
        del body["input"][0]["content"][1]["detail"]
        sent = rewrite(refusing("message"), body)
        self.assertEqual(pictures_in(sent.body)[0]["detail"], "auto")

    def test_the_upload_is_the_add_ins(self):
        attachments = Attachments()
        headers = {**session_headers(), "accept": "text/event-stream", "content-type": "application/json",
                   "origin": "https://bps.openai.com"}
        rewrite(refusing("message"), codex_body(data_url(PNG)), attachments, headers)
        request = attachments.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://bps.openai.com/basispoints/api/attachments")
        self.assertEqual(request.headers["authorization"], headers["authorization"])
        self.assertEqual(request.headers["chatgpt-account-id"], "account-id")
        self.assertEqual(request.headers["origin"], "https://bps.openai.com")
        self.assertEqual(request.headers["accept"], "application/json")
        self.assertTrue(request.headers["content-type"].startswith("multipart/form-data; boundary="))
        content = request.read()
        self.assertIn(b'name="file"; filename="picture-', content)
        self.assertIn(b".png\"\r\nContent-Type: image/png\r\n\r\n" + PNG, content)

    def test_each_picture_is_uploaded_once(self):
        attachments = Attachments()
        pictures = refusing("message", "function_call_output")
        first = rewrite(pictures, codex_body(data_url(PNG)), attachments)
        # The same picture twice in one request: one upload, and nothing reused.
        self.assertEqual([part["file_id"] for part in pictures_in(first.body)], ["file-1", "file-1"])
        self.assertEqual(first.reused, set())
        second = rewrite(pictures, codex_body(data_url(PNG), data_url(OTHER_PNG)), attachments)
        self.assertEqual([part["file_id"] for part in pictures_in(second.body)], ["file-1", "file-2"])
        self.assertEqual(second.reused, {"file-1"})
        self.assertEqual(second.uploaded, {"file-2"})
        self.assertEqual(len(attachments.requests), 2)

    def test_uploads_are_not_shared_between_accounts(self):
        attachments = Attachments()
        pictures = refusing("message")
        rewrite(pictures, codex_body(data_url(PNG)), attachments, session_headers(account="a"))
        sent = rewrite(pictures, codex_body(data_url(PNG)), attachments, session_headers(account="b"))
        self.assertEqual(pictures_in(sent.body)[0]["file_id"], "file-2")
        self.assertEqual(len(attachments.requests), 2)

    def test_forgotten_uploads_are_made_again(self):
        attachments = Attachments()
        pictures = refusing("message")
        rewrite(pictures, codex_body(data_url(PNG)), attachments)
        pictures.forget({"file-1"})
        sent = rewrite(pictures, codex_body(data_url(PNG)), attachments)
        self.assertEqual(pictures_in(sent.body)[0]["file_id"], "file-2")
        self.assertEqual(sent.reused, set())

    def test_the_oldest_uploads_are_forgotten_first(self):
        attachments = Attachments()
        pictures = refusing("message")
        with mock.patch.object(images, "CACHE_SIZE", 2):
            for data in (PNG, OTHER_PNG, JPEG):
                rewrite(pictures, codex_body(data_url(data)), attachments)
            rewrite(pictures, codex_body(data_url(JPEG)), attachments)
            self.assertEqual(len(attachments.requests), 3)
            rewrite(pictures, codex_body(data_url(PNG)), attachments)
            self.assertEqual(len(attachments.requests), 4)

    def test_upload_failures_become_a_note(self):
        attachments = Attachments(httpx.Response(413, json={"detail": "File too large"}))
        pictures = refusing("message", "function_call_output")
        with self.assertLogs("excel_codex_bridge.images", "WARNING"):
            sent = rewrite(pictures, codex_body(data_url(PNG), data_url(OTHER_PNG)), attachments)
        note = {"type": "input_text",
                "text": "[image content omitted: it could not be uploaded (HTTP 413: File too large)]"}
        self.assertEqual(pictures_in(sent.body), [note, note])
        # A refused file does not stop the next one from being tried.
        self.assertEqual(len(attachments.requests), 2)
        self.assertFalse(sent.pictures)

    def test_when_the_backend_is_out_of_reach_it_is_tried_once_per_request(self):
        attachments = Attachments(httpx.ConnectError("unreachable"))
        pictures = refusing("message", "function_call_output")
        with self.assertLogs("excel_codex_bridge.images", "WARNING"):
            sent = rewrite(pictures, codex_body(data_url(PNG), data_url(OTHER_PNG)), attachments)
        self.assertEqual(len(attachments.requests), 1)
        self.assertEqual([part["type"] for part in pictures_in(sent.body)], ["input_text", "input_text"])
        self.assertIn("ConnectError", pictures_in(sent.body)[1]["text"])

    def test_an_answer_without_a_file_id_becomes_a_note(self):
        attachments = Attachments(httpx.Response(200, json={"filename": "picture.png"}))
        with self.assertLogs("excel_codex_bridge.images", "WARNING"):
            sent = rewrite(refusing("message"), codex_body(data_url(PNG)), attachments)
        self.assertEqual(pictures_in(sent.body)[0]["text"],
                         "[image content omitted: it could not be uploaded (no file id came back)]")

    def test_undecodable_pictures_become_a_note(self):
        attachments = Attachments()
        sent = rewrite(refusing("message"), codex_body("data:image/png,not-base64"), attachments)
        self.assertEqual(pictures_in(sent.body)[0]["text"],
                         "[image content omitted: the picture could not be decoded]")
        self.assertEqual(attachments.requests, [])

    def test_omitting_leaves_every_picture_out(self):
        attachments = Attachments()
        sent = rewrite(images.Pictures(), codex_body(data_url(PNG)), attachments, omit="not today")
        self.assertEqual(pictures_in(sent.body), [{"type": "input_text", "text": "[image content omitted: not today]"}] * 2)
        self.assertNotIn("data:image", json.dumps(sent.body))
        self.assertEqual(attachments.requests, [])


class Backend:
    """The Excel backend, refusing what the test tells it to."""

    def __init__(self, refuse) -> None:
        self.refuse = refuse
        self.bodies: list[dict] = []
        self.attachments = Attachments()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachments"):
            return self.attachments(request)
        self.bodies.append(json.loads(request.content))
        refusal = self.refuse(self.bodies[-1])
        if refusal is not None:
            return refusal
        return ok_stream(request)


def invalid() -> httpx.Response:
    return httpx.Response(422, json={"detail": "Invalid request body."})


def inline_in(item_type: str):
    """Refuse data: pictures inside items of this type."""
    def refuse(body):
        items = [item for item in body["input"] if item.get("type", "message") == item_type]
        return invalid() if "data:image" in json.dumps(items) else None
    return refuse


def any_picture(body):
    return invalid() if '"input_image"' in json.dumps(body["input"]) else None


class BridgeImageTests(unittest.TestCase):
    def setUp(self):
        reader = StaticReader()
        reader.store.configure(session_headers(), persist=False, allow_expired=True)
        self.backend = Backend(lambda body: None)
        self.app = create_app(
            reader, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.backend))
        )

    def send(self, body: dict | None = None) -> httpx.Response:
        body = body or codex_body(data_url(PNG), data_url(OTHER_PNG))

        async def run():
            transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
                return await client.post("/v1/responses", json={**body, "stream": True})

        return asyncio.run(run())

    def uploads(self) -> int:
        return len(self.backend.attachments.requests)

    def test_pictures_the_backend_takes_inline_are_not_uploaded(self):
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.backend.bodies), 1)
        self.assertEqual(self.uploads(), 0)
        self.assertEqual(pictures_in(self.backend.bodies[0])[0]["image_url"], data_url(PNG))

    def test_refused_pictures_are_uploaded_and_the_request_sent_again(self):
        self.backend.refuse = inline_in("message")
        with self.assertLogs("excel_codex_bridge", "INFO") as logs:
            response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.backend.bodies), 2)
        message, tool_output = pictures_in(self.backend.bodies[1])
        self.assertEqual(message, {"type": "input_image", "detail": "high", "file_id": "file-1"})
        self.assertEqual(tool_output["image_url"], data_url(OTHER_PNG))
        self.assertEqual(self.uploads(), 1)
        self.assertIn("uploading those in message", "\n".join(logs.output))
        # Both are the same turn to the backend.
        self.assertEqual(self.backend.bodies[0]["metadata"], self.backend.bodies[1]["metadata"])

    def test_tool_output_pictures_are_uploaded_when_refused_there_too(self):
        self.backend.refuse = inline_in("function_call_output")
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.backend.bodies), 3)
        self.assertEqual([part["file_id"] for part in pictures_in(self.backend.bodies[-1])], ["file-1", "file-2"])

    def test_once_refused_they_are_uploaded_straight_away_and_only_once(self):
        self.backend.refuse = inline_in("message")
        self.send()
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.backend.bodies), 3)
        self.assertEqual(pictures_in(self.backend.bodies[2])[0]["file_id"], "file-1")
        self.assertEqual(self.uploads(), 1)

    def test_uploads_the_backend_no_longer_knows_are_made_again(self):
        self.backend.refuse = inline_in("message")
        self.send()
        self.backend.refuse = lambda body: invalid() if '"file-1"' in json.dumps(body) else None
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.backend.bodies), 4)
        message, tool_output = pictures_in(self.backend.bodies[-1])
        self.assertEqual(message["file_id"], "file-2")
        self.assertEqual(self.uploads(), 2)
        # Nothing wrong was learnt about the picture that went inline.
        self.assertEqual(tool_output["image_url"], data_url(OTHER_PNG))

    def test_pictures_are_left_out_when_nothing_else_works(self):
        self.backend.refuse = any_picture
        with self.assertLogs("excel_codex_bridge", "WARNING") as logs:
            response = self.send()
        self.assertEqual(response.status_code, 200)
        # Inline, then uploaded for the message, then for the tool output too, then left out.
        self.assertEqual(len(self.backend.bodies), 4)
        self.assertEqual(self.uploads(), 2)
        last = self.backend.bodies[-1]
        self.assertEqual([part["type"] for part in pictures_in(last)], ["input_text", "input_text"])
        self.assertIn("the Excel backend did not accept it", json.dumps(last))
        self.assertIn("sending it without them", "\n".join(logs.output))
        self.assertEqual(len({json.dumps(body["metadata"]) for body in self.backend.bodies}), 1)

    def test_a_picture_uploaded_in_this_request_is_not_uploaded_again(self):
        self.backend.refuse = any_picture
        with self.assertLogs("excel_codex_bridge", "INFO") as logs:
            self.send(codex_body(data_url(PNG)))
        # Refused inline and uploaded, in both places: left out, without a second upload.
        self.assertEqual(len(self.backend.bodies), 4)
        self.assertEqual(self.uploads(), 1)
        self.assertNotIn("uploading them again", "\n".join(logs.output))

    def test_when_uploads_fail_the_request_goes_with_a_note(self):
        self.backend.refuse = inline_in("message")
        self.backend.attachments.fail = httpx.Response(500, text="oops")
        with self.assertLogs("excel_codex_bridge", "WARNING"):
            response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.backend.bodies), 2)
        message, tool_output = pictures_in(self.backend.bodies[1])
        self.assertEqual(message["text"], "[image content omitted: it could not be uploaded (HTTP 500: oops)]")
        self.assertEqual(tool_output["image_url"], data_url(OTHER_PNG))

    def test_other_refusals_are_passed_on(self):
        self.backend.refuse = lambda body: httpx.Response(429, json={"error": {"message": "slow down"}})
        response = self.send()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.backend.bodies), 1)
        self.assertEqual(self.uploads(), 0)

    def test_requests_without_pictures_are_sent_once(self):
        self.backend.refuse = lambda body: invalid()
        response = self.send({"model": "gpt-5.6-sol-excel", "input": "hi"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.backend.bodies), 1)


if __name__ == "__main__":
    unittest.main()
