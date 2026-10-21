"""JS-aspect and ``Runtime.evaluate`` answer builders.

Split out of :mod:`fakes` (T-5431); that module re-exports every name here, so
tests keep writing ``from fakes import ...``.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import nodriver.cdp.runtime as cdp_runtime

# ---------------------------------------------------------------------------
# JS-aspect transport fidelity (F-846 animations, F-872 the other four)
# ---------------------------------------------------------------------------


def js_aspect_answer(payload: Any) -> str:
    """What a REAL tab hands back for a cloner JS aspect script (F-872).

    ``nodriver``'s ``Tab.evaluate`` always requests deep serialization, so an
    object literal comes back as BiDi ``RemoteValue`` nodes
    (``[[key, {type, value}], …]``) at EVERY depth — a plain dict is a shape the
    transport cannot produce. All six aspect scripts therefore end in
    ``JSON.stringify``; the ONE home for encoding that in a test is here, so no
    fixture can quietly re-encode the bug the fix removed.
    """
    return json.dumps(payload)


# The marker present in ``embedded/js/extract_animations.js`` — the substring an
# ``evaluate_map`` keys on to answer THAT script and no other.
ANIMATION_JS_MARKER = "animation-facts"


def animation_evaluate_map(facts: Any) -> dict[str, str]:
    """``evaluate_map`` answering the animations script the way a REAL tab does.

    The animations collector ends in ``JSON.stringify`` (F-846): a string is the
    one shape that survives ``tab.evaluate`` — a non-primitive comes back as CDP
    deep-serialization instead. So the fake must answer with the **JSON string**,
    never the dict. A fake that returned the dict would re-encode the very bug
    the transport fix removes, and every golden captured through it would prove
    nothing. This helper is the one home for that fidelity: tests state the facts
    as a dict and the encoding happens here, once.
    """
    return {ANIMATION_JS_MARKER: json.dumps(facts)}


# ---------------------------------------------------------------------------
# ``Runtime.evaluate`` answers (the F-832 execute_script seam)
# ---------------------------------------------------------------------------


def js_result(
    value: Any = None,
    type_: str = "object",
    subtype: str | None = None,
    unserializable_value: str | None = None,
    description: str | None = None,
) -> tuple[cdp_runtime.RemoteObject, None]:
    """The ``(result, exceptionDetails)`` pair Chrome answers a successful
    ``Runtime.evaluate`` with — built from nodriver's own constructor.

    Mirrors what Chrome actually sends under ``returnByValue``: ``undefined``
    carries no ``value``; ``null`` is an object with ``subtype="null"``;
    ``Infinity``/``NaN`` arrive as ``unserializableValue``; a value Chrome cannot
    send at all arrives as type + ``description`` only.
    """
    return (
        cdp_runtime.RemoteObject(
            type_=type_,
            subtype=subtype,
            value=value,
            unserializable_value=(
                None
                if unserializable_value is None
                else cdp_runtime.UnserializableValue(unserializable_value)
            ),
            description=description,
        ),
        None,
    )


class JsPromise:
    """A script whose value is a **Promise**, as Chrome answers it (F-883).

    Not a canned answer but a canned *settlement*: what
    ``Runtime.evaluate(returnByValue=true)`` hands back depends on whether the
    caller asked for ``awaitPromise``, and the whole of F-883 is that the two
    are different answers for the same script.

    * ``awaitPromise: false`` — Chrome does not look at the settlement. It
      serializes the Promise OBJECT by value, and a ``Promise`` has no own
      enumerable properties, so the answer is ``{}`` (measured, Chrome 152).
      That is why a lost value and a REJECTION and a genuine ``return {}`` were
      the same three bytes on the wire.
    * ``awaitPromise: true`` — Chrome waits and answers with the settlement:
      *settled* for a resolution, or ``exceptionDetails`` for a rejection.

    *settled* is the ``(result, exceptionDetails)`` pair the resolution or the
    rejection produces, i.e. a ``js_result``/``js_threw`` — so a test states the
    settlement once and the DOUBLE decides which of the two answers the code
    under test earned.
    """

    def __init__(self, settled: tuple[cdp_runtime.RemoteObject, object | None]) -> None:
        self.settled = settled

    #: What Chrome sends for an un-awaited Promise under ``returnByValue``.
    UNAWAITED: ClassVar[tuple[cdp_runtime.RemoteObject, None]] = (
        cdp_runtime.RemoteObject(
            type_="object",
            subtype="promise",
            class_name="Promise",
            value={},
            description="Promise",
        ),
        None,
    )


def js_promise(
    value: Any = None, type_: str = "object", rejects: str | None = None
) -> JsPromise:
    """A :class:`JsPromise` resolving to *value*, or rejecting with *rejects*."""
    return JsPromise(
        js_threw(rejects) if rejects is not None else js_result(value, type_)
    )


def js_threw(
    description: str,
) -> tuple[cdp_runtime.RemoteObject, cdp_runtime.ExceptionDetails]:
    """The pair Chrome answers with when the evaluated script THREW.

    Chrome sends BOTH a result object (the thrown value) and ``exceptionDetails``
    — which is why a reader that trusts the result reports a throw as a success
    (F-795). Built from nodriver's constructors so a guard reading the wrong
    field names cannot stay green.
    """
    thrown = cdp_runtime.RemoteObject(
        type_="object",
        class_name=description.split(":", maxsplit=1)[0],
        description=description,
    )
    return (
        thrown,
        cdp_runtime.ExceptionDetails(
            exception_id=1,
            text="Uncaught",
            line_number=0,
            column_number=0,
            exception=thrown,
        ),
    )
