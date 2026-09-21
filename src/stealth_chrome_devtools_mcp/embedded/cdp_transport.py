"""THE one home for "DELIVERING a CDP reply must not be able to kill the listener".

F-883 B1 (with F-788 and F-794) and F-902. One sentence holds the module:
**the listener task is the only thing that resolves every future and dispatches
every event on a connection, so handing it a reply must not end it.** Two ways
in were found, two years apart, and both land here:

* F-883 — the reply's own AWAIT owned it, so cancelling the caller cancelled a
  future still registered in ``mapper`` and the late answer's ``set_result``
  raised inside the listener;
* F-902 — the reply's own PARSE was unguarded, so a generated ``from_json``
  reading a field Chrome had stopped sending raised inside the listener.

They are one sentence and one ``install()``, not two modules with two seams:
both are a monkeypatch of one nodriver class applied once per process before
any tool body runs, and splitting them would put two answers to "patch nodriver
at startup" one import apart. The three halves are named separately below and
each says when to DELETE it.

**The scope is DELIVERY, and the word is exact rather than modest.** What is
covered is everything from the moment ``_listener`` has a ``Transaction`` in
hand — ``tx(**message)`` and the ``await`` that receives it. TWO raises remain
on that same result path, upstream of any Transaction, and they still end the
listener (measured, F-902 review S1)::

    message = json.loads(raw)                              # :440  <- unguarded
    if "id" in message:
        tx: Transaction = self.mapper.pop(message["id"])   # :443  <- unguarded
        tx(**message)                                      # :444  guarded here

They are named rather than fixed because **this seam cannot reach them**: both
run before a ``Transaction`` exists, and there is no seam on ``Connection`` at
all — its ``CantTouchThis`` metaclass raises
``SettingClassVarNotAllowedException`` for any class-level assignment (the same
fact that put half 1 on ``Transaction.__await__`` rather than on
``Connection.send``). Guarding them would mean replacing ``_listener`` itself,
i.e. a double of the library's own dispatch loop in the hot path of every
message — the thing every half here is written to avoid. Neither is reachable
from a real Chrome: ``json.loads`` fails only on a frame the websocket layer
delivered malformed, and ``mapper.pop`` fails only on a reply for an id we
never sent or already popped. An earlier version of this docstring claimed "no
single CDP reply may kill the connection", which was wider than the code; the
headline is the narrower true statement.

--------------------------------------------------------------------------
Half 1 (F-883 B1) — awaiting a reply must never be able to cancel it
--------------------------------------------------------------------------

**cancelling a Python await never un-sends a command Chrome already has, so the
await must not be the thing that owns the reply.**

The mechanism, measured against nodriver 0.47's ``core/connection.py``, not
assumed::

    async def send(self, cdp_obj, _is_update=False):
        ...
        tx = Transaction(cdp_obj)          # a bare asyncio.Future
        the_id = next(self.__count__)
        tx.id = the_id
        self.mapper[the_id] = tx           # registered ...
        asyncio.create_task(self.websocket.send(tx.message))
        return await tx                    # ... and awaited, with no finally

    # _listener, in an ``else:`` branch with no try/except:
        if "id" in message:
            tx: Transaction = self.mapper.pop(message["id"])
            tx(**message)                  # __call__ ends in set_result

Cancelling the coroutine suspended at ``await tx`` cancels ``tx`` itself — that
is what awaiting a future means — while its entry is STILL in ``mapper``,
because ``send`` has no ``finally``. Chrome then answers, ``__call__`` reaches
``set_result`` on a cancelled future, and the ``InvalidStateError`` propagates
out of ``_listener`` and **ends the listener task**: the one task that resolves
every future and dispatches every event on that connection. From that moment
every later call on the tab hangs until its own timeout and the operator is told
"the browser may have crashed" about a browser that is fine.

Two scopes were being confused, and separating them IS the fix:

* the await on ONE reply, which must survive the caller, because Chrome will
  answer and something has to be there to receive it;
* the rest of a multi-step tool body, which must STOP when the caller gives up —
  a cancelled ``type_text`` must stop typing, a cancelled ``navigate`` must not
  navigate anyway.

Shielding at ``tool_runtime._with_cdp_timeout`` protects the first by detaching
the second, which bought listener safety at the cost of the cancellation
contract — ``tests/test_wire_semantics.py`` caught exactly that over real
frames. Here the two separate cleanly: ``asyncio.shield`` moves the
cancellation onto a throwaway outer future, the ``CancelledError`` still reaches
the caller AT that await, and the Transaction is left pending and registered,
which is precisely the state the listener knows how to finish.

**Why ``Transaction.__await__`` and not ``Connection.send``.** Both are one
home; this one is narrower, cheaper and not a fight. ``Connection``'s metaclass
(``CantTouchThis``) raises ``SettingClassVarNotAllowedException`` for any
class-level assignment, so wrapping ``send`` means bypassing a guard the library
states in words, and it costs a task per command. ``Transaction`` is a plain
``asyncio.Future`` subclass with no such guard, and a dunder is looked up on the
type — so one assignment covers every send there is: ours, and nodriver's own
from ``Tab.evaluate``, ``Element.apply``, ``Browser.update_targets`` and
``Tab.close``, none of which pass through any seam of ours.

**A DONE reply must not be shielded, and that is not a detail.** ``shield``
shortcuts a completed future by returning *the inner future itself*
(``if inner.done(): return inner``), and here the inner future is the
``Transaction`` whose ``__await__`` is this patch — so shielding it
unconditionally recursed until ``RecursionError``. Measured, on the first
version of this module, for an already-resolved ``Transaction``, for a
re-awaited delivered reply, and for ``EventTransaction``, which nodriver
constructs COMPLETE. Latent rather than live on 0.47 — ``EventTransaction`` is
never constructed anywhere in the package, and ``send()`` has no yield point
between ``self.mapper[the_id] = tx`` and ``await tx``, so a Transaction is
always pending at the only await there is — but a library release that
constructs or re-awaits one would have turned hang protection into a crash on
every CDP command. The guard is one line: a done reply cannot be cancelled,
there is nothing left to protect, and it takes nodriver's own ``__await__``
unchanged. Three nodes in ``tests/test_cdp_transport.py`` hold it, because an
untested claim about a library's internals is exactly what this was.

What this deliberately does NOT do:

* It does not pop our entry out of ``mapper``. The listener's ``pop`` is
  unguarded too, so a missing entry is a ``KeyError`` that ends the listener the
  same way, one line earlier.
* It does not replace ``Transaction`` or guard ``set_result``. Both put a double
  of a library's own object in the hot path of every command; this changes which
  FUTURE the caller's cancellation lands on, and no behaviour of nodriver's own.
  Nothing in nodriver ever cancels a Transaction deliberately (checked: the only
  ``cancel()`` calls are ``_listener_task`` on disconnect and two helper tasks in
  ``Tab.wait``), so no library path loses anything by this.
* It adds no deadline. ``tool_runtime._clamp_timeout`` + ``_with_cdp_timeout``
  remain the one bound, and that wrapper cancels the operation again.

What it costs, named rather than hidden: an abandoned reply keeps FOUR things
alive until Chrome answers — the ``mapper`` entry, the pending ``Transaction``,
the shield's own outer future, and the done-callback ``shield`` left on the
inner one — or keeps them for the life of the connection, for a command that is
never answered (a Promise that never settles). No TASK is leaked: shielding a
future creates no task, which is the other reason this layer is cheaper than
wrapping ``send``. That is the residue 2.1.8 already left behind, minus the dead
listener. The answer itself is discarded, and ``asyncio.shield`` retrieves the
abandoned outcome itself when the outer future was cancelled, so nothing reaches
the loop as "exception was never retrieved".

Delete half 1 on a nodriver release whose ``send()`` removes its registration in
a ``finally``, or whose ``_listener`` stops calling ``set_result`` on a future
it did not check.

--------------------------------------------------------------------------
Half 2 (F-902) — a reply that cannot be PARSED must fail its own call only
--------------------------------------------------------------------------

``Transaction.__call__`` is where the listener turns a reply into a result, and
it parses by driving the generated CDP command generator::

    try:
        self.__cdp_obj__.send(response["result"])
    except KeyError as e:
        raise KeyError(f"key '{e.args}' not found in message: {response['result']}")
    except StopIteration as e:
        self.set_result(e.value)

and ``_listener`` calls it BARE. So any parse failure propagates out of the
listener and kills the connection — the F-883 wound through the other door, and
the shield does nothing for it, because nothing here was cancelled.

MEASURED, Chrome 153.0.8010.50 + nodriver 0.47: Chrome no longer sends
``Network.Cookie.sameParty`` (the removed First-Party-Sets field), the generated
``Cookie.from_json`` reads ``json['sameParty']`` unconditionally, and one
``get_cookies`` on a page holding one cookie left every later call on that tab
hanging to ``CDP_OPERATION_TIMEOUT`` under the message "the browser may have
crashed". Cookies are simply the one this walked into: **1199 unconditional
required reads across nodriver 0.47's generated ``cdp/`` classes** (counted),
every one of which is this defect waiting for Chrome to retire its field.

The guard wraps ``__call__`` and completes THAT transaction with an exception
instead: the caller of the one command that could not be read gets a real
error, every other pending future on the connection still resolves, and the
listener lives. A transaction already ``done()`` is left alone — a cancelled or
delivered future has no caller left to tell, and ``set_exception`` on one raises
``InvalidStateError``, which is the very shape this exists to stop.

**The message it delivers is SHAPE ONLY, and that is a PII rule, not taste.**
nodriver's re-raise interpolates ``response['result']`` — the WHOLE reply — into
the exception text, so for the measured failure the string carried every cookie
NAME and VALUE on the page; it escaped as an unretrieved task exception, which
is the asyncio error handler, the durable log and Sentry at once. So this module
NEVER re-raises, chains or quotes the original: it builds a fresh exception from
the CDP method, the exception's TYPE, the reply's top-level field COUNT, and a
missing FIELD name only when ``_missing_field`` can prove the failure named
nothing else (``page_storage``'s discipline, F-869). Deliberately constructed
outside the ``except`` block, so not even ``__context__`` can carry the payload
out. ``CdpReplyError`` is deliberately NOT a ``ToolError``: convention 2's class
is what ``expected_events`` DROPS from Sentry as the product working as designed,
and a reply we cannot read is the opposite of that — it must ship.

**The message is not the only way a payload can travel, and the other two are
pinned rather than assumed** (F-902 review S2). The whole reply is a local of
THIS function at the moment the error is built, so:

* the error is STORED with ``set_exception``, never raised here — so this frame
  contributes no traceback entry at all, and there is no frame for a serialiser
  to read locals off. MEASURED: the only frame in a delivered
  ``CdpReplyError``'s traceback is the awaiting caller's. That is a property of
  construct-and-store, and a refactor to ``raise`` here would silently end it,
  which is why ``tests/test_cdp_transport.py`` pins the frame's ABSENCE;
* ``response`` is deleted before the error is constructed, so the local is not
  merely unreachable-in-practice but gone;
* and ``observability.py``'s ``sentry_sdk.init`` sets
  ``include_local_variables=False``. That belongs to another module, so this
  module's PII argument DEPENDS on a setting it does not own — pinned here too,
  so flipping that flag fails a test in this file rather than quietly widening
  what a parse failure can ship.

Delete half 2 on a nodriver release whose ``_listener`` guards its result path
the way it already guards its event path.

--------------------------------------------------------------------------
Half 3 (F-902) — and a reply Chrome still calls valid must still PARSE
--------------------------------------------------------------------------

Half 2 makes ``get_cookies`` fail honestly instead of fatally; it does not make
it WORK. ``_RETIRED_COOKIE_FIELDS`` is the other half: a NAMED tolerance with
its evidence beside it, never a widened ``except`` and never "fill in whatever
is missing" — an invented ``sourcePort`` would be a lie a caller could act on,
where a field the protocol has DELETED has exactly one meaning left. Patched on
``Cookie.from_json`` rather than at the three product call sites, because that
one classmethod is also what ``Storage.getCookies`` and the three
cookie-carrying Network EVENTS reach (``network.py`` 1507 / 1538 / 1569 / 4213),
and three call sites would leave the events broken and the next reader to add a
fourth site unprotected.

Its one named cost: ``Cookie.to_json`` writes ``sameParty`` unconditionally, so
a cookie read on Chrome 153 reports ``sameParty: false`` — a value Chrome never
sent. ``False`` is what the field meant for every cookie that did not opt into a
First-Party Set, and the feature it belonged to no longer exists, so nothing a
caller decides can turn on it; it is stated here rather than hidden because it
IS a synthesised field.

Delete half 3 on a nodriver release whose ``Cookie.same_party`` is optional (or
gone), which is the same release that makes ``_RETIRED_COOKIE_FIELDS`` empty.

--------------------------------------------------------------------------

``install()`` is the seam, called once from ``tool_runtime``'s module body — the
one module loaded once where ``embedded/server.py`` is executed three times
under runpy — and it is idempotent anyway, on the precedent of
``session_hygiene.install()``.
"""

