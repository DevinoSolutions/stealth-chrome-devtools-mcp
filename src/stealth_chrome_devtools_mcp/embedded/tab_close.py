"""THE one home for what a failed tab close means (F-996).

``close_instance`` closes every tab with ``Target.closeTarget`` before it closes
the browser, and ``close_tab`` closes one. A tab Chrome had already dropped
answers that command with ``No target with given id found [code: -32602]`` --
the state the close wanted, so it is a close that already happened and not a
fault. Counting it as one made
``test_a_fleet_of_six_browsers_answers_truthfully_about_every_page`` fail on CI
run 38919791742.

The wording is matched and not the code: nodriver fills ``ProtocolException.code``
only when the reply carried one (F-828), and the wording is specific to this
case. Every other failure, including another ``-32602``, is still a fault.
"""

from nodriver.core.connection import ProtocolException
from websockets.exceptions import InvalidStatus

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

_ALREADY_GONE = "No target with given id"
_SESSION_GONE = "Session with given id not found"
# T-8944: the status Chrome answers a websocket opened on a page target id it
# no longer has (measured in F-963: the re-attach path failed a whole spawn on it).
_NO_SUCH_TARGET_STATUS = 500


def is_already_gone(exc: BaseException) -> bool:
    """Whether ``exc`` is Chrome saying the tab being closed no longer exists."""
    return isinstance(exc, ProtocolException) and _ALREADY_GONE in str(exc)


def session_gone(exc: BaseException) -> bool:
    """Whether ``exc`` is Chrome saying the session the command was sent through
    no longer exists (-32001, T-8944).

    Valid ONLY where that session was the target's own, i.e. a command sent
    through the tab itself: then the tab is gone. A command sent over the
    BROWSER connection (``Target.closeTarget``) names the browser's session, so
    this says nothing about the tab and ``close_tab`` must not use it; it uses
    :func:`is_already_gone`.
    """
    return isinstance(exc, ProtocolException) and _SESSION_GONE in str(exc)


def target_socket_gone(exc: BaseException) -> bool:
    """Whether ``exc`` is Chrome refusing the socket nodriver re-opens for a page
    target that no longer exists (T-8944).

    A tab's own websocket is re-opened by ``Connection.send`` when it is down,
    and the handshake for a closed target is answered ``HTTP 500``. The command
    never reached the page, so the failure is the tab's absence and not a fault
    in the command: callers that already report it to their own caller log it
    as a warning (``debug_logger.log_error`` does so for every caller). Only
    that status: a 403/404, a refused connection or a timeout is not this.
    """
    return (
        isinstance(exc, InvalidStatus)
        and exc.response.status_code == _NO_SUCH_TARGET_STATUS
    )


def log_failure(method: str, instance_id: str, exc: BaseException) -> bool:
    """Log a tab that would not close, for ``method`` (``close_instance`` or
    ``close_tab``): debug if it was already gone, else a WARNING.

    Returns whether it was already gone, i.e. whether the close counts as done.
    """
    gone = is_already_gone(exc)
    log = debug_logger.log_debug if gone else debug_logger.log_warning
    log(
        "browser_manager",
        method,
        f"Failed to close tab for {instance_id}: {_describe(exc)}",
    )
    return gone


def _describe(exc: BaseException) -> str:
    """The failure's type and text. The type is the part that cannot be left
    out: a ``TimeoutError`` from ``asyncio.wait_for`` has NO text, so the line
    read ``Failed to close tab for <id>: `` and named nothing (T-5900, CI run
    39564101655)."""
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
