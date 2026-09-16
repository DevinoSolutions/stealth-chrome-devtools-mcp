"""F-876: ``paste_text`` and ``click_element`` reported a dispatch, not a result.

Two tools, one shape — the shape F-873 named and left for this PR:

* **``paste_text``** sent one ``Input.insertText`` and returned ``True``.
  Measured on Chrome 152.0.7977.83, product code path, throwaway profile: a
  ``readonly`` input, a ``range``/``date``/``color`` control and a non-editable
  ``<div>`` all take the insert and move nothing — five of seven controls, every
  one of them answered ``True``.

* **``click_element``** dispatched a coordinate click and returned ``True``.
  Same run: an overlay eats the click (the page logs ``click:overlay``, not
  ``click:covered``); a ``disabled`` control, a ``pointer-events:none`` target, a
  zero-size target and a ``visibility:hidden`` target each receive **nothing**;
  and a ``display:none`` target makes ``Element.mouse_click`` raise, so the tool
  silently downgrades to the synthetic, **untrusted** ``Element.click``. All five
  answered ``True``, identically to the control.

The fixes differ because the honest answers differ. ``paste_text`` joins
``text_entry``'s read-back and RAISES when the page refused. ``click_element``
gains a RECORD — which kind of click was dispatched, and what
``document.elementFromPoint`` saw at the click point — because "did the page
react" is unbounded and its absence is not evidence.

Hermetic (``FakeTab`` + ``FakeTextField`` + ``FakeClickTarget`` from
``tests/fakes.py``); the real-Chrome half is
``tests/test_e2e_paste_click_verification.py``.
"""

from __future__ import annotations

import json

import pytest

from fakes import FakeClickTarget, FakeTab, FakeTextField
from stealth_chrome_devtools_mcp.embedded import text_entry
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

SELECTOR = "#q"
CLICK_SELECTOR = "#covered"


def _field_tab(**kwargs):
    field = FakeTextField(**kwargs)
    return FakeTab(select_result=field), field


def _click_tab(**kwargs):
    target = FakeClickTarget(**kwargs)
    return FakeTab(select_result=target), target


def _inserted(tab):
    """Every ``Input.insertText`` frame this tab was sent, params only."""
    return [
        f["params"] for f in tab.cdp_frames if f.get("method") == "Input.insertText"
    ]


# ---------------------------------------------------------------------------
# paste_text -- the page has to be asked
# ---------------------------------------------------------------------------


async def test_paste_into_a_refusing_control_raises():
    """The whole of the paste defect: every event delivered, nothing moved.

    RED before the fix: ``paste_text`` returned ``True`` here, exactly as it did
    for the five real controls in the finding's §2a matrix.
    """
    tab, field = _field_tab(value="", accepts=False)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.paste_text(tab, SELECTOR, "INJECT")

    assert _inserted(tab), "the insert must still have been attempted"
    assert field.value == ""
    assert SELECTOR in str(caught.value)


async def test_paste_failure_message_carries_no_pasted_text():
    """A raised ``ToolError`` reaches the caller, the debug ring and Sentry, and
    the field may be a password box. Shape and counts only."""
    tab, _ = _field_tab(accepts=False)
    secret = "hunter2-correct-horse"

    with pytest.raises(ToolError) as caught:
        await DOMHandler.paste_text(tab, SELECTOR, secret)

    message = str(caught.value)
    assert secret not in message
    assert str(len(secret)) in message


async def test_paste_reads_the_field_back_through_the_one_home():
    """The read-back is ``text_entry``'s, not a second one grown here."""
    tab, field = _field_tab(accepts=True)

    await DOMHandler.paste_text(tab, SELECTOR, "abc")

    assert text_entry.READ_JS in field.apply_calls, field.apply_calls


async def test_paste_baseline_is_the_state_after_the_clear():
    """``clear_first`` empties the field, so the baseline must be read AFTER it.

    A baseline read before the clear would be the preset value, the after-read
    would be the pasted text, and a control that refused everything would still
    look like it had moved.
    """
    tab, field = _field_tab(value="preset-value", accepts=False)

    with pytest.raises(ToolError):
        await DOMHandler.paste_text(tab, SELECTOR, "new-text", clear_first=True)

    assert field.value == ""


async def test_paste_into_an_accepting_control_still_returns_true():
    """The guard against over-correcting."""
    tab, field = _field_tab(accepts=True)

    assert await DOMHandler.paste_text(tab, SELECTOR, "usb c hub") is True
    assert field.value == "usb c hub"


async def test_paste_into_a_contenteditable_is_verified_the_same_way():
    tab, field = _field_tab(accepts=True, content_editable=True)

    assert await DOMHandler.paste_text(tab, SELECTOR, "hello-ce") is True
    assert field.value == "hello-ce"


async def test_paste_into_a_refusing_contenteditable_raises():
    tab, _ = _field_tab(accepts=False, content_editable=True)

    with pytest.raises(ToolError):
        await DOMHandler.paste_text(tab, SELECTOR, "hello-ce")


async def test_paste_of_empty_text_is_not_a_failure():
    """Pasting nothing changes nothing, and that is not a refusal."""
    tab, _ = _field_tab(accepts=True)

    assert await DOMHandler.paste_text(tab, SELECTOR, "") is True


