"""THE one home for "an element with no box is reported without quoting the page".

F-912. ``nodriver/core/element.py``:498-499 is the one raise in the whole
library that interpolates a live ``Element`` into its own message::

    quads = await self.tab.send(
        cdp.dom.get_content_quads(object_id=self.remote_object.object_id)
    )
    if not quads:
        raise Exception("could not find position for %s " % self)

and ``Element.__repr__`` renders the tag, **every attribute as ``name="value"``
and the element's whole recursive descendant TEXT**. MEASURED against Chrome
153 and nodriver 0.47.0::

    could not find position for <input id="pwhidden" type="password"
        value="SECRET-VALUE-MARKER" style="display:none"></input>

**It is reachable, and by three ordinary shapes.** Chrome answers
``DOM.getContentQuads`` with an EMPTY LIST rather than an error for an element
that lays out nothing — measured on ``display:none``, on an ``<option>`` inside
a ``<select>``, and by construction on any detached node — while
``visibility:hidden``, a zero-size box, an empty inline and
``content-visibility:hidden`` all answer one quad and never reach it. That is
the difference from F-907, whose three box-model WARNINGs are unreachable in
0.47 because ``Position.center`` is always truthy: **this branch runs for real
users**, and it is a bare ``Exception``, so it propagates straight past
``Element.mouse_click``'s ``except AttributeError`` and out of the call.

Where it went, measured through our own handlers on a real page:

* ``dom_handler.get_element_state`` → ``ToolError("Failed to get element
  state: could not find position for <input … value="SECRET-VALUE-MARKER" …>")``
  — the **client**, and the debug **ring** through ``log_tool_failure``;
* ``dom_handler.click_element`` → ``debug_logger.log_debug(..., str(e))`` — the
  backend log at DEBUG and, under ``--debug``, stderr; the click itself still
  lands, through ``click_target.SYNTHETIC``;
* ``dom_handler.query_elements`` → ``type(e).__name__`` only, which is the one
  of the three that was already safe.

**Why the fix is here and not at those three sites.** F-907 proved a logging
mechanism cannot reach an exception: there is no ``LogRecord``, and the factory
inside ``Logger.makeRecord`` is upstream of sinks, not of raises. A rule at each
``except`` would have to key on the message TEXT — the keying F-907 §3 rejects,
because a library is free to reword its own sentences — and would leave the next
caller of ``get_position``, ``mouse_drag`` or ``save_screenshot`` to re-open it.
The ELEMENT is still an OBJECT at the raise, which is the one place F-907's
shape rule can be applied to it the way F-907 applies it: by TYPE, never by
text. That is exactly the home F-907's own residual 9 names for a
payload-bearing library exception — "shape-only **at the source**", on F-902's
``cdp_transport.CdpReplyError`` precedent.

**Why a module of its own rather than a fourth half of ``cdp_transport``.**
That module's docstring forbids splitting ITS question across two seams, and the
question is the CONNECTION: "handing the listener a reply must not end it". An
element's box is not that. ``browser_connect`` is the standing precedent — a
separate leaf, one question, its own ``install()``, installed at the site that
needs it — and "every nodriver patch in one file" is not an invariant this tree
has ever held.

**The key is the exception's TYPE and the raise site, never its text.**
``type(exc) is Exception`` — the bare builtin, exactly, not a subclass — is what
``element.py``:499 raises and what nothing else in ``get_position`` produces: a
CDP failure there is a ``ProtocolException`` or a ``CdpReplyError``, a missing
remote object is an ``AttributeError``, a cancelled budget is a
``BaseException``. All of those still propagate UNCHANGED, because their text is
Chrome's own diagnostic and shaping it would withhold the only thing an operator
can act on (``logging_setup._carries_payload``'s argument, reached from the
other side). A nodriver release that re-types or rewords this raise makes
``tests/test_element_box_exception_repr.py``'s premise nodes RED rather than
turning this into a silent leak.

**The replacement is built by F-907's shaper and by no second one.**
``logging_setup._shape`` keeps the tag, the attribute NAMES and the child COUNT
and loses every VALUE and all descendant text, with F-907's bounds on all three;
re-spelling that rendering here would be a second answer to one question
(convention 4), and this module reaching into ``logging_setup`` for it is
``backend_launch``'s reach into ``desktop_launch``'s ``schtasks`` seam. Nothing
flows the other way: ``logging_setup`` runs in the stdio proxy and must never
import the browser stack, and ``_shape`` is duck-typed precisely so it does not.

**The new exception is raised OUTSIDE the ``except`` block, and that is a PII
rule rather than a style.** Raised inside one, Python sets ``__context__`` to
nodriver's exception, and a ``__context__`` is not a private detail: every sink
that formats a traceback prints "During handling of the above exception…"
followed by the repr, and sentry-sdk's own chain walk follows ``__context__``
whether or not ``raise … from None`` suppressed its display. Leaving the handler
first makes the chain empty, so there is nothing downstream able to re-derive
the payload — ``cdp_transport._guard_result``'s reasoning at a different seam.

Redacting is not silencing: the message still says WHICH control had no box and
that the element renders nothing, which is the whole diagnostic value of the
line it replaces — and ``ElementBoxError`` names the condition where a bare
``Exception`` named nothing, so ``query_elements``' ``type(e).__name__`` line
became more informative rather than less.

``ElementBoxError`` is deliberately NOT a ``ToolError``: convention 2's class is
what ``expected_events`` DROPS from Sentry, and this is a library-level
condition each of the three call sites already converts under its own policy —
``get_element_state`` raises, ``click_element`` falls back to a synthetic click,
``query_elements`` reports no bounding box.

``install()`` is the seam, called once from ``tool_runtime``'s module body — the
one module loaded once where ``embedded/server.py`` is executed three times under
runpy — and idempotent anyway, on ``session_hygiene.install()``'s precedent.

DELETE this module on a nodriver release whose ``get_position`` reports a
missing box without interpolating ``self``.
"""

