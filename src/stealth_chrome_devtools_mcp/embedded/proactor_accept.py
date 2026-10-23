"""A client reset during accept must not close the backend's listener (T-6363).

THE one home for how the backend's listening socket survives Windows' asyncio.

**The defect.** On Windows uvicorn serves on ``ProactorEventLoop``. Its accept
loop (``proactor_events.BaseProactorEventLoop._start_serving``) treats ANY
``OSError`` from an accept as fatal for the LISTENER: it reports "Accept failed
on a socket" and calls ``sock.close()`` on the listening socket. A client that
resets its connection while that connection is still pending in ``AcceptEx``
completes the accept with ``WinError 64`` ("The specified network name is no
longer available"), so one rude client closes the listener for good while every
established connection stays up. Measured: 200 connects closed with
``SO_LINGER(1, 0)`` against a bare ``asyncio.start_server`` on CPython 3.12 leave
the server refusing every later connection. In CI it was the 12-session startup
herd: the backend booted, the proxies' first probes reset, and ``tools/list``
from every unfinished session broke with "All connection attempts failed"
(run 39833151221). The Selector loop on POSIX only logs and keeps accepting.

**The seam.** ``IocpProactor.accept(listener)`` returns the future the accept
loop waits on, and its result is ``(conn, addr)``. That is the narrowest seam:
the wrapper hands the loop a future of its own and, when the inner accept
completes with a reset-class error, issues a fresh accept on the same listener
instead of surfacing the error. The loop never sees the failure, so its
``sock.close()`` never runs. ``_start_serving`` is NOT replaced because that
would copy ~40 lines of private loop code (the ssl branch, ``_debug`` paths)
that differ between CPython releases; ``IocpProactor.accept`` is byte-identical
in 3.12, 3.13 and 3.14 (compared), and the wrapper relies only on its contract.
A future CPython that fixes the loop makes this a harmless no-op.

**What is retried, and what is not.** Only ``winerror`` in
:data:`RESET_WINERRORS` (64 network name gone, 10053/10054 abort/reset, 995
operation aborted), and only while the listener is still open and not stopped.
An orderly ``server.close()`` cancels the loop's (outer) future, which cancels
the inner one, and closes the listener, so neither a cancellation nor a closed
or stopped listener is ever retried: close ends the loop at once and nothing
spins. Any other error propagates exactly as before. Once
:data:`MAX_CONSECUTIVE_RESETS` resets in a row (no accept between them) have
been retried, the next one is surfaced, so a listener that errors on every
accept still surfaces instead of spinning. The connection socket of a failed
accept is left to garbage collection, exactly as in the stock path
(``accept_coro`` closes it only on cancellation), and asyncio still logs its
"Task exception was never retrieved" line for it at collection; that noise is
not worth a second patch.

**Installed once, for the backend only.** ``server.py`` runs up to three times
under ``runpy`` and the module may be imported by tests, so :func:`install` is
idempotent (a marker on the wrapper). It is a no-op off Windows. The stdio
proxy never calls it: the proxy only dials, it never listens.

A leaf: stdlib only; imports no other embedded module.
"""

from __future__ import annotations

import logging
import sys

# The backend's own stream: this is a backend event, and a warning here is the
# only evidence that a client reset during accept was absorbed.
_logger = logging.getLogger("stealth.backend")

#: ``OSError.winerror`` values that mean "this ONE pending connection is gone",
#: not "the listener is broken".
RESET_WINERRORS = frozenset({64, 995, 10053, 10054})

#: Resets in a row, with no successful accept between them, past which an error
#: is surfaced as the stock loop would. Far above any real storm; it exists so a
#: listener that fails every accept cannot spin the event loop.
MAX_CONSECUTIVE_RESETS = 10_000

_MARK = "_stealth_resilient_accept"


def _is_reset(exc: BaseException | None) -> bool:
    return (
        isinstance(exc, OSError) and getattr(exc, "winerror", None) in RESET_WINERRORS
    )


#: The first reset of an accept call is logged, then every power of ten from here.
_LOG_FROM = 100


def _worth_logging(count: int) -> bool:
    """The 1st reset, then the 100th, 1000th, ... : a local RST scanner must not
    be able to flood the backend log with THIS warning (asyncio's own per-reset
    line at garbage collection is not ours to throttle; see the module doc)."""
    return count == 1 or (
        count >= _LOG_FROM and str(count) == "1" + "0" * (len(str(count)) - 1)
    )


def _close_unclaimed(accepted: tuple) -> None:
    """A connection accepted for a loop that has since gone (its future was
    cancelled by ``server.close()``): close it now rather than at GC."""
    try:
        accepted[0].close()
    except Exception:  # noqa: BLE001  PERMANENT(best-effort cleanup of a socket nobody owns)
        _logger.debug("could not close an unclaimed accepted socket", exc_info=True)


def _serving(proactor, listener) -> bool:  # noqa: ANN001  PERMANENT(a private stdlib proactor)
    """Whether the listener is still open and not stopped by ``server.close()``."""
    stopped = getattr(proactor, "_stopped_serving", ())
    return listener.fileno() != -1 and listener not in stopped


def _settle_unclaimed(inner) -> None:  # noqa: ANN001  PERMANENT(a future)
    """The inner accept finished after the loop's future was already done: close
    what it accepted, or read the error nobody else will."""
    if inner.cancelled():
        return
    if inner.exception() is None:
        _close_unclaimed(inner.result())


def _wrap(original):  # noqa: ANN001, ANN202  PERMANENT(wraps a private stdlib method; a signature would pretend to know it)
    def accept(self, listener):  # noqa: ANN001, ANN202  PERMANENT(same)
        outer = self._loop.create_future()
        current = [original(self, listener)]  # a synchronous failure is stock behaviour
        resets = 0

        def rearm(exc: BaseException) -> None:
            nonlocal resets
            resets += 1
            if _worth_logging(resets):
                _logger.warning(
                    "a client reset during accept (%s); the listener stays "
                    "open (%d in a row on this accept)",
                    exc,
                    resets,
                )
            try:
                current[0] = original(self, listener)
            except Exception as error:  # noqa: BLE001  PERMANENT(any failure to re-arm is the accept's failure)
                outer.set_exception(error)
                return
            current[0].add_done_callback(on_done)

        def on_done(inner) -> None:  # noqa: ANN001  PERMANENT(a future)
            if outer.done():
                _settle_unclaimed(inner)
            elif inner.cancelled():
                outer.cancel()
            elif (exc := inner.exception()) is None:
                outer.set_result(inner.result())
            elif (
                _is_reset(exc)
                and resets < MAX_CONSECUTIVE_RESETS
                and _serving(self, listener)
            ):
                rearm(exc)
            else:
                outer.set_exception(exc)

        def on_outer_done(done) -> None:  # noqa: ANN001  PERMANENT(a future)
            if done.cancelled():
                current[0].cancel()

        current[0].add_done_callback(on_done)
        outer.add_done_callback(on_outer_done)
        return outer

    setattr(accept, _MARK, True)
    return accept


def install() -> bool:
    """Make the Proactor accept loop survive a reset client. Returns whether it
    patched now (False off Windows, or when already installed)."""
    if sys.platform != "win32":
        return False
    from asyncio import windows_events

    proactor = windows_events.IocpProactor
    if getattr(proactor.accept, _MARK, False):
        return False
    proactor.accept = _wrap(proactor.accept)
    return True
