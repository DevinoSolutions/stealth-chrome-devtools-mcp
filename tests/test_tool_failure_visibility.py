"""F-835: a tool call that FAILS is visible in the product's own debug surface.

Live evidence 2026-08-30: 24 consecutive ``spawn_browser`` calls failed — a
total spawn outage — and ``get_debug_view`` still reported ``total_errors: 0``.
The failure path emitted INFO only; the ``ToolError`` raised to the client never
reached the in-memory ring, so the one surface an operator watches said the
system was healthy while nothing could spawn.

The fix is at the ONE wrapper every registered tool passes through
(``logging_setup.with_correlation_id``, the ``section_tool`` chokepoint), not at
the spawn site — so the property under test here is general: **any** tool's
failure lands. The spawn case is the reported one, and it leads.

Five things this file refuses to let regress, in the order they matter:

1. the failure is IN the ring, named by tool and message (and reachable through
   the real ``get_debug_view`` tool, not just the logger object);
2. the exception the client receives is byte-identical to the one it received
   before the recording existed — recording observes, it never transforms, and
   it stays in the ring rather than in the durable, Sentry-bridged backend log
   (F-782's redaction condition — see the scope test below);
3. a SUCCEEDING call records nothing (the ring stays a signal, not a log);
4. the recording can never break a tool call — a debug ring that throws is a
   debug-ring problem, and the tool's own error is what reaches the client;
5. a pydantic ``ValidationError`` our BODY raised still reaches fastmcp's tool
   logger, and so Sentry, exactly once — fastmcp 2.14 stopped logging it
   (F-943) — while a caller's bad argument still reaches nothing.

Hermetic: no Chrome, no disk profile, no network. Section 5 alone goes through
fastmcp's in-memory ``Client``, because its subject is what the library logs
around our wrapper. The ring is a process-wide singleton, so every test runs
against an emptied one.
"""

from __future__ import annotations

import logging

import pydantic
import pytest

from fakes import FakeBrowserManager, FakeTab, call_tool
from stealth_chrome_devtools_mcp import expected_events
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    desktop_launch,
    tool_errors,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.logging_setup import with_correlation_id

SPAWN_FAILURE = "Chrome failed to start: exit code 21"
SPAWN_TOOL_ERROR = f"Failed to spawn browser: {SPAWN_FAILURE}"


@pytest.fixture(autouse=True)
def empty_ring():
    """Run every test against an empty ring, and leave one behind.

    ``debug_logger`` is a process-wide singleton shared with every other test in
    the lane, and its F-204 dedup set is what decides whether a repeat error is
    stored at all — so both are reset here. Clearing the set explicitly (rather
    than leaning on ``clear_debug_view``) keeps this fixture honest about test
    isolation instead of silently depending on the product behaviour that
    ``test_debug_logger.py`` pins.
    """

    def _reset() -> None:
        debug_logger.clear_debug_view()
        debug_logger._seen_errors.clear()

    _reset()
    yield
    _reset()


@pytest.fixture()
def failing_spawn(monkeypatch, patched_server):
    """The reported outage, hermetically: every ``spawn_browser`` attempt fails.

    ``profile_role`` is ``default`` — F-896's word for the shared session, and
    the value the resolver actually issues on that branch — so no clone dir is
    created or released, and
    the retry is stubbed OFF so the first failure is final: this file's subject
    is what reaches the debug ring, and a three-attempt message would be about
    the retry protocol instead. Since F-834 stage 1 every role the resolver
    issues retries, so leaning on a role that happens not to — which is what
    this fixture used to do — would pin an unrelated rule from the wrong file.
    The retry protocol itself is pinned in
    ``tests/test_concurrent_spawn_collision.py``.
    """

    async def fake_resolve(user_data_dir, **kwargs):
        return {"user_data_dir": "/fake/dir", "profile_role": "default"}

    async def no_retry(previous_selection, attempt, *, driven=None):
        # ``driven`` is F-914's witness, NAMED rather than swallowed by a
        # ``**kwargs``: this function is the second door onto the
        # held-shared-session rule, and a double that tolerates whatever it is
        # handed cannot fail when the real surface changes
        # (``test_extra_headers_cdp``'s rule, stated on its own double).
        return None

    async def doomed_spawn(options):
        raise RuntimeError(SPAWN_FAILURE)

    monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
    monkeypatch.setattr(clone_storage, "_fallback_profile_selection", no_retry)
    monkeypatch.setattr(desktop_launch, "available", lambda: False)
    fbm = FakeBrowserManager()
    monkeypatch.setattr(fbm, "spawn_browser", doomed_spawn)
    return patched_server(browser_manager=fbm)


@pytest.fixture()
def ghost_server(patched_server):
    """A server whose only instance is ``i1`` — so ``ghost`` misses every time."""
    return patched_server(browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}))


async def _fails(server_mod, tool: str, **kwargs) -> BaseException:
    try:
        result = await call_tool(server_mod, tool, **kwargs)
    except BaseException as exc:  # noqa: BLE001  PERMANENT(the failure IS this file's subject; the type it raises is what each test asserts)
        return exc
    raise AssertionError(f"{tool} returned {result!r} instead of failing")


