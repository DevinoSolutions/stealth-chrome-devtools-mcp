"""THE pin home for "what a validation error may say about its INPUT" (F-913).

The FIFTH door of one family, and the first one that is not about a log
RECORD at all. F-906/F-908 hold a payload-rendering library family down by
LEVEL; F-907 shapes a payload-carrying ARGUMENT; F-911 withholds the rendered
TEXT of a record made by a payload-rendering MODULE. All three read the record.
This payload is inside the **exception**, and all three are blind to it by
construction:

* **by LEVEL** — ``mcp.client`` IS in :data:`PAYLOAD_LOG_FAMILIES` and IS
  floored at WARNING, and the line sits at **ERROR**. Lowering the floor to
  reach it silences the SDK's real faults on the one leg that reports them,
  which is the trade F-906 refused for nodriver;
* **by ARGUMENT** — the message is a static string literal and ``record.args``
  is empty. There is nothing to shape;
* **by SITE** — F-911's rule withholds ``record.msg``, which here is the
  static ``"Error parsing SSE message"``. Withholding it deletes the one thing
  that is safe and leaves the payload exactly where it was.

MEASURED against the installed mcp 1.27.1 / pydantic 2.11.7, by driving the
real ``StreamableHTTPTransport._handle_sse_event`` over a memory stream — no
socket, no backend, no Chrome. On the proxy leg the SSE data IS the serialised
answer to a ``tools/call``, so what ``input_value=`` quotes is a tool RESULT.

**How much it quotes was measured rather than taken from the finding, and the
finding's sentence was wrong.** F-913 §1 said the middle truncation "loses
neither end — a cookie jar's first entries and its last are both rendered".
pydantic caps EACH echo at a fixed **50 characters of the input**: the first
24 and the last 23, joined by ``...`` (truncation begins at 49). So for a
whole JSON-RPC frame the head is always the envelope ``{"jsonrpc": "2.0",
"id":`` — never the jar's first entries — and what actually leaks is:

* the **last 23 characters** of the frame, i.e. the END of the tool answer;
* **any input value SHORTER than 50 characters, echoed WHOLE** — which is the
  sharp edge, because a cookie value, a session id or a short token is
  frequently under 50; and
* **once per union arm**: ``JSONRPCMessage`` is a 4-arm union, so
  :data:`LEAF_FRAME` produced 9 errors and **276** echoed characters —
  3 whole-frame echoes of 52 plus 6 short-leaf echoes of 20. Measured on that
  fixture, and the fixture is named because a bare "measured" with no subject
  is what let the previous number (216) survive a rewrite of the frame.

And it lands in the harshest of the four sinks: ``LoggingIntegration`` ships an
ERROR as a full Sentry **EVENT**, not a breadcrumb, and ``expected_events``
recognises none of its five classes (the near miss, ``caller-input``, is
decided by FRAMES this traceback does not have).

These pins drive the SDK's own code and a real ``LoggingIntegration``, for
``test_root_logger_payload``'s reason: every sink is a property of how the
stdlib, the SDK and Sentry compose.
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

# Imported through the SDK's own module, never from ``httpx_sse`` directly: the
# pin is about what THAT file parses, so a dependency swap under it should be
# visible here rather than silently leaving the pin driving a stranger's class.
from mcp.client.streamable_http import ServerSentEvent, StreamableHTTPTransport
from sentry_sdk.integrations.logging import LoggingIntegration

import logging_state
from stealth_chrome_devtools_mcp import expected_events
from stealth_chrome_devtools_mcp.embedded import logging_setup, payload_log_sites
from stealth_chrome_devtools_mcp.settings import get_settings

#: The END of a tool answer — the 23 characters pydantic keeps at the tail of
#: a whole-frame echo. Both markers are kept SHORT deliberately: the cap is 50
#: characters, so a long marker would be cut in half and the pin would pass or
#: fail for a reason that has nothing to do with the fix
#: (``test_root_logger_payload``'s rule, re-derived from this measurement).
TAIL_MARK = "F913_ANSWER_TAIL"

#: A short input VALUE, which pydantic echoes WHOLE because it is under the
#: cap. This is the shape that actually matters — a cookie value or a session
#: id is frequently shorter than 50 characters.
LEAF_MARK = "F913_LEAF"

#: The installed SDK file the rule is keyed on, as the rule spells it.
SDK_MODULE = "mcp/client/streamable_http.py"

#: The named logger that file uses. It is NOT root, which is what makes this a
#: second table rather than an entry in F-911's.
SDK_LOGGER = "mcp.client.streamable_http"

#: A tool answer that arrives CUT — the realistic SSE failure. One
#: ``json_invalid`` error whose echo keeps the frame's last 23 characters.
#: Measured: 314 bytes in, 52 characters echoed, the marker among them.
TAIL_FRAME = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"cookies": "x" * 200}) + TAIL_MARK}
            ]
        },
    }
)[:-3]

#: A tool answer that is valid JSON and fails the UNION — every arm reports,
#: and each one echoes the sub-value it tripped over. Measured: 9 errors, and
#: the short leaf rendered in full six times over.
LEAF_FRAME = json.dumps(
    {"jsonrpc": "2.0", "id": {"tok": LEAF_MARK}, "result": {"cookies": "y" * 200}}
)


def _sdk_source() -> tuple[str, ast.Module]:
    """The INSTALLED ``mcp/client/streamable_http.py``, parsed.

    Read off disk rather than paraphrased, so a bump that moves or renames
    these sites makes the premise pins RED instead of leaving a rule standing
    over a door that closed.
    """
    import mcp.client.streamable_http as sdk

    text = Path(sdk.__file__).read_text(encoding="utf-8")
    return text, ast.parse(text)


# --------------------------------------------------------------------------
# Driving the SDK's real SSE parse
# --------------------------------------------------------------------------
async def _feed(data: str) -> None:
    transport = StreamableHTTPTransport(url="http://127.0.0.1:1/mcp/")
    send, recv = anyio.create_memory_object_stream(10)
    # The SDK logs and THEN sends the exception downstream (`:241`). The send is
    # drained so the stream does not hold it, but the object itself is the SDK's
    # to pass on -- see `TestTheSdkStillOwnsItsOwnException`.
    await transport._handle_sse_event(ServerSentEvent(event="message", data=data), send)
    with contextlib.suppress(Exception):
        recv.receive_nowait()


def emit_unparseable_tool_result(data: str = TAIL_FRAME) -> None:
    """Drive the real call site, hermetically.

    The library's own code path rather than a copy of its one line, for
    ``test_root_logger_payload.emit_payload_lines``' reason: an SDK refactor
    that changes what this site carries is then visible here rather than only
    in a docstring.
    """
    asyncio.run(_feed(data))


# --------------------------------------------------------------------------
# Harness — the sinks, as the two sibling pin files measure them
# --------------------------------------------------------------------------
class _RefusingTransport(sentry_sdk.transport.Transport):
    """Nothing may leave this machine, loudly."""

    def capture_envelope(self, envelope) -> None:
        raise AssertionError("F-913 pin tried to ship a real Sentry envelope")


class _RootCapture(logging.Handler):
    """Whatever this process has on root, formatted the way a handler would.

    The traceback is rendered explicitly, because the traceback IS the subject:
    a handler that only read ``getMessage()`` would report this leak clean.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
        self.text += formatter.format(record) + "\n"


