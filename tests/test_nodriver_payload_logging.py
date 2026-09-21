"""F-906: nodriver's raw-reply log lines may never reach one of our sinks.

``nodriver`` DEBUG-logs every raw CDP reply verbatim and INFO-logs a whole
event message when it cannot parse one; ``websockets`` DEBUG-logs the frame.
Cookie names and values ride in all three. F-902 measured that under this
product's SHIPPED configurations none of them can reach a handler — the
``nodriver`` logger's effective level is WARNING and ours sit on
``stealth.<role>`` with ``propagate = False``.

That protection was the LEVEL and nothing else, and the level was inherited
from root. So one ``logging.basicConfig(level=DEBUG)`` — a test, a notebook, a
caller embedding the backend — turned it off: MEASURED, all four payload lines
reached stderr (which for the backend is redirected into ``backend-boot.log``,
i.e. a durable file), and the INFO one additionally reached Sentry as a
breadcrumb on the next event.

These pins drive the REAL collaborators — the stdlib's own level machinery, a
real ``LoggingIntegration`` (which patches ``logging.Logger.callHandlers``) and
a real ``RotatingFileHandler`` on a tmp log dir — because every one of the four
sinks is a property of how those three compose, not of anything we could
assert about our own code in isolation.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from pathlib import Path

import pytest
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

from stealth_chrome_devtools_mcp.embedded import logging_setup
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings

# --------------------------------------------------------------------------
# The call shapes, copied from the INSTALLED nodriver 0.47.0 / websockets 16.0
# rather than paraphrased, so these pins measure the library's real lines.
# One marker each, so a failure names WHICH line escaped.
# --------------------------------------------------------------------------
CONN_LOGGER = "nodriver.core.connection"
BROWSER_LOGGER = "nodriver.core.browser"
WEBSOCKETS_LOGGER = "websockets.client"

#: ``connection.py``:445 — ``logger.debug("got answer for (message_id:%d) => %s", tx.id, message)``
REPLY_MARK = "F906_REPLY_PAYLOAD"
#: ``connection.py``:451 — the whole event message, pre-interpolated, at INFO
EVENT_MARK = "F906_EVENT_PAYLOAD"
#: ``browser.py``:824/:869 — ``(%s: %s)`` % (cookie.name, cookie.value), at DEBUG
COOKIE_MARK = "F906_COOKIE_PAYLOAD"
#: ``websockets/protocol.py``:609 — ``logger.debug("< %s", frame)``; a short
#: frame is printed whole (measured: truncation starts past ~75 chars)
FRAME_MARK = "F906_FRAME_PAYLOAD"

PAYLOAD_MARKS = {
    "connection.py:445 raw reply (DEBUG)": REPLY_MARK,
    "connection.py:451 event message (INFO)": EVENT_MARK,
    "browser.py:824 cookie name+value (DEBUG)": COOKIE_MARK,
    "websockets protocol.py:609 frame (DEBUG)": FRAME_MARK,
}

#: ``connection.py``:483 — a real diagnostic. It names the callback and the
#: event CLASS, never the payload, and it must keep working.
WARNING_MARK = "F906_REAL_WARNING"


def emit_payload_lines() -> None:
    conn = logging.getLogger(CONN_LOGGER)
    conn.debug(
        "got answer for (message_id:%d) => %s",
        7,
        {"id": 7, "result": {"cookies": [{"name": "SID", "value": REPLY_MARK}]}},
    )
    conn.info(
        "%s: %s  during parsing of json from event : %s"  # noqa: UP031  PERMANENT(this IS nodriver's own line, copied: the `%` runs BEFORE logging sees it, so the payload is already inside `record.msg` with `args` empty — which is exactly why no handler-side or argument-side rule can reach it and only the LEVEL can)
        % (
            "KeyError",
            ("sameParty",),
            {"params": {"headers": {"set-cookie": f"SID={EVENT_MARK}"}}},
        )
    )
    logging.getLogger(BROWSER_LOGGER).debug(
        "saved cookie for matching pattern '%s' => (%s: %s)", ".*", "SID", COOKIE_MARK
    )
    logging.getLogger(WEBSOCKETS_LOGGER).debug(
        "< %s", f'TEXT \'{{"v":"{FRAME_MARK}"}}\' [42 bytes]'
    )


def emit_diagnostic_warning() -> None:
    logging.getLogger(CONN_LOGGER).warning(
        "exception in callback %s for event %s => %s", "cb", "SomeEvent", WARNING_MARK
    )


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
class _RefusingTransport(sentry_sdk.transport.Transport):
    """Nothing may leave this machine. ``before_send`` already returns ``None``;
    this is the second lock, and it is loud rather than quiet."""

    def capture_envelope(self, envelope) -> None:
        raise AssertionError("F-906 pin tried to ship a real Sentry envelope")


class _RootCapture(logging.Handler):
    """Stands in for "whatever this process has on the root logger".

    Sink (d) is spelled this way rather than as stderr alone because stderr is
    only ever reached THROUGH a handler: in production root carries none, so
    ``callHandlers`` falls through to ``logging.lastResort`` (a stderr handler
    at WARNING) and the backend's stderr is redirected into
    ``backend-boot.log``; under a caller's ``basicConfig`` it is that caller's
    own ``StreamHandler``. Measuring at root covers both, and every other
    handler a caller may have installed. It also keeps the pin honest under
    pytest, whose own capture handler on root suppresses ``lastResort`` — the
    first draft asserted on stderr and so measured the test runner.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.text += f"{record.name} {record.levelname} {record.getMessage()}\n"


