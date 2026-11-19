"""Page-side detection of a login form, for the vault's sealed login (T-19366).

``login_flow`` needs four answers about the sealed tab and nothing else: is a
username field showing, is a password field showing, is a one-time-code field
showing, and did a password field keep what was typed. This module asks them, in
the package's isolated world (:func:`page_natives.frame_world`) so the page's own
scripts cannot change an answer (a page that redefines ``HTMLInputElement`` or
``getComputedStyle`` is answered by the browser's originals), and hands back the
field itself for typing.

**Rules** (the design's §6), all about VISIBLE inputs. Visible means a non-zero
box, not ``display:none``, not ``visibility:hidden``, not ``disabled``: a hidden
decoy password field a page plants is not "the" password field.

* password: ``input[type=password]``;
* username: ``autocomplete`` containing ``username`` or ``email``, or
  ``type=email``, or (only when a password field is also showing) the text input
  that comes before it in the same form;
* one-time code: ``autocomplete=one-time-code``, or ``inputmode=numeric`` with a
  ``maxlength`` of 8 or less.

A one-time-code field is never also reported as the username.

**Where.** The top document and its DIRECT child frames, top first (a login widget
in one iframe is common; a frame inside a frame is not a login form a vault should
type into). A child frame that Chrome serves from another process answers nothing
here and reads as absent, which fails toward "unknown form" (``needs_user``).
:func:`clear_passwords` is the exception: it walks EVERY frame, because clearing
is the safe direction.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from nodriver import cdp
from nodriver.core import element as nodriver_element

from stealth_chrome_devtools_mcp.embedded import page_natives
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from nodriver import Tab
    from nodriver.core.element import Element

KINDS = ("username", "password")

#: The longest :func:`wait_for_frame` waits for the page to produce a frame.
FRAME_WAIT_S = 2.0

#: The shared helpers every script below starts with. Everything is read through
#: the isolated world's own prototypes, and nothing leaves the page except booleans
#: and (for :func:`find_field`) one node reference.
_LIB_JS = """
const visible = (el) => {
    if (el.disabled) return false;
    if (el.getClientRects().length === 0) return false;
    const box = el.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden';
};
const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
const autocomplete = (el) => (el.getAttribute('autocomplete') || '').toLowerCase();
const isOtp = (el) => {
    if (autocomplete(el).split(/\\s+/).includes('one-time-code')) return true;
    const mode = (el.getAttribute('inputmode') || '').toLowerCase();
    return mode === 'numeric' && el.maxLength > 0 && el.maxLength <= 8;
};
const passwords = inputs.filter((el) => el.type === 'password');
const otps = inputs.filter((el) => el.type !== 'password' && isOtp(el));
const textual = (el) => ['text', 'email', 'tel', 'search', ''].includes(el.type);
const named = (el) => /(^|\\s)(username|email)(\\s|$)/.test(autocomplete(el))
    || el.type === 'email';