class Sinks:
    def __init__(
        self,
        durable: str,
        events: list[dict],
        crumbs: list[dict],
        downstream: str,
        capture: _RootCapture,
    ) -> None:
        self.durable = durable
        self.events = events
        self.sentry = json.dumps(events, default=str)
        self.breadcrumbs = json.dumps(crumbs, default=str)
        self.downstream = downstream
        self.capture = capture

    def reached(self, mark: str) -> dict[str, bool]:
        return {
            "durable": mark in self.durable,
            "sentry-event": mark in self.sentry,
            "breadcrumbs": mark in self.breadcrumbs,
            "downstream": mark in self.downstream,
        }

    def leaked(self, mark: str) -> list[str]:
        return sorted(name for name, hit in self.reached(mark).items() if hit)

    @property
    def exception_values(self) -> list[dict]:
        return [
            value
            for event in self.events
            for value in (event.get("exception") or {}).get("values") or []
        ]

    @property
    def frames(self) -> list[str]:
        return [
            f"{frame.get('module')}.{frame.get('function')}"
            for value in self.exception_values
            for frame in ((value.get("stacktrace") or {}).get("frames") or [])
        ]

    @property
    def restatement(self) -> str:
        """What the record carries INSTEAD of the quoting exception.

        Read from the :class:`WithheldInputError` value specifically, never
        from everything this run rendered. That distinction is the whole point
        of the property: the SDK's own traceback frames name ``JSONRPCMessage``
        and ``pydantic_core`` on their own, so an assertion over
        ``sentry + downstream`` passes off the NEIGHBOURING text and pins
        nothing about the restatement at all.

        Measured before this existed: dropping :func:`payload_log_sites._title`,
        dropping :func:`payload_log_sites._count`, and reducing the chain walk
        to ``exc_info[1]`` alone each left all 38 nodes in this file GREEN.

        The type name is DERIVED from the class rather than typed, so a rename
        makes this empty — and an empty restatement fails every assertion that
        reads it, which is the right direction.
        """
        wanted = payload_log_sites.WithheldInputError.__name__
        return "\n".join(
            value.get("value") or ""
            for value in self.exception_values
            if (value.get("type") or "") == wanted
        )


