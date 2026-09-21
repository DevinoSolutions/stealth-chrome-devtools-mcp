"""F-907: nodriver renders a page element into its own WARNING text.

``nodriver/core/element.py``:537/:624/:633 log
``"could not calculate box model for %s"`` with a live :class:`Element` as the
argument, at **WARNING** — above F-906's floor, so the floor does not touch it.
``Element.__repr__`` renders the element's tag, **every attribute as
``name="value"``, and the element's whole recursive TEXT CONTENT** (measured on
the installed nodriver 0.47.0 — the text half is not in F-906's residual note,
which said "tag and attributes"). A password field's ``value=``, a
``data-*`` bearing a session token and a balance in a ``<div>`` all ride in it,
and `dom_handler.click_element` reaches the first of those three lines for any
element with no box model — which is exactly the ``display: none`` case its own
synthetic fallback exists for.

These pins drive the REAL collaborators for F-906's reason — the stdlib's own
record machinery, a real ``LoggingIntegration`` and a real
``RotatingFileHandler`` on a tmp log dir — and they build a REAL
``nodriver.core.element.Element`` from nodriver's own constructors
(``cdp.dom.Node.from_json`` on a captured-shape dict, then ``Element(node,
tab)``). A hand-written double would render whatever ``__repr__`` we gave it and
so could only ever measure the test against itself.

The mechanism pins are here because two measured facts forced the design and a
future refactor must fail rather than quietly re-open the leak:

* a ``logging.Filter`` on the family ROOT ``nodriver`` **never fires** for a
  record from ``nodriver.core.element`` — ``Logger.handle`` consults only the
  filters of the logger the call was made on, and ``callHandlers`` walks
  ancestors for HANDLERS, never for filters;
* a filter on a HANDLER is no use either, because in the configuration this
  finding is about we do not OWN the handler — production root carries none
  (``logging.lastResort``) and under a caller's ``basicConfig`` it is theirs.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import logging.config
from pathlib import Path

import pytest
import sentry_sdk
from nodriver import cdp
from nodriver.core.element import Element
from sentry_sdk.integrations.logging import LoggingIntegration

from stealth_chrome_devtools_mcp.embedded import logging_setup
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings

ELEMENT_LOGGER = "nodriver.core.element"
CONN_LOGGER = "nodriver.core.connection"
WEBSOCKETS_LOGGER = "websockets.client"

#: The three markers a redacted render must never carry. Distinct strings, so a
#: failure names WHICH half of ``__repr__`` escaped.
ATTR_VALUE_MARK = "F907_ATTR_VALUE"
TOKEN_VALUE_MARK = "F907_TOKEN_VALUE"
TEXT_CONTENT_MARK = "F907_TEXT_CONTENT"
TAB_URL_MARK = "F907_TAB_URL"

SECRETS = {
    "attribute value": ATTR_VALUE_MARK,
    "data-* token value": TOKEN_VALUE_MARK,
    "element text content": TEXT_CONTENT_MARK,
    "tab url": TAB_URL_MARK,
}

#: What a redacted render must still SAY, so the fix is not "silence it".
KEPT_SHAPE = ("input", "type", "value", "data-session-token")


# --------------------------------------------------------------------------
# Real nodriver objects, from nodriver's own constructors
# --------------------------------------------------------------------------
def make_element() -> Element:
    """An ``<input type=password>`` carrying a value and a token, plus text.

    The dict is the shape ``DOM.describeNode`` answers with; ``from_json`` is
    nodriver's own parser for it, so every field this element exposes is one
    nodriver itself would have built.
    """
    node = cdp.dom.Node.from_json(
        {
            "nodeId": 42,
            "backendNodeId": 4242,
            "nodeType": 1,
            "nodeName": "INPUT",
            "localName": "input",
            "nodeValue": "",
            "childNodeCount": 1,
            "attributes": [
                "type",
                "password",
                "value",
                ATTR_VALUE_MARK,
                "data-session-token",
                TOKEN_VALUE_MARK,
            ],
            "children": [
                {
                    "nodeId": 43,
                    "backendNodeId": 4343,
                    "nodeType": 3,
                    "nodeName": "#text",
                    "localName": "",
                    "nodeValue": TEXT_CONTENT_MARK,
                    "childNodeCount": 0,
                }
            ],
        }
    )
    return Element(node, tab=None)


class _TabLike:
    """Stands in for ``nodriver.core.tab.Tab`` for the one property that
    matters here: its ``__repr__`` renders ``self.target.url``, and a tab's URL
    carries whatever the page put in its query string.

    It is declared with ``Tab``'s own module name because the rule is keyed on
    the TYPE's package and nothing else — which is the point being pinned.
    """

    __module__ = "nodriver.core.tab"

    def __repr__(self) -> str:
        return f"<Tab [T1] [page] [url: https://example.test/?sso={TAB_URL_MARK}]>"


class _Hostile:
    """A nodriver-typed object whose shape read RAISES.

    The redactor runs inside ``Logger.makeRecord`` — it cannot log about its own
    failure without recursion, and an exception escaping it breaks every log
    call in the process. So it must degrade to the type name.
    """

    __module__ = "nodriver.core.element"

    @property
    def tag(self):
        raise RuntimeError("shape read exploded")

    def __repr__(self) -> str:
        return f"<hostile {ATTR_VALUE_MARK}>"


# --------------------------------------------------------------------------
# Harness (F-906's, extended to reset the record factory)
# --------------------------------------------------------------------------
class _RefusingTransport(sentry_sdk.transport.Transport):
    def capture_envelope(self, envelope) -> None:
        raise AssertionError("F-907 pin tried to ship a real Sentry envelope")


class _RootCapture(logging.Handler):
    """Whatever this process has on root — see F-906's note on sink (d)."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.text += f"{record.name} {record.levelname} {record.getMessage()}\n"


