"""T-5332 E2E - a tool's keys and clicks reach a tab Chrome would call ``hidden`` (real Chrome).

F-1011 keeps every driven tab ``visible`` and focused with
``Emulation.setFocusEmulationEnabled`` (``window_sizing.enable_focus_emulation``, called
from ``BrowserManager._apply_tab_overrides``). ``tests/test_e2e_background_tab_frames.py``
pins what that does to PAINTING; nothing pinned what a page that gates its INPUT on
``document.visibilityState`` (a login form, an editor that ignores a hidden tab) would
see. ``fixture_app/hidden_input_probe.html`` drops every key and click while the page
reads ``hidden`` and records them otherwise.

One tab Chrome calls hidden gives the control: opened straight through ``tab_open`` so
nothing armed it, it reads ``hidden`` and the fixture drops what is dispatched at it,
which is what shows the fixture can tell. The driven tab, opened by ``new_tab`` (armed)
and covered by a later tab, takes ``type_text`` and ``click_element`` as a visible,
focused page would.

The premise is a PLATFORM fact and is measured: where Chrome does not report a covered
tab ``hidden`` there is nothing for the emulation to lift, so the node skips by name.
"""

from __future__ import annotations

import json

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    runtime,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import display_context, tab_open

pytestmark = integration_pytestmark()

TYPED = "hello@example.com"

PAGE_STATE_JS = """
return {
  value: document.getElementById("email").value,
  clicks: document.getElementById("clicks").textContent,
  probe: window.__probe,
  visibility: document.visibilityState,
  hasFocus: document.hasFocus(),
};
"""


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


def _modes() -> list:
    """Headless always; headed only where a window could be seen (F-808)."""
    headed = pytest.param(
        False,
        id="headed",
        marks=pytest.mark.skipif(
            not display_context.can_show_windows(),
            reason="no display to show a headed window on",
        ),
    )
    return [pytest.param(True, id="headless"), headed]


async def _spawn(tmp_path, headless: bool) -> str:
    result = await get_fn("spawn_browser")(
        headless=headless,
        user_data_dir=str(tmp_path / "hidden-input"),
        **sandbox_kwargs(),
    )
    return result["instance_id"]


#: Keys and a click at the page's own listeners, from inside the page: whether a
#: hidden page receives input from the browser varies with the rest of the window, but
#: whether THIS fixture drops what it receives while hidden does not.
POKE_JS = f"""
(() => {{
  const email = document.getElementById("email");
  email.focus();
  for (const ch of {json.dumps(TYPED)}) {{
    email.dispatchEvent(new KeyboardEvent("keydown", {{key: ch, cancelable: true}}));
  }}
  document.getElementById("go").click();
  return JSON.stringify({{
    value: email.value,
    probe: window.__probe,
    visibility: document.visibilityState,
  }});
}})()
"""


async def _poke(tab) -> dict:
    """:data:`POKE_JS` on a tab no tool armed or drove, read back through CDP."""
    state = await tab.evaluate(POKE_JS, return_by_value=True)
    return json.loads(getattr(state, "value", state))


@pytest.mark.parametrize("headless", _modes())
async def test_a_tool_reaches_a_page_chrome_would_call_hidden(
    tmp_path, headless, fixture_app_server
):
    url = f"{fixture_app_server}/hidden_input_probe.html"
    new_tab, close = get_fn("new_tab"), get_fn("close_instance")
    type_text, click = get_fn("type_text"), get_fn("click_element")
    execute = get_fn("execute_script")
    iid = await _spawn(tmp_path, headless)
    try:
        browser = await runtime.browser_manager.get_browser(iid)
        # Opened through ``tab_open`` alone: nothing arms it, Chrome decides.
        control = await tab_open.open_tab(browser, url)
        driven = (await new_tab(instance_id=iid, url=url))["tab_id"]
        # Something in front of both, so neither is the visible tab.
        await tab_open.open_tab(browser, "about:blank")

        seen = await control.evaluate("document.visibilityState", return_by_value=True)
        visibility = getattr(seen, "value", seen)
        if visibility != "hidden":
            pytest.skip(
                f"Chrome on this host reports a covered tab {visibility!r}: there is "
                f"nothing hidden for the focus emulation to lift"
            )

        assert (
            await type_text(
                instance_id=iid,
                selector="#email",
                text=TYPED,
                delay_ms=10,
                tab_id=driven,
            )
            is True
        )
        await click(instance_id=iid, selector="#go", tab_id=driven)

        answer = await execute(instance_id=iid, script=PAGE_STATE_JS, tab_id=driven)
        assert isinstance(answer, dict) and answer.get("success") is True, answer
        page = answer["result"]
        assert page["visibility"] == "visible", page
        assert page["hasFocus"] is True, page
        assert page["value"] == TYPED, page
        assert page["clicks"] == "1", page
        assert page["probe"]["clicks"] == 1, page
        assert page["probe"]["dropped"] == 0, page

        # The fixture can tell: the covered, unarmed tab reads hidden, receives the
        # same keys and click and drops every one.
        undriven = await _poke(control)
        assert undriven["visibility"] == "hidden", undriven
        assert undriven["value"] == "", undriven
        assert undriven["probe"]["clicks"] == 0, undriven
        assert undriven["probe"]["received"] == undriven["probe"]["dropped"] > 0, (
            undriven
        )
    finally:
        await close(instance_id=iid)