@pytest.fixture(autouse=True)
def _isolated_logging(tmp_path, monkeypatch):
    """Own every process-global these pins mutate, and hand them all back.

    The browser-session root is pointed at ``tmp_path`` EXPLICITLY even though
    nothing here spawns anything: F-841's rule is that a suite never resolves
    the operator's real root, and "this file happens not to reach it" is not
    the kind of claim that survives an edit.
    """
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("STEALTH_MCP_BROWSER_SESSION_ROOT", str(tmp_path))
    with logging_state.owned():
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def drive(
    tmp_path: Path,
    *,
    role: str = "proxy",
    configure: bool = True,
    data: str = TAIL_FRAME,
) -> Sinks:
    """Run one configuration and report what each sink received.

    ``configure=False`` is how the RED half is measured: the identical drive
    with our own setup never called, which is 2.1.12's behaviour exactly.

    ``role`` defaults to ``proxy`` because that is the leg this finding is
    about — the stdio proxy reading the backend's answer back.
    """
    events: list[dict] = []
    crumbs: list[dict] = []
    err = io.StringIO()
    downstream = _RootCapture()

    with contextlib.redirect_stderr(err), contextlib.ExitStack() as stack:
        log_path = (
            logging_setup.configure_logging(role)
            if configure
            else tmp_path / f"{role}-unconfigured.log"
        )

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
            # The shipped setting (`observability.sentry_init`). Without it the
            # SDK captures frame LOCALS, which hold `sse.data` itself -- so a
            # pin run without it would measure a leak production does not have
            # and would go green on a fix that changed nothing.
            include_local_variables=False,
        )
        assert isinstance(client.transport, _RefusingTransport), (
            "refusing to drive a pin against anything but the transport double"
        )
        scope = stack.enter_context(sentry_sdk.isolation_scope())
        scope.set_client(client)

        emit_unparseable_tool_result(data)

    for handler in logging.getLogger(f"stealth.{role}").handlers:
        with contextlib.suppress(Exception):
            handler.flush()

    durable = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return Sinks(durable, events, crumbs, downstream.text + err.getvalue(), downstream)


SHIPPED = [
    pytest.param({"role": "proxy"}, id="proxy"),
    pytest.param({"role": "backend"}, id="backend"),
]


