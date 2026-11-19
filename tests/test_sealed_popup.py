"""A page the vault's sealed login tab opens is sealed, hidden and closed (T-19366).

``window.open`` from the login page makes a target no tool asked for. The review
reproduced a 21 ms window in which ``list_tabs`` listed it and ``execute_script``
ran in it (a same-origin popup can reach ``window.opener``). The ``targetCreated``
handler therefore seals and hides the popup BEFORE it creates the close task, and
only drops the seal once Chrome no longer lists it.

The popup fixtures are ``test_popup_arming``'s: the same real ``spawn_browser`` and
the same event delivery.
"""

from __future__ import annotations

import pytest
from nodriver import cdp

import test_popup_arming as base
from stealth_chrome_devtools_mcp.embedded import login_seal, tab_open
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from vault_helpers import reset

manager, launched = base.manager, base.launched
IID, SEALED, POPUP = base.IID, "T-sealed", "T-popup"


@pytest.fixture(autouse=True)
def _clean_seal():
    reset()
    yield
    reset()


def wire_close(browser, closes):
    """``Target.closeTarget`` on the browser connection: record it, and (unless
    ``closes`` is false) take the target out of Chrome's listing, as Chrome does."""

    def close(_name):
        target = browser.cdp_frames[-1]["params"]["targetId"]
        browser.close_requests.append(target)
        if closes:
            browser.tabs[:] = [t for t in browser.tabs if t.target.target_id != target]
        return {}

    browser.close_requests = []
    browser._cdp_responses["close_target"] = close


async def sealed_login_tab(manager, launched, closes=True):
    browser, main = launched
    await base.spawn(manager, browser, main)
    sealed = base.popup_in(browser, SEALED)
    login_seal.begin(IID)
    login_seal.seal_tab(IID, SEALED)
    tab_open.mark_internal(browser, sealed)
    wire_close(browser, closes)
    return browser


async def test_the_popup_is_sealed_and_hidden_the_moment_it_is_announced(
    manager, launched
):
    browser = await sealed_login_tab(manager, launched)
    base.popup_in(browser, POPUP)

    browser._deliver(base.created(POPUP, opener=SEALED))
    # NOTHING has run yet but the handler: the close task is only scheduled.
    assert login_seal.is_sealed(IID, POPUP)
    with pytest.raises(ToolError):
        login_seal.refuse_if_sealed(
            "execute_script", {"instance_id": IID, "tab_id": POPUP}
        )
    assert POPUP not in [
        str(t.target.target_id) for t in tab_open.visible_tabs(browser)
    ]
    assert browser.close_requests == []

    await base.settle()
    assert browser.close_requests == [POPUP]
    # gone from Chrome: the seal is released, and nothing is left behind
    assert not login_seal.is_sealed(IID, POPUP)


async def test_a_popup_that_will_not_close_stays_sealed(manager, launched):
    browser = await sealed_login_tab(manager, launched, closes=False)
    base.popup_in(browser, POPUP)

    browser._deliver(base.created(POPUP, opener=SEALED))
    await base.settle()

    assert browser.close_requests == [POPUP]
    assert login_seal.is_sealed(IID, POPUP)
    # even after the login's own seal is over
    login_seal.end(IID)
    assert login_seal.is_sealed(IID, POPUP)
    with pytest.raises(ToolError):
        login_seal.refuse_if_sealed(
            "execute_script", {"instance_id": IID, "tab_id": POPUP}
        )


async def test_the_popup_of_an_ordinary_tab_is_not_sealed(manager, launched):
    browser = await sealed_login_tab(manager, launched)
    base.popup_in(browser, "T-agent-page")
    base.popup_in(browser, POPUP)

    browser._deliver(base.created(POPUP, opener="T-agent-page"))
    await base.settle()

    assert not login_seal.is_sealed(IID, POPUP)
    assert browser.close_requests == []


async def test_the_popup_of_a_popup_is_sealed_too(manager, launched):
    """A page can chain: the sealed tab opens A, A opens B."""
    browser = await sealed_login_tab(manager, launched, closes=False)
    base.popup_in(browser, "T-a")
    base.popup_in(browser, "T-b")

    browser._deliver(base.created("T-a", opener=SEALED))
    browser._deliver(base.created("T-b", opener="T-a"))
    await base.settle()

    assert login_seal.is_sealed(IID, "T-a")
    assert login_seal.is_sealed(IID, "T-b")


def test_release_forgets_a_popup_and_only_that_popup():
    login_seal.begin(IID)
    login_seal.seal_tab(IID, SEALED)
    assert login_seal.seal_popup(SEALED, "T-1")
    assert login_seal.seal_popup(SEALED, "T-2")
    login_seal.release("T-1")
    assert not login_seal.is_sealed(IID, "T-1")
    assert login_seal.is_sealed(IID, "T-2")
    login_seal.release("T-2")
    login_seal.end(IID)
    assert not login_seal.any_sealing()


def test_a_popup_of_a_tab_that_is_not_sealed_is_not_taken():
    assert login_seal.seal_popup("T-nobody", "T-1") is False
    assert not login_seal.any_sealing()


def test_the_event_type_is_a_real_target_created():
    assert isinstance(base.created(POPUP), cdp.target.TargetCreated)
