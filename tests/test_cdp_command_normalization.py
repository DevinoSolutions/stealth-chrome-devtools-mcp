"""Pins for F-813: execute_cdp_command accepts every spelling of a CDP command.

Callers send the name they read in the CDP docs — ``Emulation.setDeviceMetrics
Override`` — or the one they read in nodriver — ``emulation.set_device_metrics_
override``. The executor resolved only ``getattr(uc.cdp.runtime, command)``, so
both raised ``Unknown CDP command`` (Sentry STEALTH-CHROME-DEVTOOLS-MCP-1N, 42
events). Two separate defects hid in that one lookup:

* it was Runtime-only, so no other domain could ever be reached, and
* it was casing-exact, so of the 21 camelCase names ``list_cdp_commands``
  ADVERTISES, only ``evaluate`` (identical in both conventions) could run — the
  tool's own docstring example, ``callFunctionOn``, could not.

``resolve_cdp_command`` folds case and underscores and resolves through the SAME
lookup: the exact attribute is tried first, so a name that worked before still
resolves to the identical object. Hermetic — the resolver is pure, and the
executor runs against a ``FakeTab``.
"""

import nodriver as uc
import pytest

from fakes import FakeBrowserManager, FakeTab, call_tool
from stealth_chrome_devtools_mcp.embedded import cdp_function_executor, server
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
    build_cdp_call,
    resolve_cdp_command,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

VIEWPORT = {
    "width": 390,
    "height": 844,
    "device_scale_factor": 3.0,
    "mobile": True,
}


@pytest.mark.parametrize(
    "spelling",
    [
        "evaluate",  # the one spelling that worked before
        "Runtime.evaluate",  # CDP wire form
        "runtime.evaluate",  # nodriver module form
        "Runtime.Evaluate",
    ],
)
def test_every_spelling_of_a_runtime_command_resolves_to_one_object(spelling):
    found, tried = resolve_cdp_command(spelling)

    assert found is uc.cdp.runtime.evaluate, "must be the same callable, not a copy"
    assert tried == "runtime.evaluate"


@pytest.mark.parametrize(
    "spelling",
    [
        "Emulation.setDeviceMetricsOverride",
        "emulation.set_device_metrics_override",
        "emulation.setDeviceMetricsOverride",
    ],
)
def test_a_non_runtime_domain_resolves_from_the_reported_spellings(spelling):
    """The exact Sentry payload: a domain the old lookup could not reach."""
    found, tried = resolve_cdp_command(spelling)

    assert found is uc.cdp.emulation.set_device_metrics_override
    assert tried == "emulation.set_device_metrics_override"


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        # Folding, not a camelCase->snake_case rewrite: nodriver's generated
        # names keep acronyms whole and escape one Python keyword, and no
        # rewrite rule reproduces those.
        ("DOM.getDocument", uc.cdp.dom.get_document),
        ("IndexedDB.requestDatabaseNames", uc.cdp.indexed_db.request_database_names),
        ("Input.dispatchKeyEvent", uc.cdp.input_.dispatch_key_event),
        ("IO.read", uc.cdp.io.read),
    ],
)
def test_acronym_and_keyword_domains_resolve(spelling, expected):
    assert resolve_cdp_command(spelling)[0] is expected


@pytest.mark.asyncio
async def test_every_command_the_tool_advertises_can_actually_run():
    """``list_cdp_commands`` is a promise; this is the pin that it is kept.

    Before F-813 exactly one of these 21 names resolved. A name that cannot be
    executed has no business being advertised, so the list and the resolver are
    held together here rather than drifting apart again.
    """
    advertised = await CDPFunctionExecutor().list_cdp_commands()

    unresolvable = [name for name in advertised if resolve_cdp_command(name)[0] is None]

    assert not unresolvable, f"advertised but not executable: {unresolvable}"


def test_an_unknown_command_stays_unknown():
    """Folding must not turn a typo into some other command."""
    assert resolve_cdp_command("Runtime.noSuchThing")[0] is None
    assert resolve_cdp_command("noSuchThing")[0] is None
    assert resolve_cdp_command("NoSuchDomain.evaluate")[0] is None


@pytest.mark.asyncio
async def test_the_executor_sends_the_resolved_command():
    executor = CDPFunctionExecutor()
    tab = FakeTab(
        cdp_responses={"enable": None, "set_device_metrics_override": {"ok": True}}
    )

    result = await executor.execute_cdp_command(
        tab, "Emulation.setDeviceMetricsOverride", VIEWPORT
    )

    assert result["success"] is True
    assert result["result"] == {"ok": True}
    assert "set_device_metrics_override" in tab.send_calls
    # The caller's own spelling is echoed back — it is what they asked for.
    assert result["command"] == "Emulation.setDeviceMetricsOverride"


@pytest.mark.asyncio
async def test_the_executor_still_runs_a_bare_runtime_command_unchanged():
    executor = CDPFunctionExecutor()
    tab = FakeTab(cdp_responses={"enable": None, "evaluate": ("remote", None)})

    result = await executor.execute_cdp_command(
        tab, "evaluate", {"expression": "6 * 7", "return_by_value": True}
    )

    assert result["success"] is True
    assert result["result"] == ("remote", None)
    assert tab.send_calls == ["enable", "evaluate"]


@pytest.mark.asyncio
async def test_an_unknown_command_reports_what_was_looked_for():
    executor = CDPFunctionExecutor()
    tab = FakeTab(cdp_responses={"enable": None})

    result = await executor.execute_cdp_command(tab, "Emulation.setNoSuchThing", {})

    assert result["success"] is False
    assert "Unknown CDP command: Emulation.setNoSuchThing" in result["error"]
    # Naming the resolved form tells a caller the DOMAIN was found and the
    # METHOD was not — the difference between a typo and a wrong domain.
    assert "emulation.setNoSuchThing" in result["error"]