from collections.abc import Callable

from nodriver.core.element import Element

from stealth_chrome_devtools_mcp.embedded.logging_setup import _shape

__all__ = ["ElementBoxError", "install", "installed"]

#: On the WRAPPER and never on this module, so "is the protection in place" is a
#: question about the object Python will actually call — ``cdp_transport``'s
#: reasoning. It also holds nodriver's own function, which is what lets the
#: premise pins drive the UNGUARDED raise in a process where we are installed.
_MARKER = "__stealth_element_box__"


class ElementBoxError(Exception):
    """An element lays out no box, said without quoting the page (F-912).

    Carries SHAPE only — never an attribute value and never any descendant
    text. See the module docstring: the sentence nodriver raises here renders
    the whole element, and for the failure this was written against that
    rendering was a password field's ``value=``.
    """


def _guard(original: Callable) -> Callable:
    async def get_position(self: Element, *args: object, **kwargs: object) -> object:
        """Where is this element's box, reported without the page's values."""
        try:
            return await original(self, *args, **kwargs)
        # The bare ``Exception`` IS the thing being caught here; every other
        # type is re-raised one line down, untouched.
        except Exception as exc:
            if type(exc) is not Exception:
                # A ProtocolException, a CdpReplyError, an AttributeError: the
                # text is Chrome's or Python's own diagnostic, not a rendering
                # of the page, and shaping it would withhold the one thing an
                # operator can act on.
                raise
            shape = _shape(self)
        # OUTSIDE the handler: an exception constructed while another is being
        # handled carries it as ``__context__``, and nodriver's message is the
        # payload this exists to withhold. Out here the chain is empty.
        raise ElementBoxError(
            f"The element has no layout box, so its position cannot be read: "
            f"{shape}. display:none, an <option> and a detached node all render "
            f"nothing."
        )

    setattr(get_position, _MARKER, original)
    return get_position


def installed() -> bool:
    """Is the guard on the class nodriver constructs?"""
    return hasattr(Element.get_position, _MARKER)


def install() -> None:
    """Stop an element's own values riding out in nodriver's exception text."""
    if not hasattr(Element.get_position, _MARKER):
        Element.get_position = _guard(Element.get_position)