async def test_paste_read_back_that_cannot_be_read_raises():
    """ "I could not read it" and "it is empty" are different facts."""

    class _Mute(FakeTextField):
        """A field whose read-back answers nothing — which is the shape
        ``Element.apply`` hands back when the script THREW."""

        async def apply(self, js_function, *args, **kwargs):
            await super().apply(js_function, *args, **kwargs)

    tab = FakeTab(select_result=_Mute())
    with pytest.raises(ToolError) as caught:
        await DOMHandler.paste_text(tab, SELECTOR, "abc")
    assert "could not read" in str(caught.value)


# ---------------------------------------------------------------------------
# click_element -- the record
# ---------------------------------------------------------------------------


async def test_click_answers_a_record_naming_the_dispatch():
    """RED before the fix: the answer was the bare ``True`` that cannot say
    which kind of click happened, let alone where it landed."""
    tab, _ = _click_tab()

    result = await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert isinstance(result, dict), result
    assert result["selector"] == CLICK_SELECTOR
    assert result["dispatch"] == "coordinate"
    assert result["reason"] is None


async def test_click_record_names_the_element_under_the_click_point():
    """The overlay case, which is the one a caller cannot otherwise see."""
    tab, _ = _click_tab(
        element_id="covered", hit=("div", "overlay", ("scrim", "modal"))
    )

    result = await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert result["hit"] == {
        "tag": "div",
        "id": "overlay",
        "classes": ["scrim", "modal"],
    }
    assert result["hit_is_target"] is False
    assert result["reason"] == "covered"
    assert result["target"] == {"tag": "button", "id": "covered", "classes": []}


async def test_click_point_is_the_centre_of_the_first_client_rect():
    """``Element.mouse_click`` clicks ``Position(quads[0]).center``. Measured on
    Chrome 152: ``getClientRects()[0]``'s centre is byte-equal to it, while
    ``getBoundingClientRect()``'s is 28.5 px off for an inline element wrapped
    over four line boxes — so the record would otherwise name a point the click
    never used."""
    tab, _ = _click_tab(rect=(100.0, 200.0, 60.0, 20.0))

    result = await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert result["point"] == {"x": 130.0, "y": 210.0}
    assert result["size"] == {"width": 60.0, "height": 20.0}


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"disabled": True}, "disabled"),
        (
            {"pointer_events": "none", "hit": ("body", "", ())},
            "pointer-events-none",
        ),
        (
            {"rect": (10.0, 20.0, 0.0, 0.0), "hit": ("body", "", ())},
            "zero-size",
        ),
        (
            {"visibility": "hidden", "hit": ("body", "", ())},
            "not-visible",
        ),
        ({"hit": ("div", "overlay", ())}, "covered"),
    ],
)
async def test_click_record_names_why_the_target_could_not_have_taken_it(
    kwargs, expected
):
    """The closed reason code set, one row per measured shape (§2c).

    Each of these dispatches a real coordinate click that the target does not
    receive; the tool used to answer ``True`` for every one of them.
    """
    tab, _ = _click_tab(**kwargs)

    result = await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert result["dispatch"] == "coordinate"
    assert result["reason"] == expected


async def test_a_target_with_no_box_is_labelled_synthetic():
    """``display:none``: ``mouse_click`` raises (no content quads), the product
    falls back to ``Element.click`` -- an UNTRUSTED in-page ``el.click()``. The
    fallback is kept, because it is the only thing that reaches such an element
    at all; what changes is that the caller is told."""
    tab, target = _click_tab(
        rendered=False, mouse_click_error=RuntimeError("could not find position")
    )

    result = await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert result["dispatch"] == "synthetic"
    assert result["reason"] == "not-rendered"
    assert result["point"] is None
    assert result["size"] is None
    assert result["hit"] is None
    assert "click" in target.calls


async def test_the_aim_is_read_before_the_click_is_dispatched():
    """Reading afterwards would describe a page the click may already have
    changed -- and for a ``display:none`` target there is no node left to ask."""
    tab, target = _click_tab()

    await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert target.calls == ["scroll_into_view", "aim", "mouse_click"]


async def test_the_record_carries_no_text_content():
    """An overlay is frequently a consent banner or a modal; its words are the
    page's, not the tool's to echo into an MCP payload."""
    tab, _ = _click_tab(text="SECRET-BUTTON-LABEL", hit=("div", "overlay", ()))

    result = await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert "SECRET-BUTTON-LABEL" not in json.dumps(result)


async def test_a_long_class_list_is_bounded():
    """A page can put two hundred utility classes on one element; a record that
    copied them all would be a payload, not a diagnostic."""
    from stealth_chrome_devtools_mcp.embedded import click_target

    many = tuple(f"c{i}" for i in range(50))
    tab, _ = _click_tab(classes=many)

    result = await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert len(result["target"]["classes"]) == click_target.MAX_CLASSES


async def test_an_aim_that_cannot_be_read_raises():
    """Same discipline as ``text_entry.entered_text``: the promised answer is a
    JSON string, and anything else is "could not be read", never a blank
    record. The aim is read BEFORE the click, so raising here costs a click that
    never happened rather than hiding one that did."""
    tab, _ = _click_tab(aim_answer=None)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, CLICK_SELECTOR)

    assert "could not read" in str(caught.value)