class Sinks:
    def __init__(self, durable: str, ring: str, sentry: str, downstream: str) -> None:
        self.durable = durable
        self.ring = ring
        self.sentry = sentry
        self.downstream = downstream

    @property
    def everything(self) -> str:
        return self.durable + self.ring + self.sentry + self.downstream

    def leaked(self) -> set[str]:
        return {name for name, mark in SECRETS.items() if mark in self.everything}


_PRISTINE_FACTORY = logging.getLogRecordFactory()


def reset_logging() -> None:
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        logger.filters = []
        logger.disabled = False
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    root.setLevel(logging.WARNING)
    logging.setLogRecordFactory(_PRISTINE_FACTORY)


@pytest.fixture(autouse=True)
def _isolated_logging():
    reset_logging()
    get_settings.cache_clear()
    yield
    reset_logging()
    get_settings.cache_clear()


def emit_element_warnings() -> None:
    """``element.py``:537/:624/:633, copied rather than paraphrased."""
    logger = logging.getLogger(ELEMENT_LOGGER)
    logger.warning("could not calculate box model for %s", make_element())
    logger.warning("could not calculate box model for %s", _TabLike())


def drive(
    log_dir: Path,
    *,
    role: str = "backend",
    basicconfig: str | None = None,
    debug_ring: bool = False,
) -> Sinks:
    """One shipped configuration; what each sink received.

    ``stderr`` is redirected for the WHOLE call — ``logging.basicConfig`` binds
    ``sys.stderr`` into its ``StreamHandler`` at CREATION time, so a redirect
    entered afterwards reads as a false negative (F-906's measurement trap).
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

        emit_element_warnings()
        # Force an event, so breadcrumbs accrued above are serialized with it.
        logging.getLogger("stealth.probe").error("F907 probe")

    for handler in logging.getLogger(f"stealth.{role}").handlers:
        with contextlib.suppress(Exception):
            handler.flush()

    return Sinks(
        log_path.read_text(encoding="utf-8") if log_path.exists() else "",
        json.dumps(debug_logger.get_debug_view(), default=str),
        json.dumps(events, default=str),
        downstream.text + err.getvalue(),
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
# The premise: the leak is still live in the INSTALLED nodriver
# --------------------------------------------------------------------------
class TestReachability:
    """Whether the three box-model WARNINGs can fire AT ALL in nodriver 0.47.

    The F-906 review reported them as a hot path — ``click_element`` →
    ``Element.mouse_click`` → the warning "on exactly the zero-size /
    not-rendered case". MEASURED, that is not so, and the correction matters
    because it is the difference between an every-user leak and insurance:

    * ``Position.center`` is ``(left + width/2, top + height/2)`` — a non-empty
      2-tuple, so **always truthy**, even for a zero-size quad at the origin.
      ``if not center:`` therefore cannot be reached through a real
      ``Position``;
    * and whichever branch ``get_position`` takes, the warning is not reached
      anyway: ``if not quads: raise Exception(...)`` propagates straight past
      ``mouse_click``'s ``except AttributeError``, and the ``except IndexError``
      branch returns ``None``, which that same handler swallows with a bare
      ``return`` BEFORE the warning line.

    So the redaction is insurance against one nodriver change, not a patch for
    a live every-user leak — and these pins go RED the day that change lands,
    which is when the finding's severity really does rise.
    """

    @pytest.mark.parametrize(
        ("label", "quad"),
        [
            ("zero-size at origin", [0, 0, 0, 0, 0, 0, 0, 0]),
            ("zero-size offscreen", [10, 20, 10, 20, 10, 20, 10, 20]),
            ("ordinary box", [0, 0, 100, 0, 100, 50, 0, 50]),
            ("negative offscreen", [-500, -500, -400, -500, -400, -450, -500, -450]),
        ],
    )
    def test_position_center_is_always_truthy(self, label, quad):
        """The guard the three WARNINGs sit behind, and why it never opens."""
        from nodriver.core.element import Position

        assert Position(quad).center, (
            f"{label}: a falsy center makes element.py:537/:624 REACHABLE — "
            "F-907's severity rises and the finding's §1 must be rewritten"
        )

    def test_mouse_click_returns_before_the_warning_on_a_none_position(self):
        """``except AttributeError: return`` sits between ``get_position``
        answering ``None`` and the warning line."""
        import inspect

        import nodriver.core.element as nd_element

        body = inspect.getsource(nd_element.Element.mouse_click)
        guard = body.index("except AttributeError")
        warning = body.index("could not calculate box model")
        assert guard < warning, (
            "the AttributeError guard no longer precedes the warning; "
            "element.py:537 may now be reachable"
        )


class TestPremise:
    def test_element_repr_still_renders_values_and_text(self):
        """If this goes green on its own, nodriver fixed it and the redaction
        can be deleted rather than maintained."""
        # `str()` and not an f-string: this is exactly the conversion `%s`
        # applies, which is the one the three log sites use.
        rendered = str(make_element())
        assert ATTR_VALUE_MARK in rendered
        assert TOKEN_VALUE_MARK in rendered
        assert TEXT_CONTENT_MARK in rendered, (
            "__repr__ used to render child TEXT as well as attributes; "
            f"got {rendered!r}"
        )

    def test_the_three_sites_are_still_warning_with_a_live_arg(self):
        """The finding is "above F-906's floor, rendered lazily". Both halves
        are premises: WARNING passes the floor, and a lazy ``%s`` is what keeps
        the object in ``record.args`` where a record factory can reach it. A
        nodriver bump that pre-interpolates would put this out of reach exactly
        as ``connection.py``:451 is."""
        import nodriver.core.element as nd_element

        source = Path(nd_element.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "warning"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and "box model" in str(node.args[0].value)
        ]
        assert len(sites) == 3, (
            f"expected element.py's 3 box-model WARNINGs, got {len(sites)}"
        )
        for site in sites:
            assert len(site.args) == 2, (
                "the payload must still be a lazy %s ARGUMENT, not pre-interpolated"
            )


# --------------------------------------------------------------------------
# The finding
# --------------------------------------------------------------------------
class TestNoPageContentReachesAnySink:
    @pytest.mark.parametrize("config", SHIPPED + CALLER_DEBUG)
    def test_no_page_content_reaches_any_sink(self, config, tmp_path, monkeypatch):
        """The whole finding in one assertion, over all four sinks at once."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        assert sinks.leaked() == set(), (
            f"page content reached a sink: {sorted(sinks.leaked())}\n"
            f"durable={sinks.durable!r}\ndownstream={sinks.downstream!r}\n"
            f"sentry={sinks.sentry!r}"
        )

    @pytest.mark.parametrize("config", CALLER_DEBUG)
    def test_caller_root_debug_reaches_neither_stderr_nor_sentry(
        self, config, tmp_path, monkeypatch
    ):
        """The two cells that were RED, named individually.

        MEASURED before the fix: a password field's ``value=``, a session token
        and the element's text all reached a root handler (hence stderr, hence
        ``backend-boot.log``) AND Sentry as a breadcrumb — at WARNING, in every
        configuration including the shipped ones, because WARNING is above
        F-906's floor and the floor is all that stood there.
        """
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        for name, mark in SECRETS.items():
            assert mark not in sinks.downstream, f"{name} reached stderr/root"
            assert mark not in sinks.sentry, f"{name} reached Sentry"

    @pytest.mark.parametrize("config", SHIPPED)
    def test_the_line_still_says_which_element(self, config, tmp_path, monkeypatch):
        """Redacting is not silencing. The tag and the attribute NAMES stay, so
        an operator still learns which control had no box model."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        sinks = drive(tmp_path, **config)
        reached = sinks.downstream
        assert "could not calculate box model for" in reached
        for kept in KEPT_SHAPE:
            assert kept in reached, (
                f"{kept!r} should survive redaction; got {reached!r}"
            )


class TestLastResortSink:
    """The sink the production backend actually uses, and the one the
    all-sinks pins above could not see.

    ``drive`` adds a ``_RootCapture`` to stand in for "whatever this process
    has on root" — but installing ANY handler SUPPRESSES ``logging.lastResort``,
    so that harness measures the caller-has-a-handler world and never the
    shipped one. In production root carries **no** handler at all, so
    ``callHandlers`` falls through to ``lastResort``: a ``_StderrHandler`` at
    WARNING. And the backend's stderr is redirected into ``backend-boot.log``,
    a durable file — which is what makes this the cell that matters.

    The measurement is only possible because ``lastResort``'s stream is bound
    at EMIT time (``_StderrHandler.stream`` is a property reading
    ``sys.stderr``), the exact opposite of ``logging.basicConfig``, which binds
    ``sys.stderr`` into its handler at CREATION — F-906's trap, and the reason
    a ``redirect_stderr`` works here and reads as a false negative there. Both
    halves are asserted, so a stdlib change cannot turn this pin green by
    making it measure nothing.
    """

    @staticmethod
    def _emit_with_no_handlers() -> str:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        assert root.handlers == [], (
            "this pin measures the NO-handler world; a handler here suppresses "
            "lastResort and the assertion below would prove nothing"
        )
        assert logging.lastResort is not None
        assert logging.lastResort.level == logging.WARNING
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            emit_element_warnings()
        return err.getvalue()

    def test_without_configure_logging_the_page_content_does_reach_stderr(self):
        """The other half of the pair, and the one that keeps the next one
        honest: with ``configure_logging`` NOT called — so no redaction — the
        record reaches stderr through ``lastResort`` carrying the page's own
        content. If this ever goes green on its own, either nodriver stopped
        rendering values or the stdlib stopped binding ``lastResort``'s stream
        at emit time, and the pin below would be measuring nothing."""
        text = self._emit_with_no_handlers()
        assert "could not calculate box model for" in text, (
            "lastResort did not carry the record; this pin now measures nothing"
        )
        assert ATTR_VALUE_MARK in text, "the unredacted leak is no longer visible"
        assert TEXT_CONTENT_MARK in text

    def test_no_page_content_reaches_lastresort(self, tmp_path, monkeypatch):
        """The shipped backend's real stderr path, hence ``backend-boot.log``."""
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        get_settings.cache_clear()
        logging_setup.configure_logging("backend")
        text = self._emit_with_no_handlers()
        for name, mark in SECRETS.items():
            assert mark not in text, f"{name} reached stderr through lastResort"
        assert "input" in text and "type" in text


