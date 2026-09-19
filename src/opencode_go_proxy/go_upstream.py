"""Family-aware OpenCode Go upstream dispatch."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from .accounts import connect_failed_rotatable, connect_with_failover
from .config import ProxyConfig
from .errors import ProxyError
from .go_models import ANTHROPIC_MESSAGES, OPENAI_CHAT, go_family_for
from .meter import record_usage_event
from .opencode_session import opencode_session_headers
from .passthrough import _relay_stream
from .protocol import DEFAULT_MODEL
from .secrets import resolve_api_key
from .streaming import _ConnectFailed, _open_upstream_stream
from .trace import trace
from .upstream import default_max_retries, retriable_http_status, retry_sleep
from .upstream_headers import upstream_user_agent
from .zen_upstream import (
    ANTHROPIC_VERSION,
    _relay_upstream_error,
    handle_family_responses_request,
)

Json = dict[str, Any]

GO_PREFIX = "opencode-go/"
GO_PROVIDER = "opencode-go"


def _client_status(status: int) -> int:
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return HTTPStatus.BAD_GATEWAY
    return status


def _network_error(exc: BaseException, retries: int) -> ProxyError:
    if isinstance(exc, TimeoutError) or (
        isinstance(exc, urllib.error.URLError)
        and isinstance(exc.reason, TimeoutError)
    ):
        return ProxyError(
            HTTPStatus.GATEWAY_TIMEOUT,
            "opencode-go upstream timeout",
            retries=retries,
        )
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return ProxyError(
        HTTPStatus.BAD_GATEWAY,
        f"opencode-go upstream network error: {reason}",
        retries=retries,
    )


def bare_go_id(slug: str) -> str:
    """Strip the explicit Go provider prefix at the wire boundary."""
    return slug.removeprefix(GO_PREFIX)


def go_request_identity(model: str) -> tuple[str, str]:
    """Return the bare id and certified family for one selected Go model."""
    bare_id = bare_go_id(model)
    try:
        return bare_id, go_family_for(bare_id)
    except KeyError as exc:
        raise ProxyError(
            HTTPStatus.BAD_REQUEST,
            f"OpenCode Go model {model!r} has no certified protocol family",
            error_type="model_not_found",
        ) from exc


def _resolve_go_key(
    config: ProxyConfig,
    request_id: str,
    model: str,
    started: float,
) -> str:
    try:
        return resolve_api_key(config, request_id)
    except ProxyError as exc:
        record_usage_event(
            model=model,
            status=int(exc.status),
            duration_ms=int((time.time() - started) * 1000),
        )
        raise


def handle_go_responses_request(
    handler: Any,
    payload: Json,
    config: ProxyConfig,
    request_id: str,
) -> None:
    """Translate a local Responses request to its documented Go endpoint."""
    started = time.time()
    model = payload.get("model") or DEFAULT_MODEL
    bare_id, family = go_request_identity(model)
    handle_family_responses_request(
        handler,
        payload,
        config,
        request_id,
        model=model,
        bare_id=bare_id,
        family=family,
        single_key=lambda config_value, rid: _resolve_go_key(
            config_value, rid, model, started
        ),
        base_url=config.chat_base_url,
        provider=GO_PROVIDER,
        extra_headers=opencode_session_headers(handler.headers, payload),
        function_tools_only=True,
        restore_namespaces=True,
        caption_images=True,
    )


def _handle_go_verbatim_request(
    handler: Any,
    payload: Json,
    config: ProxyConfig,
    request_id: str,
    *,
    family: str,
    endpoint: str,
    auth_headers_for: Callable[[str], dict[str, str]],
) -> None:
    started = time.time()
    model = str(payload["model"])
    bare_id = bare_go_id(model)
    body = dict(payload)
    body["model"] = bare_id
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    stream = payload.get("stream") is True
    url = f"{config.chat_base_url}{endpoint}"
    session_headers = opencode_session_headers(handler.headers, payload)

    def _build(key: str) -> urllib.request.Request:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "user-agent": upstream_user_agent(),
            **session_headers,
            **auth_headers_for(key),
        }
        return urllib.request.Request(url, data=raw, headers=headers, method="POST")

    def _resolve_single(config_value: ProxyConfig, rid: str) -> str:
        return _resolve_go_key(config_value, rid, model, started)

    trace(
        "opencode-go.start",
        request_id=request_id,
        url=url,
        bytes=len(raw),
        family=family,
        stream=stream,
    )
    if stream:
        try:
            response, retries = connect_with_failover(
                config,
                request_id,
                connect=lambda key: _open_upstream_stream(
                    _build(key), config, request_id, default_max_retries()
                ),
                rotatable=connect_failed_rotatable,
                resolve_single=_resolve_single,
            )
        except _ConnectFailed as fail:
            exc = fail.exc
            if isinstance(exc, urllib.error.HTTPError):
                status = _client_status(exc.code)
                _relay_upstream_error(handler, status, fail.body, exc.headers)
                record_usage_event(
                    model=model,
                    status=status,
                    duration_ms=int((time.time() - started) * 1000),
                    retries=fail.attempts or None,
                )
                return
            raise _network_error(exc, fail.attempts) from exc
        handler.send_response(HTTPStatus.OK)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.end_headers()
        outcome = _relay_stream(response, handler, request_id)
        record_usage_event(
            model=model,
            status=200 if outcome == "done" else 0 if outcome == "gone" else 502,
            duration_ms=int((time.time() - started) * 1000),
            stream_aborted=outcome != "done",
            retries=retries or None,
        )
        return

    max_retries = default_max_retries()

    def _post(key: str) -> tuple[bytes, int, str, int]:
        """One account's non-stream attempt with the inner transient retry."""
        retries = 0
        while True:
            req = _build(key)
            try:
                with urllib.request.urlopen(req, timeout=config.timeout_sec) as response:
                    return (
                        response.read(),
                        response.status,
                        response.headers.get("content-type", "application/json"),
                        retries,
                    )
            except urllib.error.HTTPError as exc:
                response_body = exc.read()
                status = exc.code
                if retriable_http_status(status) and retries < max_retries:
                    retries += 1
                    retry_sleep(retries)
                    continue
                raise _ConnectFailed(exc, retries, response_body) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if retries < max_retries:
                    retries += 1
                    retry_sleep(retries)
                    continue
                raise _ConnectFailed(exc, retries) from exc

    retry_after = None
    try:
        response_body, status, content_type, retries = connect_with_failover(
            config,
            request_id,
            connect=_post,
            rotatable=connect_failed_rotatable,
            resolve_single=_resolve_single,
        )
    except _ConnectFailed as fail:
        exc = fail.exc
        if not isinstance(exc, urllib.error.HTTPError):
            raise _network_error(exc, fail.attempts) from exc
        response_body = fail.body
        status = exc.code
        content_type = exc.headers.get("content-type", "application/json")
        retry_after = exc.headers.get("retry-after") if exc.headers else None
        retries = fail.attempts
    client_status = _client_status(status)
    record_usage_event(
        model=model,
        status=client_status,
        duration_ms=int((time.time() - started) * 1000),
        retries=retries or None,
    )
    handler.send_response(client_status)
    handler.send_header("content-type", content_type)
    if retry_after:
        handler.send_header("retry-after", retry_after)
    handler.send_header("content-length", str(len(response_body)))
    handler.end_headers()
    handler.wfile.write(response_body)
    handler.wfile.flush()


