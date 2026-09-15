"""THE one home for reading a cloner JS aspect's ``tab.evaluate`` answer (F-872).

Two decisions live here and nowhere else.

**1. The shape a JS aspect may answer with is a STRING.** ``nodriver``'s
``Tab.evaluate`` sends ``SerializationOptions(serialization="deep",
max_depth=10, …)`` on EVERY call and returns ``deep_serialized_value.value``
verbatim (``nodriver/core/tab.py``; ``cdp/runtime.py``'s ``DeepSerializedValue``
keeps ``json["value"]`` exactly as the wire delivered it). Chrome's BiDi
``RemoteValue`` encoding is recursive, so a returned JS object arrives as
``[[key, {type, value}], …]`` at every depth and ``return_by_value`` cannot undo
it. All five evaluated aspect scripts therefore end in ``JSON.stringify``; this
module reads that string back. There is deliberately **no** BiDi decoder here —
a string is the shape, not a thing to decode, and the partial unwrapper that
preceded this (top level only, every nested array left as raw transport nodes)
is exactly the defect F-872 removed.

**2. What a thrown script means.** ``Tab.evaluate`` returns the
``ExceptionDetails`` record ITSELF in the value's place (``if errors: return
errors``) — it does not raise, and the record has no ``exception_details``
attribute, so a ``hasattr`` probe for one can never fire. Worse, its ``.text``
is the literal string ``"Uncaught"`` for every throw there is (measured against
real headless Chrome 2026-09-15: ReferenceError, TypeError, an explicit
``throw new Error(…)`` and a SyntaxError all produce it), so the diagnostic a
caller needs is ``.exception.description``.

A leaf: it imports ``nodriver`` and ``tool_errors``, nothing else — no
``server``, no engine. Deliberately **not** shared with F-869's ``page_storage``
reader: the two use the same ``JSON.stringify`` + ``json.loads`` IDIOM but carry
different failure policies (this one raises ``ToolError``; that one distinguishes
blocked storage from a read failure), and an idiom is not a home.
"""

import json

import nodriver as uc

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: How much of Chrome's error text a message may carry. The TEXT is Chrome's,
#: but its LENGTH is the page's — ``throw new Error(<anything>)`` plus a stack
#: trace is unbounded — so it is clamped to a diagnostic, never a transcript
#: (the same bound F-869 puts on a storage error).
MAX_ERROR_CHARS = 200

#: Appended when the clamp actually cut something, so a reader can tell a
#: truncated message from one that simply ended there — a silent cut reads as
#: Chrome's complete words and is not (F-869's marker, same style).
TRUNCATION_MARKER = "…"


def js_error(details: uc.cdp.runtime.ExceptionDetails) -> ToolError:
    """The ``ToolError`` for a script that threw, named by Chrome's own text."""
    exception = getattr(details, "exception", None)
    described = str(getattr(exception, "description", None) or details.text)
    if len(described) > MAX_ERROR_CHARS:
        described = described[:MAX_ERROR_CHARS] + TRUNCATION_MARKER
    return ToolError(
        f"JavaScript error: {described} "
        f"(line {details.line_number}, column {details.column_number})"
    )


def unexpected_type(value: object) -> ToolError:
    """THE one way an aspect reports a payload shape it cannot read (F-858).

    The offending value rides in the message because a raise has no second field
    to carry it — it used to sit in a ``raw_data`` key beside the error, and
    dropping it would leave "unexpected type" undebuggable.
    """
    return ToolError(f"Unexpected return type: {type(value)} (raw: {value!s:.400})")


def parsed(raw: object) -> dict[str, object]:
    """One aspect script's ``tab.evaluate`` answer, as a plain Python dict."""
    if isinstance(raw, uc.cdp.runtime.ExceptionDetails):
        raise js_error(raw)
    if not isinstance(raw, str):
        raise unexpected_type(raw)
    answer = json.loads(raw)
    if not isinstance(answer, dict):
        raise unexpected_type(answer)
    return answer