# --------------------------------------------------------------------------
# The premises — measured off the INSTALLED SDK and pydantic, so a bump that
# closes the door makes these RED rather than leaving a rule over nothing.
# --------------------------------------------------------------------------
class TestTheExceptionDoorIsReal:
    def test_the_sites_log_a_static_message_with_no_arguments(self):
        """``logger.exception("<literal>")`` — nothing for F-907 or F-911.

        The whole reason this needs a third mechanism. An SDK that starts
        interpolating here makes this RED, at which point F-911's site table is
        the right home and this rule can be re-decided.
        """
        _text, tree = _sdk_source()
        static = {
            node.lineno: node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "exception"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        assert static, (
            "mcp/client/streamable_http.py no longer has a static-message "
            "logger.exception site; F-913's premise needs re-measuring"
        )

    def test_the_file_uses_a_named_logger_so_this_is_not_f911s_door(self):
        """``record.name`` is ``mcp.client.streamable_http``, not ``root``.

        F-911's table is for modules that root-log. This one does not, which is
        why the exception table is a SECOND table and not an entry in the first.
        """
        _text, tree = _sdk_source()
        root_calls = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logging"
            and node.func.attr
            in {"debug", "info", "warning", "error", "exception", "critical"}
        ]
        assert not root_calls, (
            f"the file grew root-logger calls at {root_calls}; it may now also "
            f"belong in PAYLOAD_LOG_SITES"
        )

    def test_the_record_arrives_at_error_above_f908s_floor(self):
        """F-908 floors ``mcp.client`` at WARNING and ERROR is above it."""
        logging_setup.apply_payload_log_floor()
        assert logging.getLogger(SDK_LOGGER).getEffectiveLevel() <= logging.ERROR
        assert any(
            family == SDK_LOGGER or SDK_LOGGER.startswith(f"{family}.")
            for family in logging_setup.PAYLOAD_LOG_FAMILIES
        ), "mcp.client is no longer a floored family; F-913's §2 needs re-reading"

    def test_the_static_message_is_the_only_safe_half(self):
        """Driven: ``record.msg`` is safe, ``record.args`` empty, payload in
        ``exc_info``. This is what makes F-911's rule close nothing here."""
        seen: list[logging.LogRecord] = []

        class Watch(logging.Handler):
            def emit(self, record):
                if record.name == SDK_LOGGER:
                    seen.append(record)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(Watch())
        emit_unparseable_tool_result()
        assert seen, "the SDK's parse site logged nothing; the drive is broken"
        record = seen[-1]
        assert record.args in ((), None), record.args
        assert TAIL_MARK not in str(record.msg)
        assert record.exc_info is not None, "the payload's carrier is gone"
        assert TAIL_MARK in str(record.exc_info[1])

    def test_pydantic_really_does_echo_its_input(self):
        """Without this the rule would be redacting nothing."""
        from mcp.types import JSONRPCMessage

        with pytest.raises(Exception) as caught:  # noqa: PT011  PERMANENT(F-913 - the exception TYPE is the subject)
            JSONRPCMessage.model_validate_json(TAIL_FRAME)
        assert TAIL_MARK in str(caught.value)

    def test_the_echo_is_capped_at_fifty_characters_of_the_input(self):
        """The finding's §1 sentence, corrected by measurement.

        F-913 said the truncation "loses neither end — a cookie jar's first
        entries and its last are both rendered". It does not: the cap is a
        fixed 50 characters (24 + 23 + ``...``), so a whole-frame echo's head
        is always the JSON-RPC envelope. The leak is the frame's TAIL and any
        value short enough to escape the cap, and this pin is what keeps that
        claim honest if pydantic ever changes the number.
        """
        from mcp.types import JSONRPCMessage

        with pytest.raises(Exception) as caught:  # noqa: PT011  PERMANENT(F-913 - the exception TYPE is the subject)
            JSONRPCMessage.model_validate_json(TAIL_FRAME)
        echoed = [
            line for line in str(caught.value).splitlines() if "input_value=" in line
        ]
        assert echoed, "pydantic stopped echoing its input; F-913 may be moot"
        assert "..." in echoed[0], "the echo is no longer truncated at all"
        assert '{"jsonrpc": "2.0", "id":' in echoed[0], (
            "the head of a whole-frame echo is the envelope, not the payload"
        )

    def test_a_short_value_escapes_the_cap_and_is_echoed_whole(self):
        """The sharp edge: under the cap, nothing is truncated.

        A cookie value, a session id or a short token is frequently under 50
        characters, and a union failure echoes the sub-value each arm tripped
        over. Measured: 9 errors for one frame, the leaf rendered in full.
        """
        from mcp.types import JSONRPCMessage

        with pytest.raises(Exception) as caught:  # noqa: PT011  PERMANENT(F-913 - the exception TYPE is the subject)
            JSONRPCMessage.model_validate_json(LEAF_FRAME)
        text = str(caught.value)
        assert f"'{LEAF_MARK}'" in text, "the short leaf was not echoed whole"
        assert caught.value.error_count() > 1, "the union no longer reports per arm"

    def test_the_library_offers_an_input_free_rendering(self):
        """``errors(include_input=False)`` is what the restatement is BUILT from.

        The restatement is not a regex over pydantic's sentence — it is the
        library's own structured accessor with the input left out. If that
        accessor ever stops omitting it, this goes RED here rather than silently
        shipping a jar.
        """
        from mcp.types import JSONRPCMessage

        for frame, mark in ((TAIL_FRAME, TAIL_MARK), (LEAF_FRAME, LEAF_MARK)):
            with pytest.raises(Exception) as caught:  # noqa: PT011  PERMANENT(F-913 - the exception TYPE is the subject)
                JSONRPCMessage.model_validate_json(frame)
            safe = caught.value.errors(
                include_input=False, include_url=False, include_context=False
            )
            assert safe, "pydantic reported no errors at all"
            assert mark not in json.dumps(safe, default=str)
            assert all("input" not in error for error in safe)
            assert {"type", "loc"} <= set(safe[0])


# --------------------------------------------------------------------------
# The leak, and its closure
# --------------------------------------------------------------------------
class TestTheLeakIsReal:
    """A pin for a fix must first be able to FAIL."""

    def test_the_end_of_a_tool_answer_reaches_a_sink(self, tmp_path):
        """Truncation is NOT a mitigation: the frame's last 23 characters ride
        out whole, and they are the END of the tool answer."""
        sinks = drive(tmp_path, configure=False)
        assert sinks.leaked(TAIL_MARK), (
            "nothing leaked even unconfigured — this pin would pass vacuously"
        )

    def test_it_reaches_sentry_as_an_event_not_a_breadcrumb(self, tmp_path):
        """The harshest of the four sinks, and the one F-908's floor cannot
        reach: ``LoggingIntegration(event_level=ERROR)`` ships it whole."""
        sinks = drive(tmp_path, configure=False)
        assert sinks.events, "no Sentry event at all; the drive is broken"
        assert TAIL_MARK in sinks.sentry

    def test_a_short_value_reaches_a_sink_in_full(self, tmp_path):
        """The shape that actually matters — under the cap nothing is cut."""
        sinks = drive(tmp_path, configure=False, data=LEAF_FRAME)
        assert sinks.leaked(LEAF_MARK), (
            "the short leaf no longer leaks; re-measure the finding"
        )

    def test_expected_events_does_not_recognise_it(self, tmp_path):
        """§3: it is an UNRECOGNISED error, so ``before_send`` ships it.

        The near miss is ``caller-input``, which is decided by FRAMES this
        traceback does not have.
        """
        sinks = drive(tmp_path, configure=False)
        assert sinks.events, "no Sentry event at all; the drive is broken"
        for event in sinks.events:
            assert expected_events.classify(event, chain=None, error_base=None) is None