import asyncio
import re
from collections.abc import Callable, Generator

from nodriver.cdp.network import Cookie
from nodriver.core.connection import Transaction

__all__ = ["CdpReplyError", "install", "installed"]

# The markers live on the wrappers, never on this module, so "is the protection
# in place" is a question about the objects Python will actually call.
_MARKER = "__stealth_cdp_transport__"
_RESULT_MARKER = "__stealth_cdp_result_guard__"
_COOKIE_MARKER = "__stealth_cdp_cookie_compat__"


def _protect(original: Callable) -> Callable:
    def __await__(self: Transaction) -> Generator:  # noqa: N807 - PERMANENT(the name IS the dunder we are replacing)
        """Await this CDP reply without being able to cancel it (F-883 B1)."""
        if self.done():
            # ``shield`` SHORTCUTS a done future by returning the inner future
            # itself — which is ``self``, whose ``__await__`` is this function.
            # Shielding here would recurse until ``RecursionError``. A done
            # reply cannot be cancelled anyway: there is nothing left to
            # protect, so it takes nodriver's own ``__await__`` unchanged.
            return original(self)
        return asyncio.shield(self).__await__()

    setattr(__await__, _MARKER, original)
    return __await__


# ---------------------------------------------------------------------------
# Half 2 — a reply that cannot be parsed fails its own call, not the connection.
# ---------------------------------------------------------------------------