class Sinks:
    """What each of the four sinks received, by marker NAME."""

    def __init__(
        self, durable: str, ring: str, sentry: str, downstream: str, warnings: dict
    ) -> None:
        self.durable = self._found(durable)
        self.ring = self._found(ring)
        self.sentry = self._found(sentry)
        self.downstream = self._found(downstream)
        self.warnings = warnings

    @staticmethod
    def _found(text: str) -> set[str]:
        return {name for name, mark in PAYLOAD_MARKS.items() if mark in text}

    @property
    def all_payload_reach(self) -> set[str]:
        return self.durable | self.ring | self.sentry | self.downstream


def reset_logging() -> None:
    """Every library logger back to NOTSET, every handler gone. The pins set
    global process state, so each one starts from the same floor."""
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    root.setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def _isolated_logging():
    reset_logging()
    get_settings.cache_clear()
    yield
    reset_logging()
    get_settings.cache_clear()


def drive(
    log_dir: Path,
    *,
    role: str = "backend",
    basicconfig: str | None = None,
    debug_ring: bool = False,
) -> Sinks:
    """Run one shipped configuration and report what each sink received.

    ``stderr`` is redirected for the WHOLE call, because ``logging.basicConfig``
    binds ``sys.stderr`` into its ``StreamHandler`` at CREATION time — a
    redirect entered afterwards measures nothing and reads as a false negative
    (that is how the first draft of this measurement said "no leak").
    """
    debug_logger.clear_debug_view()
    debug_logger.enable() if debug_ring else debug_logger.disable()

    events: list[dict] = []
    err = io.StringIO()
    downstream = _RootCapture()

    with contextlib.redirect_stderr(err), contextlib.ExitStack() as stack:
        if basicconfig == "before":
            logging.basicConfig(level=logging.DEBUG, force=True)

        log_path = logging_setup.configure_logging(role)

        if basicconfig == "after":
            logging.basicConfig(level=logging.DEBUG, force=True)

        root = logging.getLogger()
        root.addHandler(downstream)
        stack.callback(root.removeHandler, downstream)

        client = sentry_sdk.Client(
            dsn="https://public@example.invalid/1",
            integrations=[LoggingIntegration(event_level=logging.ERROR)],
            default_integrations=False,
            transport=_RefusingTransport(),
            before_send=lambda event, hint: events.append(event) or None,
        )
        assert isinstance(client.transport, _RefusingTransport), (
            "refusing to drive a pin against anything but the transport double"
        )
        scope = stack.enter_context(sentry_sdk.isolation_scope())
        scope.set_client(client)

        emit_payload_lines()
        emit_diagnostic_warning()
        # Force an event, so any breadcrumbs accrued above are serialized with
        # it — a breadcrumb that is never attached to anything is not a sink.
        logging.getLogger("stealth.probe").error("F906 probe")

    for handler in logging.getLogger(f"stealth.{role}").handlers:
        with contextlib.suppress(Exception):
            handler.flush()

    durable = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    ring = json.dumps(debug_logger.get_debug_view(), default=str)
    sentry = json.dumps(events, default=str)
    # Either door onto a process-wide handler counts as sink (d).
    reached = downstream.text + err.getvalue()
    return Sinks(
        durable,
        ring,
        sentry,
        reached,
        {
            "durable": WARNING_MARK in durable,
            "downstream": WARNING_MARK in reached,
            "sentry": WARNING_MARK in sentry,
        },
    )