class TestNoToolResultReachesAnySink:
    @pytest.mark.parametrize("config", SHIPPED)
    def test_no_tool_result_reaches_any_sink(self, config, tmp_path):
        """The whole finding in one assertion, over every sink at once."""
        sinks = drive(tmp_path, **config)
        assert sinks.leaked(TAIL_MARK) == [], sinks.leaked(TAIL_MARK)

    @pytest.mark.parametrize("config", SHIPPED)
    def test_a_short_value_is_gone_too(self, config, tmp_path):
        """The union shape, where nine errors each echoed the leaf."""
        sinks = drive(tmp_path, data=LEAF_FRAME, **config)
        assert sinks.leaked(LEAF_MARK) == [], sinks.leaked(LEAF_MARK)


# --------------------------------------------------------------------------
# Restating is not silencing
# --------------------------------------------------------------------------
class TestRestatingIsNotSilencing:
    """The same lens F-906 applies to its floor and F-911 to its withholding: a
    rule that DROPS the diagnostic is the wrong fix."""

    def test_the_pydantic_type_and_the_field_path_survive(self, tmp_path):
        """What an operator acts on: WHICH pydantic error, at WHICH field.

        F-911 kept the module and the line for exactly this reason. Here the
        equivalent is the error ``type`` slug and the ``loc`` path, both read
        from the library's own ``errors()`` and neither derived from the input.

        Asserted against :attr:`Sinks.restatement` and NOT against everything
        rendered: this node read ``sentry + downstream`` until the F-913 review,
        where ``"JSONRPCMessage" in rendered`` was satisfied by the SDK's own
        traceback frames, so dropping the model name from the restatement left
        it green.
        """
        sinks = drive(tmp_path)
        restated = sinks.restatement
        assert restated, "no WithheldInputError was serialized; the drive is broken"
        assert "ValidationError" in restated, "the pydantic type was lost"
        assert "pydantic_core" in restated, "the defining module was lost"
        assert "JSONRPCMessage" in restated, "the model being validated was lost"
        assert "json_invalid" in restated, "the pydantic error TYPE was lost"

    def test_the_restatement_names_the_error_count(self, tmp_path):
        """The COUNT survives — and it is the library's, not a literal.

        ``_count`` is the one field of the four this class claims that nothing
        read until the F-913 review: blanking it left 38/38 green. The expected
        number is taken from pydantic itself for the same frame, so a version
        that reports a different number moves the pin with it instead of
        pinning a stale 9.
        """
        from mcp.types import JSONRPCMessage

        with pytest.raises(Exception) as caught:  # noqa: PT011  PERMANENT(F-913 - the exception TYPE is the subject)
            JSONRPCMessage.model_validate_json(LEAF_FRAME)
        expected = caught.value.error_count()
        assert expected > 1, "the union no longer reports per arm; re-measure"

        sinks = drive(tmp_path, data=LEAF_FRAME)
        restated = sinks.restatement
        assert restated, "no WithheldInputError was serialized; the drive is broken"
        assert f"{expected} error(s)" in restated, restated

    def test_the_restatement_overflows_its_cap_and_says_so(self, tmp_path):
        """``MAX_RESTATED_ERRORS`` bounds a real list, visibly.

        The nine errors of :data:`LEAF_FRAME` are NINE distinct ``type``/``loc``
        pairs — the dedup collapses none of them — so the restatement really
        does exceed the cap and ends with the overflow marker. Pinned because
        the constant's docstring claimed the opposite until the F-913 review,
        and a cap nobody has watched bind anything is a cap nobody can trust.
        """
        sinks = drive(tmp_path, data=LEAF_FRAME)
        restated = sinks.restatement
        assert restated, "no WithheldInputError was serialized; the drive is broken"
        assert payload_log_sites.RESTATED_OVERFLOW in restated, restated

    def test_the_record_still_arrives_at_its_own_level_and_logger(self, tmp_path):
        sinks = drive(tmp_path)
        mine = [r for r in sinks.capture.records if r.name == SDK_LOGGER]
        assert mine, "the SDK's record was dropped rather than restated"
        assert {r.levelno for r in mine} == {logging.ERROR}
        assert all("Error parsing SSE message" in r.getMessage() for r in mine), (
            "the static message is the safe half and must survive untouched"
        )

    def test_the_traceback_frames_are_unchanged(self, tmp_path):
        """The substitute carries the ORIGINAL traceback, so WHERE is intact.

        Asserted as equality against the unconfigured run rather than as a
        count, because a rule that quietly shortened the stack would still
        satisfy "there are some frames".
        """
        before = drive(tmp_path, configure=False).frames
        after = drive(tmp_path).frames
        assert before, "the unconfigured run reported no frames; drive is broken"
        assert before == after, f"before={before} after={after}"

    def test_the_restatement_says_how_much_it_withheld(self, tmp_path):
        """A count says how much was dropped without saying any of it —
        ``logging_setup._shape``'s ``children=`` rule, and F-911's ``measured``."""
        sinks = drive(tmp_path)
        values = sinks.exception_values
        assert values, "no serialized exception at all"
        assert "chars" in values[-1]["value"], values[-1]["value"]


