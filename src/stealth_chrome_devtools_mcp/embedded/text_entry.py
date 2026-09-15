"""THE one home for pressing a key in a page, and for proving the text landed.

F-873. ``type_text`` used to answer ``True`` for two things it had not done,
and both answers came from the same place: nothing here asked Chrome anything.

**What a key press is.** Every key this tool sends goes out as
``Input.dispatchKeyEvent`` with a ``keyDown``/``keyUp`` pair, and the
``keyDown`` carries ``text``. That is not decoration:

* nodriver's ``Element.send_keys`` dispatches a lone ``char`` event per
  character (``core/element.py:708``). A ``char`` commits the character —
  ``keypress`` and ``input`` fire, trusted — but ``keydown`` and ``keyup``
  never do, so a page whose autocomplete, shortcut or validation handling is
  bound to ``keydown`` (which is most of them) saw a value appear without a
  single key being pressed.
* Enter is the same key with ``text="\\r"``, and the ``text`` is the whole of
  it. A form's *implicit submission* is performed by Blink on the **keypress**,
  and only a ``keyDown`` carrying ``text`` makes Chrome synthesise one.
  Measured against a one-input form with a submit listener, Chrome 152:
  a page-constructed ``KeyboardEvent`` (``isTrusted: false``) -> 0 submits;
  ``rawKeyDown`` (trusted, no keypress) -> 0 submits; ``keyDown(text="\\r")``
  -> keydown + keypress + **1 submit**. Adding a separate ``char`` event on top
  produced a second keypress and **2 submits** — which is why :func:`press_key`
  sends exactly two events and never a third.

**What a success is.** :func:`verify_received` is the answer to "did the page
take it". It compares the element's own text against the baseline read just
before the characters went out, and a control that took every event and moved
nothing is a FAILURE that raises. Measured on the same Chrome, every key event
delivered and the value unchanged: ``readonly``, ``range``, ``date``,
``color``. The tool used to report all four as success.

The test is "did anything change", deliberately, and not "does it now contain
exactly what I typed": an input mask, an autocomplete that rewrites, a
``number`` field that normalises, a page that upper-cases — all of those DID
receive the input, and a stricter test would have turned each of them into a
new false alarm, which is the same defect wearing the opposite sign.

**No message here ever carries the text.** A failed tool call reaches the
durable log, the debug ring and Sentry at once, and the field that refused may
be a password box — so every message reports shape and count only (the
selector, how many characters were typed, how many the element holds). Same
discipline as F-869's storage reader, for the same reason.

A leaf: ``nodriver`` and ``tool_errors`` only. The tab and the element both
arrive as arguments.
"""

from __future__ import annotations

import asyncio
import json

from nodriver import Tab, cdp

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: The Enter key, as Chrome's own keyboard produces it. ``text="\r"`` is what
#: makes Blink synthesise the keypress that performs implicit submission.
ENTER_KEY = "Enter"
ENTER_TEXT = "\r"
ENTER_VK = 13

#: ``Input.dispatchKeyEvent`` modifier bits: Alt=1, Ctrl=2, Meta=4, Shift=8.
SHIFT = 8
CTRL = 2

#: Select-all + Delete, the keyboard clear a programmatic ``elem.value = ''``
#: falls back to. ``("a", "KeyA", 65)`` under Ctrl, then Delete.
_SELECT_ALL_VK = 65
_DELETE_VK = 46

#: The ONE read-back. It answers with a JSON **string** because that is the one
#: shape ``Element.apply`` cannot corrupt or silently empty: ``apply`` returns
#: ``result[0].value`` and a script that THREW lands there as ``None``, so a
#: non-``str`` answer is "could not be read" and is reported as such rather
#: than mistaken for an empty field. ``contentEditable`` is read from
#: ``textContent`` because such an element has no ``value`` at all.
READ_JS = """(elem) => JSON.stringify({
    editable: !!elem.isContentEditable,
    text: elem.isContentEditable
        ? String(elem.textContent == null ? '' : elem.textContent)
        : String(elem.value == null ? '' : elem.value)
})"""


def _virtual_key_code(char: str) -> int:
    """The Windows virtual-key code for *char*, or 0 where there is none.

    Only the ASCII alphanumerics and space have one that means anything; a
    CJK ideograph, an accented letter or an astral emoji has no VK, and 0 is
    what Chrome itself reports for them. Measured: the full lifecycle carries
    ``"héllo 日本語 👍🏽 ß"`` into an input byte-for-byte with VK 0, exactly as
    the char-only dispatch and ``Input.insertText`` do.
    """
    if char.isascii() and (char.isalnum() or char == " "):
        return ord(char.upper())
    return 0


