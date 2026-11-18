"""The ``list_logins`` and ``login`` tools through the registry (T-19366)."""

from __future__ import annotations

import json

import pytest

from fakes import FakeBrowserManager, call_tool
from stealth_chrome_devtools_mcp.embedded import (
    login_flow,
    secret_registry,
    vault_crypto,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from vault_helpers import (
    password_entry,
    provider_entry,
    reset,
    unlocked_vault,
)

HINT = "a person must run `stealthy login unlock`"
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
def server_mod(patched_server):
    return patched_server(browser_manager=FakeBrowserManager())


async def test_list_logins_when_locked_names_what_a_person_must_do(server_mod):
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, "list_logins")
    assert HINT in str(caught.value)
    assert "locked" in str(caught.value)


async def test_login_when_locked_names_what_a_person_must_do(server_mod):
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, "login", instance_id="inst-1", label="work")
    assert HINT in str(caught.value)


async def test_list_logins_when_unlocked_has_the_documented_shape(server_mod):
    unlocked_vault(
        {
            "work": password_entry(password=PASSWORD, show_username=True),
            "shop": provider_entry(),
        }
    )
    result = await call_tool(server_mod, "list_logins")
    assert result["unlocked"] is True
    shop, work = result["logins"]
    assert shop == {
        "label": "shop",
        "kind": "provider",
        "site": "shop.example",
        "via": "work",
        "frozen": None,
    }
    assert work["username"] == "someone@example.com"
    assert set(work) == {"label", "kind", "site", "frozen", "username"}


def _keys(value):
    """Every dict key at any depth."""
    if isinstance(value, dict):
        return {*value, *(k for v in value.values() for k in _keys(v))}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


async def test_list_logins_never_returns_a_password_anywhere(server_mod):
    unlocked_vault(
        {"work": password_entry(password=PASSWORD), "shop": provider_entry()}
    )
    result = await call_tool(server_mod, "list_logins")
    text = json.dumps(result)
    assert PASSWORD not in text
    assert "password" not in _keys(result)
    assert "someone@example.com" not in text  # show_username is off


async def test_list_logins_hides_the_username_unless_the_owner_chose(server_mod):
    unlocked_vault({"work": password_entry(show_username=False)})
    (row,) = (await call_tool(server_mod, "list_logins"))["logins"]
    assert "username" not in row


async def test_list_logins_shows_a_frozen_login_as_frozen(server_mod):
    unlocked_vault({"work": password_entry(frozen="2026-11-18T00:00:00+00:00")})
    (row,) = (await call_tool(server_mod, "list_logins"))["logins"]
    assert row["frozen"] == "2026-11-18T00:00:00+00:00"


async def test_login_for_an_unknown_label_is_a_tool_error(server_mod):
    unlocked_vault({"work": password_entry()})
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, "login", instance_id="inst-1", label="nowhere")
    assert "nowhere" in str(caught.value)


async def test_login_for_an_unknown_instance_is_instance_not_found(server_mod):
    unlocked_vault({"work": password_entry()})
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, "login", instance_id="inst-9", label="work")
    assert "inst-9" in str(caught.value)


async def test_login_passes_the_flow_its_arguments_and_returns_its_answer(
    server_mod, monkeypatch
):
    unlocked_vault({"work": password_entry()})
    seen = {}

    async def fake_run(manager, instance_id, label, caller):
        seen.update(instance_id=instance_id, label=label, caller=caller)
        return {"outcome": "logged_in", "label": label, "detail": "signed in"}

    monkeypatch.setattr(login_flow, "run", fake_run)
    result = await call_tool(server_mod, "login", instance_id="inst-1", label="work")
    assert result == {"outcome": "logged_in", "label": "work", "detail": "signed in"}
    assert seen == {"instance_id": "inst-1", "label": "work", "caller": "in-process"}


async def test_a_vault_that_locks_mid_call_reads_as_locked(server_mod, monkeypatch):
    async def fake_run(*_args):
        raise vault_store.VaultLocked("locked")

    monkeypatch.setattr(login_flow, "run", fake_run)
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, "login", instance_id="inst-1", label="work")
    assert HINT in str(caught.value)


async def test_an_unexpected_exception_reaches_the_caller_as_its_class_only(
    server_mod, monkeypatch
):
    unlocked_vault({"work": password_entry(password=PASSWORD)})

    async def fake_run(*_args):
        raise RuntimeError(f"the page went wrong near {PASSWORD}")

    monkeypatch.setattr(login_flow, "run", fake_run)
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, "login", instance_id="inst-1", label="work")
    assert "RuntimeError" in str(caught.value)
    assert PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None


async def test_no_secret_is_in_any_error_these_tools_raise(server_mod):
    unlocked_vault({"work": password_entry(password=PASSWORD)})
    messages = []
    for name, arguments in (
        ("login", {"instance_id": "inst-9", "label": "work"}),
        ("login", {"instance_id": "inst-1", "label": "nowhere"}),
    ):
        with pytest.raises(ToolError) as caught:
            await call_tool(server_mod, name, **arguments)
        messages.append(str(caught.value))
    assert all(PASSWORD not in message for message in messages)
    assert secret_registry.redact(PASSWORD) != PASSWORD


def test_the_logins_section_is_registered_with_both_tools():
    from stealth_chrome_devtools_mcp.embedded import server

    assert server.SECTION_TOOLS["logins"] == ["list_logins", "login"]
