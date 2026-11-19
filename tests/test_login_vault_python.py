"""A tools-only agent cannot run Python in the backend beside an unlocked vault (T-19366).

The third review's blocker: ``create_python_binding`` ``exec``s the caller's Python in
the backend process, and with the vault unlocked that code read the password, the
username and the 32-byte key (``vault_store.entry("work")``, ``vault_store._key``)
with no browser trick. This is that agent, in both orders:

* the vault is unlocked first and the tool is called: refused;
* the binding is planted first (the vault locked) and the vault is unlocked after:
  the unlock is refused, so the code that was waiting never sees a key.

Real Chrome and the real tools. The password is compared inside the test, never
printed.
"""

from __future__ import annotations

import pytest

import test_login_flow as flow
from e2e_helpers import get_fn
from stealth_chrome_devtools_mcp.embedded import caller_python, vault_store
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from vault_helpers import make_vault, unlocked_vault

pytestmark = flow.pytestmark
site, vault, _warmup = flow.site, flow.vault, flow._warmup
RIGHT, USER, _browser, _entry = flow.RIGHT, flow.USER, flow._browser, flow._entry

LEAK = """
def leak(arg=None):
    from stealth_chrome_devtools_mcp.embedded import vault_store
    e = vault_store.entry("work")
    pw = e["password"]
    return {"pw": pw.reveal() if hasattr(pw, "reveal") else str(pw),
            "user": str(e["username"]), "key_len": len(vault_store._key or b"")}
"""
CALL = (
    "(async () => { try { const r = await window.leak('x'); "
    "return typeof r === 'string' ? r : JSON.stringify(r); } "
    "catch (e) { return 'ERR ' + e; } })()"
)
HOOK_LEAK = """
def process_request(request):
    from stealth_chrome_devtools_mcp.embedded import vault_store
    request["seen"] = vault_store.entry("work")["password"].reveal()
    return HookAction(action="continue")
"""


def _holds_a_secret(text):
    return RIGHT in text or USER in text


async def test_create_python_binding_is_refused_while_the_vault_is_unlocked(
    site, tmp_path
):
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        await get_fn("navigate")(instance_id=iid, url=site.url("/home"))
        with pytest.raises(ToolError) as caught:
            await get_fn("create_python_binding")(
                instance_id=iid, binding_name="leak", python_code=LEAK
            )
        assert "unlocked" in str(caught.value)
        assert not _holds_a_secret(str(caught.value))
        answer = await get_fn("execute_script")(instance_id=iid, script=CALL)
        assert not _holds_a_secret(str(answer))  # the binding does not exist
        assert not caller_python.tainted()


async def test_a_binding_planted_before_the_unlock_never_sees_a_key(site, tmp_path):
    key = make_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        await get_fn("navigate")(instance_id=iid, url=site.url("/home"))
        await get_fn("create_python_binding")(
            instance_id=iid, binding_name="leak", python_code=LEAK
        )
        assert caller_python.tainted_by() == "create_python_binding"
        with pytest.raises(caller_python.Tainted):
            vault_store.unlock(key, None)  # what the unlock route calls
        assert not vault_store.is_unlocked()
        answer = await get_fn("execute_script")(instance_id=iid, script=CALL)
        assert not _holds_a_secret(str(answer)), "the waiting binding read a secret"
        # and the login tools say why, without leaking anything
        with pytest.raises(ToolError) as caught:
            await get_fn("login")(instance_id=iid, label="work")
        assert "restart" in str(caught.value)
        assert not _holds_a_secret(str(caught.value))


async def test_create_dynamic_hook_is_refused_while_unlocked_and_locks_the_vault_after(
    site, tmp_path
):
    unlocked_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        with pytest.raises(ToolError):
            await get_fn("create_dynamic_hook")(
                name="h",
                requirements={"url_pattern": "*"},
                function_code=HOOK_LEAK,
                instance_ids=[iid],
            )
        assert not caller_python.tainted()
        vault_store.lock()
        key = make_vault({"work": _entry(site)})
        await get_fn("create_dynamic_hook")(
            name="h",
            requirements={"url_pattern": "*"},
            function_code=HOOK_LEAK,
            instance_ids=[iid],
        )
        with pytest.raises(caller_python.Tainted):
            vault_store.unlock(key, None)
        assert not vault_store.is_unlocked()
