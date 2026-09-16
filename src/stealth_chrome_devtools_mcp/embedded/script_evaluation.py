"""THE one home for "run a caller's JS in the page and read its answer".

``execute_script`` is the tool; this module is the mechanism, and it is the ONLY
place in the tree that evaluates caller-authored source. Four findings are welded
together here, and each one is a decision that must not be re-derived:

**F-795 — a thrown script is a failure.** Chrome answers a throw with BOTH a
result object (the thrown value) and ``exceptionDetails``. The details are read
FIRST, and the record is handed to ``tool_errors._require_js_value`` — the ONE
place a thrown script becomes the error convention — rather than raised here.

**F-832 (issue #17) — the answer is the value ITSELF.** A raw
``Runtime.evaluate`` with ``return_by_value=True``, never ``nodriver``'s
``Tab.evaluate``, which always asks for a *deep-serialized* result: a BiDi graph
of ``{"type": …, "value": …}`` nodes capped at depth 10 (see
``js_aspect_answer``'s docstring for why that cannot be undone by a decoder). No
``serialization_options`` is sent, because CDP documents it as **overriding**
``returnByValue`` and passing both would quietly reinstate the envelope this
exists to remove. Reading the answer is :func:`json_value`'s job, and its test is
None-vs-ABSENT, never truthiness — ``0`` / ``""`` / ``false`` / ``null`` are
legitimate answers.

**F-812 — a top-level ``return`` is not a defect.** ``return document.title;``
is how an agent writes a script and was the single largest error by volume on the
wire. It is a *compile* complaint about how we evaluated the source, not about
the source, so the script is re-evaluated ONCE as a function body.

**F-883 — a Promise is a value, and a script may ``await``.** Measured on Chrome
152 against 2.1.8: ``return fetch(u).then(r => r.text())`` answered
``{"success": true, "result": {}}`` — ``returnByValue`` serialized the *Promise
object*, which has no own enumerable properties, so a caller could not tell a
resolved value from an empty object; ``Promise.reject(new Error('boom'))``
answered the same ``{}`` and called it a success; and a top-level ``await`` — the
one non-blocking wait the tool's own docstring demands — died with ``SyntaxError:
await is only valid in async functions``. Three changes answer all of it and no
more:

* ``await_promise=True`` on the ONE send, so Chrome resolves a returned Promise
  and reports a rejection as the exception it is;
* the retry wrapper is an **async** function, so the source may ``await`` at top
  level and still ``return``;
* the retry fires for the top-level-``await`` SyntaxError as well as the illegal
  ``return`` — the two compile complaints that mean "this source is a function
  body", nothing else.

**Why a retry and not an unconditional wrapper.** A wrapper changes what a script
MEANS: ``var installed = 1`` / ``function f() {}`` at top level are expected to
land on the *page*, and inside a wrapper they become locals that are thrown away.
So the source is evaluated as written first, and only Chrome's own complaint
sends it round again — which is also why an ordinary failure (a ``ReferenceError``
in the body) is never evaluated twice and never acquires a second side effect.

**What bounds a script that never finishes.** Nothing here. ``awaitPromise`` on a
Promise that never settles blocks the send exactly as a ``while(true)`` blocks the
renderer, and the bound for both is the caller's: ``tool_runtime._clamp_timeout``
+ ``_with_cdp_timeout`` at the tool body, the ONE home for that clamp. This module
deliberately does not pass CDP's own ``timeout`` as well — a second deadline is a
second answer to "how long may a script run", and the two would drift.

A leaf: ``nodriver`` + ``tool_errors``, tab as an argument. It imports no other
embedded module, and nothing else in the tree may evaluate caller JS.
"""

import json

from nodriver import Tab, cdp

from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    ToolError,
    _require_js_value,
)

#: Chrome's compile-time complaint when a script carries a top-level ``return``
#: (lower-cased for matching) — F-812.
ILLEGAL_RETURN = "illegal return statement"

#: Chrome's compile-time complaint when a script carries a top-level ``await``
#: (lower-cased for matching) — F-883. Chrome's full text continues "… and the
#: top level bodies of modules"; only the stable head is matched.
TOP_LEVEL_AWAIT = "await is only valid in async functions"

#: The two complaints above, each paired with the phrase the retry's message uses
#: to name WHICH of them sent the script round again. A caller told "top-level
#: 'return'" about an ``await`` would go fixing the wrong line.
_FUNCTION_BODY_COMPLAINTS: tuple[tuple[str, str], ...] = (
    (ILLEGAL_RETURN, "top-level 'return'"),
    (TOP_LEVEL_AWAIT, "top-level 'await'"),
)

#: "the CDP result carried no ``value`` field at all", which is NOT the same
#: thing as a ``value`` that IS ``None`` (an explicit JS ``null``). Reading an
#: evaluate result needs both cases named, and the whole of F-832 is that they
#: were conflated with "the value was falsy" — see :func:`json_value`.
_ABSENT = object()