SHIPPED = [
    pytest.param({"role": "backend"}, id="backend"),
    pytest.param({"role": "proxy"}, id="proxy"),
    pytest.param({"role": "backend", "debug_ring": True}, id="backend---debug"),
]
CALLER_DEBUG = [
    pytest.param({"role": "backend", "basicconfig": "before"}, id="basicConfig-before"),
    pytest.param({"role": "backend", "basicconfig": "after"}, id="basicConfig-after"),
]


# --------------------------------------------------------------------------
# The pins
# --------------------------------------------------------------------------
class TestNoPayloadReachesAnySink:
    @pytest.mark.parametrize("config", SHIPPED + CALLER_DEBUG)
    def test_no_raw_payload_reaches_any_sink(self, config, tmp_path, monkeypatch):
        """The whole finding in one assertion, over all four sinks at once."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        assert sinks.all_payload_reach == set(), (
            "a raw CDP payload reached a sink: "
            f"durable={sorted(sinks.durable)} ring={sorted(sinks.ring)} "
            f"sentry={sorted(sinks.sentry)} downstream={sorted(sinks.downstream)}"
        )

    @pytest.mark.parametrize("config", CALLER_DEBUG)
    def test_caller_root_debug_reaches_neither_stderr_nor_sentry(
        self, config, tmp_path, monkeypatch
    ):
        """The two cells that were RED, named individually.

        MEASURED before the fix: all four lines reached a root handler (and so
        stderr, which for the backend is redirected into ``backend-boot.log``)
        under BOTH orders, and ``connection.py``:451 — the only one at INFO,
        and ``LoggingIntegration``'s breadcrumb handler sits at INFO — reached
        Sentry attached to the next event.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        assert sinks.downstream == set(), (
            f"payload reached a root handler: {sorted(sinks.downstream)}"
        )
        assert sinks.sentry == set(), f"payload in Sentry: {sorted(sinks.sentry)}"

    def test_log_level_debug_does_not_open_the_door_either(self, tmp_path, monkeypatch):
        """``STEALTH_MCP_LOG_LEVEL`` is OUR level and must stay ours.

        It was already harmless (it is applied to ``stealth.<role>``, never to
        root), and the floor must not be what makes that true — so this asserts
        both halves: no payload escapes, AND our own logger really is at DEBUG.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("STEALTH_MCP_LOG_LEVEL", "DEBUG")
        get_settings.cache_clear()
        sinks = drive(tmp_path, role="backend")
        assert sinks.all_payload_reach == set()
        assert logging.getLogger("stealth.backend").level == logging.DEBUG


class TestDiagnosticsSurvive:
    """A floor that silences nodriver's real complaints is the wrong fix."""

    @pytest.mark.parametrize("config", SHIPPED + CALLER_DEBUG)
    def test_nodriver_warning_still_reaches_the_process_handlers(
        self, config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        assert sinks.warnings["downstream"], "nodriver's WARNING was silenced"
        assert sinks.warnings["sentry"], "nodriver's WARNING lost its breadcrumb"

    def test_a_nodriver_warning_is_not_in_the_durable_stealth_log(
        self, tmp_path, monkeypatch
    ):
        """Stated so the matrix cannot be misread: ``stealth.<role>``'s file
        handler never carried nodriver's records and still does not. Our
        handler sits on ``stealth.*`` with ``propagate = False``, so nodriver's
        diagnostics reach the process's handlers (stderr, hence
        ``backend-boot.log`` for the backend) and Sentry — not
        ``backend-<pid>.log``. The floor changes nothing about that."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, role="backend")
        assert not sinks.warnings["durable"]

    def test_the_floor_is_exactly_todays_shipped_level(self, tmp_path, monkeypatch):
        """WARNING is not a new policy — it is what every shipped configuration
        already had (measured), which is why the floor changes nothing but the
        caller-``basicConfig`` door."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        for family in logging_setup.PAYLOAD_LOG_FAMILIES:
            assert logging.getLogger(family).getEffectiveLevel() == logging.WARNING, (
                f"{family} is not at the shipped WARNING level"
            )

    def test_our_own_loggers_are_untouched_by_the_floor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        assert logging.getLogger("stealth").level == logging.NOTSET
        assert logging.getLogger("stealth.backend").level == logging.INFO


class TestTheFloorIsSetOnTheFamilyItself:
    """WHY this works in both orders, asserted rather than assumed.

    ``basicConfig`` only ever sets the ROOT logger's level, and
    ``getEffectiveLevel`` stops at the first ancestor carrying a non-``NOTSET``
    level. Putting an EXPLICIT level on ``nodriver`` therefore wins over root
    whichever way round the two calls happen — which is the whole mechanism,
    and is why no filter and no ``before_breadcrumb`` is needed beside it.
    """

    def test_floor_is_explicit_not_inherited(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        for family in logging_setup.PAYLOAD_LOG_FAMILIES:
            assert logging.getLogger(family).level == logging.WARNING

    def test_a_later_root_debug_cannot_lower_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
            logging.basicConfig(level=logging.DEBUG, force=True)
        assert logging.getLogger(CONN_LOGGER).getEffectiveLevel() == logging.WARNING
        assert not logging.getLogger(CONN_LOGGER).isEnabledFor(logging.DEBUG)

    def test_the_floor_survives_a_failed_handler_install(self, tmp_path, monkeypatch):
        """``configure_logging`` degrades to a no-op when the log dir cannot be
        made, and a process with no durable log still has stderr and Sentry —
        so the floor must be applied BEFORE the part that can fail."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(blocker / "logs"))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        # The explicit level is on the FAMILY ROOT; the child inherits it, so
        # the child's own ``.level`` is still NOTSET and the question to ask of
        # it is the effective one.
        assert logging.getLogger("nodriver").level == logging.WARNING
        assert logging.getLogger(CONN_LOGGER).getEffectiveLevel() == logging.WARNING


class TestPremises:
    """The two facts this fix rests on, measured against the installed
    libraries rather than assumed — so a dependency bump that invalidates
    either one fails HERE instead of shipping cookies."""

    def test_the_payload_lines_are_all_below_the_floor(self):
        """If nodriver ever logs a raw reply at WARNING, a WARNING floor stops
        covering it. Read the installed source and check the two known sites."""
        from nodriver.core import connection

        source = Path(connection.__file__).read_text(encoding="utf-8").splitlines()
        reply_lines = [line for line in source if "got answer for (message_id" in line]
        event_lines = [
            index
            for index, line in enumerate(source)
            if "during parsing of json from event" in line
        ]
        assert reply_lines, "nodriver no longer logs the raw reply — re-measure F-906"
        assert all("logger.debug(" in line for line in reply_lines), (
            f"the raw-reply line moved off DEBUG: {reply_lines}"
        )
        assert event_lines, "nodriver no longer logs the raw event — re-measure F-906"
        for index in event_lines:
            window = "".join(source[max(0, index - 3) : index + 1])
            assert "logger.info(" in window, (
                f"the raw-event line moved off INFO near line {index + 1}"
            )

    def test_sentry_hooks_below_the_level_check(self):
        """``LoggingIntegration`` patches ``logging.Logger.callHandlers``, which
        ``Logger.handle`` reaches only for a record ``isEnabledFor`` already
        admitted. That is why the LEVEL is upstream of Sentry and a
        ``before_breadcrumb`` rule would be a second home for one decision."""
        import inspect

        from sentry_sdk.integrations import logging as sentry_logging

        source = inspect.getsource(sentry_logging.LoggingIntegration.setup_once)
        assert "logging.Logger.callHandlers" in source, (
            "the SDK moved its patch point; re-measure whether the level still "
            "sits in front of it"
        )

    def test_the_families_named_are_the_families_that_exist(self):
        """``nodriver``'s loggers are ``__name__``-based, so the family root is
        ``nodriver`` — there is no ``uc`` logger to cap, and naming one would be
        a claim the evidence does not support."""
        from nodriver.core import browser, connection

        for module in (connection, browser):
            assert module.logger.name.split(".")[0] == "nodriver"
            assert module.logger.name == module.__name__
        assert set(logging_setup.PAYLOAD_LOG_FAMILIES) == {"nodriver", "websockets"}