def handle_go_chat_request(
    handler: Any,
    payload: Json,
    config: ProxyConfig,
    request_id: str,
) -> None:
    """Relay the local Chat Completions surface to a Go Chat model."""
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ProxyError(
            HTTPStatus.BAD_REQUEST,
            "model must be a non-empty string",
            error_type="invalid_request_error",
        )
    _, family = go_request_identity(model)
    if family != OPENAI_CHAT:
        expected = "/messages" if family == ANTHROPIC_MESSAGES else "/responses"
        raise ProxyError(
            HTTPStatus.BAD_REQUEST,
            f"OpenCode Go model {model!r} uses {expected}, not /chat/completions",
            error_type="invalid_request_error",
        )
    _handle_go_verbatim_request(
        handler,
        payload,
        config,
        request_id,
        family=family,
        endpoint="/chat/completions",
        auth_headers_for=lambda key: {"authorization": f"Bearer {key}"},
    )


def handle_go_messages_request(
    handler: Any,
    payload: Json,
    config: ProxyConfig,
    request_id: str,
) -> None:
    """Relay the local Anthropic Messages surface to a Go Messages model."""
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ProxyError(
            HTTPStatus.BAD_REQUEST,
            "model must be a non-empty string",
            error_type="invalid_request_error",
        )
    _, family = go_request_identity(model)
    if family != ANTHROPIC_MESSAGES:
        expected = "/chat/completions" if family == OPENAI_CHAT else "/responses"
        raise ProxyError(
            HTTPStatus.BAD_REQUEST,
            f"OpenCode Go model {model!r} uses {expected}, not /messages",
            error_type="invalid_request_error",
        )
    anthropic_version = (
        handler.headers.get("anthropic-version") or ANTHROPIC_VERSION
    )
    _handle_go_verbatim_request(
        handler,
        payload,
        config,
        request_id,
        family=family,
        endpoint="/messages",
        auth_headers_for=lambda key: {
            "x-api-key": key,
            "anthropic-version": anthropic_version,
        },
    )
