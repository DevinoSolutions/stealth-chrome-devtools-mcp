"""THE pin home for ``logging_setup``'s ROOT-logger withholding rule (F-911).

The fourth door of one family. F-906/F-908 hold a payload-rendering library
family at WARNING on its family ROOT; F-907 shapes a payload-carrying ARGUMENT
inside the record factory. Neither can reach this one, and the reason is
structural rather than a matter of degree:

``mcp/shared/session.py`` logs with the module-level ``logging.warning`` /
``logging.debug`` / ``logging.exception`` functions, i.e. **on the root logger
itself**. So the record's ``name`` is ``root``:

* no entry in :data:`PAYLOAD_LOG_FAMILIES` can reach it, however the tuple
  grows — there is no family to name. Capping ROOT is not an option either:
  root's level is the caller's, and lowering it would silence the whole process;
* the payload is **pre-interpolated by an f-string**, so ``record.args`` is
  empty and F-907's argument rule sees nothing.

MEASURED against the installed mcp 1.27.1 (``uv.lock``), by driving the real
``BaseSession._receive_loop`` over memory streams — no socket, no Chrome:

* ``:383`` ``logging.warning(f"Failed to validate request: {e}")`` — pydantic's
  middle-truncated ``input_value=`` echo of the caller's own arguments, at
  **WARNING**, so it needs no ``basicConfig`` from anybody;
* ``:384`` ``logging.debug(f"Message that failed validation: {message.message.root}")``
  — the WHOLE request, arguments and all, at DEBUG;
* ``:430`` ``logging.warning(f"Failed to validate notification: {e}. Message was:
  {message.message.root}")`` — the whole notification at **WARNING**, and NOT
  truncated, because the model is rendered by the f-string rather than by
  pydantic's error formatter.

And one second-order fact, measured here because it is what makes the WARNING
pair reach a durable file as shipped: the module-level ``logging.warning``
calls ``logging.basicConfig()`` when root has no handlers, so the SDK's first
such line permanently installs a stderr ``StreamHandler`` on root — in a
process where root deliberately carried none.

These pins drive the SDK's own code and a real ``LoggingIntegration``, for
``test_payload_log_floor.py``'s reason: every sink is a property of how the
stdlib, the SDK and Sentry compose, not of anything we could assert about our
own code alone.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import logging
from pathlib import Path

import anyio
import pytest
import sentry_sdk
from mcp.shared.message import SessionMessage
from mcp.shared.session import BaseSession
from mcp.types import (
    ClientNotification,
    ClientRequest,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
)
from sentry_sdk.integrations.logging import LoggingIntegration

import logging_state
from stealth_chrome_devtools_mcp.embedded import logging_setup, payload_log_sites
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings

# --------------------------------------------------------------------------
# The payload, by the door it comes through. One marker each, so a failure
# names WHICH of the SDK's three lines escaped.
# --------------------------------------------------------------------------
#: ``session.py``:383 (WARNING) and :384 (DEBUG) — a request whose params will
#: not validate. The params are deliberately SHORT: pydantic truncates
#: ``input_value=`` in the middle, so a long dict hides the marker and the pin
#: would pass for a reason that has nothing to do with the fix.
REQUEST_MARK = "F911_REQUEST_ARGUMENT_PAYLOAD"
#: ``session.py``:430 (WARNING) — a notification that will not validate.
NOTIFICATION_MARK = "F911_NOTIFICATION_PAYLOAD"

PAYLOAD_MARKS = {
    "session.py:383/384 request arguments (WARNING/DEBUG)": REQUEST_MARK,
    "session.py:430 notification body (WARNING)": NOTIFICATION_MARK,
}

#: ``session.py``:478 — a real diagnostic on the SAME module, at WARNING. The
#: rule withholds its TEXT; what must survive is the record itself, at its own
#: level, naming its own site. See :class:`TestWithholdingIsNotSilencing`.
DIAGNOSTIC_LINE = 478

#: The installed SDK file the rule is keyed on, as the rule spells it.
SDK_MODULE = "mcp/shared/session.py"


def _sdk_source() -> tuple[str, ast.Module]:
    """The INSTALLED ``mcp/shared/session.py``, parsed.

    Read off disk rather than paraphrased, so a dependency bump that moves
    these lines onto a named logger makes the premise pins RED and the rule
    retirable, instead of leaving a rule standing over a door that closed.
    """
    import mcp.shared.session as sdk

    text = Path(sdk.__file__).read_text(encoding="utf-8")
    return text, ast.parse(text)


# --------------------------------------------------------------------------
# Driving the SDK's real receive loop
# --------------------------------------------------------------------------
async def _feed(messages: list[JSONRPCMessage]) -> None:
    read_send, read_recv = anyio.create_memory_object_stream(10)
    write_send, _write_recv = anyio.create_memory_object_stream(10)
    session = BaseSession(read_recv, write_send, ClientRequest, ClientNotification)
    async with session:
        for message in messages:
            await read_send.send(SessionMessage(message=message))
        # The loop is a task in the session's own group; give it a turn. The
        # bound is the SDK's, not ours -- there is nothing to wait FOR here,
        # only work to let run.
        await anyio.sleep(0.25)


def emit_payload_lines() -> None:
    """Drive the three real call sites, hermetically.

    Deliberately the library's own code path rather than a copy of its three
    lines: ``_receive_loop`` needs two memory streams and nothing else, so the
    pin can afford it -- and then an SDK refactor that changes WHAT these lines
    interpolate is visible here rather than only in a docstring
    (``test_payload_log_floor``'s ``emit_real_sse_tool_result`` reasoning).
    """
    bad_request = JSONRPCMessage(
        JSONRPCRequest(
            jsonrpc="2.0",
            id=1,
            method="x/unknown",
            params={"tok": REQUEST_MARK},
        )
    )
    bad_notification = JSONRPCMessage(
        JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/unknown",
            params={"tok": NOTIFICATION_MARK},
        )
    )
    asyncio.run(_feed([bad_request, bad_notification]))


def emit_sdk_diagnostic() -> None:
    """``session.py``:478's shape, emitted FROM that file's own site.

    It cannot be driven through ``_receive_loop`` without a response whose id
    the SDK refuses to normalise, so the record is made by hand -- but with the
    real module's ``pathname`` and ``lineno``, because those two are the whole
    key the rule reads and faking anything else would make the pin measure a
    different record than production has.
    """
    import mcp.shared.session as sdk

    record = logging.getLogger().makeRecord(
        "root",
        logging.WARNING,
        sdk.__file__,
        DIAGNOSTIC_LINE,
        "Response ID %r cannot be normalized to match pending requests",
        ("abc",),
        None,
    )
    logging.getLogger().handle(record)


# --------------------------------------------------------------------------
# Harness — the four sinks, as ``test_payload_log_floor`` measures them
# --------------------------------------------------------------------------
class _RefusingTransport(sentry_sdk.transport.Transport):
    """Nothing may leave this machine, loudly."""

    def capture_envelope(self, envelope) -> None:
        raise AssertionError("F-911 pin tried to ship a real Sentry envelope")


class _RootCapture(logging.Handler):
    """Whatever this process has on root — see ``test_payload_log_floor``."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.text += f"{record.name} {record.levelname} {record.getMessage()}\n"


class Sinks:
    def __init__(
        self,
        durable: str,
        sentry: str,
        breadcrumbs: str,
        downstream: str,
        capture: _RootCapture,
    ) -> None:
        self.durable = self._found(durable)
        self.sentry = self._found(sentry)
        self.breadcrumbs = self._found(breadcrumbs)
        self.downstream = self._found(downstream)
        self.capture = capture

    @staticmethod
    def _found(text: str) -> set[str]:
        return {name for name, mark in PAYLOAD_MARKS.items() if mark in text}

    @property
    def all_payload_reach(self) -> set[str]:
        return self.durable | self.sentry | self.breadcrumbs | self.downstream


@pytest.fixture(autouse=True)
def _isolated_logging():
    """Own every process-global these pins mutate, and hand them all back."""
    ring_was_enabled = debug_logger._enabled
    with logging_state.owned():
        get_settings.cache_clear()
        try:
            yield
        finally:
            debug_logger.enable() if ring_was_enabled else debug_logger.disable()
            get_settings.cache_clear()


def drive(
    log_dir: Path,
    *,
    role: str = "backend",
    basicconfig: str | None = None,
    configure: bool = True,
) -> Sinks:
    """Run one configuration and report what each sink received.

    ``configure=False`` is how the RED half is measured: the identical drive
    with our own setup never called, which is 2.1.12's behaviour exactly.
    """
    events: list[dict] = []
    crumbs: list[dict] = []
    err = io.StringIO()
    downstream = _RootCapture()

    with contextlib.redirect_stderr(err), contextlib.ExitStack() as stack:
        if basicconfig == "before":
            logging.basicConfig(level=logging.DEBUG, force=True)

        log_path = (
            logging_setup.configure_logging(role)
            if configure
            else log_dir / f"{role}-unconfigured.log"
        )

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
            before_breadcrumb=lambda crumb, hint: crumbs.append(crumb) or crumb,
        )
        assert isinstance(client.transport, _RefusingTransport), (
            "refusing to drive a pin against anything but the transport double"
        )
        scope = stack.enter_context(sentry_sdk.isolation_scope())
        scope.set_client(client)

        emit_payload_lines()
        emit_sdk_diagnostic()
        # Force an event, so breadcrumbs accrued above are serialized with it.
        logging.getLogger("stealth.probe").error("F911 probe")

    for handler in logging.getLogger(f"stealth.{role}").handlers:
        with contextlib.suppress(Exception):
            handler.flush()

    durable = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return Sinks(
        durable,
        json.dumps(events, default=str),
        json.dumps(crumbs, default=str),
        downstream.text + err.getvalue(),
        downstream,
    )