class CdpReplyError(Exception):
    """A CDP reply arrived that its generated parser could not read (F-902).

    Carries SHAPE only — never any part of the reply. See the module docstring:
    the text nodriver raises here interpolates the whole payload, and for the
    failure this was written against that payload was a page's cookie jar.
    """


#: A CDP field name, as the protocol spells them. The one thing a ``KeyError``
#: out of a generated ``from_json`` can name is the key it looked up, and every
#: such key is a literal protocol field — but nodriver's own re-raise builds a
#: ``KeyError`` whose single arg is the whole interpolated payload, so the shape
#: is CHECKED rather than trusted. A payload dump can never match this.
_FIELD_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _missing_field(exc: BaseException) -> str | None:
    """The protocol FIELD a parse failure named, when that is all it named.

    Walks the exception chain outermost-first and answers the first link that
    is a ``KeyError`` carrying exactly one field-shaped string. nodriver's outer
    re-raise fails that test (its arg is the payload); the inner ``KeyError``
    the generated parser actually raised passes it. Anything else — a
    ``ValueError`` from an enum, a ``TypeError`` — answers ``None`` rather than
    risking a message built out of a VALUE.

    **The shape test proves "identifier", and the step from there to "protocol
    field" is a PREMISE about nodriver, not something the regex establishes**
    (F-902 review S3): a cookie NAME is frequently identifier-shaped too, so if
    a generated parser ever indexed a payload dict with a page-controlled key,
    that key could be echoed. The premise is that every ``KeyError`` a generated
    ``from_json`` can raise names a LITERAL protocol field. MEASURED true of
    nodriver 0.47 — 810 generated ``from_json`` methods, **zero** subscripts
    with a non-literal key — and pinned by an AST scan in
    ``tests/test_cdp_transport.py``, so a release that introduces a computed
    lookup fails there instead of turning this into a leak.
    """
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, KeyError) and len(cursor.args) == 1:
            key = cursor.args[0]
            if isinstance(key, str) and _FIELD_NAME.match(key):
                return key
        cursor = cursor.__cause__ or cursor.__context__
    return None