def json_value(remote_object: object) -> object:
    """Read a by-value ``Runtime.evaluate`` result as a plain JSON value (F-832).

    The test is None-vs-ABSENT, never truthiness. ``nodriver``'s ``Tab.evaluate``
    reads its result with ``if remote_object.value:`` / ``if
    remote_object.deep_serialized_value:``, so ``0``, ``""``, ``false`` and
    ``null`` — all legitimate answers — failed the test and fell through to a
    bare ``RemoteObject`` husk in their place. That is the trap this function
    exists not to fall into.

    The mapping, in branch order:

    * ``undefined`` → ``None`` (Python has one nullish, so JS's two agree here)
    * a present ``value`` → that value, **verbatim**, falsy or not
    * ``null`` (by ``subtype``) → ``None``, reached by its own named branch
    * ``unserializableValue`` (``Infinity`` / ``NaN`` / ``-0``) → its token
    * nothing serializable (a live DOM node, a cycle) → the ``description``

    A ``RemoteObject`` is never returned: it is not JSON-serializable, so the
    tool composing it into its payload must not be able to crash on it.
    """
    if remote_object is None:
        return None
    if getattr(remote_object, "type_", None) == "undefined":
        return None
    value = getattr(remote_object, "value", _ABSENT)
    if value is not _ABSENT and value is not None:
        return value
    # No value came back. WHICH of the ways that happens decides the answer —
    # "it was falsy" is not one of them, and never reaches this point.
    if getattr(remote_object, "subtype", None) == "null":
        return None
    unserializable = getattr(remote_object, "unserializable_value", None)
    if unserializable is not None:
        return str(unserializable)
    description = getattr(remote_object, "description", None)
    return None if description is None else str(description)


def script_value(remote_object: object, exception_details: object) -> object:
    """Turn one ``Runtime.evaluate`` answer into the script's value, or raise.

    Chrome answers a thrown script — and, under ``awaitPromise``, a REJECTED one
    — with BOTH a result object (the thrown/rejected value) and
    ``exceptionDetails``, so the details are consulted FIRST: reading the result
    would report the rejection as the script's value and call it a success, which
    is the F-795 defect and (for a rejection) the F-883 one. The record is routed
    into ``tool_errors._require_js_value`` rather than raising here — that is the
    ONE place a thrown script becomes the error convention, and the one place its
    message is clamped to a diagnostic rather than a page-authored transcript.
    """
    if exception_details is not None:
        _require_js_value(exception_details)
    return json_value(remote_object)


def _function_body_reason(message: str) -> str | None:
    """The phrase naming which compile complaint means "evaluate this as a
    function body", or ``None`` for any other failure.

    Narrowness is the point: a script that fails for a reason of its own keeps
    its error and is never evaluated twice, so nothing that already worked
    acquires a second execution or a changed meaning.
    """
    lowered = message.lower()
    for complaint, reason in _FUNCTION_BODY_COMPLAINTS:
        if complaint in lowered:
            return reason
    return None


async def evaluate(tab: Tab, expression: str) -> tuple[object, object]:
    """Send ONE ``Runtime.evaluate`` and return ``(result, exceptionDetails)``.

    THE one send. ``return_by_value`` is F-832's; ``await_promise`` is F-883's —
    a script whose value is a Promise is answered with what it resolves to, and
    one that rejects is answered as the exception it is. ``user_gesture`` and
    ``allow_unsafe_eval_blocked_by_csp`` are carried over from ``nodriver``'s own
    call: dropping either would regress a page whose CSP blocks unsafe-eval, or a
    handler gated on user activation.

    Reading the pair is :func:`script_value`'s job; a transport failure (a dead
    connection, an unserializable argument) is this function's, and is reported
    as operational rather than as the script's own.
    """
    try:
        return await tab.send(
            cdp.runtime.evaluate(
                expression=expression,
                return_by_value=True,
                await_promise=True,
                user_gesture=True,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
    except Exception as e:
        # Deliberately open: nodriver raises ProtocolException, the websocket
        # layer raises OSError / ConnectionResetError and asyncio raises its own
        # on a closed loop. What this branch decides is not WHICH failure it was
        # but that it was OURS and not the script's — narrowing it would let one
        # of them past as though the page had thrown.
        raise ToolError(f"Failed to execute script: {e!s}") from e


async def as_async_function_body(tab: Tab, script: str, reason: str) -> object:
    """Re-evaluate *script* as the body of an ASYNC function (F-812, F-883).

    An async wrapper makes both compile complaints go away at once: a top-level
    ``return`` is legal in any function, a top-level ``await`` is legal in an
    async one, and the Promise the wrapper now returns is resolved by
    :func:`evaluate`'s ``await_promise`` — so the caller gets the value, not the
    Promise object.

    The wrapped attempt's error is the one surfaced: the complaint that sent us
    here is an artifact of how the FIRST attempt evaluated the script, so
    re-reporting it would name our strategy instead of the caller's actual defect
    (a ``ReferenceError`` in the body, say). The wrapper is named in the message
    because it is visible in the JS stack, and *reason* names which complaint
    triggered it.
    """
    answer = await evaluate(tab, f"(async () => {{\n{script}\n}})()")
    try:
        return script_value(*answer)
    except ToolError as exception:
        raise ToolError(
            f"{exception} (the script was re-evaluated inside an async wrapper "
            f"function because it has a {reason})"
        ) from None


async def run(tab: Tab, script: str, args: list[object] | None = None) -> object:
    """Run *script* in *tab* and return its value as plain JSON.

    Evaluated as written, so a top-level declaration still lands on the page;
    re-evaluated ONCE as an async function body if — and only if — Chrome names
    one of the two complaints in :data:`_FUNCTION_BODY_COMPLAINTS`.

    A script given *args* is already a function body, so it is wrapped straight
    away — and the wrapper is async for the same reason the retry's is, which is
    what keeps ``await`` legal on both paths rather than on one of them.
    """
    if args:
        serialized_args = ",".join(json.dumps(a) for a in args)
        expression = f"(async function() {{ {script} }})({serialized_args})"
    else:
        expression = script

    answer = await evaluate(tab, expression)

    # Outside the send on purpose: a script that THREW is a failure of the
    # script, not of the CDP call, so it must not be re-wrapped in the
    # "Failed to execute script" (operational) message. F-795.
    try:
        return script_value(*answer)
    except ToolError as exception:
        reason = _function_body_reason(str(exception))
        if reason is None:
            raise

    return await as_async_function_body(tab, script, reason)
