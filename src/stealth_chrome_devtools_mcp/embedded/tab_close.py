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

from nodriver import Tab
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded import tab_open
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

_ALREADY_GONE = "No target with given id"
_SESSION_GONE = "Session with given id not found"


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


async def target_unlisted(tab: Tab) -> bool:
    """Whether Chrome, asked over the BROWSER connection, no longer lists *tab*.

    The one confirmation a gone-target error needs (T-8944): ``-32001`` and
    ``No target with given id`` only say what the failed command saw. ``False``
    whenever it cannot be shown: no browser behind the tab, a target that is not
    a page, or a listing that itself fails.
    """
    browser = getattr(tab, "parent", None)
    if browser is None or getattr(tab.target, "type_", None) != "page":
        return False
    try:
        await tab_open.update_targets(browser)
    except Exception:  # noqa: BLE001  PERMANENT(a failed confirmation means not confirmed)
        return False
    return tab_open.find(browser, str(tab.target.target_id)) is None


async def report_gone_aware(
    component: str, method: str, error: Exception, tab: Tab
) -> None:
    """Report a CDP command that failed on *tab*'s own connection: a WARNING when
    the target is gone, else ``debug_logger.log_error`` as before.

    *Gone* is an error that says so (:func:`session_gone`, or
    :func:`is_already_gone` from a re-attach) AND a fresh listing that no longer
    has the target (:func:`target_unlisted`); either alone leaves it an ERROR, so
    a ``-32001`` on a target Chrome still lists is not hidden. The session rule
    holds here because a site calls this only for a command sent through the
    tab it passes, and whose caller already gets the failure back. Nothing about
    a websocket's HTTP status is read: on nodriver 0.50 a tab shares the
    BROWSER endpoint (``Connection.__init__`` takes the parent's
    ``websocket_url``), so a closed page cannot fail a handshake with a 500; a
    500 there is the browser's own and stays an ERROR.
    """
    if (session_gone(error) or is_already_gone(error)) and await target_unlisted(tab):
        debug_logger.log_warning(
            component, method, f"{type(error).__name__}: {error}", error=error
        )
    else:
        debug_logger.log_error(component, method, error)


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