# --------------------------------------------------------------------------
# What must NOT change
# --------------------------------------------------------------------------
class TestUntouched:
    @staticmethod
    def _capture() -> _RootCapture:
        sink = _RootCapture()
        logging.getLogger().addHandler(sink)
        logging.getLogger().setLevel(logging.DEBUG)
        return sink

    def test_nodrivers_real_warning_passes_unchanged(self):
        """``connection.py``:483 names the callback, the event CLASS and the
        exception — MEASURED: all three args are builtins, so the rule that
        redacts the leak costs nodriver's real diagnostic nothing."""
        logging_setup.install_payload_arg_redaction()
        sink = self._capture()
        logging.getLogger(CONN_LOGGER).warning(
            "exception in callback %s for event %s => %s",
            "a_callback",
            "TargetInfoChanged",
            ValueError("boom"),
        )
        assert "a_callback" in sink.text
        assert "TargetInfoChanged" in sink.text
        assert "boom" in sink.text

    def test_websockets_warnings_pass_unchanged(self):
        """MEASURED across websockets 16.0: every WARNING+ site there is a
        static message or a ``str``. Nothing to redact, so nothing is — and the
        rule is keyed on the nodriver package alone rather than claiming a
        second family the evidence does not support (F-906's "no uc" reasoning).
        """
        logging_setup.install_payload_arg_redaction()
        sink = self._capture()
        logging.getLogger(WEBSOCKETS_LOGGER).warning(
            "skipped broadcast: failed to write message: %s", "ConnectionResetError"
        )
        assert "ConnectionResetError" in sink.text

    def test_our_own_records_pass_unchanged(self):
        """The rule is keyed on the record's LOGGER package, so a
        ``stealth.*`` record carrying a nodriver object is untouched — our own
        sites own their own PII discipline (F-869/F-873/F-876) and a blanket
        rewrite here would be a second, invisible answer to it."""
        logging_setup.install_payload_arg_redaction()
        sink = self._capture()
        logging.getLogger("stealth.backend").warning("ours: %s", make_element())
        assert ATTR_VALUE_MARK in sink.text