// An explicit error the page is SHOWING: an alert with text, or a field marked
// invalid. It is what tells a rejected password from a page that merely blanked
// the field while its request is in flight.
const errors = () => Array.from(document.querySelectorAll(
    '[role=alert], [aria-live=assertive], [aria-invalid=true]'
)).filter((el) => visible(el) && (
    el.getAttribute('aria-invalid') === 'true' || (el.innerText || '').trim() !== ''
));
const usernames = () => {
    const direct = inputs.filter((el) => !isOtp(el) && textual(el) && named(el));
    if (direct.length) return direct;
    if (!passwords.length) return [];
    const password = passwords[0];
    const siblings = inputs.filter(
        (el) => el.form === password.form && !isOtp(el) && textual(el)
    );
    const before = siblings.filter(
        (el) => el.compareDocumentPosition(password) & 4 /* following */
    );
    return before.length ? [before[before.length - 1]] : [];
};
"""

#: Answers ``{"username", "password", "otp", "password_has_value", "error"}``, all
#: booleans.
PROBE_JS = (
    "(() => {"
    + _LIB_JS
    + """
    return {
        username: usernames().length > 0,
        password: passwords.length > 0,
        otp: otps.length > 0,
        password_has_value: passwords.some((el) => el.value !== ''),
        error: errors().length > 0,
    };
})()"""
)

#: Sets every password input to ``''`` (visible or not), answers how many it found.
_CLEAR_JS = """(() => {
    const all = Array.from(document.querySelectorAll('input[type=password]'));
    for (const el of all) el.value = '';
    return all.length;
})()"""


#: Where the form around a field would send what is typed in it: the default
#: button's ``formaction`` when it has one (implicit submission uses that button),
#: else the form's own ``action``, both RESOLVED against the document by the
#: browser. ``null`` for a field in no form (a script submits those).
_ACTION_JS = """const form = (target) => {
    if (!target || !target.form) return null;
    const fallback = target.form.querySelector(
        'button:not([type]), button[type=submit], input[type=submit], input[type=image]'
    );
    return fallback && fallback.hasAttribute('formaction')
        ? fallback.formAction : target.form.action;
};"""


def _find_js(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    picked = "usernames()[0]" if kind == "username" else "passwords[0]"
    return "(() => {" + _LIB_JS + f"return {picked} || null; }})()"


def _frames(tree: cdp.page.FrameTree, *, everything: bool) -> list[cdp.page.FrameId]:
    """The main frame first, then its direct children; every frame when
    ``everything``."""
    ids = [tree.frame.id_]
    for child in tree.child_frames or []:
        ids.extend(_frames(child, everything=True) if everything else [child.frame.id_])
    return ids


async def _frame_ids(tab: Tab, *, everything: bool = False) -> list[cdp.page.FrameId]:
    return _frames(await tab.send(cdp.page.get_frame_tree()), everything=everything)


async def _world(
    tab: Tab, frame_id: cdp.page.FrameId
) -> cdp.runtime.ExecutionContextId | None:
    """The isolated world of ``frame_id``, or ``None`` when Chrome will not give
    one (the frame went away, or it lives in another process)."""
    try:
        return await page_natives.frame_world(tab, frame_id)
    except Exception as error:  # noqa: BLE001  PERMANENT(a frame that has gone or lives elsewhere reads as absent)
        debug_logger.log_debug(
            "login_form", "_world", f"no world for a frame: {type(error).__name__}"
        )
        return None


async def _evaluate(
    tab: Tab, world: cdp.runtime.ExecutionContextId, expression: str, *, by_value: bool
) -> cdp.runtime.RemoteObject | None:
    result, thrown = await tab.send(
        cdp.runtime.evaluate(
            expression, context_id=world, return_by_value=by_value, user_gesture=False
        )
    )
    return None if thrown is not None else result


async def probe(tab: Tab, frame_id: str | None = None) -> dict[str, bool]:
    """What the sealed tab is showing: ``{"username", "password", "otp",
    "password_has_value"}``.

    With ``frame_id`` that one frame; without, the top document and its direct
    child frames together (a flag is true when any of them has it)."""
    frames = [cdp.page.FrameId(frame_id)] if frame_id else await _frame_ids(tab)
    answer = dict.fromkeys(
        ("username", "password", "otp", "password_has_value", "error"), False
    )
    for frame in frames:
        world = await _world(tab, frame)
        if world is None:
            continue
        result = await _evaluate(tab, world, PROBE_JS, by_value=True)
        if result is not None and isinstance(result.value, dict):
            for key in answer:
                answer[key] = answer[key] or result.value.get(key) is True
    return answer


async def find_field(tab: Tab, kind: str) -> tuple[Element, str] | None:
    """The visible ``kind`` (``"username"`` or ``"password"``) field as
    ``(element, frame_id)``, or ``None``. The top document first, then each
    direct child frame."""
    script = _find_js(kind)
    for frame in await _frame_ids(tab):
        world = await _world(tab, frame)
        if world is None:
            continue
        result = await _evaluate(tab, world, script, by_value=False)
        if result is None or result.object_id is None or result.subtype == "null":
            continue
        node = await tab.send(cdp.dom.describe_node(object_id=result.object_id))
        return nodriver_element.create(node, tab), str(frame)
    return None


async def form_action(tab: Tab, kind: str, frame_id: str) -> str | None:
    """Where the form around the visible ``kind`` field of ``frame_id`` submits to,
    as an absolute URL the browser resolved, or ``None`` when that field is in no
    form (or is gone)."""
    picked = "usernames()[0]" if kind == "username" else "passwords[0]"
    script = "(() => {" + _LIB_JS + _ACTION_JS + f"return form({picked}); }})()"
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    world = await _world(tab, cdp.page.FrameId(frame_id))
    if world is None:
        return None
    result = await _evaluate(tab, world, script, by_value=True)
    value = None if result is None else result.value
    return value if isinstance(value, str) else None


async def wait_for_frame(tab: Tab, frame_id: str) -> None:
    """Return once ``frame_id`` has produced a frame (two animation frames), or after
    :data:`FRAME_WAIT_S`. Best effort: it never raises.

    The first key sent to a tab opened behind another one is dropped, even after
    its page already reads ``visible`` and focused (focus emulation): measured in a
    headed Chrome 158, the first character of the first field was lost in about
    one load in three, and never once a frame had been produced. A frame is the
    page's own sign that its widget is live. A page that never makes one (its
    window cannot paint) costs the bound and then fails toward "did not take the
    text" (:func:`holds`), never toward a wrong submit."""
    world = await _world(tab, cdp.page.FrameId(frame_id))
    if world is None:
        return
    script = (
        "new Promise((done) => requestAnimationFrame("
        "() => requestAnimationFrame(() => done(true))))"
    )
    with contextlib.suppress(Exception):
        async with asyncio.timeout(FRAME_WAIT_S):
            await tab.send(
                cdp.runtime.evaluate(
                    script, context_id=world, await_promise=True, user_gesture=False
                )
            )


async def holds(element: Element, length: int) -> bool:
    """Whether ``element`` holds exactly ``length`` characters (code points, as
    Python counts them). Answers a boolean: the text is never read out of the page,
    so a dropped key is caught without the value leaving it."""
    script = f"(e) => [...(e.value || '')].length === {int(length)}"
    return (await element.apply(script)) is True


async def clear_passwords(tab: Tab) -> None:
    """Set every password input of every frame to ``""``. Best effort per frame:
    a frame that has gone is skipped, which leaves nothing to clear in it."""
    for frame in await _frame_ids(tab, everything=True):
        world = await _world(tab, frame)
        if world is None:
            continue
        try:
            await _evaluate(tab, world, _CLEAR_JS, by_value=True)
        except Exception as error:  # noqa: BLE001  PERMANENT(clearing is best effort; the tab is closed next)
            debug_logger.log_debug(
                "login_form",
                "clear_passwords",
                f"a frame could not be cleared: {type(error).__name__}",
            )


__all__ = [
    "KINDS",
    "PROBE_JS",
    "clear_passwords",
    "find_field",
    "form_action",
    "holds",
    "probe",
    "wait_for_frame",
]
