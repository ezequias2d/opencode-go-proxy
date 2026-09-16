"""Stable OpenCode routing headers for proxied conversations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

OPENCODE_SESSION_HEADER = "x-opencode-session"
_SESSION_HEADER_ALIASES = (
    OPENCODE_SESSION_HEADER,
    "thread-id",
    "session-id",
    "session_id",
)
_METADATA_ID_KEYS = frozenset(
    {"conversationId", "conversation_id", "sessionId", "session_id", "threadId", "thread_id"}
)


def _header(headers: Mapping[str, Any] | None, name: str) -> str | None:
    if headers is None:
        return None
    for key, value in headers.items():
        if str(key).lower() == name:
            text = str(value).strip()
            return text or None
    return None


def _metadata_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key, item in value.items():
        if key in _METADATA_ID_KEYS and isinstance(item, str) and item.strip():
            return item.strip()
    for item in value.values():
        found = _metadata_id(item)
        if found:
            return found
    return None


def _conversation_anchor(payload: dict[str, Any]) -> str:
    input_value = payload.get("input")
    if not isinstance(input_value, list):
        input_value = [input_value] if input_value is not None else []
    anchors: list[Any] = []
    for item in input_value:
        if isinstance(item, dict) and item.get("type") in {
            "compaction_trigger",
            "context_compaction",
        }:
            continue
        anchors.append(item)
        if len(anchors) == 2:
            break
    source = anchors or [payload.get("instructions", "")]
    return json.dumps(source, sort_keys=True, separators=(",", ":"), default=str)


def resolve_opencode_session(
    headers: Mapping[str, Any] | None,
    payload: dict[str, Any],
) -> str:
    """Preserve a client session identifier or derive a stable conversation ID."""
    for name in _SESSION_HEADER_ALIASES:
        value = _header(headers, name)
        if value:
            return value
    metadata = _header(headers, "x-codex-turn-metadata")
    if metadata:
        try:
            value = _metadata_id(json.loads(metadata))
        except json.JSONDecodeError:
            value = None
        if value:
            return value
    digest = hashlib.sha256(_conversation_anchor(payload).encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=digest))


def opencode_session_headers(
    headers: Mapping[str, Any] | None,
    payload: dict[str, Any],
) -> dict[str, str]:
    """Return the one stable affinity header OpenCode expects."""
    return {OPENCODE_SESSION_HEADER: resolve_opencode_session(headers, payload)}
