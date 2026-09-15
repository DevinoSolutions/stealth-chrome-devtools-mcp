"""F-873: ``type_text`` returned ``True`` for two things it had not done.

Two defects, one shape — "reported success, changed nothing":

* **A — the Enter that cannot submit.** ``parse_newlines``'s Enter was a
  ``KeyboardEvent`` constructed in the page by ``element.apply``. An event a
  script constructs is ``isTrusted: false`` and carries no ``charCode``, and a
  form's *implicit submission* is performed by Blink on the **keypress** of a
  trusted Enter. Measured on Chrome 152, against a one-input form with a submit
  listener: synthetic keydown -> 0 submits; ``rawKeyDown`` (trusted, no
  keypress) -> 0 submits; ``keyDown`` carrying ``text="\\r"`` -> keydown +
  keypress + **1 submit**. Adding a separate ``char`` event on top produced a
  SECOND keypress and **2 submits**, so the one keyDown is not merely
  sufficient, it is the whole of it.

* **B — the success that was never checked.** Nothing between "dispatch the
  events" and ``return True`` ever asked the page whether the characters
  landed. Measured, same Chrome: ``readonly``, ``range``, ``date`` and
  ``color`` controls each take every key event and leave their value exactly
  where it was — and the tool answered ``True`` for all four.

The pins here are hermetic (``FakeTab`` + ``FakeTextField`` from
``tests/fakes.py``); the real-Chrome half is
``tests/test_e2e_type_text_verification.py``.
"""

from __future__ import annotations

import pytest

from fakes import FakeTab, FakeTextField
from stealth_chrome_devtools_mcp.embedded import text_entry
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

SELECTOR = "#q"


def _key_frames(tab, *, type_=None):
    """Every ``Input.dispatchKeyEvent`` this tab was sent, params only."""
    frames = [
        f["params"]
        for f in tab.cdp_frames
        if f.get("method") == "Input.dispatchKeyEvent"
    ]
    if type_ is not None:
        frames = [p for p in frames if p.get("type") == type_]
    return frames


def _field_tab(**kwargs):
    field = FakeTextField(**kwargs)
    return FakeTab(select_result=field), field


# ---------------------------------------------------------------------------
# A -- the Enter must be a real key press
# ---------------------------------------------------------------------------


async def test_enter_is_dispatched_as_a_real_key_press():
    """The Enter reaches Chrome as ``Input.dispatchKeyEvent``, not page JS.

    RED before the fix: the only Enter was a ``KeyboardEvent`` built inside
    ``element.apply``, so no key event was dispatched at all.
    """
    tab, field = _field_tab(multiline=True)

    assert await DOMHandler.type_text(
        tab, SELECTOR, "abc\ndef", delay_ms=0, parse_newlines=True
    )

    enters = [p for p in _key_frames(tab) if p.get("key") == "Enter"]
    assert enters, tab.cdp_frames
    assert not any("KeyboardEvent" in js for js in field.apply_calls), field.apply_calls


async def test_enter_keydown_carries_the_text_that_makes_chrome_submit():
    """``keyDown`` must carry ``text="\\r"`` -- that is what produces the
    keypress a form's implicit submission is performed on (measured, Chrome
    152). ``key``/``code``/``windowsVirtualKeyCode`` come with it so a page's
    own keydown handler sees the key it would see from a keyboard."""
    tab, _ = _field_tab(multiline=True)

    await DOMHandler.type_text(
        tab, SELECTOR, "abc\ndef", delay_ms=0, parse_newlines=True
    )

    downs = [p for p in _key_frames(tab, type_="keyDown") if p.get("key") == "Enter"]
    assert len(downs) == 1, downs
    assert downs[0]["text"] == "\r"
    assert downs[0]["code"] == "Enter"
    assert downs[0]["windowsVirtualKeyCode"] == 13


async def test_enter_is_not_dispatched_twice():
    """No separate ``char`` event on top of the ``keyDown``.

    Measured: ``keyDown(text="\\r")`` + ``char(text="\\r")`` fires keypress
    TWICE and submits the form TWICE. One key press is one submit.
    """
    tab, _ = _field_tab(multiline=True)

    await DOMHandler.type_text(
        tab, SELECTOR, "abc\ndef", delay_ms=0, parse_newlines=True
    )

    enters = [p for p in _key_frames(tab) if p.get("key") == "Enter"]
    assert [p["type"] for p in enters] == ["keyDown", "keyUp"], enters


async def test_shift_enter_carries_the_shift_modifier():
    """``shift_enter=True`` must reach the page as a real Shift+Enter, so a
    chat app's ``event.shiftKey`` branch is taken. Shift is bit 8."""
    tab, _ = _field_tab(multiline=True)

    await DOMHandler.type_text(
        tab,
        SELECTOR,
        "abc\ndef",
        delay_ms=0,
        parse_newlines=True,
        shift_enter=True,
    )

    downs = [p for p in _key_frames(tab, type_="keyDown") if p.get("key") == "Enter"]
    assert downs and downs[0].get("modifiers") == 8, downs


