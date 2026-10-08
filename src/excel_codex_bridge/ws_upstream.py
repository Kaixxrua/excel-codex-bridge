"""Single-attempt native WebSocket upstream; local clients still use Responses HTTP."""

import asyncio
import json
import os

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from . import sse
from .native_upstream import NativeBridge

URL = "wss://chatgpt.com/backend-api/codex/responses"


class FixedEndpointConnect(connect):
    def process_redirect(self, exc):
        return exc  # Never forward credentials to a redirect, including same-origin redirects.


class SocketStream(httpx.AsyncByteStream):
    def __init__(self, socket):
        self.socket = socket

    async def __aiter__(self):
        try:
            while True:
                raw = await asyncio.wait_for(self.socket.recv(), 600)
                try:
                    event = json.loads(raw)
                    kind = event["type"]
                    if not isinstance(kind, str):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise httpx.ReadError("Invalid WebSocket event") from None
                yield sse.sse_encode(kind, event)
                if kind in {"response.completed", "response.failed", "response.incomplete", "error"}:
                    return
        except (WebSocketException, OSError, TimeoutError) as exc:
            raise httpx.ReadError("WebSocket closed before a terminal response") from None

    async def aclose(self):
        await self.socket.close()


async def open_response(payload, headers, *, connector=FixedEndpointConnect):
    headers = {k: v for k, v in headers.items() if k.lower() not in {"accept", "content-type"}}
    headers["OpenAI-Beta"] = "responses_websockets=2026-02-06"
    body = {k: v for k, v in payload.items() if k not in {"stream", "background"}}
    body["type"] = "response.create"
    socket = None
    try:
        socket = await connector(URL, additional_headers=headers, user_agent_header=None,
                                 proxy=os.environ.get("EXCEL_BRIDGE_PROXY") or True,
                                 open_timeout=30, close_timeout=2, max_size=32 * 1024 * 1024,
                                 ping_interval=20, ping_timeout=30)
        await socket.send(json.dumps(body, ensure_ascii=False))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SocketStream(socket))
    except InvalidStatus as exc:
        status = exc.response.status_code
        return httpx.Response(status if status >= 400 else 502,
                              json={"error": {"code": "websocket_handshake_failed", "message": f"WebSocket handshake returned HTTP {status}."}})
    except (WebSocketException, OSError, TimeoutError):
        if socket is not None:
            await socket.close()
        raise httpx.ConnectError("Could not open the selected WebSocket route") from None
    except BaseException:
        if socket is not None:
            await socket.close()
        raise


class WebSocketBridge(NativeBridge):
    route = "codex-ws"

    async def send(self, payload, headers):
        return await open_response(payload, headers)
