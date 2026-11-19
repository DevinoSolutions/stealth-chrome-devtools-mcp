"""The sealed login in a HEADED browser (T-19366, merge blocker).

Every other login test runs headless. In a headed window the login's own tab is
opened BEHIND the current one, where Chrome reads it ``hidden`` and unfocused and
drops the key events typed into it: both fields stayed empty and the login ended
``needs_user`` ("the password field did not take the typed text") with no POST.
The cure is the product's one focus emulation (F-1011) on that tab
(``login_flow._open_sealed_tab``); ``tests/test_login_flow_guards.py`` pins that it
is applied everywhere, and this file proves the whole login in a real window.

Real Chrome, a window on a desktop, a local site and synthetic credentials.
Windows only, like the other headed-window proof
(``TestHeadedSpawnIsSeenOrRefused``): the Linux gate cell runs a bare Xvfb with no
window manager, where nothing here has been measured, so the nodes are skipped there
rather than claimed.
"""

from __future__ import annotations

import sys

import pytest

import test_login_flow as flow
from e2e_helpers import get_fn, sandbox_kwargs
from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    display_context,
    vault_store,
)
from vault_helpers import spawn_before_the_vault, unlocked_vault

pytestmark = [
    flow.pytestmark,
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="a headed login needs a window; the Linux gate cell is a bare Xvfb "
        "with no window manager and has not been measured",
    ),
]
site, vault, _warmup = flow.site, flow.vault, flow._warmup


@pytest.fixture(autouse=True)
def _the_real_vault_path_is_left_as_it_was():
    real = backend_registry.STATE_DIR / "vault.json"
    existed = real.exists()
    yield
    assert real.exists() == existed, "a headed test touched the real vault path"


async def _headed(tmp_path):
    if not display_context.can_show_windows():
        pytest.skip("this context cannot show a window")
    spawn = get_fn("spawn_browser")
    with spawn_before_the_vault():
        spawned = await spawn(
            headless=False,
            user_data_dir=str(tmp_path / "login-headed"),
            **sandbox_kwargs(),
        )
    assert spawned["headless"] is False, spawned
    return spawned["instance_id"]


def _never_the_real_vault(tmp_path):
    """A headed test must not create a vault where the person keeps theirs."""
    assert vault_store.VAULT_PATH.parent == tmp_path
    assert vault_store.VAULT_PATH != backend_registry.STATE_DIR / "vault.json"


async def test_a_one_step_login_signs_a_headed_browser_in(site, tmp_path):
    _never_the_real_vault(tmp_path)
    unlocked_vault({"work": flow._entry(site)})
    iid = await _headed(tmp_path)
    try:
        before = await flow._tab_ids(iid)
        result = await flow._run(iid)
        assert result["outcome"] == "logged_in", result
        assert flow.RIGHT not in str(result)
        assert set(await flow._tab_ids(iid)) <= set(before)
        assert await flow._is_home(iid, site)
    finally:
        await get_fn("close_instance")(instance_id=iid)
    assert site.posts == [
        {"path": "/login", "username": flow.USER, "password": flow.RIGHT}
    ]


async def test_a_wrong_password_is_rejected_and_frozen_in_a_headed_browser(
    site, tmp_path
):
    _never_the_real_vault(tmp_path)
    unlocked_vault({"work": flow._entry(site, password=flow.WRONG)})
    iid = await _headed(tmp_path)
    try:
        result = await flow._run(iid)
        assert result["outcome"] == "rejected", result
        assert flow.WRONG not in str(result)
        assert vault_store.entry("work")["frozen"]
    finally:
        await get_fn("close_instance")(instance_id=iid)
    assert len(site.posts) == 1