# --------------------------------------------------------------------------
# The mechanism — every one of these forced the design
# --------------------------------------------------------------------------
class TestMechanism:
    def test_a_filter_on_the_family_root_never_fires(self):
        """The measurement that killed the obvious design.

        ``Logger.handle`` consults ``self.filter(record)`` for the logger the
        call was made ON; ``callHandlers`` then walks ancestors for HANDLERS and
        never for their filters. So a ``logging.Filter`` added to ``nodriver``
        sees nothing a ``nodriver.core.element`` record does — and a fix written
        that way would pass a test that emitted on the family root and leak
        every real line.
        """
        seen: list[str] = []

        class Spy(logging.Filter):
            def filter(self, record):
                seen.append(record.name)
                return True

        logging.getLogger("nodriver").addFilter(Spy())
        logging.getLogger("nodriver").setLevel(logging.WARNING)
        logging.getLogger(ELEMENT_LOGGER).warning("box model for %s", "x")
        assert seen == [], (
            "a family-root filter fired; if this is now true the design note in "
            "logging_setup is stale, but do NOT move the redaction to a filter "
            "without re-measuring the Sentry sink too"
        )

    def test_redaction_reaches_a_logger_created_after_install(self):
        """The future-nodriver-module case, and the reason this is a record
        FACTORY and not an enumeration of today's loggers: at
        ``configure_logging`` time not one ``nodriver.*`` logger exists yet —
        the proxy never imports nodriver and the backend imports it later."""
        logging_setup.install_payload_arg_redaction()
        sink = self._capture()
        logging.getLogger("nodriver.core.brandnew").warning("%s", make_element())
        assert ATTR_VALUE_MARK not in sink.text
        assert "input" in sink.text

    @staticmethod
    def _capture() -> _RootCapture:
        sink = _RootCapture()
        logging.getLogger().addHandler(sink)
        logging.getLogger().setLevel(logging.DEBUG)
        return sink

    def test_survives_dictconfig_disable_existing_loggers(self):
        """A record factory is not a logger, so ``disable_existing_loggers``
        cannot reach it. MEASURED: it also silences every nodriver logger that
        exists at that moment — which is why the pin drives a logger created
        AFTER the dictConfig, the one case still able to leak."""
        logging_setup.install_payload_arg_redaction()
        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": True,
                "handlers": {},
                "root": {"level": "DEBUG"},
            }
        )
        sink = self._capture()
        logging.getLogger("nodriver.core.after_dictconfig").warning(
            "%s", make_element()
        )
        assert ATTR_VALUE_MARK not in sink.text

    def test_install_is_idempotent(self):
        """``configure_logging`` runs this ahead of its own idempotency guard
        (F-906's placement, for F-906's reason), so it is called again on every
        re-configure — and ``embedded/server.py`` is executed three times under
        runpy."""
        logging_setup.install_payload_arg_redaction()
        first = logging.getLogRecordFactory()
        logging_setup.install_payload_arg_redaction()
        logging_setup.install_payload_arg_redaction()
        assert logging.getLogRecordFactory() is first

    def test_chains_to_a_factory_already_installed(self):
        """A caller's factory is not replaced, it is wrapped — so whatever they
        stamp on every record still arrives."""
        previous = logging.getLogRecordFactory()

        def caller_factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            record.caller_stamp = "kept"
            return record

        logging.setLogRecordFactory(caller_factory)
        logging_setup.install_payload_arg_redaction()

        stamped: list[object] = []

        class Peek(logging.Handler):
            def emit(self, record):
                stamped.append(getattr(record, "caller_stamp", None))

        logging.getLogger().addHandler(Peek())
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("nodriver.core.element").warning("%s", make_element())
        assert stamped == ["kept"]

    def test_a_shape_read_that_raises_degrades_to_the_type_name(self):
        """It runs inside record creation, so anything escaping breaks every
        log call in the process — including the one that would report it.

        The tolerance must therefore be TOTAL: written first as a narrow tuple,
        a ``RuntimeError`` raised by a property walked straight through it and
        took the whole ``logger.warning`` call with it. And it is not a
        swallow: the line names the exception's TYPE, which is the only channel
        left when logging about it would recurse. Never ``str(exc)`` — on a
        page-derived object that string is page-authored, which is the subject
        of this finding.
        """
        logging_setup.install_payload_arg_redaction()
        sink = self._capture()
        logging.getLogger(ELEMENT_LOGGER).warning("%s", _Hostile())
        assert ATTR_VALUE_MARK not in sink.text
        assert "_Hostile" in sink.text
        assert "RuntimeError" in sink.text, (
            f"the failed shape read must name its cause; got {sink.text!r}"
        )
        assert "exploded" not in sink.text, "str(exc) must never be rendered"

    def test_the_tab_url_is_redacted_to_the_type(self):
        """``Tab.__repr__`` renders ``target.url`` and a URL carries tokens in
        its query string. There is no tag and no attribute list to keep, so the
        shape is the type — which is all that line could honestly say."""
        logging_setup.install_payload_arg_redaction()
        sink = self._capture()
        logging.getLogger(ELEMENT_LOGGER).warning("%s", _TabLike())
        assert TAB_URL_MARK not in sink.text
        assert "_TabLike" in sink.text