def _describe(tx: Transaction, exc: BaseException, result: object) -> str:
    """A report about a reply, built from nothing that was IN the reply."""
    field = _missing_field(exc)
    fields = len(result) if isinstance(result, dict) else "?"
    return (
        f"The CDP reply to {tx.method or '<unknown method>'} could not be "
        f"parsed: {type(exc).__name__}"
        + (f" (no {field!r} in the reply)" if field else "")
        + f". Reply carried {fields} top-level field(s); its contents are "
        "deliberately not reported. The CDP connection is unaffected."
    )


def _guard_result(original: Callable) -> Callable:
    def __call__(self: Transaction, **response: object) -> object:  # noqa: N807 - PERMANENT(the name IS the dunder we are replacing)
        """Complete this CDP reply, or fail it — never the listener (F-902)."""
        try:
            return original(self, **response)
        except Exception as exc:  # noqa: BLE001 - PERMANENT(the whole point: the listener may not die of this)
            report = _describe(self, exc, response.get("result"))
        # Outside the ``except``: an exception CONSTRUCTED while another is
        # being handled would be raised later with that one as ``__context__``,
        # and the original's text is the payload this must never carry out.
        del response  # the reply is not a live local while the error is built
        if self.done():
            # Cancelled or already delivered: nobody is left to tell, and
            # ``set_exception`` would raise the ``InvalidStateError`` that ends
            # the listener — the exact shape half 1 exists to prevent.
            return None
        self.set_exception(CdpReplyError(report))
        return None

    setattr(__call__, _RESULT_MARKER, original)
    return __call__


