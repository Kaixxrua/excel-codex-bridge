"""Responses item-id helper (from ghcp_proxy's responses_replay_ids, Unlicense)."""

from __future__ import annotations

import hashlib

_MAX_RESPONSES_ITEM_ID_LENGTH = 64


def function_item_id(call_id: str) -> str:
    """Return a stable function item id within the 64-character item-id limit."""
    candidate = f"fc_{call_id}"
    if len(candidate) <= _MAX_RESPONSES_ITEM_ID_LENGTH:
        return candidate
    digest_length = _MAX_RESPONSES_ITEM_ID_LENGTH - len("fc_")
    return f"fc_{hashlib.sha256(call_id.encode('utf-8')).hexdigest()[:digest_length]}"
