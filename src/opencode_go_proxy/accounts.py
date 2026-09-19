"""Account pool and automatic failover for OpenCode Go credentials.

The proxy normally resolves exactly one credential per process, so a user with
several OpenCode Go subscriptions must hand-edit an env var and restart to
switch. This module adds a key pool that fails over automatically when a
credential is rejected (401/403) or rate limited (429), before any byte of the
upstream response is committed to the client. Cooldown bookkeeping is
best-effort like the usage meter: a corrupt or unwritable state file never
fails a live request, and a single-credential install keeps the existing
no-bookkeeping behaviour.

Pool precedence (first source with a usable key wins, de-duplicated by key
fingerprint, order preserved):

1. ``OPENCODE_GO_API_KEYS`` — comma- or newline-separated list.
2. The accounts file (``OPENCODE_GO_PROXY_ACCOUNTS_FILE``, default
   ``~/.config/opencode/opencode-go/accounts.json``), with the ``active``
   account first.
3. The single-credential resolution in :mod:`secrets` — a pool of one with
   ``id == name == "single"``.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, TypeVar

from .config import ProxyConfig
from .errors import ProxyError
from .meter import state_dir
from .trace import trace

T = TypeVar("T")

KEYS_ENV = "OPENCODE_GO_API_KEYS"
ACCOUNTS_FILE_ENV = "OPENCODE_GO_PROXY_ACCOUNTS_FILE"
DEFAULT_ACCOUNTS_FILE = "~/.config/opencode/opencode-go/accounts.json"
COOLDOWN_ENV = "OPENCODE_GO_PROXY_ACCOUNT_COOLDOWN_SEC"
AUTH_COOLDOWN_ENV = "OPENCODE_GO_PROXY_ACCOUNT_AUTH_COOLDOWN_SEC"
FAILOVER_ENV = "OPENCODE_GO_PROXY_KEY_FAILOVER"

DEFAULT_COOLDOWN_SEC = 300
DEFAULT_AUTH_COOLDOWN_SEC = 900

_ROTATABLE_STATUSES = frozenset({401, 403, 429})

_json = dict[str, Any]


@dataclass(frozen=True)
class Account:
    """One credential: the secret key plus its display identity."""

    key: str  # secret, never logged or serialized
    id: str
    name: str

    @property
    def fingerprint(self) -> str:
        """A stable, non-secret identifier for state and de-duplication."""
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:16]

    @property
    def masked(self) -> str:
        """First 6 + last 4 characters; the full key never leaves this class."""
        if len(self.key) <= 10:
            return "…"
        return f"{self.key[:6]}…{self.key[-4:]}"


class AccountRejected(Exception):
    """A terminal upstream HTTP rejection, carrying enough to relay verbatim.

    Raised by verbatim connect closures so the failover loop can rotate on
    401/403/429 while still relaying the final upstream status and body when
    the pool is exhausted.
    """

    def __init__(
        self,
        status: int,
        retry_after: str | None = None,
        *,
        body: bytes = b"",
        headers: Any = None,
        content_type: str | None = None,
        retries: int = 0,
    ) -> None:
        super().__init__(f"upstream HTTP {status}")
        self.status = status
        self.retry_after = retry_after
        self.body = body
        self.headers = headers
        self.content_type = content_type
        self.retries = retries


def accounts_file_path() -> str:
    return os.path.expanduser(os.environ.get(ACCOUNTS_FILE_ENV) or DEFAULT_ACCOUNTS_FILE)


def failover_enabled() -> bool:
    """Rotation is on unless ``OPENCODE_GO_PROXY_KEY_FAILOVER=0``."""
    return os.environ.get(FAILOVER_ENV, "1") != "0"


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _dedupe(accounts: list[Account]) -> list[Account]:
    """Drop duplicate keys, preserving first-seen order."""
    seen: set[str] = set()
    result: list[Account] = []
    for account in accounts:
        if account.fingerprint in seen:
            continue
        seen.add(account.fingerprint)
        result.append(account)
    return result


def _env_accounts() -> list[Account]:
    raw = os.environ.get(KEYS_ENV, "")
    parts = [part.strip() for part in re.split(r"[,\n]", raw) if part.strip()]
    return _dedupe([Account(part, f"env:{i}", f"env:{i}") for i, part in enumerate(parts)])


# (stat fingerprint, parsed dict or None) memo for the accounts file, so the
# pool re-reads only when the file's mtime/inode changes (a switch via
# ``accounts use`` takes effect without a restart).
_file_cache: tuple[tuple[int, int, int, int], _json | None] | None = None


def _accounts_file_data() -> _json | None:
    global _file_cache
    path = accounts_file_path()
    try:
        stat = os.stat(path)
    except OSError:
        _file_cache = None
        return None
    fingerprint = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if _file_cache is not None and _file_cache[0] == fingerprint:
        return _file_cache[1]
    data: _json | None = None
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            data = value
    except (OSError, ValueError):
        data = None
    _file_cache = (fingerprint, data)
    return data


def _file_accounts() -> list[Account]:
    data = _accounts_file_data()
    if not data:
        return []
    entries = data.get("accounts")
    if not isinstance(entries, list):
        return []
    accounts: list[Account] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        account_id = str(entry.get("id") or "").strip()
        if not account_id:
            continue
        name = str(entry.get("name") or account_id)
        accounts.append(Account(key.strip(), account_id, name))
    active = data.get("active")
    ordered = [a for a in accounts if a.id == active] + [a for a in accounts if a.id != active]
    return _dedupe(ordered)


def resolve_accounts(config: ProxyConfig) -> tuple[list[Account], str]:
    """Resolve the pool and its source: env, file, or single.

    ``single`` returns an empty pool; the key is resolved lazily by the caller
    (via :func:`select_account` or the failover helper) so the single-credential
    path keeps its existing resolution order and error surface.
    """
    env_accounts = _env_accounts()
    if env_accounts:
        return env_accounts, "env"
    file_accounts = _file_accounts()
    if file_accounts:
        return file_accounts, "file"
    return [], "single"


def _single_account(config: ProxyConfig, request_id: str) -> Account:
    from .secrets import resolve_api_key

    return Account(resolve_api_key(config, request_id), "single", "single")


# (stat fingerprint, state dict) memo for accounts-state.json.
_state_cache: tuple[tuple[int, int, int, int], _json] | None = None


def state_path() -> str:
    return os.path.join(state_dir(), "accounts-state.json")


def _read_state() -> _json:
    global _state_cache
    path = state_path()
    try:
        stat = os.stat(path)
    except OSError:
        _state_cache = None
        return {"accounts": {}}
    fingerprint = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if _state_cache is not None and _state_cache[0] == fingerprint:
        return _state_cache[1]
    result: _json = {"accounts": {}}
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict) and isinstance(value.get("accounts"), dict):
            result = {"accounts": value["accounts"]}
    except (OSError, ValueError):
        result = {"accounts": {}}
    _state_cache = (fingerprint, result)
    return result


def _write_state(state: _json) -> None:
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError:
        # Cooldown bookkeeping is best-effort; never break a live request.
        pass


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After seconds (number or HTTP date), or None when unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if number >= 0 else None
    except ValueError:
        pass
    try:
        instant = email.utils.parsedate_to_datetime(text)
        return max(0.0, instant.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _cooldown_sec(status: int, retry_after: str | None) -> float:
    if status in (401, 403):
        return float(_env_int(AUTH_COOLDOWN_ENV, DEFAULT_AUTH_COOLDOWN_SEC))
    parsed = _parse_retry_after(retry_after)
    if parsed is not None:
        return parsed
    return float(_env_int(COOLDOWN_ENV, DEFAULT_COOLDOWN_SEC))


def _pool_order(config: ProxyConfig, request_id: str) -> list[Account]:
    """Failover order: eligible accounts first, then cooling by soonest end."""
    pool, source = resolve_accounts(config)
    if source == "single":
        return [_single_account(config, request_id)]
    state = _read_state()
    now = time.time()

    def cooldown_until(account: Account) -> float | None:
        entry = state["accounts"].get(account.fingerprint)
        if not isinstance(entry, dict):
            return None
        until = entry.get("cooldownUntil")
        if isinstance(until, (int, float)) and until > now:
            return float(until)
        return None

    eligible = [a for a in pool if cooldown_until(a) is None]
    cooling = [a for a in pool if cooldown_until(a) is not None]
    cooling.sort(key=lambda a: cooldown_until(a) or 0.0)
    return eligible + cooling


def select_account(
    config: ProxyConfig, request_id: str, *, allow_cooldown: bool = False
) -> Account:
    """Return the account a request should use.

    ``allow_cooldown=False`` (default) returns the first eligible account; when
    every account is cooling down it returns the one whose cooldown ends first
    instead of failing closed. ``allow_cooldown=True`` returns the raw first
    account (the file's active one) regardless of cooldown.
    """
    pool, source = resolve_accounts(config)
    if source == "single":
        return _single_account(config, request_id)
    account = pool[0] if allow_cooldown else _pool_order(config, request_id)[0]
    trace("credential.source", request_id=request_id, source=source, account=account.masked)
    return account


def note_success(account: Account) -> None:
    """Clear failures/cooldown for one fingerprint after a successful turn."""
    if not account.key:
        return
    state = _read_state()
    if not isinstance(state["accounts"].get(account.fingerprint), dict):
        return
    accounts = dict(state["accounts"])
    entry = dict(accounts[account.fingerprint])
    entry["failures"] = 0
    entry.pop("cooldownUntil", None)
    entry["sampledAt"] = time.time()
    accounts[account.fingerprint] = entry
    _write_state({"accounts": accounts})


def note_failure(account: Account, status: int, retry_after: str | None) -> None:
    """Record one rejection: bump failures and set the cooldown deadline."""
    if not account.key:
        return
    state = _read_state()
    accounts = dict(state["accounts"])
    existing = accounts.get(account.fingerprint)
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry["failures"] = int(entry.get("failures", 0) or 0) + 1
    entry["lastStatus"] = int(status)
    entry["sampledAt"] = time.time()
    entry["cooldownUntil"] = time.time() + _cooldown_sec(status, retry_after)
    accounts[account.fingerprint] = entry
    _write_state({"accounts": accounts})


def pool_snapshot(config: ProxyConfig) -> _json:
    """Masked, JSON-safe pool view for GET /accounts (never the raw key)."""
    pool, source = resolve_accounts(config)
    if source == "single":
        try:
            account = _single_account(config, "accounts-snapshot")
        except ProxyError:
            account = Account("", "single", "single")
        masked = account.masked if account.key else "unset"
        return {
            "source": "single",
            "active": {"id": "single", "name": "single", "masked": masked},
            "pool": [
                {
                    "id": "single",
                    "name": "single",
                    "masked": masked,
                    "eligible": True,
                    "cooldownUntil": None,
                    "lastStatus": None,
                    "failures": 0,
                }
            ],
        }
    state = _read_state()
    now = time.time()
    active = pool[0]

    def entry_for(account: Account) -> _json:
        raw_entry = state["accounts"].get(account.fingerprint)
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        until = entry.get("cooldownUntil")
        eligible = not (isinstance(until, (int, float)) and until > now)
        return {
            "id": account.id,
            "name": account.name,
            "masked": account.masked,
            "eligible": eligible,
            "cooldownUntil": until if isinstance(until, (int, float)) else None,
            "lastStatus": entry.get("lastStatus"),
            "failures": entry.get("failures", 0),
        }

    return {
        "source": source,
        "active": {"id": active.id, "name": active.name, "masked": active.masked},
        "pool": [entry_for(account) for account in pool],
    }


def _atomic_write_json(path: str, data: _json) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def set_active_account(config: ProxyConfig, needle: str) -> Account:
    """Rewrite the accounts file's ``active`` field; file source only."""
    global _file_cache
    data = _accounts_file_data()
    if not data or not isinstance(data.get("accounts"), list) or not data["accounts"]:
        raise ProxyError(
            HTTPStatus.BAD_REQUEST,
            "no accounts file configured; set OPENCODE_GO_PROXY_ACCOUNTS_FILE or add an accounts file",
        )
    match: _json | None = None
    for entry in data["accounts"]:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == needle or entry.get("name") == needle:
            match = entry
            break
    if match is None:
        raise ProxyError(HTTPStatus.BAD_REQUEST, f"no account matches {needle!r}")
    data["active"] = match["id"]
    _atomic_write_json(accounts_file_path(), data)
    _file_cache = None
    return Account(
        str(match.get("key") or ""),
        str(match.get("id") or ""),
        str(match.get("name") or match.get("id") or ""),
    )


def proxy_error_rotatable(exc: BaseException) -> tuple[int, str | None] | None:
    """Rotatable status for a :class:`ProxyError` (401/403/429), else None."""
    if isinstance(exc, ProxyError):
        status = exc.upstream_status if exc.upstream_status is not None else int(exc.status)
        if status in _ROTATABLE_STATUSES:
            return status, (exc.headers or {}).get("retry-after")
    return None


def account_rejected_rotatable(exc: BaseException) -> tuple[int, str | None] | None:
    if isinstance(exc, AccountRejected) and exc.status in _ROTATABLE_STATUSES:
        return exc.status, exc.retry_after
    return None


def connect_failed_rotatable(exc: BaseException) -> tuple[int, str | None] | None:
    """Rotatable status for a :class:`streaming._ConnectFailed`, else None."""
    inner = getattr(exc, "exc", None)
    if isinstance(inner, urllib.error.HTTPError) and inner.code in _ROTATABLE_STATUSES:
        return inner.code, (inner.headers or {}).get("retry-after")
    return None


def any_rotatable(exc: BaseException) -> tuple[int, str | None] | None:
    """Rotatable status from any failure shape the call sites raise.

    A single predicate for :func:`connect_with_failover` so a connect closure
    may raise ``AccountRejected``, a :class:`ProxyError` (the ``_zen_post``
    family), or a :class:`streaming._ConnectFailed` without the caller having
    to know which shape it produced.
    """
    return (
        account_rejected_rotatable(exc)
        or proxy_error_rotatable(exc)
        or connect_failed_rotatable(exc)
    )


def connect_with_failover(
    config: ProxyConfig,
    request_id: str,
    *,
    connect: Callable[[str], T],
    rotatable: Callable[[BaseException], tuple[int, str | None] | None],
    resolve_single: Callable[[ProxyConfig, str], str],
) -> T:
    """Run the account failover outer loop around a connect closure.

    ``connect(key)`` performs one connection attempt (including the inner
    transient retry) and returns the success value or raises a terminal
    exception; ``rotatable(exc)`` reports a 401/403/429 rejection as
    ``(status, retry_after)`` so the loop rotates to the next account, or None
    to re-raise. Rotation is bounded to one attempt per account; a single
    credential or disabled failover keeps today's behaviour. The final
    rejection is re-raised so the caller relays it exactly as before.
    """
    pool, source = resolve_accounts(config)
    if source == "single":
        return connect(resolve_single(config, request_id))
    if not failover_enabled() or len(pool) <= 1:
        return connect(pool[0].key)

    order = _pool_order(config, request_id)
    last: BaseException | None = None
    for index, account in enumerate(order):
        try:
            result = connect(account.key)
        except BaseException as exc:
            status_retry = rotatable(exc)
            if status_retry is None:
                raise
            status, retry_after = status_retry
            note_failure(account, status, retry_after)
            nxt = order[index + 1] if index + 1 < len(order) else None
            trace(
                "credential.rotate",
                request_id=request_id,
                **{
                    "from": account.masked,
                    "to": nxt.masked if nxt is not None else None,
                    "reason": f"HTTP {status}",
                    "status": status,
                },
            )
            last = exc
            continue
        note_success(account)
        return result
    assert last is not None
    raise last


def clear_account_caches() -> None:
    """Drop the memoized accounts-file and state reads.

    Tests point the accounts file and state dir at a scratch path per case; the
    stat-fingerprint memos would otherwise serve one case's file to the next.
    """
    global _file_cache, _state_cache
    _file_cache = None
    _state_cache = None


def _cmd_list(config: ProxyConfig) -> int:
    snapshot = pool_snapshot(config)
    active_id = snapshot["active"]["id"]
    print(f"source: {snapshot['source']}")
    for entry in snapshot["pool"]:
        marker = "*" if entry["id"] == active_id else " "
        status = "cooling down" if not entry["eligible"] else "ready"
        print(f"{marker} {entry['id']}\t{entry['name']}\t{entry['masked']}\t{status}")
    return 0


def accounts_cmd(argv: list[str] | None = None) -> int:
    """CLI entry: ``opencode-go-proxy accounts list|use <name|id>``."""
    from .config import ProxyConfig
    from .secrets import configured_key_env

    args = list(argv) if argv is not None else []
    if not args or args[0] not in {"list", "use"}:
        print("usage: opencode-go-proxy accounts list|use <name|id>", file=sys.stderr)
        return 2
    config = ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env=configured_key_env(),
        timeout_sec=180,
        max_body_bytes=20 * 1024 * 1024,
    )
    if args[0] == "list":
        return _cmd_list(config)
    if len(args) < 2:
        print("usage: opencode-go-proxy accounts use <name|id>", file=sys.stderr)
        return 2
    try:
        account = set_active_account(config, args[1])
    except ProxyError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    print(f"active: {account.id}\t{account.name}\t{account.masked}")
    return 0
