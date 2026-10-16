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

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

_ALREADY_GONE = "No target with given id"


def is_already_gone(exc: BaseException) -> bool:
    """Whether ``exc`` is Chrome saying the tab being closed no longer exists."""
    return isinstance(exc, ProtocolException) and _ALREADY_GONE in str(exc)


def log_failure(method: str, instance_id: str, exc: BaseException) -> bool:
    """Log a tab that would not close, for ``method`` (``close_instance`` or
    ``close_tab``): debug if it was already gone, else a WARNING.

    Returns whether it was already gone, i.e. whether the close counts as done.
    """
    gone = is_already_gone(exc)
    log = debug_logger.log_debug if gone else debug_logger.log_warning
    log("browser_manager", method, f"Failed to close tab for {instance_id}: {exc}")
    return gone