# --------------------------------------------------------------------------
# The key is the structure, never the text
# --------------------------------------------------------------------------
class TestTheKeyIsTheStructureAndNotTheText:
    @staticmethod
    def _record_from(
        pathname: str,
        exc: BaseException,
        message: str = "boom",
        lineno: int = 240,
    ) -> logging.LogRecord:
        """One record built at ``pathname`` carrying ``exc``, through the factory."""
        return logging.getLogger(SDK_LOGGER).makeRecord(
            SDK_LOGGER,
            logging.ERROR,
            pathname,
            lineno,
            message,
            (),
            (type(exc), exc, exc.__traceback__),
        )

    @pytest.mark.parametrize(
        "lineno",
        [
            pytest.param(240, id="sse-leg"),
            pytest.param(394, id="json-response-leg"),
            pytest.param(574, id="post-writer-catch-all"),
        ],
    )
    def test_all_three_legs_are_covered_by_the_one_table_entry(self, lineno):
        """Finding §6.0 — the scope claim, driven rather than asserted.

        The three sites are the same shape on three legs: the SSE leg, the
        non-SSE response leg, and the catch-all around the whole writer. One
        entry covers all of them because the unit is the MODULE — a line-keyed
        rule would close one and go silently inert the next time an edit above
        `:240` moved the other two.

        A fix that closed one leg of three reads identically to this one from
        the CHANGELOG, so the difference is pinned here.
        """
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        import mcp.client.streamable_http as sdk

        record = self._record_from(
            sdk.__file__, self._validation_error(LEAF_MARK), lineno=lineno
        )
        assert LEAF_MARK not in str(record.exc_info[1]), lineno
        assert "ValidationError" in str(record.exc_info[1]), lineno

    @staticmethod
    def _validation_error(text: str = LEAF_MARK) -> BaseException:
        """A real ``ValidationError`` whose rendering quotes ``text`` WHOLE.

        The union shape, not the truncated one: a short leaf escapes pydantic's
        50-character cap, so the assertion "the marker is gone" is about the
        rule and not about a cap that would have cut it anyway.
        """
        from mcp.types import JSONRPCMessage

        try:
            JSONRPCMessage.model_validate_json(
                json.dumps(
                    {"jsonrpc": "2.0", "id": {"tok": text}, "result": {"c": "y" * 200}}
                )
            )
        except Exception as exc:  # noqa: BLE001  PERMANENT(F-913 - the exception TYPE is the subject)
            return exc
        raise AssertionError("the frame parsed after all")

    def test_a_wrapper_that_quotes_its_cause_is_restated_too(self):
        """The chain is WALKED, and reading only ``exc_info[1]`` is not enough.

        A wrapper commonly interpolates what it wrapped (``f"...: {e}"``), so
        the payload rides out in the WRAPPER's own text while ``exc_info[1]``
        is an ordinary ``RuntimeError`` that quotes nobody. A rule reading the
        head alone answers "nothing to do" and every formatting sink then
        renders the cause — which is exactly what ``restated_exc_info``'s
        docstring promises it does not do.

        Measured before this pin existed: reducing the walk to the head left
        all 38 nodes in this file GREEN, so the promise was unguarded.
        """
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        import mcp.client.streamable_http as sdk

        quoting = self._validation_error(LEAF_MARK)
        wrapped = f"re-raised while parsing: {quoting}"
        try:
            raise RuntimeError(wrapped) from quoting  # noqa: TRY301  PERMANENT(F-913 - a real __cause__ is only set by a real raise-from)
        except RuntimeError as wrapper:
            caught = wrapper
        assert LEAF_MARK in str(caught), "the wrapper does not carry the payload"

        record = self._record_from(sdk.__file__, caught)
        restated = str(record.exc_info[1])
        assert LEAF_MARK not in restated, restated
        assert "RuntimeError" in restated, "the wrapper's own TYPE was lost"
        assert "ValidationError" in restated, "the quoting cause was not restated"

    def test_a_reworded_sdk_message_is_still_covered(self):
        """Keyed on the SITE and the EXCEPTION's structure, never on wording —
        F-906's rule about pattern-matching a library's strings."""
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        import mcp.client.streamable_http as sdk

        record = self._record_from(
            sdk.__file__,
            self._validation_error(LEAF_MARK),
            "a wording nobody has written yet",
        )
        assert LEAF_MARK not in str(record.exc_info[1])

    def test_an_ordinary_exception_from_the_same_site_keeps_its_text(self):
        """The structural half is what keeps this a redaction and not a blanket.

        A ``ConnectionError`` out of ``:574`` quotes nobody's input, and its
        text is the diagnostic — F-907's exception clause, still standing for
        every exception that does not quote its own input.
        """
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        import mcp.client.streamable_http as sdk

        ordinary = ConnectionError("connection refused by 127.0.0.1:52554")
        record = self._record_from(sdk.__file__, ordinary)
        assert record.exc_info[1] is ordinary
        assert "connection refused" in str(record.exc_info[1])

    @pytest.mark.parametrize(
        "pathname",
        [
            pytest.param("/opt/other/client/streamable_http.py", id="another-package"),
            pytest.param("/opt/fakemcp/client/streamable_http.py", id="suffix-of-name"),
        ],
    )
    def test_a_quoting_exception_from_another_site_is_untouched(self, pathname):
        """The site gate, and the separator BOUNDARY inside it (F-911 review S1).

        Two reasons this half matters. A stranger's module that merely SHARES
        the filename must keep its diagnostics; and the gate is what keeps this
        rule away from ``FastMCP``'s own argument validation, where substituting
        the exception would break ``expected_events``' ``caller-input`` — see
        :class:`TestExpectedEventsIsUnaffected`.
        """
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        quoting = self._validation_error(LEAF_MARK)
        record = self._record_from(pathname, quoting)
        assert record.exc_info[1] is quoting
        assert LEAF_MARK in str(record.exc_info[1])

    def test_both_host_pathname_flavours_match(self):
        """The table is written one way; ``pathname`` arrives in the host's.

        Driven under BOTH shapes on every host — F-911's gate run 35624857318
        went red on every POSIX cell while the Windows lane was green.
        """
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        for pathname in (
            f"/usr/lib/python3.13/site-packages/{SDK_MODULE}",
            r"C:\venv\Lib\site-packages\mcp\client\streamable_http.py",
        ):
            record = self._record_from(pathname, self._validation_error(LEAF_MARK))
            assert LEAF_MARK not in str(record.exc_info[1]), pathname

    def test_adding_the_file_to_f911s_table_is_not_the_fix(self):
        """§6: that rule would delete the safe half and leave the payload.

        Pinned so the next reader cannot 'simplify' this into one table. The two
        tables answer different halves of one record and are disjoint.
        """
        assert SDK_MODULE not in payload_log_sites.PAYLOAD_LOG_SITES
        assert SDK_MODULE in payload_log_sites.PAYLOAD_EXCEPTION_SITES
        assert not set(payload_log_sites.PAYLOAD_LOG_SITES) & set(
            payload_log_sites.PAYLOAD_EXCEPTION_SITES
        )


