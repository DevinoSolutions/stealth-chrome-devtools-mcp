"""THE one home for "awaiting a CDP reply must never be able to cancel it".

F-883 B1, and with it F-788 and F-794. One sentence holds the module:
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

``install()`` is the seam, called once from ``tool_runtime``'s module body — the
one module loaded once where ``embedded/server.py`` is executed three times
under runpy — and it is idempotent anyway, on the precedent of
``session_hygiene.install()``.
"""

import asyncio
from collections.abc import Callable, Generator

from nodriver.core.connection import Transaction

__all__ = ["install", "installed"]

# The marker lives on the wrapper, never on this module, so "is the protection
# in place" is a question about the object Python will actually call.
_MARKER = "__stealth_cdp_transport__"


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


def installed() -> bool:
    """Is the protection in place on the class nodriver will construct?"""
    return hasattr(Transaction.__await__, _MARKER)


def install() -> None:
    """Make every CDP reply survive its caller's cancellation. Idempotent."""
    if installed():
        return
    Transaction.__await__ = _protect(Transaction.__await__)