# ---------------------------------------------------------------------------
# 1. the reported defect
# ---------------------------------------------------------------------------
async def test_a_failed_spawn_lands_in_the_debug_ring(failing_spawn):
    exc = await _fails(failing_spawn, "spawn_browser", headless=True, sandbox=False)
    assert isinstance(exc, tool_errors.ToolError)

    view = debug_logger.get_debug_view()
    assert view["summary"]["total_errors"] == 1
    entry = view["all_errors"][-1]
    assert entry["method"] == "spawn_browser"  # names the tool
    assert entry["error_message"] == SPAWN_TOOL_ERROR  # and what it said
    assert entry["error_type"] == "ToolError"
    assert entry["correlation_id"] != "-"  # the call is greppable in the log


async def test_the_outage_is_visible_through_the_get_debug_view_tool(failing_spawn):
    """The operator's actual surface: 24 consecutive failures, then the tool.

    ``total_errors`` is 1 rather than 24 because F-204 dedups identical
    signatures — that is the ring's long-standing design and not what F-835
    changes. What must never again be true is ``0``; the per-tool stat carries
    the true occurrence count.
    """
    for _ in range(24):
        await _fails(failing_spawn, "spawn_browser", headless=True, sandbox=False)

    view = await call_tool(failing_spawn, "get_debug_view")
    assert view["summary"]["total_errors"] == 1
    assert view["summary"]["stats"]["tool.spawn_browser.errors"] == 24
    assert view["summary"]["error_types"] == {"ToolError": 1}
    assert view["component_breakdown"]["tool"]["errors"] == 1


# ---------------------------------------------------------------------------
# 2. the property is general — every tool, not just spawn
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tool",
    ["get_page_content", "get_element_state", "take_screenshot"],
)
async def test_any_tools_failure_lands_too(ghost_server, tool):
    kwargs = {"instance_id": "ghost"}
    if tool == "get_element_state":
        kwargs["selector"] = "#nope"
    exc = await _fails(ghost_server, tool, **kwargs)

    view = debug_logger.get_debug_view()
    assert view["summary"]["total_errors"] == 1
    entry = view["all_errors"][-1]
    assert entry["method"] == tool
    assert entry["error_message"] == str(exc)


async def test_the_exception_reaching_the_client_is_unchanged(ghost_server):
    """Recording observes; it never transforms. Same type, same args, no
    attributes bolted on (``test_observability`` pins ``vars(exc) == {}`` for
    the same reason: the client's error must stay byte-identical)."""
    exc = await _fails(ghost_server, "get_page_content", instance_id="ghost")
    assert type(exc) is tool_errors.InstanceNotFoundError
    assert exc.args == ("Instance not found: ghost",)
    assert vars(exc) == {}