# --------------------------------------------------------------------------
# What this must not break
# --------------------------------------------------------------------------
class TestExpectedEventsIsUnaffected:
    """The regression this fix could most easily have caused, pinned.

    ``expected_events.CALLER_VALIDATION`` matches a pydantic ``ValidationError``
    by type NAME and MODULE. A rule that substituted the exception on every
    record would change both for FastMCP's own argument validation and re-open
    the 466-a-week ``caller-input`` class. The site gate is what prevents it,
    and "measured, not argued" is the standard the rest of this family is held
    to.
    """

    @staticmethod
    def _caller_input_record() -> logging.LogRecord:
        import pydantic

        class Args(pydantic.BaseModel):
            viewport_width: int

        try:
            Args(viewport_width="F913_CALLER_ARGUMENT")
        except pydantic.ValidationError as exc:
            caught = exc
        return logging.getLogger(expected_events.TOOL_MANAGER_LOGGER).makeRecord(
            expected_events.TOOL_MANAGER_LOGGER,
            logging.ERROR,
            "/venv/lib/site-packages/fastmcp/tools/tool_manager.py",
            220,
            "Error calling tool 'spawn_browser'",
            (),
            (type(caught), caught, caught.__traceback__),
        )

    def test_fastmcps_validation_error_is_not_substituted(self):
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        record = self._caller_input_record()
        kept = record.exc_info[1]
        assert type(kept).__qualname__ == "ValidationError"
        assert type(kept).__module__.startswith("pydantic")

    def test_caller_input_still_classifies(self):
        """The rule ``expected_events`` actually applies, end to end."""
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("backend")
        record = self._caller_input_record()
        exc = record.exc_info[1]
        link = expected_events.Link(
            type_name=type(exc).__qualname__,
            module=type(exc).__module__,
            frames=(),
            live=exc,
        )
        assert expected_events.CALLER_VALIDATION.matches(link), (
            "the substitution reached FastMCP's validation error; caller-input "
            "would stop classifying and 466 events a week would ship again"
        )

    def test_the_two_spellings_of_a_pydantic_validation_error_agree(self):
        """Two homes name the same class; neither may drift from the other.

        ``payload_log_sites`` stays a stdlib-only leaf (it is why the stdio
        proxy pays nothing for it), so it spells this itself rather than
        importing ``expected_events``. The spellings are pinned against each
        other instead — two spellings of one fact is how a rule comes to cover
        nothing (``_SITE_FILENAMES``' reasoning).
        """
        for name in payload_log_sites.INPUT_QUOTING_NAMES:
            for root in payload_log_sites.INPUT_QUOTING_MODULE_ROOTS:
                assert expected_events.CALLER_VALIDATION.matches(
                    expected_events.Link(type_name=name, module=root)
                ), (name, root)