async def press_key(  # noqa: PLR0913  PERMANENT(a key event's own shape)
    tab: Tab,
    *,
    key: str,
    text: str,
    code: str | None = None,
    virtual_key_code: int = 0,
    modifiers: int = 0,
) -> None:
    """Press and release one key: the ONE way a key reaches the page.

    Exactly two events. The ``keyDown`` carries ``text`` (so Chrome synthesises
    the keypress); the ``keyUp`` does not, because a ``keyUp`` with ``text`` is
    meaningless and a third event carrying it would fire a second keypress.
    """
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyDown",
            modifiers=modifiers,
            text=text,
            unmodified_text=text,
            code=code,
            key=key,
            windows_virtual_key_code=virtual_key_code,
            native_virtual_key_code=virtual_key_code,
        )
    )
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyUp",
            modifiers=modifiers,
            code=code,
            key=key,
            windows_virtual_key_code=virtual_key_code,
            native_virtual_key_code=virtual_key_code,
        )
    )


async def press_enter(tab: Tab, *, shift: bool = False) -> None:
    """Press Enter, optionally with Shift held.

    ``shift`` is for the page's benefit, not Chrome's: implicit submission does
    not consult the modifier (measured — a shifted Enter submits a one-input
    form just the same), and a textarea takes a newline either way. What the
    modifier buys is that a chat app's ``event.shiftKey`` branch is reachable
    at all, which is the entire reason ``type_text`` has a ``shift_enter``.
    """
    await press_key(
        tab,
        key=ENTER_KEY,
        code=ENTER_KEY,
        text=ENTER_TEXT,
        virtual_key_code=ENTER_VK,
        modifiers=SHIFT if shift else 0,
    )


async def type_characters(tab: Tab, element: object, text: str, delay: float) -> None:
    """Type *text* one character at a time, full key lifecycle per character.

    The element is re-focused before each character, as the shipped path was:
    a page that moves focus mid-typing (an autocomplete flyout, a re-render)
    would otherwise send the rest of the string somewhere else.
    """
    for char in text:
        await element.focus()
        await press_key(
            tab, key=char, text=char, virtual_key_code=_virtual_key_code(char)
        )
        if delay:
            await asyncio.sleep(delay)


async def clear_via_keyboard(tab: Tab) -> None:
    """Select-all + Delete, for when a programmatic ``elem.value = ''`` fails.

    The ONE keyboard clear. ``type_text`` used to send WebDriver's private-use
    codepoints here (``"\\ue009"`` for Ctrl, ``"\\ue017"`` for Delete) through
    ``send_keys``, which dispatches them as literal ``char`` text — CDP has
    never spoken that protocol, so the fallback inserted two junk characters
    and cleared nothing.
    """
    await press_key(
        tab,
        key="a",
        code="KeyA",
        text="",
        virtual_key_code=_SELECT_ALL_VK,
        modifiers=CTRL,
    )
    await press_key(
        tab, key="Delete", code="Delete", text="", virtual_key_code=_DELETE_VK
    )


async def entered_text(element: object, selector: str) -> str:
    """What *element* currently holds, read through the one :data:`READ_JS`.

    Raises ``ToolError`` when the answer is not the promised JSON string:
    "I could not read it" and "it is empty" are different facts, and only one
    of them may be reported as an empty field.
    """
    answer = await element.apply(READ_JS)
    if not isinstance(answer, str):
        raise ToolError(
            f"could not read '{selector}' back to verify the text landed: "
            f"the page answered with {type(answer).__name__}, "
            "not the promised JSON string"
        )
    try:
        record = json.loads(answer)
    except ValueError:
        raise ToolError(
            f"could not read '{selector}' back to verify the text landed: "
            f"the {len(answer)}-character answer is not valid JSON"
        ) from None
    if not isinstance(record, dict) or not isinstance(record.get("text"), str):
        raise ToolError(
            f"could not read '{selector}' back to verify the text landed: "
            f"the answer carries no text field ({type(record).__name__})"
        )
    return record["text"]


def verify_received(selector: str, typed: str, before: str, after: str) -> None:
    """Raise unless the page took the characters.

    The failure this exists for is the one that used to answer ``True``: every
    key event delivered, the element's text exactly as it was.
    """
    if after != before:
        return
    raise ToolError(
        f"typed {len(typed)} character(s) into '{selector}' but the element's "
        f"text did not change (still {len(after)} character(s)) — the page did "
        "not accept the input. The control may be read-only or disabled, a "
        "non-text input type (range/date/color cannot be typed into), or "
        "governed by a script that cancels key events."
    )