# ---------------------------------------------------------------------------
# Half 3 — a field Chrome has RETIRED still parses, with the value it meant.
# ---------------------------------------------------------------------------

#: Fields nodriver 0.47's generated ``Cookie.from_json`` reads unconditionally
#: that Chrome no longer sends, mapped to the value the protocol gave them.
#:
#: ``sameParty``: MEASURED absent from every cookie in
#: ``Network.getCookies`` / ``Network.getAllCookies`` / ``Storage.getCookies``
#: on Chrome 153.0.8010.50 (raw websocket, no nodriver in the path), and absent
#: ONLY it — every other field the parser requires is still sent. It carried
#: First-Party Sets / SameParty, which Chrome removed; ``False`` is what it read
#: for every cookie outside such a set, i.e. all of them.
#:
#: This table is a NAMED tolerance. A field is added here only with a measured
#: reply showing Chrome has stopped sending it AND a defensible value; anything
#: else is left to half 2, which reports it honestly instead of guessing.
_RETIRED_COOKIE_FIELDS: dict[str, object] = {"sameParty": False}


def _tolerate_retired(original: Callable) -> classmethod:
    # ``_cls`` is unused on purpose: ``original`` is the classmethod ALREADY
    # bound to ``Cookie`` at install time, so this wrapper has no use for the
    # class it is handed — but the descriptor still passes one.
    def from_json(_cls: type[Cookie], json: dict[str, object]) -> Cookie:
        """Parse a cookie, supplying the fields Chrome has retired (F-902)."""
        absent = {k: v for k, v in _RETIRED_COOKIE_FIELDS.items() if k not in json}
        return original({**json, **absent} if absent else json)

    setattr(from_json, _COOKIE_MARKER, original)
    return classmethod(from_json)


# ---------------------------------------------------------------------------


def installed() -> bool:
    """Are all three protections in place on the classes nodriver constructs?"""
    return (
        hasattr(Transaction.__await__, _MARKER)
        and hasattr(Transaction.__call__, _RESULT_MARKER)
        and hasattr(Cookie.from_json, _COOKIE_MARKER)
    )


def install() -> None:
    """Make no single CDP reply able to kill the connection. Idempotent."""
    if not hasattr(Transaction.__await__, _MARKER):
        Transaction.__await__ = _protect(Transaction.__await__)
    if not hasattr(Transaction.__call__, _RESULT_MARKER):
        Transaction.__call__ = _guard_result(Transaction.__call__)
    if not hasattr(Cookie.from_json, _COOKIE_MARKER):
        Cookie.from_json = _tolerate_retired(Cookie.from_json)