async def test_characters_are_typed_with_the_full_key_lifecycle():
    """Each character gets a keyDown AND a keyUp.

    RED before the fix: ``Element.send_keys`` dispatched a lone ``char`` event
    per character, so ``keypress``+``input`` fired and ``keydown``/``keyup``
    never did -- a page whose autocomplete or shortcut handling is bound to
    ``keydown`` saw nothing at all.
    """
    tab, _ = _field_tab()

    await DOMHandler.type_text(tab, SELECTOR, "ab", delay_ms=0)

    assert [p["type"] for p in _key_frames(tab)] == [
        "keyDown",
        "keyUp",
        "keyDown",
        "keyUp",
    ]
    assert [p["text"] for p in _key_frames(tab, type_="keyDown")] == ["a", "b"]


# ---------------------------------------------------------------------------
# B -- success must be verified against the page
# ---------------------------------------------------------------------------


async def test_raises_when_the_control_does_not_accept_the_text():
    """A control that takes the events and keeps its value is a FAILURE.

    This is the measured Amazon/Gmail shape and the measured readonly/range/
    date/color shape alike: every key event delivered, the element's text
    unchanged. Before the fix the tool returned ``True``.
    """
    tab, _ = _field_tab(accepts=False)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.type_text(tab, SELECTOR, "usb c hub", delay_ms=0)

    message = str(caught.value)
    assert SELECTOR in message
    assert "9" in message  # the number of characters typed


async def test_the_failure_never_carries_the_typed_text():
    """The message reports shape and count only.

    A ``type_text`` failure reaches the durable log, the debug ring and Sentry
    at once, and the field it failed on may be a password box -- so the one
    thing it must never repeat is what was typed (same discipline as F-869's
    storage reader).
    """
    secret = "hunter2-correct-horse"
    tab, _ = _field_tab(accepts=False)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.type_text(tab, SELECTOR, secret, delay_ms=0)

    assert secret not in str(caught.value)
    assert "hunter" not in str(caught.value)


async def test_raises_when_the_readback_is_not_the_promised_json():
    """An unreadable element is "cannot be verified", which is also a failure.

    ``Element.apply`` hands back ``result[0].value``, and a script that THREW
    lands there as ``None`` -- silently. Answering ``True`` on the strength of
    an answer we could not read would be the same lie in a new costume.
    """

    class _MuteField(FakeTextField):
        async def apply(self, js_function, *args, **kwargs):
            if "JSON.stringify" in js_function:
                return None
            return await super().apply(js_function, *args, **kwargs)

    field = _MuteField()
    tab = FakeTab(select_result=field)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.type_text(tab, SELECTOR, "abc", delay_ms=0)

    assert SELECTOR in str(caught.value)


async def test_returns_true_when_the_text_lands():
    """The guard against over-correcting: a control that accepts the text still
    answers ``True``, and the value is what was typed."""
    tab, field = _field_tab()

    assert await DOMHandler.type_text(tab, SELECTOR, "usb c hub", delay_ms=0) is True
    assert field.value == "usb c hub"


async def test_clear_first_then_typing_is_verified_against_the_cleared_state():
    """``clear_first`` empties the field programmatically; the verification
    baseline is what is there AFTER the clear, so the pre-existing text can
    never stand in for text that was never typed."""
    tab, field = _field_tab(value="old value", accepts=False)

    with pytest.raises(ToolError):
        await DOMHandler.type_text(tab, SELECTOR, "new", delay_ms=0)

    assert field.value == ""


async def test_the_clear_fallback_is_a_real_keyboard_clear():
    """``clear_first``'s fallback presses Ctrl+A then Delete over CDP.

    It used to send WebDriver's private-use codepoints (U+E009 for Ctrl, U+E017
    for Delete) through ``send_keys``, which dispatches them as literal ``char``
    text — CDP has never spoken that protocol. Measured against an
    ``<input value="preset-value">``: the old fallback left
    ``"\\ue009a\\ue017preset-value"``; ``clear_via_keyboard`` leaves ``""``.
    One home, shared with ``paste_text``.
    """
    field = FakeTextField(value="preset")
    tab = FakeTab(select_result=field)

    await text_entry.clear_via_keyboard(tab)

    downs = _key_frames(tab, type_="keyDown")
    # Modifier bits: Alt=1, Ctrl=2, Meta=4, Shift=8.
    assert [(p.get("key"), p.get("modifiers")) for p in downs] == [
        ("a", 2),
        ("Delete", 0),
    ], downs
    assert downs[0]["code"] == "KeyA"
    assert downs[1]["code"] == "Delete"


async def test_empty_text_is_not_a_failure():
    """Typing nothing changes nothing, and that is not a defect -- the
    verification must not fire on a caller that only wanted the clear."""
    tab, field = _field_tab(value="old value")

    assert await DOMHandler.type_text(tab, SELECTOR, "", delay_ms=0) is True
    assert field.value == ""