SHIPPED = [
    pytest.param({"role": "backend"}, id="backend"),
    pytest.param({"role": "proxy"}, id="proxy"),
]
CALLER_DEBUG = [
    pytest.param({"role": "backend", "basicconfig": "before"}, id="basicConfig-before"),
    pytest.param({"role": "backend", "basicconfig": "after"}, id="basicConfig-after"),
]


# --------------------------------------------------------------------------
# The premises — measured off the INSTALLED SDK, so a bump that closes the
# door makes these RED rather than leaving a rule standing over nothing.
# --------------------------------------------------------------------------
class TestTheRootLoggerDoorIsReal:
    def test_the_three_lines_are_root_logger_calls_in_the_installed_sdk(self):
        """``logging.<level>(...)``, never ``logger.<level>(...)``.

        This is the whole reason no family cap can reach them. An SDK that
        gives ``shared/session.py`` a module logger closes the door, and this
        pin is how that becomes a decision rather than a silent carry.
        """
        _text, tree = _sdk_source()
        root_calls = {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logging"
            and node.func.attr
            in {"debug", "info", "warning", "error", "exception", "critical"}
        }
        assert root_calls, (
            "mcp/shared/session.py no longer logs on the root logger; F-911's "
            "rule has nothing left to cover and should be retired"
        )
        named_calls = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ]
        assert not named_calls, (
            f"mcp/shared/session.py grew a NAMED logger at {named_calls}; the "
            f"module is no longer uniformly root-logging and the rule's "
            f"module granularity needs re-deciding"
        )

    def test_a_root_record_carries_no_family_any_floor_could_name(self, tmp_path):
        """``record.name`` is ``root``, so ``PAYLOAD_LOG_FAMILIES`` is inert here.

        Stated as an assertion rather than a sentence because it is the one
        fact that makes this a separate finding from F-906/F-908.
        """
        seen: list[str] = []

        class Watch(logging.Handler):
            def emit(self, record):
                if record.module == "session":
                    seen.append(record.name)

        logging_setup.apply_payload_log_floor()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(Watch())
        emit_payload_lines()
        assert seen, "the SDK's receive loop logged nothing; the drive is broken"
        assert set(seen) == {"root"}, seen
        for family in logging_setup.PAYLOAD_LOG_FAMILIES:
            assert not "root".startswith(family), (
                f"{family} would reach a root record; the premise is wrong"
            )

    def test_the_payload_is_pre_interpolated_so_record_args_is_empty(self, tmp_path):
        """F-907's argument rule cannot see an f-string. Measured, not assumed."""
        args_seen: list[object] = []

        class Watch(logging.Handler):
            def emit(self, record):
                if record.module == "session" and record.lineno in (383, 384, 430):
                    args_seen.append(record.args)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(Watch())
        emit_payload_lines()
        assert args_seen, "none of the three lines fired"
        assert all(not args for args in args_seen), (
            f"a payload line passed %-args after all: {args_seen}"
        )

    def test_the_sdks_first_root_call_installs_a_handler_on_root(self):
        """``logging.warning`` module-level calls ``basicConfig()``.

        A dependency permanently mutating OUR process's root logger is worth
        pinning on its own: it is why these WARNING lines reach stderr — hence
        ``backend-boot.log`` — with no ``basicConfig`` from any caller, and it
        is a fact about the stdlib that the finding's severity rests on.

        Root's handlers are stripped locally — ``logging_state.reset`` keeps
        pytest's on purpose, and this pin is about the world where root carries
        NONE, which is the shipped one. ``logging_state.owned`` puts them back.
        """
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        assert root.handlers == [], "this pin measures the NO-handler world"
        with contextlib.redirect_stderr(io.StringIO()):
            # LOG015 says "use your own logger" — which is precisely what the
            # SDK does not do, and is the whole subject of this pin.
            logging.warning("F911 premise probe")  # noqa: LOG015  PERMANENT(F-911 — the root-logger call IS what is being measured)
        assert root.handlers, (
            "logging.warning no longer installs a root handler; the reach "
            "argument in the finding needs re-measuring"
        )


