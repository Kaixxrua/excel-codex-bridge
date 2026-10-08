"""Route selection is explicit and stays fixed for the server's lifetime."""

import os

ENV = "EXCEL_BRIDGE_ROUTE"
CHOICES = ("codex", "codex-ws", "excel")
NATIVE = ("codex", "codex-ws")


def default() -> str:
    value = os.environ.get(ENV, "codex").strip().lower()
    if value not in CHOICES:
        raise ValueError(f"{ENV} must be codex, codex-ws or excel")
    return value


def selected(args) -> str:
    # Older Python callers may construct a Namespace directly. CLI parsers
    # always supply the route; keep the old internal API's Excel contract.
    return getattr(args, "route", "excel")


def create_bridge(reader, client_factory, route: str):
    if route == "codex-ws":
        from .ws_upstream import WebSocketBridge
        return WebSocketBridge(reader, client_factory)
    if route == "codex":
        from .native_upstream import NativeBridge
        return NativeBridge(reader, client_factory)
    if route == "excel":
        from .server import Bridge
        return Bridge(reader, client_factory)
    raise ValueError("route must be codex, codex-ws or excel")
