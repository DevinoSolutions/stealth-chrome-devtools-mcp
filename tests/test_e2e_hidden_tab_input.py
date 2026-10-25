"""T-5332 E2E - a tool's keys and clicks reach a tab Chrome would call ``hidden`` (real Chrome).

F-1011 keeps every driven tab ``visible`` and focused with
``Emulation.setFocusEmulationEnabled`` (``window_sizing.enable_focus_emulation``, called
from ``BrowserManager._apply_tab_overrides``). ``tests/test_e2e_background_tab_frames.py``
pins what that does to PAINTING; nothing pinned what a page that gates its INPUT on
``document.visibilityState`` (a login form, an editor that ignores a hidden tab) would
see. ``fixture_app/hidden_input_probe.html`` drops every key and click while the page
reads ``hidden`` and records them otherwise.

Each node stages the same window: a control tab opened straight through ``tab_open``
(nothing armed it), a driven tab opened by ``new_tab`` (armed), and a later tab in front
of both. Typing and clicking are separate nodes, so a failure names which half broke.

* ``test_type_text_lands_in_a_covered_tab`` and
  ``test_click_element_lands_in_a_covered_tab`` are the Chrome-level evidence: a real
  ``type_text`` / ``click_element`` on the covered armed tab must be taken as a visible,
  focused page takes them. Neutering ``enable_focus_emulation`` is what turns them RED.
* ``test_the_fixture_drops_input_while_hidden`` is a FIXTURE SELF-TEST, not evidence
  about Chrome's input path: the unarmed covered tab reads ``hidden`` and the fixture
  drops in-page events. It shows the other two can fail; it delivers nothing through
  Chrome.

The premise (a covered tab reads ``hidden``) is a PLATFORM fact. It is asserted
everywhere except headed Chrome on Linux's bare Xvfb (no window manager), where a
covered tab can stay visible and there is nothing for the emulation to lift.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time

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
    """``headless`` always; ``headed`` only where a window could be seen (F-808)."""
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
    value = getattr(state, "value", state)
    # nodriver hands back ``ExceptionDetails`` when the script threw in the page.
    text = getattr(state, "text", None)
    description = getattr(getattr(state, "exception", None), "description", None)
    assert isinstance(value, str), (
        f"POKE_JS threw in the control tab: {description or text or repr(state)}"
    )
    return json.loads(value)


READY_JS = """
JSON.stringify({
  readyState: document.readyState,
  email: document.getElementById("email") !== null,
  url: location.href,
})
"""


async def _wait_probe_page(tab, timeout: float = 15.0) -> None:
    """Poll until the probe page has loaded and ``#email`` exists in ``tab``.

    ``open_tab`` returns before the page's load, and :data:`POKE_JS` reads
    ``#email`` at once, so a poke that beat the load threw in the page (T-8597).
    """
    deadline = time.monotonic() + timeout
    while True:
        seen = await tab.evaluate(READY_JS, return_by_value=True)
        raw = getattr(seen, "value", seen)
        state = json.loads(raw) if isinstance(raw, str) else {}
        if state.get("readyState") == "complete" and state.get("email"):
            return
        if time.monotonic() >= deadline:
            missing = (
                "#email"
                if state.get("readyState") == "complete"
                else ('readyState "complete"')
            )
            raise AssertionError(
                f"the control tab never showed {missing} within {timeout:g} s: "
                f"{state or repr(seen)}"
            )
        await asyncio.sleep(0.1)


@contextlib.asynccontextmanager
async def _staged(tmp_path, headless: bool, url: str):
    """Spawn, then yield ``(instance_id, control_tab, driven_tab_id)`` with both tabs
    covered by a third; the browser is closed on exit.

    The control is opened through ``tab_open`` alone, so nothing arms it and Chrome
    decides what it reads. That it reads ``hidden`` is the premise of every node.
    """
    new_tab, close = get_fn("new_tab"), get_fn("close_instance")
    iid = await _spawn(tmp_path, headless)
    try:
        browser = await runtime.browser_manager.get_browser(iid)
        control = await tab_open.open_tab(browser, url)
        await _wait_probe_page(control)
        driven = (await new_tab(instance_id=iid, url=url))["tab_id"]
        # Something in front of both, so neither is the visible tab.
        await tab_open.open_tab(browser, "about:blank")

        seen = await control.evaluate("document.visibilityState", return_by_value=True)
        visibility = getattr(seen, "value", seen)
        # Bare Xvfb has no window manager: a covered tab can stay visible there.
        if not headless and sys.platform.startswith("linux") and visibility != "hidden":
            pytest.skip(
                f"headed Chrome on this Linux display reports a covered tab "
                f"{visibility!r} (bare Xvfb, no window manager): there is nothing "
                f"hidden for the focus emulation to lift"
            )
        assert visibility == "hidden", f"a covered, unarmed tab reads {visibility!r}"
        yield iid, control, driven
    finally:
        await close(instance_id=iid)


async def _page_state(iid: str, tab_id: str) -> dict:
    answer = await get_fn("execute_script")(
        instance_id=iid, script=PAGE_STATE_JS, tab_id=tab_id
    )
    assert answer.get("success") is True, answer
    return answer["result"]


@pytest.mark.parametrize("headless", _modes())
async def test_type_text_lands_in_a_covered_tab(tmp_path, headless, fixture_app_server):
    url = f"{fixture_app_server}/hidden_input_probe.html"
    async with _staged(tmp_path, headless, url) as (iid, _control, driven):
        typed = await get_fn("type_text")(
            instance_id=iid, selector="#email", text=TYPED, delay_ms=10, tab_id=driven
        )
        assert typed is True

        page = await _page_state(iid, driven)
        assert page["visibility"] == "visible", page
        assert page["hasFocus"] is True, page
        assert page["value"] == TYPED, page
        assert page["probe"]["dropped"] == 0, page


@pytest.mark.parametrize("headless", _modes())
async def test_click_element_lands_in_a_covered_tab(
    tmp_path, headless, fixture_app_server
):
    url = f"{fixture_app_server}/hidden_input_probe.html"
    async with _staged(tmp_path, headless, url) as (iid, _control, driven):
        await get_fn("click_element")(instance_id=iid, selector="#go", tab_id=driven)

        page = await _page_state(iid, driven)
        assert page["visibility"] == "visible", page
        assert page["hasFocus"] is True, page
        assert page["clicks"] == "1", page
        assert page["probe"]["clicks"] == 1, page
        assert page["probe"]["dropped"] == 0, page


@pytest.mark.parametrize("headless", _modes())
async def test_the_fixture_drops_input_while_hidden(
    tmp_path, headless, fixture_app_server
):
    """Fixture self-test: the same keys and click, poked in-page at the covered,
    unarmed tab, are all received and all dropped. It proves the two nodes above can
    fail and says nothing about Chrome delivering input (whether a hidden page gets
    input from the browser varies with the rest of the window, which is why this
    pokes from inside the page)."""
    url = f"{fixture_app_server}/hidden_input_probe.html"
    async with _staged(tmp_path, headless, url) as (_iid, control, _driven):
        undriven = await _poke(control)
        assert undriven["visibility"] == "hidden", undriven
        assert undriven["value"] == "", undriven
        assert undriven["probe"]["clicks"] == 0, undriven
        assert undriven["probe"]["received"] == undriven["probe"]["dropped"] > 0, (
            undriven
        )
