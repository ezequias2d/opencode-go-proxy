"""Account pool resolution, cooldown bookkeeping, and request failover.

The pool is resolved from ``OPENCODE_GO_API_KEYS``, then an accounts file, then
the existing single-credential path. These tests pin the precedence, the
masking contract (a raw key must never reach a snapshot, a CLI line, or the
log), the cooldown state machine, and the rotation at the connect phase of a
real request.
"""

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from opencode_go_proxy import accounts, ops
from opencode_go_proxy.accounts import (
    Account,
    clear_account_caches,
    note_failure,
    note_success,
    pool_snapshot,
    resolve_accounts,
    select_account,
    set_active_account,
)
from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.errors import ProxyError


def make_config(**overrides) -> ProxyConfig:
    values = {
        "bind": "127.0.0.1",
        "port": 8787,
        "chat_base_url": "https://up.test/v1",
        "api_key_env": "OPENCODE_GO_API_KEY",
        "timeout_sec": 2,
        "max_body_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return ProxyConfig(**values)


def write_accounts_file(path, accounts_list, active):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"version": 1, "active": active, "accounts": accounts_list}, handle
        )
    clear_account_caches()


def use_accounts_file(monkeypatch, path):
    monkeypatch.setenv(accounts.ACCOUNTS_FILE_ENV, str(path))
    clear_account_caches()
    return str(path)


class TestSources:
    def test_env_list_wins_over_file(self, monkeypatch, tmp_path):
        path = use_accounts_file(
            monkeypatch,
            tmp_path / "accounts.json",
        )
        write_accounts_file(
            path,
            [{"id": "file-a", "name": "file-a", "key": "file-key-a"}],
            "file-a",
        )
        monkeypatch.setenv(accounts.KEYS_ENV, "env-key-a,env-key-b")

        pool, source = resolve_accounts(make_config())

        assert source == "env"
        assert [a.key for a in pool] == ["env-key-a", "env-key-b"]

    def test_file_source_puts_active_first(self, monkeypatch, tmp_path):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        write_accounts_file(
            path,
            [
                {"id": "beta", "name": "beta", "key": "key-beta"},
                {"id": "alpha", "name": "alpha", "key": "key-alpha"},
            ],
            "alpha",
        )

        pool, source = resolve_accounts(make_config())

        assert source == "file"
        assert [a.id for a in pool] == ["alpha", "beta"]

    def test_single_source_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv(accounts.KEYS_ENV, raising=False)
        clear_account_caches()

        pool, source = resolve_accounts(make_config())

        assert source == "single"
        assert pool == []

    def test_dedupe_by_fingerprint_preserves_order(self, monkeypatch):
        monkeypatch.setenv(accounts.KEYS_ENV, "same-key,same-key,other-key")

        pool, source = resolve_accounts(make_config())

        assert source == "env"
        assert [a.key for a in pool] == ["same-key", "other-key"]

    def test_masked_never_contains_full_key(self):
        key = "sk-1234567890abcdefghij"
        account = Account(key, "id", "name")

        assert account.masked == "sk-123…ghij"
        assert key not in account.masked
        assert account.fingerprint == __import__("hashlib").sha256(
            key.encode()
        ).hexdigest()[:16]