class TestTheSdkStillOwnsItsOwnException:
    """The live object is never touched, and that is load-bearing.

    ``streamable_http.py``:241 sends the very exception it just logged
    downstream to ``BaseSession._receive_loop``. A fix that mutated it — or that
    reached for pydantic's ``hide_input_in_errors`` on the SDK's model — would
    change the SDK's own control flow. This one replaces only what the RECORD
    carries.
    """

    def test_the_original_exception_is_not_mutated(self):
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        original = TestTheKeyIsTheStructureAndNotTheText._validation_error(LEAF_MARK)
        rendered_before = str(original)
        logging.getLogger(SDK_LOGGER).makeRecord(
            SDK_LOGGER,
            logging.ERROR,
            "/x/site-packages/" + SDK_MODULE,
            240,
            "Error parsing SSE message",
            (),
            (type(original), original, original.__traceback__),
        )
        assert str(original) == rendered_before
        assert LEAF_MARK in str(original), (
            "the SDK's own exception was altered; :241 would send a different "
            "object downstream than the SDK raised"
        )

    def test_the_substitute_does_not_chain_back_to_the_original(self):
        """``cdp_transport``'s trap exactly: an exception built while another is
        being handled must not carry it as ``__context__``, or every formatting
        sink renders the text we just withheld."""
        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        import mcp.client.streamable_http as sdk

        original = TestTheKeyIsTheStructureAndNotTheText._validation_error(LEAF_MARK)
        try:
            raise original  # noqa: TRY301  PERMANENT(F-913 - raising IS the condition under test)
        except Exception:  # noqa: BLE001  PERMANENT(F-913 - handling one IS the condition under test)
            record = logging.getLogger(SDK_LOGGER).makeRecord(
                SDK_LOGGER,
                logging.ERROR,
                sdk.__file__,
                240,
                "Error parsing SSE message",
                (),
                (type(original), original, original.__traceback__),
            )
        substitute = record.exc_info[1]
        assert substitute is not original
        assert substitute.__context__ is None, substitute.__context__
        assert substitute.__cause__ is None, substitute.__cause__


class TestTheRuleIsDerivedAndIdempotent:
    def test_the_cheap_gate_is_derived_from_the_site_table(self):
        expected = frozenset(
            site.rpartition("/")[2]
            for site in payload_log_sites.PAYLOAD_EXCEPTION_SITES
        )
        assert expected == payload_log_sites._EXCEPTION_SITE_FILENAMES
        assert all(name.endswith(".py") for name in expected)

    def test_installing_twice_chains_one_factory(self):
        """``server.py`` is executed three times under runpy."""
        logging_setup.install_payload_arg_redaction()
        first = logging.getLogRecordFactory()
        logging_setup.install_payload_arg_redaction()
        assert logging.getLogRecordFactory() is first

    def test_f911s_site_rule_still_fires(self):
        """Three rules, ONE factory — the composition, not three installs."""
        import mcp.shared.session as sdk

        with contextlib.redirect_stderr(io.StringIO()):
            logging_setup.configure_logging("proxy")
        capture = _RootCapture()
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(capture)
        record = root.makeRecord(
            "root",
            logging.WARNING,
            sdk.__file__,
            430,
            f"Failed to validate notification: {LEAF_MARK}",
            (),
            None,
        )
        root.handle(record)
        assert LEAF_MARK not in capture.text
        assert "mcp/shared/session.py:430" in capture.text

    def test_the_restatement_is_bounded(self):
        """A union can report dozens of errors; the restatement names a cap.

        ``ClientRequest`` reported 31 in F-911's measurement. An unbounded join
        would put a page-sized string where a diagnostic belongs.
        """
        assert payload_log_sites.MAX_RESTATED_ERRORS > 0
        assert payload_log_sites.MAX_RESTATED_ERRORS <= 32
