"""F-873 in a REAL Chrome: the Enter submits, and a refusal is reported.

The hermetic half (``tests/test_type_text_verification.py``) pins the CDP
frames ``type_text`` emits. This half pins what Chrome DOES with them, because
every claim F-873 rests on is a claim about Blink:

* only a trusted **keypress** performs a form's implicit submission, and the
  only way to get one from CDP is a ``keyDown`` carrying ``text``;
* a ``readonly``/``range``/``date``/``color`` control accepts every key event
  and moves nothing — the shape the tool used to answer ``True`` for.

Runs against ``interactions.html``, which already logs
``key:<down|up|press>:<key>:<trust>``, ``input:<id>:<value>`` and
``submit:<form-id>`` for exactly this purpose. Its own session root is a
``tmp_path`` (``tmp_empty_root``), so no run of this file can touch a real
profile.
"""

from __future__ import annotations

import json

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    read_actions,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _wait_action(iid, entry, timeout=5.0):
    js = f"window.__actions.indexOf({json.dumps(entry)}) >= 0"
    return await wait_for_js(iid, js, True, timeout=timeout)


async def test_parse_newlines_enter_submits_the_form(
    fixture_app_server, tmp_empty_root
):
    """The whole of defect A: ``#enter-form`` has ONE field and NO submit
    button, so nothing but a trusted Enter keypress can submit it. Before the
    fix the Enter was a page-constructed ``KeyboardEvent`` and this never
    fired, while ``type_text`` answered ``True``."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    type_text = get_fn("type_text")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        assert await type_text(
            instance_id=iid,
            selector="#enter-input",
            text="hello\n",
            parse_newlines=True,
        )

        assert await _wait_action(iid, "submit:enter-form")
    finally:
        await close(instance_id=iid)


async def test_typing_emits_the_full_trusted_key_lifecycle(
    fixture_app_server, tmp_empty_root
):
    """keydown AND keyup now fire, both trusted — a page whose autocomplete or
    shortcut handling is bound to ``keydown`` sees the keys at last."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    type_text = get_fn("type_text")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        assert await type_text(instance_id=iid, selector="#key-probe", text="ab")

        actions = await read_actions(iid)
        assert any(a.startswith("key:down:") for a in actions), actions
        assert any(a.startswith("key:up:") for a in actions), actions
        assert not any(
            a.startswith("key:") and a.endswith(":untrusted") for a in actions
        ), actions
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize(
    "selector,text",
    [
        ("#readonly-input", "INJECT"),
        ("#range-input", "80"),
        ("#date-input", "2024-01-02"),
        ("#color-input", "#123456"),
    ],
)
async def test_a_control_that_refuses_the_text_raises(
    fixture_app_server, tmp_empty_root, selector, text
):
    """Defect B, in the four shapes a real Chrome reproduces deterministically.

    Each of these takes every key event and leaves its value exactly where it
    was (measured, Chrome 152). ``type_text`` used to answer ``True`` for all
    four; it now names the selector and says nothing landed.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    type_text = get_fn("type_text")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        with pytest.raises(ToolError) as caught:
            await type_text(instance_id=iid, selector=selector, text=text)

        message = str(caught.value)
        assert selector in message
        assert text not in message  # shape and count only, never the content
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize(
    "selector,text",
    [("#number-input", "42"), ("#key-probe", "usb c hub"), ("#enter-input", "héllo")],
)
async def test_a_control_that_accepts_the_text_still_succeeds(
    fixture_app_server, tmp_empty_root, selector, text
):
    """The guard against over-correcting: every control that really does take
    typed characters still answers ``True``, with the value to prove it."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    type_text = get_fn("type_text")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/interactions.html")

        assert await type_text(instance_id=iid, selector=selector, text=text)
        assert await eval_js(iid, f"document.querySelector({selector!r}).value") == text
    finally:
        await close(instance_id=iid)