# --------------------------------------------------------------------------
# The leak, and its closure
# --------------------------------------------------------------------------
class TestTheLeakIsReal:
    """Without our setup, the payload really does reach the sinks.

    A pin for a fix must first be able to FAIL. This is that half: the same
    drive, our ``configure_logging`` never called — i.e. 2.1.12 exactly.
    """

    @pytest.mark.parametrize("config", SHIPPED + CALLER_DEBUG)
    def test_unconfigured_process_leaks_to_a_root_handler(
        self, config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, configure=False, **config)
        assert sinks.downstream, (
            "nothing leaked even unconfigured — this pin would pass vacuously"
        )

    def test_unconfigured_process_leaks_to_sentry(self, tmp_path, monkeypatch):
        """WARNING is a breadcrumb (``LoggingIntegration``'s handler sits at
        INFO), attached to the next event."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, configure=False)
        assert sinks.breadcrumbs, "nothing reached Sentry; the drive is broken"


class TestNoPayloadReachesAnySink:
    @pytest.mark.parametrize("config", SHIPPED + CALLER_DEBUG)
    def test_no_root_logger_payload_reaches_any_sink(
        self, config, tmp_path, monkeypatch
    ):
        """The whole finding in one assertion, over every sink at once."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        assert sinks.all_payload_reach == set(), (
            "a root-logger payload reached a sink: "
            f"durable={sorted(sinks.durable)} sentry={sorted(sinks.sentry)} "
            f"breadcrumbs={sorted(sinks.breadcrumbs)} "
            f"downstream={sorted(sinks.downstream)}"
        )

    @pytest.mark.parametrize("config", SHIPPED + CALLER_DEBUG)
    def test_the_sentry_breadcrumb_is_clean_without_a_before_breadcrumb_hook(
        self, config, tmp_path, monkeypatch
    ):
        """Why there is no ``observability.before_breadcrumb`` beside this.

        The factory runs in ``Logger.makeRecord``, which is upstream of
        ``Logger.callHandlers`` — the one method ``LoggingIntegration`` patches
        — so the breadcrumb is built from an ALREADY-withheld record. A hook in
        ``observability`` would be a second home for one decision (convention
        4) that could only ever matter if this one were removed.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        assert sinks.breadcrumbs == set(), sorted(sinks.breadcrumbs)

    def test_sentry_sees_the_record_only_through_call_handlers(self):
        """The premise the paragraph above rests on, asserted rather than said.

        ``test_payload_log_floor``'s Sentry premise pin, re-made for the hook
        THIS fix relies on: if the SDK ever moved its hook upstream of
        ``makeRecord``, the factory would stop being upstream of Sentry and the
        breadcrumb assertion above would be passing for the wrong reason.
        """
        integration = LoggingIntegration(event_level=logging.ERROR)
        integration.setup_once()
        assert getattr(logging.Logger.callHandlers, "__module__", "").startswith(
            "sentry_sdk"
        ), (
            "LoggingIntegration no longer patches Logger.callHandlers; "
            "re-measure whether a record factory is still upstream of Sentry"
        )
        assert logging.Logger.makeRecord.__module__ == "logging", (
            "something has patched Logger.makeRecord; the factory's position "
            "upstream of every sink is no longer a stdlib guarantee"
        )


class TestWithholdingIsNotSilencing:
    """A rule that DROPS the record is the wrong fix — the same lens F-906
    applies to its floor and F-907 to its shape."""

    def test_the_record_still_arrives_at_its_own_level_and_site(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path)
        withheld = [r for r in sinks.capture.records if r.module == "session"]
        assert withheld, "every SDK record was dropped rather than withheld"
        assert {r.levelno for r in withheld} <= {
            logging.DEBUG,
            logging.WARNING,
            logging.ERROR,
        }
        for record in withheld:
            message = record.getMessage()
            assert SDK_MODULE in message, message
            assert str(record.lineno) in message, message

    def test_the_real_diagnostic_line_keeps_its_site(self, tmp_path, monkeypatch):
        """``session.py``:478 carries no payload and its TEXT is withheld anyway.

        That cost is named rather than hidden (finding §6): the rule's unit is
        the MODULE, because a line number is the least stable thing in a
        dependency and a message template is text the library may reword. What
        survives is what makes the record actionable — which line fired.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path)
        diagnostic = [r for r in sinks.capture.records if r.lineno == DIAGNOSTIC_LINE]
        assert diagnostic, "the diagnostic record never arrived"
        assert f"{SDK_MODULE}:{DIAGNOSTIC_LINE}" in diagnostic[0].getMessage()

    def test_our_own_records_are_untouched(self, tmp_path, monkeypatch):
        """The key is the SITE, and our own sites are not it."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        capture = _RootCapture()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        logging.getLogger("stealth.test").propagate = True
        logging.getLogger("stealth.test").warning("ours: %s", REQUEST_MARK)
        assert REQUEST_MARK in capture.text


class TestTheKeyIsTheSiteAndNotTheText:
    def test_a_reworded_sdk_line_is_still_withheld(self, tmp_path, monkeypatch):
        """Keyed on the record's own ``pathname``, so the SDK may reword every
        one of these lines — F-906's rule about pattern-matching a library's
        text, applied to the mechanism that replaced it."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        import mcp.shared.session as sdk

        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        capture = _RootCapture()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        record = root.makeRecord(
            "root",
            logging.WARNING,
            sdk.__file__,
            999,
            f"a wording nobody has written yet: {REQUEST_MARK}",
            (),
            None,
        )
        root.handle(record)
        assert REQUEST_MARK not in capture.text
        assert f"{SDK_MODULE}:999" in capture.text

    def test_a_same_named_module_elsewhere_is_not_withheld(self, tmp_path, monkeypatch):
        """The stem is the cheap first gate; the PATH is the answer.

        Without the path half, any third party's own ``session.py`` — and
        there are several in a typical tree — would be silently withheld.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        capture = _RootCapture()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        record = root.makeRecord(
            "somelib",
            logging.WARNING,
            "/opt/somelib/shared/session.py",
            10,
            f"not the mcp SDK: {REQUEST_MARK}",
            (),
            None,
        )
        root.handle(record)
        assert REQUEST_MARK in capture.text


class TestTheShippedStderrPath:
    """The sink the production backend actually uses, which ``drive`` hides.

    ``drive`` puts a ``_RootCapture`` on root to stand in for "whatever this
    process has on root" — but a handler there is exactly what stops the SDK's
    module-level ``logging.warning`` calling ``basicConfig()``. In production
    root carries none, so the SDK's first line installs its OWN
    ``StreamHandler`` on our root and writes to stderr — which for the backend
    is redirected into ``backend-boot.log``, a durable file. That is the cell
    that matters, and it is only measurable with root bare.

    ``basicConfig``'s handler binds ``sys.stderr`` at CREATION, so the
    ``redirect_stderr`` must be entered BEFORE the drive (F-906's trap, where
    entering it afterwards read as a false negative).
    """

    @staticmethod
    def _emit_with_no_handlers() -> str:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        assert root.handlers == [], (
            "this pin measures the NO-handler world; a handler here stops the "
            "SDK's basicConfig() and the assertion below would prove nothing"
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            emit_payload_lines()
        return err.getvalue()

    def test_without_configure_logging_the_payload_does_reach_stderr(self):
        """The half that keeps the next one honest: unconfigured, the request
        arguments and the whole notification reach stderr with no
        ``basicConfig`` from any caller, because the SDK arranges its own."""
        text = self._emit_with_no_handlers()
        assert "Failed to validate" in text, (
            "the SDK's lines never reached stderr; this pin now measures nothing"
        )
        assert REQUEST_MARK in text, "the unredacted leak is no longer visible"
        assert NOTIFICATION_MARK in text

    def test_no_payload_reaches_the_backends_stderr(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        text = self._emit_with_no_handlers()
        for name, mark in PAYLOAD_MARKS.items():
            assert mark not in text, f"{name} reached stderr, hence backend-boot.log"
        assert SDK_MODULE in text, "the record was dropped rather than withheld"


class TestWithholdingCannotBreakFormatting:
    def test_a_percent_args_record_from_the_module_still_renders(
        self, tmp_path, monkeypatch
    ):
        """``session.py``:422 logs with ``%s`` args, and the withheld message
        has no ``%s`` left — so the arguments have to go WITH it.

        Left behind, ``record.getMessage()`` raises ``TypeError: not all
        arguments converted`` inside every handler that formats, which turns a
        redaction into an outage. Driven through a real handler rather than by
        calling ``getMessage()``, because that is where it would have bitten.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        import mcp.shared.session as sdk

        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        capture = _RootCapture()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        record = root.makeRecord(
            "root",
            logging.ERROR,
            sdk.__file__,
            422,
            "Progress callback raised an exception: %s",
            (ValueError(REQUEST_MARK),),
            None,
        )
        root.handle(record)
        assert f"{SDK_MODULE}:422" in capture.text
        assert "args=1" in capture.text
        assert REQUEST_MARK not in capture.text

    def test_a_dict_style_record_from_the_module_still_renders(
        self, tmp_path, monkeypatch
    ):
        """logging's ``%(name)s`` single-mapping special case.

        F-907's argument rule leaves a mapping alone rather than guess a
        rewrite; this rule cannot, because it replaces the message. So the
        mapping is cleared with it, and ``args=`` is omitted — the count would
        be the dict's key count and would read as an argument count.

        The mapping is passed WRAPPED IN A TUPLE, which is how a real caller
        reaches this: ``Logger._log`` hands ``makeRecord`` the ``*args`` tuple
        and ``LogRecord.__init__`` unwraps a sole ``Mapping`` out of it. Handed
        the bare dict, that unwrap does ``args[0]`` on a mapping with no ``0``
        key and dies in the stdlib — measured, and a pin that constructed the
        record that way would be measuring its own mistake.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        import mcp.shared.session as sdk

        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        capture = _RootCapture()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        # The premise, on a CONTROL record built without the factory: the
        # stdlib really does unwrap a sole mapping out of the args tuple. It
        # cannot be asserted on the record below, because by the time
        # `makeRecord` returns, the rule under test has already cleared it —
        # which is the behaviour, not a way to check the premise.
        control = logging.LogRecord(
            "root",
            logging.WARNING,
            sdk.__file__,
            430,
            "%(tok)s",
            ({"tok": NOTIFICATION_MARK},),
            None,
        )
        assert control.args == {"tok": NOTIFICATION_MARK}, (
            "the stdlib no longer unwraps a sole mapping; this pin is not "
            "measuring logging's single-mapping special case"
        )

        record = root.makeRecord(
            "root",
            logging.WARNING,
            sdk.__file__,
            430,
            "%(tok)s",
            ({"tok": NOTIFICATION_MARK},),
            None,
        )
        assert record.args == (), "the mapping was left beside a withheld message"
        root.handle(record)
        assert NOTIFICATION_MARK not in capture.text
        assert f"{SDK_MODULE}:430" in capture.text
        assert "args=" not in capture.text


class TestTheRuleIsDerivedAndIdempotent:
    def test_the_cheap_gate_is_derived_from_the_module_list(self):
        """Two spellings of one fact is how a rule comes to cover nothing."""
        expected = frozenset(
            site.rpartition("/")[2].removesuffix(".py")
            for site in payload_log_sites.PAYLOAD_LOG_SITES
        )
        assert expected == payload_log_sites._SITE_STEMS

    def test_installing_twice_chains_one_factory(self, tmp_path, monkeypatch):
        """``server.py`` is executed three times under runpy."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        logging_setup.install_payload_arg_redaction()
        first = logging.getLogRecordFactory()
        logging_setup.install_payload_arg_redaction()
        assert logging.getLogRecordFactory() is first

    def test_f907s_argument_rule_still_fires(self, tmp_path, monkeypatch):
        """One factory, two rules — the composition, not two installs.

        The element is a REAL ``nodriver.core.element.Element`` from nodriver's
        own constructors, for F-907's pin file's reason: a hand-written double
        renders whatever ``__repr__`` we gave it and could only measure the
        test against itself.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        from nodriver import cdp
        from nodriver.core.element import Element

        node = cdp.dom.Node.from_json(
            {
                "nodeId": 42,
                "backendNodeId": 4242,
                "nodeType": 1,
                "nodeName": "INPUT",
                "localName": "input",
                "nodeValue": "",
                "childNodeCount": 0,
                "attributes": ["type", "password", "value", REQUEST_MARK],
            }
        )
        element = Element(node, tab=None)
        assert REQUEST_MARK in repr(element), (
            "nodriver no longer renders the attribute value; this pin would "
            "pass without F-907's rule doing anything"
        )

        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        capture = _RootCapture()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        logging.getLogger("nodriver.core.element").warning(
            "could not calculate box model for %s", element
        )
        assert REQUEST_MARK not in capture.text
        assert "attrs=[type, value]" in capture.text
