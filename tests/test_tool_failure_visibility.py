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

Four things this file refuses to let regress, in the order they matter:

1. the failure is IN the ring, named by tool and message (and reachable through
   the real ``get_debug_view`` tool, not just the logger object);
2. the exception the client receives is byte-identical to the one it received
   before the recording existed — recording observes, it never transforms, and
   it stays in the ring rather than in the durable, Sentry-bridged backend log
   (F-782's redaction condition — see the scope test below);
3. a SUCCEEDING call records nothing (the ring stays a signal, not a log);
4. the recording can never break a tool call — a debug ring that throws is a
   debug-ring problem, and the tool's own error is what reaches the client.

Hermetic: no Chrome, no disk profile, no transport. The ring is a process-wide
singleton, so every test runs against an emptied one.
"""

from __future__ import annotations

import logging

import pytest

from fakes import FakeBrowserManager, FakeTab, call_tool
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

    ``profile_role`` is ``master`` so no clone dir is created, released, or
    fallen back to — ``_fallback_profile_selection`` returns ``None`` for a
    non-clone role, which is what makes the first failure final.
    """

    async def fake_resolve(user_data_dir, **kwargs):
        return {"user_data_dir": "/fake/dir", "profile_role": "master"}

    async def doomed_spawn(options):
        raise RuntimeError(SPAWN_FAILURE)

    monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
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
