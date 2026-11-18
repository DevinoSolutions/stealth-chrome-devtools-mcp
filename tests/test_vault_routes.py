"""The login vault's unlock, lock and status routes (T-19366).

Driven through a real Starlette app with the backend's own bearer-token
middleware in front of it, so "no token is a 401" is the real middleware's answer,
not a re-implementation of it.
"""

from __future__ import annotations

import base64

import pytest
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from stealth_chrome_devtools_mcp.embedded import (
    backend_token,
    secret_registry,
    vault_crypto,
    vault_routes,
    vault_store,
)
from vault_helpers import PASSPHRASE, make_vault, password_entry, reset

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PASSWORD = "Tr0ub4dor&3-marker"


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)
    monkeypatch.setattr(vault_store, "VAULT_PATH", tmp_path / "vault.json")
    monkeypatch.setattr(vault_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    reset()
    yield
    reset()


@pytest.fixture
def client():
    server = FastMCP("vault-routes-test")
    vault_routes.install(server)
    app = server.http_app(
        path="/mcp/",
        middleware=[Middleware(backend_token.BearerTokenMiddleware, token=TOKEN)],
    )
    return TestClient(app)


def good_key():
    header = vault_crypto.read_file(vault_store.VAULT_PATH)
    return vault_crypto.derive_key(PASSPHRASE, header)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def post_unlock(client, key, ttl=None, headers=AUTH):
    return client.post(
        vault_routes.UNLOCK_PATH,
        json={"key": b64(key), "ttl_seconds": ttl},
        headers=headers,
    )


def test_the_right_key_unlocks_and_reports_the_entry_count(client):
    make_vault({"work": password_entry(password=PASSWORD)})
    reply = post_unlock(client, good_key())
    assert reply.status_code == 200
    assert reply.json() == {"unlocked": True, "entries": 1}
    assert vault_store.status()["unlocked"] is True
    assert PASSWORD not in secret_registry.redact(f"x {PASSWORD}")


def test_a_wrong_key_is_403_and_the_vault_stays_locked(client):
    make_vault({"work": password_entry()})
    wrong = vault_crypto.derive_key(
        "other", vault_crypto.read_file(vault_store.VAULT_PATH)
    )
    reply = post_unlock(client, wrong)
    assert reply.status_code == 403
    assert vault_store.status()["unlocked"] is False
    assert b64(wrong) not in reply.text


def test_a_missing_vault_file_is_403(client):
    key = vault_crypto.derive_key(PASSPHRASE, vault_crypto.new_header())
    assert post_unlock(client, key).status_code == 403


def test_a_ttl_is_applied(client):
    make_vault({"work": password_entry()})
    assert post_unlock(client, good_key(), ttl=600).status_code == 200
    left = vault_store.status()["expires_in"]
    assert 590 <= left <= 600


@pytest.mark.parametrize("ttl", [0, -5, "soon", True])
def test_a_bad_ttl_is_400_and_changes_nothing(client, ttl):
    make_vault({"work": password_entry()})
    reply = post_unlock(client, good_key(), ttl=ttl)
    assert reply.status_code == 400
    assert vault_store.status()["unlocked"] is False


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"key": 5},
        {"key": "not base64!!"},
        {"key": b64(b"short")},
        {"key": b64(b"x" * 33)},
    ],
)
def test_a_malformed_request_is_400(client, body):
    make_vault({"work": password_entry()})
    reply = client.post(vault_routes.UNLOCK_PATH, json=body, headers=AUTH)
    assert reply.status_code == 400
    assert vault_store.status()["unlocked"] is False


def test_a_body_that_is_not_json_is_400(client):
    reply = client.post(vault_routes.UNLOCK_PATH, content=b"{nope", headers=AUTH)
    assert reply.status_code == 400


def test_an_oversized_body_is_400(client):
    reply = client.post(vault_routes.UNLOCK_PATH, content=b"x" * 10_000, headers=AUTH)
    assert reply.status_code == 400


def test_an_error_never_echoes_the_posted_key(client):
    make_vault({"work": password_entry()})
    key = b64(b"k" * 32)
    reply = client.post(vault_routes.UNLOCK_PATH, json={"key": key}, headers=AUTH)
    assert key not in reply.text


def test_lock_drops_the_key(client):
    make_vault({"work": password_entry(password=PASSWORD)})
    post_unlock(client, good_key())
    reply = client.post(vault_routes.LOCK_PATH, headers=AUTH)
    assert reply.status_code == 200
    assert reply.json() == {"locked": True}
    assert vault_store.status()["unlocked"] is False
    assert secret_registry.redact(PASSWORD) == PASSWORD


def test_status_reports_without_unlocking(client):
    reply = client.get(vault_routes.STATUS_PATH, headers=AUTH)
    assert reply.json() == {"exists": False, "unlocked": False, "expires_in": None}
    make_vault({"work": password_entry()})
    assert client.get(vault_routes.STATUS_PATH, headers=AUTH).json()["exists"] is True
    post_unlock(client, good_key())
    status = client.get(vault_routes.STATUS_PATH, headers=AUTH).json()
    assert status["unlocked"] is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", vault_routes.UNLOCK_PATH),
        ("post", vault_routes.LOCK_PATH),
        ("get", vault_routes.STATUS_PATH),
    ],
)
def test_no_token_is_401_and_a_wrong_token_is_401(client, method, path):
    make_vault({"work": password_entry()})
    key = b64(good_key())
    for headers in ({}, {"Authorization": "Bearer " + "x" * 43}):
        reply = client.request(method, path, json={"key": key}, headers=headers)
        assert reply.status_code == 401
    assert vault_store.status()["unlocked"] is False


def test_an_unauthenticated_lock_does_not_lock(client):
    make_vault({"work": password_entry()})
    post_unlock(client, good_key())
    assert client.post(vault_routes.LOCK_PATH).status_code == 401
    assert vault_store.status()["unlocked"] is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", vault_routes.UNLOCK_PATH),
        ("post", vault_routes.LOCK_PATH),
        ("get", vault_routes.STATUS_PATH),
    ],
)
def test_no_route_returns_an_entry_a_username_a_password_or_a_key(client, method, path):
    make_vault({"work": password_entry(password=PASSWORD)})
    key = good_key()
    reply = client.request(
        method, path, json={"key": b64(key), "ttl_seconds": None}, headers=AUTH
    )
    for forbidden in (PASSWORD, "someone@example.com", b64(key), key.hex()):
        assert forbidden not in reply.text
    assert set(reply.json()) <= {
        "unlocked",
        "entries",
        "locked",
        "exists",
        "expires_in",
    }


def test_install_is_idempotent_per_server():
    server = FastMCP("twice")
    vault_routes.install(server)
    vault_routes.install(server)
    paths = [getattr(route, "path", None) for route in server._additional_http_routes]
    assert paths.count(vault_routes.UNLOCK_PATH) == 1