class TestBounds:
    @staticmethod
    def _render(value) -> str:
        sink = _RootCapture()
        logging.getLogger().addHandler(sink)
        logging.getLogger().setLevel(logging.DEBUG)
        logging_setup.install_payload_arg_redaction()
        logging.getLogger(ELEMENT_LOGGER).warning("%s", value)
        return sink.text

    @staticmethod
    def _element_with(attributes: list[str], node_name: str = "DIV") -> Element:
        return Element(
            cdp.dom.Node.from_json(
                {
                    "nodeId": 1,
                    "backendNodeId": 1,
                    "nodeType": 1,
                    "nodeName": node_name,
                    "localName": node_name.lower(),
                    "nodeValue": "",
                    "childNodeCount": 0,
                    "attributes": attributes,
                }
            ),
            tab=None,
        )

    def test_attribute_names_are_capped(self):
        """Attribute names are page-authored, so their COUNT is the page's."""
        many: list[str] = []
        for index in range(60):
            many += [f"data-attr-{index}", "v"]
        text = self._render(self._element_with(many))
        rendered = text.split("data-attr-", 1)[1] if "data-attr-" in text else ""
        assert text.count("data-attr-") <= logging_setup.SHAPE_MAX_ATTRS, (
            f"rendered {text.count('data-attr-')} names; "
            f"cap is {logging_setup.SHAPE_MAX_ATTRS} ({rendered[:80]!r})"
        )

    def test_a_long_attribute_name_is_clamped(self):
        """And their LENGTH is the page's too."""
        long_name = "data-" + ("x" * 500)
        text = self._render(self._element_with([long_name, "v"]))
        assert long_name not in text
        assert "data-xxx" in text

    def test_a_long_tag_name_is_clamped(self):
        """A custom element's tag name is page-authored on the same terms."""
        long_tag = "my-" + ("y" * 500)
        text = self._render(self._element_with(["id", "v"], node_name=long_tag))
        assert long_tag.lower() not in text
        assert "my-yyy" in text

    def test_redacted_args_still_format(self):
        """The rewrite must not break ``record.getMessage()``. Every replaced
        arg is a ``str``, so a ``%s`` conversion is all that may be applied to
        one — MEASURED: no nodriver line applies a numeric conversion to a
        nodriver-typed arg (``connection.py``:445's ``%d`` takes ``tx.id``)."""
        text = self._render(make_element())
        assert "Logging error" not in text
        assert "could not calculate" not in text  # this pin emits a bare "%s"
        assert "input" in text
