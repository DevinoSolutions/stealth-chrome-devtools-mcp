"""THE pin home for ``logging_setup.apply_payload_log_floor`` (F-906, F-908).

One mechanism, so one file: a third-party logger held at an explicit level on
its family ROOT, which is upstream of every sink at once. These pins were
``test_nodriver_payload_logging.py`` while nodriver was the only family; F-908
added a third and renamed the file rather than opening a second home for the
same function's evidence.

**F-906 — the raw CDP reply.** ``nodriver`` DEBUG-logs every raw reply verbatim
and INFO-logs a whole event message when it cannot parse one; ``websockets``
DEBUG-logs the frame. Cookie names and values ride in all three.

**F-908 — the serialised tool RESULT.** ``sse_starlette/sse.py``:362 is
``logger.debug("chunk: %s", chunk)``, and for this backend that chunk IS the
answer to a ``tools/call`` — ``get_cookies``' jar, ``get_page_content``'s HTML,
``get_instance_state``'s localStorage — because FastMCP leaves
``json_response`` at its ``False`` default, so every answer leaves as an SSE
frame. Measured by driving the real ``EventSourceResponse`` (see
``TestTheSseChunkIsTheToolResult``), not by paraphrasing the call.

F-902 measured that under this product's SHIPPED configurations none of these
can reach a handler — their effective level is WARNING and ours sit on
``stealth.<role>`` with ``propagate = False``.

That protection was the LEVEL and nothing else, and the level was inherited
from root. So one ``logging.basicConfig(level=DEBUG)`` — a test, a notebook, a
caller embedding the backend — turned it off: MEASURED, every payload line
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

import ast
import asyncio
import contextlib
import inspect
import io
import json
import logging
from pathlib import Path

import fastmcp
import pytest
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

from stealth_chrome_devtools_mcp.embedded import backend_env, logging_setup
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
SSE_LOGGER = "sse_starlette.sse"

#: ``connection.py``:445 — ``logger.debug("got answer for (message_id:%d) => %s", tx.id, message)``
REPLY_MARK = "F906_REPLY_PAYLOAD"
#: ``connection.py``:451 — the whole event message, pre-interpolated, at INFO
EVENT_MARK = "F906_EVENT_PAYLOAD"
#: ``browser.py``:824/:869 — ``(%s: %s)`` % (cookie.name, cookie.value), at DEBUG
COOKIE_MARK = "F906_COOKIE_PAYLOAD"
#: ``websockets/protocol.py``:609 — ``logger.debug("< %s", frame)``; a short
#: frame is printed whole (measured: truncation starts past ~75 chars)
FRAME_MARK = "F906_FRAME_PAYLOAD"
#: F-908. ``sse_starlette/sse.py``:362 — ``logger.debug("chunk: %s", chunk)``,
#: where ``chunk`` is the whole serialised SSE frame, i.e. the tool's answer.
CHUNK_MARK = "F908_TOOL_RESULT_PAYLOAD"

PAYLOAD_MARKS = {
    "connection.py:445 raw reply (DEBUG)": REPLY_MARK,
    "connection.py:451 event message (INFO)": EVENT_MARK,
    "browser.py:824 cookie name+value (DEBUG)": COOKIE_MARK,
    "websockets protocol.py:609 frame (DEBUG)": FRAME_MARK,
    "sse_starlette sse.py:362 tool result (DEBUG)": CHUNK_MARK,
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
    emit_real_sse_tool_result()


#: The answer shape ``mcp/server/streamable_http.py`` hands to
#: ``EventSourceResponse`` for a ``tools/call``: a ``message`` event whose data
#: is the JSON-RPC response, ``structuredContent`` and all.
TOOL_ANSWER = {
    "jsonrpc": "2.0",
    "id": 3,
    "result": {
        "content": [],
        "structuredContent": {"cookies": [{"name": "SID", "value": CHUNK_MARK}]},
    },
}


def emit_real_sse_tool_result() -> None:
    """Drive the REAL ``EventSourceResponse`` over one tool answer.

    Deliberately not a copy of ``sse.py``:362's call the way the nodriver lines
    above are copies: nodriver's line is reached only by a live Chrome, while
    this one is three statements from a plain async generator — so the pin can
    afford to measure the library's own code path, and a refactor that moves
    the log line or changes what it interpolates is then visible here rather
    than only in a docstring.
    """
    from sse_starlette import EventSourceResponse

    async def body():
        yield {"event": "message", "data": json.dumps(TOOL_ANSWER)}

    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    async def drain() -> None:
        await EventSourceResponse(body())._stream_response(send)
        # The payload really does go out on the wire — this pin is about the
        # LOG, so the absence of a marker must never be the absence of a frame.
        assert any(CHUNK_MARK in str(message.get("body", "")) for message in sent), (
            "the SSE body never carried the answer; this pin would pass vacuously"
        )

    asyncio.run(drain())


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
    global process state, so each one starts from the same floor.

    It RESETS and does not RESTORE, and that is worth knowing rather than
    hiding (F-906 review N2): anything an earlier test module configured on a
    logger is destroyed here, not put back. It is safe in this suite because
    the only cross-module logging state is `stealth.<role>`'s handler, which
    `configure_logging` reinstalls on demand — and it was verified by running
    the whole logging/observability slice with this file placed FIRST. If
    random test ordering is ever introduced, snapshot-and-restore instead.
    """
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
    # The two knobs TOGETHER. Without this cell the debug-ring column is only
    # ever asserted where no record was flowing in the first place: `SHIPPED`
    # turns the ring on but admits nothing, `CALLER_DEBUG` admits everything
    # but leaves the ring off. The ring is unreachable by construction —
    # `debug_logger.enable()` sets a flag and echoes to stderr, and registers
    # no handler anywhere — but "unreachable by construction" is worth
    # DEMONSTRATING under flowing records rather than asserting.
    pytest.param(
        {"role": "backend", "basicconfig": "before", "debug_ring": True},
        id="basicConfig-before-and---debug",
    ),
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
        ``before_breadcrumb`` rule would be a second home for one decision.

        The premise is not "the SDK patches ``callHandlers``" — it is **"the
        SDK patches nothing UPSTREAM of ``isEnabledFor``"**, so the pin needs
        both halves (F-906 review S1). A bump that kept the ``callHandlers``
        patch and ADDED a ``Logger.handle`` / ``_log`` / ``makeRecord`` hook
        would leave a presence-only assertion green while the premise it
        stands for was gone. At 2.64.0 exclusivity holds: ``setup_once`` binds
        exactly one name (measured).

        F-908 rests on the identical premise for a third family, so this pin
        is SHARED rather than restated — there is one mechanism and one place
        its foundation is measured.
        """
        import re

        from sentry_sdk.integrations import logging as sentry_logging

        source = inspect.getsource(sentry_logging.LoggingIntegration.setup_once)
        assert "logging.Logger.callHandlers" in source, (
            "the SDK moved its patch point; re-measure whether the level still "
            "sits in front of it"
        )
        for upstream in ("logging.Logger.handle", "logging.Logger._log", "makeRecord"):
            assert upstream not in source, (
                f"the SDK now also patches {upstream}, which runs UPSTREAM of "
                "isEnabledFor — re-measure whether a level still covers Sentry "
                "before trusting F-906's 'one mechanism closes all four sinks'"
            )
        # Exclusivity stated positively too: one bound name, and it is ours.
        bound = re.findall(r"^\s*(logging\.[\w.]+)\s*=", source, re.MULTILINE)
        assert bound == ["logging.Logger.callHandlers"], (
            f"setup_once now binds {bound}; F-906 rests on it binding exactly "
            "logging.Logger.callHandlers and nothing else"
        )

    def test_the_families_named_are_the_families_that_exist(self):
        """Every family is named by its ROOT, and every root is one a logger
        really sits under: all three libraries build loggers from ``__name__``,
        so there is no ``uc`` to cap, and naming one would be a claim the
        evidence does not support."""
        import sse_starlette.sse
        from nodriver.core import browser, connection

        for module in (connection, browser, sse_starlette.sse):
            assert module.logger.name == module.__name__
            assert (
                module.logger.name.split(".")[0] in logging_setup.PAYLOAD_LOG_FAMILIES
            )
        assert set(logging_setup.PAYLOAD_LOG_FAMILIES) == {
            "nodriver",
            "websockets",
            "sse_starlette",
        }


def _calls_below_warning(package) -> list[str]:
    """Every ``logger.debug``/``.info``/``.log`` call in an installed package.

    AST and never a grep: a text search for these is exactly the shape that has
    returned false negatives on this machine, and a false negative here reads
    as proof that a library logs nothing — which is how a family gets left out.
    """
    root = Path(package.__file__).parent
    found = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        found.extend(
            f"{path.name}:{node.lineno} {ast.unparse(node)[:120]}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"debug", "info", "log"}
            and "log" in ast.unparse(node.func.value).lower()
        )
    return found


class TestTheSseChunkIsTheToolResult:
    """F-908's central claim, measured against the installed SDK rather than
    argued: the backend answers a ``tools/call`` over SSE, and the line that
    logs the frame logs the whole answer."""

    def test_the_sse_path_is_the_live_one(self):
        """FastMCP leaves ``json_response`` at ``False``, and we never pass it
        — so every answer leaves as an SSE frame.

        Read off the SDK's own DEFAULTS. Deliberately NOT off
        ``_create_json_response``'s ``# pragma: no cover``, which the brief for
        this finding offered as the evidence: that pragma is on ~40 lines of
        ``streamable_http.py`` including ``_handle_get_request`` and
        ``_validate_session``, which are unarguably live, so it witnesses
        nothing about which branch runs.
        """
        import fastmcp.server.http
        from mcp.server import streamable_http

        transport = inspect.signature(streamable_http.StreamableHTTPServerTransport)
        assert transport.parameters["is_json_response_enabled"].default is False
        builder = inspect.signature(fastmcp.server.http.create_streamable_http_app)
        assert builder.parameters["json_response"].default is False

        # Two ways it could be turned on, and neither is open. We never PASS
        # it (searched as a keyword, so `backend_env`'s docstring naming it as
        # a scrubbed knob is not a false positive) …
        ours = Path(logging_setup.__file__).parent.parent
        passes = [
            f"{path.name}:{node.lineno}"
            for path in ours.rglob("*.py")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg in {"json_response", "is_json_response_enabled"}
        ]
        assert passes == [], (
            f"{passes} now asks for JSON responses; re-measure whether the SSE "
            "frame is still what carries a tool answer"
        )
        # … and an inherited `FASTMCP_JSON_RESPONSE` cannot do it either,
        # because F-890 drops the whole prefix before fastmcp parses it.
        assert backend_env.FASTMCP_PREFIX.lower() == "fastmcp_"
        inherited = {"FASTMCP_JSON_RESPONSE": "1"}
        assert backend_env.scrub(inherited) == ["FASTMCP_JSON_RESPONSE"]
        assert inherited == {}

    def test_the_chunk_line_is_below_the_floor_and_carries_the_whole_frame(self):
        """If ``sse_starlette`` ever logged the chunk at WARNING, a WARNING
        floor would stop covering it."""
        from sse_starlette import sse

        source = Path(sse.__file__).read_text(encoding="utf-8").splitlines()
        chunk_lines = [line.strip() for line in source if "chunk: %s" in line]
        assert chunk_lines == ['logger.debug("chunk: %s", chunk)'], (
            f"sse_starlette's chunk log line changed: {chunk_lines}"
        )

    async def test_the_chunk_really_is_the_serialised_answer(self):
        """Drive the real ``EventSourceResponse`` with root at DEBUG and read
        the record back: the log line carries the tool's answer verbatim.

        This is the pin that would have to be DELETED rather than adjusted if
        the leak were ever argued away, which is why it asserts the payload is
        present rather than asserting a level.
        """
        from sse_starlette import EventSourceResponse

        seen: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.getMessage())

        async def body():
            yield {"event": "message", "data": json.dumps(TOOL_ANSWER)}

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        handler = Capture()

        async def send(message) -> None:
            return None

        root.addHandler(handler)
        try:
            await EventSourceResponse(body())._stream_response(send)
        finally:
            root.removeHandler(handler)

        assert any(CHUNK_MARK in line for line in seen), (
            "sse.py:362 no longer renders the frame; re-measure F-908"
        )


class TestTheFamiliesDeliberatelyLeftOut:
    """A census is only a finding if its NEGATIVE half is pinned too.

    Each of these is a family the census looked at and left out, with the
    measurement that justifies leaving it out. A dependency bump that makes one
    of them start rendering payloads fails HERE, which is the only thing that
    keeps "we checked" from decaying into "we assumed".
    """

    @pytest.mark.parametrize("name", ["starlette", "anyio"])
    def test_these_libraries_log_nothing_below_warning_at_all(self, name):
        """Nothing to cap. Capping them would be naming a door that is not a
        door — F-906's argument against a ``uc`` entry, reached from the other
        side."""
        package = __import__(name)
        assert _calls_below_warning(package) == []

    def test_uvicorns_whole_message_logger_redacts_bodies_by_construction(self):
        """``uvicorn`` sees every ASGI message, including the SSE body — so it
        is the obvious next candidate and it is OUT on two independent grounds.

        (1) ``MessageLoggerMiddleware`` replaces ``body``/``bytes``/``text``/
        ``headers`` with a ``<N bytes>`` placeholder before logging, so it
        cannot render a payload even when enabled; (2) it logs at TRACE (5),
        which a caller's ``basicConfig(DEBUG)`` does not admit — so the door
        F-906 and F-908 exist to close does not reach it.
        """
        from uvicorn.logging import TRACE_LOG_LEVEL
        from uvicorn.middleware.message_logger import (
            PLACEHOLDER_FORMAT,
            message_with_placeholders,
        )

        assert set(PLACEHOLDER_FORMAT) == {"body", "bytes", "text", "headers"}
        redacted = message_with_placeholders(
            {"type": "http.response.body", "body": CHUNK_MARK.encode()}
        )
        assert CHUNK_MARK not in str(redacted)
        assert TRACE_LOG_LEVEL < logging.DEBUG

    def test_httpcore_traces_response_headers_but_never_the_body(self):
        """``httpcore`` DEBUG-logs a trace's kwargs, which for
        ``receive_response_headers.complete`` are the response HEADERS. The
        BODY traces set no ``return_value``, so their message is the trace NAME
        alone — measured, and it is why the tool result cannot escape here.
        """
        from httpcore._async import http11

        lines = Path(http11.__file__).read_text(encoding="utf-8").splitlines()
        body_traces = [
            index
            for index, line in enumerate(lines)
            if "Trace(" in line and "response_body" in line
        ]
        assert body_traces, "httpcore's body traces moved; re-measure F-908"
        for index in body_traces:
            # `Trace.trace` renders `info`, which for a body trace is only ever
            # the kwargs — never a `return_value`, so the message is the trace
            # NAME and the response BODY cannot appear in it.
            block = "".join(lines[index : index + 8])
            assert "trace.return_value" not in block, (
                f"httpcore now attaches a return value to {lines[index].strip()}"
            )

    def test_the_fastmcp_argument_line_is_unreachable_from_root(self):
        """``fastmcp/server/server.py``:672 DEBUG-logs a tool call's ARGUMENTS
        — a real payload line, and the reason this family is OUT is that the
        library already closes it: its loggers hang under a ``FastMCP`` root
        carrying its own level and ``propagate = False``, so a caller's root
        DEBUG never reaches them.

        Pinned as a PREMISE: an upstream change that drops either half makes
        this RED, which is the signal to re-open the family question.

        ``fastmcp/__init__.py`` establishes this AT IMPORT, and this module's
        ``reset_logging`` then wipes it — so the pin re-runs the library's own
        configurator rather than asserting against the wreckage. That call is
        the premise: what is being tested is that fastmcp still shields its own
        family, not that some particular process happened to be configured.
        """
        import fastmcp.server.server as fastmcp_server
        from fastmcp.utilities.logging import configure_logging as fastmcp_configure

        source = Path(fastmcp.__file__).read_text(encoding="utf-8")
        assert "configure_logging" in source, (
            "fastmcp no longer configures its own family at import; the "
            "fastmcp family may now need the floor"
        )
        fastmcp_configure()

        logger = fastmcp_server.logger
        assert logger.name.startswith("FastMCP."), logger.name
        family = logging.getLogger("FastMCP")
        assert family.level != logging.NOTSET
        assert family.propagate is False

        logging.basicConfig(level=logging.DEBUG, force=True)
        assert not logger.isEnabledFor(logging.DEBUG), (
            "FastMCP's own level no longer shields its tool-argument line; "
            "the fastmcp family now needs the floor"
        )

    def test_the_mcp_incoming_message_line_renders_no_payload(self):
        """``mcp/server/lowlevel/server.py``:676 logs the whole incoming
        message and IS admitted at DEBUG — so it looks like the argument-side
        twin of F-908 and is not.

        A REQUEST arrives there as a ``RequestResponder``, which defines
        neither ``__repr__`` nor ``__str__``, so ``%s`` renders
        ``<... object at 0x...>`` and no argument escapes. Measured, because
        the level alone would have put this family IN.
        """
        from mcp.shared.session import RequestResponder

        assert RequestResponder.__repr__ is object.__repr__
        assert RequestResponder.__str__ is object.__str__

    def test_the_one_argument_line_a_family_cap_could_never_reach(self):
        """RECORDED, not fixed: ``mcp/shared/session.py``:383-384 use
        module-level ``logging.warning``/``logging.debug``, i.e. the ROOT
        logger — so no family cap can reach them however the list grows, and
        :383 is at WARNING, which is above any floor this mechanism sets.

        Pinned so the finding's claim stays true of the installed SDK: if
        these ever move onto ``mcp.shared.session``, the family question
        re-opens and this goes RED.
        """
        from mcp.shared import session

        source = Path(session.__file__).read_text(encoding="utf-8")
        assert 'logging.warning(f"Failed to validate request:' in source
        assert 'logging.debug(f"Message that failed validation:' in source
