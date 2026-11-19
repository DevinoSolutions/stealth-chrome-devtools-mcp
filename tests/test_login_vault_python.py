"""A tools-only agent cannot run Python in the backend beside a vault (T-19366).

The third review's blocker: ``create_python_binding`` ``exec``s the caller's Python in
the backend process, and with the vault unlocked that code read the password, the
username and the 32-byte key (``vault_store.entry("work")``, ``vault_store._key``)
with no browser trick. The fourth review: a per-process taint does not help, since
such code can persist itself and come back after a restart. The rule is now about
what is on disk (``caller_python``): while a vault file exists, in every backend,
locked or not, the tools are refused; with none they work as they always did.

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
PING = """
def ping(arg=None):
    return "pong-" + str(arg)
"""
CALL = (
    "(async () => { try { const r = await window.%s('x'); "
    "return typeof r === 'string' ? r : JSON.stringify(r); } "
    "catch (e) { return 'ERR ' + e; } })()"
)
HOOK = """
def process_request(request):
    return HookAction(action="continue")
"""


def _holds_a_secret(text):
    return RIGHT in text or USER in text


async def _no_such_binding(iid, name):
    answer = await get_fn("execute_script")(instance_id=iid, script=CALL % name)
    return not _holds_a_secret(str(answer)) and "pong" not in str(answer)


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
        assert "disabled" in str(caught.value)
        assert not _holds_a_secret(str(caught.value))
        assert await _no_such_binding(iid, "leak")  # the binding does not exist
        assert caller_python.ran() is None


async def test_a_locked_vault_is_just_as_closed_to_both_tools(site, tmp_path):
    """The old taint rule admitted a binding while the vault was locked; code that
    waits for the unlock (or persists itself) is exactly what must not exist."""
    make_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        await get_fn("navigate")(instance_id=iid, url=site.url("/home"))
        with pytest.raises(ToolError) as caught:
            await get_fn("create_python_binding")(
                instance_id=iid, binding_name="leak", python_code=LEAK
            )
        assert "stealthy login destroy" in str(caught.value)
        with pytest.raises(ToolError):
            await get_fn("create_dynamic_hook")(
                name="h",
                requirements={"url_pattern": "*"},
                function_code=HOOK,
                instance_ids=[iid],
            )
        assert await _no_such_binding(iid, "leak")
        assert caller_python.ran() is None
        # and the vault still unlocks and signs in
        key = make_vault({"work": _entry(site)})
        vault_store.unlock(key, None)
        result = await get_fn("login")(instance_id=iid, label="work")
        assert result["outcome"] in {"logged_in", "already_logged_in"}, result


async def test_with_no_vault_the_binding_works_exactly_as_it_did(site, tmp_path):
    async with _browser(tmp_path) as iid:
        await get_fn("navigate")(instance_id=iid, url=site.url("/home"))
        await get_fn("create_python_binding")(
            instance_id=iid, binding_name="ping", python_code=PING
        )
        answer = await get_fn("execute_script")(instance_id=iid, script=CALL % "ping")
        assert "pong-x" in str(answer)
        assert caller_python.ran() == "create_python_binding"
        await get_fn("create_dynamic_hook")(
            name="h",
            requirements={"url_pattern": "*"},
            function_code=HOOK,
            instance_ids=[iid],
        )


async def test_removing_the_vault_turns_the_tools_back_on(site, tmp_path):
    make_vault({"work": _entry(site)})
    async with _browser(tmp_path) as iid:
        await get_fn("navigate")(instance_id=iid, url=site.url("/home"))
        with pytest.raises(ToolError):
            await get_fn("create_python_binding")(
                instance_id=iid, binding_name="ping", python_code=PING
            )
        vault_store.VAULT_PATH.unlink()
        await get_fn("create_python_binding")(
            instance_id=iid, binding_name="ping", python_code=PING
        )
        answer = await get_fn("execute_script")(instance_id=iid, script=CALL % "ping")
        assert "pong-x" in str(answer)