@pytest.mark.asyncio
async def test_an_unknown_command_is_reported_as_an_expected_tool_failure(monkeypatch):
    """The unknown-command failure must be a ToolError, not a ValueError.

    Nothing OUTSIDE this method can see the difference — the executor catches its
    own raise and returns the ``{"success": False}`` dict either way. The type is
    observable at exactly one place, the hand-off to ``debug_logger.log_error``,
    which passes it to the backend logger as ``exc_info``; from there Sentry's
    ``before_send`` drops it only if it is the project's own error convention.
    As a ValueError this expected failure shipped as a crash (42 events), so the
    class is the whole fix and this is where it can be pinned.
    """
    reported: list[BaseException] = []
    monkeypatch.setattr(
        cdp_function_executor.debug_logger,
        "log_error",
        lambda component, method, error, context=None: reported.append(error),
    )

    result = await CDPFunctionExecutor().execute_cdp_command(
        FakeTab(cdp_responses={"enable": None}), "Runtime.noSuchThing", {}
    )

    assert result["success"] is False
    assert [type(error) for error in reported] == [ToolError]
    assert not isinstance(reported[0], ValueError)


@pytest.mark.asyncio
async def test_the_tool_surfaces_one_unwrapped_failure(patched_server):
    """What the caller actually receives: ONE message, wrapped exactly zero times.

    The executor catches its own raise, so the ToolError never reaches the tool
    layer and ``execute_cdp_command`` still answers with the executor's
    ``{"success": False}`` dict rather than raising. Pinned because the type
    change above deliberately did NOT change this contract — nothing re-wraps the
    message into a second error shape on the way out.
    """
    tab = FakeTab(cdp_responses={"enable": None})
    patched_server(browser_manager=FakeBrowserManager(tabs={"i1": tab}))

    result = await call_tool(
        server, "execute_cdp_command", instance_id="i1", command="Runtime.noSuchThing"
    )

    assert result["success"] is False
    assert result["error"] == (
        "Unknown CDP command: Runtime.noSuchThing (tried runtime.noSuchThing)"
    )
    assert result["command"] == "Runtime.noSuchThing"


# ---------------------------------------------------------------------------
# F-816: the same forgiveness, one level down — the command's PARAMETERS
#
# Resolving the command name only moved the identical mistake one line along:
# the caller who reads ``Runtime.evaluate`` in the CDP docs reads its parameters
# there too, and sends ``awaitPromise`` to a generated function that declares
# ``await_promise`` (Sentry STEALTH-CHROME-DEVTOOLS-MCP-22, 3 events, 2.0.6).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"expression": "6 * 7", "awaitPromise": True, "returnByValue": True},
        {"expression": "6 * 7", "await_promise": True, "return_by_value": True},
        {"expression": "6 * 7", "awaitPromise": True, "return_by_value": True},
    ],
    ids=["wire", "nodriver", "mixed"],
)
@pytest.mark.asyncio
async def test_every_spelling_of_a_param_reaches_the_wire(params):
    """All three spellings must build the ONE frame the protocol defines.

    The frame is the assertion rather than the kwargs: it is what Chrome
    receives, so it proves the value arrived under the right name instead of
    being silently dropped.
    """
    tab = FakeTab(cdp_responses={"enable": None, "evaluate": ("remote", None)})

    result = await CDPFunctionExecutor().execute_cdp_command(tab, "evaluate", params)

    assert result["success"] is True
    assert tab.cdp_frames[-1] == {
        "method": "Runtime.evaluate",
        "params": {"expression": "6 * 7", "returnByValue": True, "awaitPromise": True},
    }
    # The caller's own spelling is echoed back, exactly as the command name is.
    assert result["params"] == params


def test_the_param_folding_is_not_runtime_only():
    """Folding is a property of the resolved callable, not of one domain."""
    call = build_cdp_call(
        uc.cdp.emulation.set_device_metrics_override,
        {"width": 390, "height": 844, "deviceScaleFactor": 3.0, "mobile": True},
    )

    assert next(call)["params"]["deviceScaleFactor"] == 3.0


def test_an_exact_param_name_is_never_folded_onto_another():
    """The exact name wins, so a name that worked before is passed untouched."""
    call = build_cdp_call(uc.cdp.runtime.evaluate, {"expression": "1", "silent": True})

    assert next(call)["params"] == {"expression": "1", "silent": True}


@pytest.mark.asyncio
async def test_an_unknown_param_names_the_ones_the_command_takes(monkeypatch):
    """A param no folding can match is a caller mistake: an EXPECTED failure.

    Same shape as the unknown-command path above — a ToolError the executor
    catches itself, so Sentry's ``before_send`` drops it by type and the caller
    still receives the method's ``{"success": False}`` KEEP contract. The valid
    names go in the message because that is the one thing the caller needs.
    """
    reported: list[BaseException] = []
    monkeypatch.setattr(
        cdp_function_executor.debug_logger,
        "log_error",
        lambda component, method, error, context=None: reported.append(error),
    )
    tab = FakeTab(cdp_responses={"enable": None, "evaluate": ("remote", None)})

    result = await CDPFunctionExecutor().execute_cdp_command(
        tab, "Runtime.evaluate", {"expression": "1", "awaitPromis": True}
    )

    assert result["success"] is False
    assert "awaitPromis" in result["error"]
    assert "await_promise" in result["error"], "must list what the command takes"
    assert [type(error) for error in reported] == [ToolError]
    assert "evaluate" not in tab.send_calls, "a bad call must not reach the browser"