class TestCooldowns:
    def _pool(self, monkeypatch):
        monkeypatch.setenv(accounts.KEYS_ENV, "key-one,key-two")
        pool, _source = resolve_accounts(make_config())
        return pool

    def test_429_uses_retry_after_seconds(self, monkeypatch):
        pool = self._pool(monkeypatch)
        note_failure(pool[0], 429, "42")

        state = accounts._read_state()
        entry = state["accounts"][pool[0].fingerprint]

        assert entry["lastStatus"] == 429
        assert entry["failures"] == 1
        assert entry["cooldownUntil"] >= time.time() + 40

    def test_401_uses_auth_cooldown(self, monkeypatch):
        monkeypatch.setenv(accounts.AUTH_COOLDOWN_ENV, "7")
        pool = self._pool(monkeypatch)
        note_failure(pool[0], 401, None)

        entry = accounts._read_state()["accounts"][pool[0].fingerprint]

        assert entry["cooldownUntil"] <= time.time() + 8

    def test_retry_after_http_date_is_parsed(self, monkeypatch):
        pool = self._pool(monkeypatch)
        future = time.time() + 30
        stamp = __import__("email.utils").utils.formatdate(future, usegmt=True)
        note_failure(pool[0], 429, stamp)

        entry = accounts._read_state()["accounts"][pool[0].fingerprint]

        assert 25 <= entry["cooldownUntil"] - time.time() <= 31

    def test_success_clears_state(self, monkeypatch):
        pool = self._pool(monkeypatch)
        note_failure(pool[0], 429, "60")
        note_success(pool[0])

        entry = accounts._read_state()["accounts"][pool[0].fingerprint]

        assert entry["failures"] == 0
        assert "cooldownUntil" not in entry

    def test_select_skips_cooling_account(self, monkeypatch):
        pool = self._pool(monkeypatch)
        note_failure(pool[0], 429, "60")

        chosen = select_account(make_config(), "req")

        assert chosen.key == "key-two"

    def test_select_falls_back_to_cooling_when_all_cooling(self, monkeypatch):
        pool = self._pool(monkeypatch)
        note_failure(pool[0], 429, "60")
        note_failure(pool[1], 429, "30")

        chosen = select_account(make_config(), "req")

        assert chosen.key == "key-two"

    def test_corrupt_state_file_is_tolerated(self, monkeypatch):
        pool = self._pool(monkeypatch)
        clear_account_caches()
        with open(accounts.state_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")

        note_failure(pool[0], 429, "5")
        entry = accounts._read_state()["accounts"][pool[0].fingerprint]

        assert entry["lastStatus"] == 429


class TestAccountsFile:
    def test_set_active_writes_only_active(self, monkeypatch, tmp_path):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        entries = [
            {"id": "alpha", "name": "alpha", "key": "key-alpha"},
            {"id": "beta", "name": "beta", "key": "key-beta"},
        ]
        write_accounts_file(path, entries, "alpha")

        account = set_active_account(make_config(), "beta")

        assert account.id == "beta"
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
        assert stored["active"] == "beta"
        assert stored["accounts"] == entries

    def test_set_active_rejects_unknown(self, monkeypatch, tmp_path):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        write_accounts_file(
            path, [{"id": "alpha", "name": "alpha", "key": "key-alpha"}], "alpha"
        )

        with pytest.raises(ProxyError):
            set_active_account(make_config(), "missing")

    def test_pool_cache_invalidated_by_rewrite(self, monkeypatch, tmp_path):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        write_accounts_file(
            path, [{"id": "alpha", "name": "alpha", "key": "key-one"}], "alpha"
        )
        assert [a.key for a in resolve_accounts(make_config())[0]] == ["key-one"]

        write_accounts_file(
            path, [{"id": "alpha", "name": "alpha", "key": "key-two-longer"}], "alpha"
        )
        os.utime(path, (time.time() + 5, time.time() + 5))

        assert [a.key for a in resolve_accounts(make_config())[0]] == ["key-two-longer"]


class TestSnapshot:
    def test_snapshot_file_source_is_masked(self, monkeypatch, tmp_path):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        write_accounts_file(
            path,
            [{"id": "alpha", "name": "alpha", "key": "sk-secret-value-1234"}],
            "alpha",
        )

        snapshot = pool_snapshot(make_config())
        rendered = json.dumps(snapshot)

        assert snapshot["source"] == "file"
        assert snapshot["active"]["id"] == "alpha"
        assert snapshot["active"]["masked"] == "sk-sec…1234"
        assert "sk-secret-value-1234" not in rendered
        assert snapshot["pool"][0]["eligible"] is True

    def test_snapshot_single_source(self, monkeypatch):
        monkeypatch.delenv(accounts.KEYS_ENV, raising=False)
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-single-key-5678")
        from opencode_go_proxy.secrets import clear_api_key_cache

        clear_api_key_cache()

        snapshot = pool_snapshot(make_config())

        assert snapshot["source"] == "single"
        assert snapshot["active"]["id"] == "single"
        assert json.dumps(snapshot).count("sk-single-key-5678") == 0


class _FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.headers_list: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers_list.append((name, value))

    def end_headers(self):
        pass

    def flush(self):
        pass


class _JsonResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.headers = {"content-type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _ExplodingStream:
    status = 200

    def __init__(self):
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n'
        raise OSError("connection reset")


def _bearer_key(request: urllib.request.Request) -> str:
    value = request.get_header("Authorization") or ""
    return value.removeprefix("Bearer ")


def _ok_chat_body(text: str = "ok") -> bytes:
    return json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
    ).encode()


def _http_error(url: str, status: int, body: bytes, retry_after: str | None = None):
    headers = {"retry-after": retry_after} if retry_after else {}
    return urllib.error.HTTPError(url, status, "upstream error", headers, io.BytesIO(body))


class TestFailover:
    def test_nonstream_429_then_200_uses_two_attempts(self, monkeypatch):
        from opencode_go_proxy.go_upstream import handle_go_chat_request

        monkeypatch.setenv(accounts.KEYS_ENV, "key-one,key-two")
        monkeypatch.setenv("OPENCODE_GO_PROXY_MAX_RETRIES", "0")
        monkeypatch.setenv(accounts.COOLDOWN_ENV, "60")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            key = _bearer_key(request)
            calls.append(key)
            if key == "key-one":
                raise _http_error(request.full_url, 429, b'{"error":"one"}', "3")
            return _JsonResponse(200, _ok_chat_body())

        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handle_go_chat_request(
                handler,
                {
                    "model": "opencode-go/deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
                make_config(),
                "req",
            )

        assert handler.status == 200
        assert calls == ["key-one", "key-two"]

    def test_pool_exhausted_relays_last_429_verbatim(self, monkeypatch):
        from opencode_go_proxy.go_upstream import handle_go_chat_request

        monkeypatch.setenv(accounts.KEYS_ENV, "key-one,key-two")
        monkeypatch.setenv("OPENCODE_GO_PROXY_MAX_RETRIES", "0")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            key = _bearer_key(request)
            calls.append(key)
            raise _http_error(
                request.full_url,
                429,
                json.dumps({"error": key}).encode(),
                "9",
            )

        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handle_go_chat_request(
                handler,
                {
                    "model": "opencode-go/deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
                make_config(),
                "req",
            )

        assert calls == ["key-one", "key-two"]
        assert handler.status == 429
        assert b'"key-two"' in handler.wfile.getvalue()
        assert ("retry-after", "9") in handler.headers_list

    def test_failover_disabled_does_not_rotate(self, monkeypatch):
        from opencode_go_proxy.go_upstream import handle_go_chat_request

        monkeypatch.setenv(accounts.KEYS_ENV, "key-one,key-two")
        monkeypatch.setenv(accounts.FAILOVER_ENV, "0")
        monkeypatch.setenv("OPENCODE_GO_PROXY_MAX_RETRIES", "0")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            calls.append(_bearer_key(request))
            raise _http_error(request.full_url, 429, b'{"error":"one"}', "1")

        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handle_go_chat_request(
                handler,
                {
                    "model": "opencode-go/deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
                make_config(),
                "req",
            )

        assert calls == ["key-one"]
        assert handler.status == 429

    def test_mid_stream_failure_does_not_rotate(self, monkeypatch):
        from opencode_go_proxy.streaming import handle_streaming_request

        monkeypatch.setenv(accounts.KEYS_ENV, "key-one,key-two")
        monkeypatch.setenv("OPENCODE_GO_PROXY_MAX_RETRIES", "0")
        monkeypatch.setenv("OPENCODE_GO_PROXY_KEEPALIVE_SEC", "60")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            calls.append(_bearer_key(request))
            return _ExplodingStream()

        wfile = io.BytesIO()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handle_streaming_request(
                {"model": "deepseek-v4-flash", "input": "hi", "stream": True},
                make_config(),
                "req",
                wfile,
            )

        assert calls == ["key-one"]
        assert b"response.output_text.delta" in wfile.getvalue()

    def test_go_responses_nonstream_rotates(self, monkeypatch):
        from opencode_go_proxy.go_upstream import handle_go_responses_request

        monkeypatch.setenv(accounts.KEYS_ENV, "key-one,key-two")
        monkeypatch.setenv("OPENCODE_GO_PROXY_MAX_RETRIES", "0")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            key = _bearer_key(request)
            calls.append(key)
            if key == "key-one":
                raise _http_error(request.full_url, 429, b'{"error":"one"}', "1")
            return _JsonResponse(200, _ok_chat_body())

        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handle_go_responses_request(
                handler,
                {"model": "opencode-go/deepseek-v4-flash", "input": "hi", "stream": False},
                make_config(),
                "req",
            )

        assert handler.status == 200
        assert calls == ["key-one", "key-two"]

    def test_zen_chat_nonstream_rotates(self, monkeypatch):
        from opencode_go_proxy.zen_upstream import handle_zen_chat_request

        monkeypatch.setenv(accounts.KEYS_ENV, "key-one,key-two")
        monkeypatch.setenv("OPENCODE_GO_PROXY_MAX_RETRIES", "0")
        monkeypatch.setenv("OPENCODE_ZEN_BASE_URL", "https://zen.test/v1")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            key = _bearer_key(request)
            calls.append(key)
            if key == "key-one":
                raise _http_error(request.full_url, 429, b'{"error":"one"}', "1")
            return _JsonResponse(200, _ok_chat_body("zen ok"))

        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handle_zen_chat_request(
                handler,
                {"model": "zen/gemini-3-pro", "messages": [], "stream": False},
                make_config(),
                "req",
            )

        assert handler.status == 200
        assert calls == ["key-one", "key-two"]


class _ScratchServer(ThreadingHTTPServer):
    daemon_threads = True
    config: ProxyConfig


def _start_proxy(config: ProxyConfig):
    server = _ScratchServer(("127.0.0.1", 0), ResponsesProxyHandler)
    server.config = config
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


class TestAccountsHttp:
    def _get(self, port: int, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_accounts_route_masks_file_pool(self, monkeypatch, tmp_path):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        write_accounts_file(
            path,
            [{"id": "alpha", "name": "alpha", "key": "sk-raw-secret-9999"}],
            "alpha",
        )
        server, port = _start_proxy(make_config())
        try:
            status, body = self._get(port, "/accounts")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        payload = json.loads(body)
        assert payload["source"] == "file"
        assert payload["active"]["masked"] == "sk-raw…9999"
        assert "sk-raw-secret-9999" not in body

        server, port = _start_proxy(make_config())
        try:
            status, body = self._get(port, "/v1/accounts")
        finally:
            server.shutdown()
            server.server_close()
        assert status == 200
        assert json.loads(body)["source"] == "file"

    def test_accounts_route_single_source_masked(self, monkeypatch):
        from opencode_go_proxy.secrets import clear_api_key_cache

        monkeypatch.delenv(accounts.KEYS_ENV, raising=False)
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-only-key-4242")
        clear_api_key_cache()
        server, port = _start_proxy(make_config())
        try:
            status, body = self._get(port, "/accounts")
        finally:
            server.shutdown()
            server.server_close()

        assert status == 200
        payload = json.loads(body)
        assert payload["source"] == "single"
        assert payload["pool"][0]["id"] == "single"
        assert "sk-only-key-4242" not in body
        assert payload["active"]["masked"] == "sk-onl…4242"


class TestCli:
    def test_list_prints_masked_pool(self, monkeypatch, tmp_path, capsys):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        write_accounts_file(
            path,
            [
                {"id": "alpha", "name": "alpha", "key": "sk-alpha-key-1111"},
                {"id": "beta", "name": "beta", "key": "sk-beta-key-2222"},
            ],
            "alpha",
        )

        assert accounts.accounts_cmd(["list"]) == 0
        out = capsys.readouterr().out

        assert "source: file" in out
        assert "* alpha" in out
        assert "sk-alp…1111" in out
        assert "sk-alpha-key-1111" not in out

    def test_use_rewrites_active(self, monkeypatch, tmp_path, capsys):
        path = use_accounts_file(monkeypatch, tmp_path / "accounts.json")
        write_accounts_file(
            path,
            [
                {"id": "alpha", "name": "alpha", "key": "sk-alpha-key-1111"},
                {"id": "beta", "name": "beta", "key": "sk-beta-key-2222"},
            ],
            "alpha",
        )

        assert accounts.accounts_cmd(["use", "beta"]) == 0
        out = capsys.readouterr().out

        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
        assert stored["active"] == "beta"
        assert "sk-bet…2222" in out
        assert "sk-beta-key-2222" not in out

    def test_use_without_file_source_errors(self, monkeypatch, capsys):
        monkeypatch.delenv(accounts.ACCOUNTS_FILE_ENV, raising=False)
        clear_account_caches()

        assert accounts.accounts_cmd(["use", "beta"]) == 1
        assert "error:" in capsys.readouterr().err


class TestDoctor:
    def test_check_accounts_reports_pool(self, monkeypatch):
        monkeypatch.setenv(accounts.KEYS_ENV, "sk-doctor-key-aaaa,sk-doctor-key-bbbb")

        check = ops.check_accounts()

        assert check.status == "ok"
        assert "2 account(s) from env" in check.detail
        assert "sk-doctor-key-aaaa" not in check.detail

    def test_check_accounts_counts_cooling(self, monkeypatch):
        monkeypatch.setenv(accounts.KEYS_ENV, "sk-cool-key-aaaa,sk-cool-key-bbbb")
        pool, _source = resolve_accounts(make_config())
        note_failure(pool[0], 429, "120")

        check = ops.check_accounts()

        assert "1 cooling down" in check.detail
