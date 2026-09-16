"""Shared identity headers for provider requests."""

from __future__ import annotations

import os

from . import __version__


def upstream_user_agent() -> str:
    """Return the adapter identity, with an explicit operator override."""
    return os.environ.get(
        "OPENCODE_GO_PROXY_USER_AGENT",
        f"opencode-go-proxy/{__version__}",
    )
