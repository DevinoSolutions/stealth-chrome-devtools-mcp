"""The login vault's three HTTP routes (T-19366): unlock, lock, status.

Custom routes of the backend's own app, NOT MCP tools: a model cannot call a
route. They sit behind the backend's bearer-token middleware like every other
path (``backend_token.BearerTokenMiddleware`` wraps the whole app), so only a
client that can read the user's token file reaches them, and a person is who
holds the passphrase.

* ``POST /_stealth/vault/unlock`` body ``{"key": "<base64 of 32 bytes>",
  "ttl_seconds": <number>|null}``: the CLI derived the key locally; the backend
  proves it by decrypting the vault file and keeps it in memory only. Answers
  ``{"unlocked": true, "entries": n}``, ``403`` for a key that does not open the
  file (and changes nothing), ``400`` for a malformed request.
* ``POST /_stealth/vault/lock``: drops the key. ``{"locked": true}``.
* ``GET /_stealth/vault/status``: ``{"exists", "unlocked", "expires_in"}``.

**No route returns an entry, a username, a password or a key**, and no error text
echoes what was posted.
"""

from __future__ import annotations

import base64
import binascii
import weakref
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from stealth_chrome_devtools_mcp.embedded import vault_crypto, vault_store

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response

UNLOCK_PATH = "/_stealth/vault/unlock"
LOCK_PATH = "/_stealth/vault/lock"
STATUS_PATH = "/_stealth/vault/status"

_KEY_BYTES = 32
_MAX_BODY_BYTES = 4096
_routed: weakref.WeakSet[FastMCP] = weakref.WeakSet()


def _bad_request(reason: str) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=400)


def _parse_unlock(body: object) -> tuple[bytes, float | None] | str:
    """``(key, ttl)`` from a request body, or the sentence saying what is wrong."""
    if not isinstance(body, dict) or not isinstance(body.get("key"), str):
        return "the body must be an object with a base64 'key'"
    try:
        key = base64.b64decode(body["key"], validate=True)
    except (binascii.Error, ValueError):
        return "'key' is not base64"
    if len(key) != _KEY_BYTES:
        return f"'key' must be {_KEY_BYTES} bytes"
    ttl = body.get("ttl_seconds")
    if ttl is not None and (
        isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not ttl > 0
    ):
        return "'ttl_seconds' must be a positive number or null"
    return key, ttl


async def _unlock(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return _bad_request("the request is too large")
    try:
        parsed = _parse_unlock(await request.json())
    except ValueError:
        return _bad_request("the body must be JSON")
    if isinstance(parsed, str):
        return _bad_request(parsed)
    try:
        entries = vault_store.unlock(*parsed)
    except vault_crypto.VaultError:
        return JSONResponse(
            {"error": "the key does not open the vault"}, status_code=403
        )
    return JSONResponse({"unlocked": True, "entries": entries})


async def _lock(_request: Request) -> Response:
    vault_store.lock()
    return JSONResponse({"locked": True})


async def _status(_request: Request) -> Response:
    return JSONResponse(vault_store.status())


def install(server: FastMCP) -> None:
    """Give ``server`` the three routes. Idempotent per server."""
    if server in _routed:
        return
    server.custom_route(UNLOCK_PATH, methods=["POST"], include_in_schema=False)(_unlock)
    server.custom_route(LOCK_PATH, methods=["POST"], include_in_schema=False)(_lock)
    server.custom_route(STATUS_PATH, methods=["GET"], include_in_schema=False)(_status)
    _routed.add(server)
