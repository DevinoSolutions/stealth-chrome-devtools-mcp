"""A Proactor transport closes its socket even when ``shutdown()`` raises (T-15387).

THE one home for how the backend's Windows event loop ends a transport.

**The defect.** ``asyncio.proactor_events._ProactorBasePipeTransport.
_call_connection_lost`` ends every pipe and socket transport. Its ``finally`` block
calls ``self._sock.shutdown(socket.SHUT_RDWR)`` and only THEN ``self._sock.close()``,
detaches the transport from its server and marks the transport lost. When the peer
has already reset, ``shutdown`` raises ``ConnectionResetError`` (``WinError 10054``),
and everything after it is skipped: the socket is never closed (the connection stays
in ``CLOSE_WAIT`` for as long as the process lives), the transport is never detached
from its ``Server`` (so ``wait_closed`` can never finish), and the callback is not
marked done. Measured on the shared backend of the 25-session PC on 2026-11-07:
918 occurrences in 2.3 h, one per Chrome or proxy connection that died abruptly,
while the hung backend held the descriptors. Until now the repo only hid the log
line (``expected_events`` ``proactor-teardown``); the leak was left in place.

**The seam.** ``_call_connection_lost`` itself, replaced by the stock body with one
change: ``shutdown`` runs inside its own ``try``/``finally`` whose ``finally`` is the
rest of the stock cleanup. So the socket is closed, the server detached and the
transport marked lost whether or not ``shutdown`` raises, and the exception
propagates exactly as before (same type, same chain, the same line in the loop's
default exception handler), which is what ``expected_events`` still recognises.

**A shape check, not a blind patch.** The body differs between releases (3.13 passes
the transport to ``Server._detach``; 3.11 and 3.12 do not), so :func:`install`
compares the stock function's AST, comments and whitespace aside, with the two
reference bodies below (:data:`_STOCK`: 3.11-3.12 and 3.13-3.14, compared). A body
that matches neither (a release that already fixed it, or one that changed it some
other way), or whose source cannot be read, is left untouched and logs a WARNING;
nothing is patched blind. A future CPython that closes the socket itself makes the
check fail and this a no-op, with that warning as the only trace.

**Installed once, for the backend only**, beside ``proactor_accept.install`` and
``proactor_spawn.install``: idempotent (a marker on the replacement) and a no-op off
Windows. The stdio proxy is not a server, but its transports end the same way; it
does not install this (its process is short-lived, and a leak there ends with it).

A leaf: stdlib only; imports no other embedded module.
"""

from __future__ import annotations

import ast
import inspect
import logging
import socket
import sys
import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_logger = logging.getLogger("stealth.backend")

_MARK = "_stealth_closes_socket_when_shutdown_raises"
#: The one private stdlib name this replaces, named once.
_CLASS = "_ProactorBasePipeTransport"
_METHOD = "_call_connection_lost"

#: The stock body (comment omitted), parametrised by how ``Server._detach`` is
#: called: 3.11 and 3.12 take no argument, 3.13 and 3.14 the transport.
_STOCK = """
def _call_connection_lost(self, exc):
    if self._called_connection_lost:
        return
    try:
        self._protocol.connection_lost(exc)
    finally:
        if hasattr(self._sock, 'shutdown') and self._sock.fileno() != -1:
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()
        self._sock = None
        server = self._server
        if server is not None:
            server._detach({detach})
            self._server = None
        self._called_connection_lost = True
"""


def _shape(source: str) -> str:
    return ast.dump(ast.parse(textwrap.dedent(source)))


#: ``ast.dump`` of each reference body, keyed by whether ``_detach`` takes the
#: transport.
_SHAPES = {
    False: _shape(_STOCK.format(detach="")),
    True: _shape(_STOCK.format(detach="self")),
}


def _detach_takes_transport(stock: Callable[..., object]) -> bool | None:
    """Which reference body ``stock`` is, or None when it is neither (or its
    source cannot be read)."""
    try:
        shape = _shape(inspect.getsource(stock))
    except (OSError, TypeError, SyntaxError):
        return None
    for takes_transport, reference in _SHAPES.items():
        if shape == reference:
            return takes_transport
    return None


def _replacement(detach_takes_transport: bool) -> Callable[..., None]:
    def _call_connection_lost(self, exc):  # noqa: ANN001, ANN202  PERMANENT(a private stdlib signature)
        if self._called_connection_lost:
            return
        try:
            self._protocol.connection_lost(exc)
        finally:
            try:
                if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
                    self._sock.shutdown(socket.SHUT_RDWR)
            finally:
                # What the stock ``finally`` skipped when ``shutdown`` raised.
                self._sock.close()
                self._sock = None
                server = self._server
                if server is not None:
                    if detach_takes_transport:
                        server._detach(self)
                    else:
                        server._detach()
                    self._server = None
                self._called_connection_lost = True

    setattr(_call_connection_lost, _MARK, True)
    return _call_connection_lost


def install() -> bool:
    """Make a Proactor transport close its socket when ``shutdown`` raises.
    Returns whether it patched now (False off Windows, when already installed, or
    when the stock body is not one this module knows; the last logs a WARNING)."""
    if sys.platform != "win32":
        return False
    from asyncio import proactor_events

    transport = getattr(proactor_events, _CLASS)
    stock = getattr(transport, _METHOD)
    if getattr(stock, _MARK, False):
        return False
    takes_transport = _detach_takes_transport(stock)
    if takes_transport is None:
        _logger.warning(
            "not patching %s.%s on Python %d.%d.%d: its body is not one this "
            "build knows, so a transport whose peer has reset may leave its "
            "socket open (T-15387)",
            _CLASS,
            _METHOD,
            *sys.version_info[:3],
        )
        return False
    setattr(transport, _METHOD, _replacement(takes_transport))
    return True