async def test_the_recording_stays_in_the_ring_and_out_of_the_log(ghost_server):
    """The scope line, pinned: the ring, not the backend log.

    A failure message echoes the caller's own arguments, and F-782's finding
    conditions any LOG fix on redacting the record first — an ERROR record on
    ``stealth.backend`` is durable and is bridged to Sentry by
    ``LoggingIntegration(event_level=ERROR)``. The ring is process-local and
    reaches only the client that already holds those bytes. If this test goes
    red because the failure now reaches the log, that is a disclosure decision
    and needs the redaction question answered, not a green tick.
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect(level=logging.DEBUG)
    logger = logging.getLogger("stealth.backend")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        exc = await _fails(ghost_server, "get_page_content", instance_id="ghost")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert str(exc) not in "\n".join(r.getMessage() for r in records)
    # …and it IS in the ring, so this test cannot pass by recording nothing.
    assert debug_logger.get_debug_view()["summary"]["total_errors"] == 1


# ---------------------------------------------------------------------------
# 3. the ring stays a signal
# ---------------------------------------------------------------------------
async def test_a_successful_call_records_no_error(ghost_server):
    assert await call_tool(ghost_server, "list_instances") == []
    assert debug_logger.get_debug_view()["summary"]["total_errors"] == 0


async def test_an_error_the_body_already_logged_is_not_recorded_twice():
    """No double-recording: a tool body that logs its own failure and then
    raises it produces ONE entry, not two."""

    @with_correlation_id
    async def flaky_tool():
        error = tool_errors.ToolError("boom")
        debug_logger.log_error("server", "flaky_tool", error)
        raise error

    with pytest.raises(tool_errors.ToolError):
        await flaky_tool()
    assert debug_logger.get_debug_view()["summary"]["total_errors"] == 1


async def test_a_different_error_logged_by_the_body_does_not_hide_the_failure():
    """The de-duplication is exact, not a blanket "this call already logged
    something" — an unrelated error logged mid-call must not swallow the record
    of the failure that actually reached the client."""

    @with_correlation_id
    async def noisy_tool():
        debug_logger.log_error("server", "noisy_tool", ValueError("unrelated"))
        raise tool_errors.ToolError("boom")

    with pytest.raises(tool_errors.ToolError):
        await noisy_tool()
    view = debug_logger.get_debug_view()
    assert view["summary"]["total_errors"] == 2
    assert view["all_errors"][-1]["error_message"] == "boom"


# ---------------------------------------------------------------------------
# 4. the recording can never break a tool call
# ---------------------------------------------------------------------------
async def test_a_throwing_debug_ring_does_not_break_the_tool(monkeypatch, ghost_server):
    def boom(*args, **kwargs):
        raise RuntimeError("the ring is on fire")

    monkeypatch.setattr(debug_logger, "log_tool_failure", boom)
    exc = await _fails(ghost_server, "get_page_content", instance_id="ghost")
    assert type(exc) is tool_errors.InstanceNotFoundError
    assert exc.args == ("Instance not found: ghost",)


async def test_a_throwing_debug_ring_does_not_break_a_succeeding_tool(
    monkeypatch, ghost_server
):
    def boom(*args, **kwargs):
        raise RuntimeError("the ring is on fire")

    monkeypatch.setattr(debug_logger, "log_tool_failure", boom)
    assert await call_tool(ghost_server, "list_instances") == []


async def test_cancellation_is_not_recorded_as_an_error():
    """``CancelledError`` is a shutdown signal, not a tool failure — recording
    it would fill the ring with noise every time a client disconnects."""
    import asyncio

    @with_correlation_id
    async def cancelled_tool():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancelled_tool()
    assert debug_logger.get_debug_view()["summary"]["total_errors"] == 0


# ---------------------------------------------------------------------------
# 5. a ValidationError OUR body raised still reaches Sentry (F-943)
# ---------------------------------------------------------------------------
class _Port(pydantic.BaseModel):
    port: int


async def broken_settings_tool() -> str:
    """Raises a REAL pydantic ``ValidationError`` from inside the body — the
    shape of an unknown ``STEALTH_MCP_*`` key failing ``Settings()``."""
    _Port.model_validate({"port": "not-a-port"})
    return "unreachable"


async def crashing_tool() -> str:
    raise RuntimeError("boom")


async def counting_tool(count: int) -> int:
    return count


async def _tool_manager_records(tool: str, arguments: dict) -> list[logging.LogRecord]:
    """Call ``tool`` through fastmcp's REAL ``Client`` → ``ToolManager`` →
    ``FunctionTool.run`` path and return what reached fastmcp's tool logger.

    A real FastMCP server and not ``call_tool``'s ``.fn`` seam, because the
    subject is what the LIBRARY logs around our wrapper; the seam skips it. The
    handler sits on the logger itself because the ``fastmcp`` family does not
    propagate to root (Sentry still sees it: it patches ``callHandlers``).
    """
    import fastmcp

    app = fastmcp.FastMCP("f943")
    for fn in (broken_settings_tool, crashing_tool, counting_tool):
        app.tool(with_correlation_id(fn))

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect(level=logging.DEBUG)
    logger = logging.getLogger(expected_events.TOOL_MANAGER_LOGGER)
    logger.addHandler(handler)
    try:
        async with fastmcp.Client(app) as client:
            with pytest.raises(Exception):  # noqa: B017, PT011  PERMANENT(the client's error type is fastmcp's; this test is about the log, not it)
                await client.call_tool(tool, arguments)
    finally:
        logger.removeHandler(handler)
    return [r for r in records if r.levelno >= logging.ERROR]


def test_the_tool_manager_logger_is_the_installed_librarys():
    """``expected_events`` keys ``caller-input`` on this name and the report
    below logs on it. fastmcp 2.14 renamed it from ``FastMCP.fastmcp.tools.
    tool_manager``; read off the library so the next rename fails HERE."""
    from fastmcp.tools import tool_manager

    assert tool_manager.logger.name == expected_events.TOOL_MANAGER_LOGGER


async def test_a_validation_error_our_body_raised_is_reported():
    """fastmcp 2.14 re-raises a ``ValidationError`` out of a tool WITHOUT
    logging it, so ours — which fails every call, as F-887 measured — would
    reach no log and no Sentry. It must be reported exactly once, with the
    exception attached, the way fastmcp 2.11.2 reported it."""
    records = await _tool_manager_records("broken_settings_tool", {})

    assert len(records) == 1
    (record,) = records
    assert record.getMessage() == "Error calling tool 'broken_settings_tool'"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], pydantic.ValidationError)


async def test_any_other_failure_is_reported_once_not_twice():
    """fastmcp still logs every other exception itself; adding a second
    report for those would double every Sentry event."""
    records = await _tool_manager_records("crashing_tool", {})

    assert len(records) == 1
    assert isinstance(records[0].exc_info[1], RuntimeError)


async def test_a_callers_bad_argument_is_not_reported():
    """The caller's own typo fails fastmcp's argument validation BEFORE our
    wrapper runs, and the caller already got the message. Nothing ships."""
    assert await _tool_manager_records("counting_tool", {"count": "abc"}) == []
