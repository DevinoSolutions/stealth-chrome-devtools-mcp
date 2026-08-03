"""F-808 last line of defense: a headed spawn served by a backend that cannot
display a window raises instead of returning a ghost.

Discovery (task 4) normally routes a headed request to a window-capable backend,
so this guard only fires when that routing did not save us — a backend started
from a service context, an SSH session, or a macOS Background domain. The
integration twin (task 8) proves the guard reads the REAL machine; THIS file owns
the message contract and the ordering contract.

Tests here state their display premise through ``fakes.pretend_display_context``
rather than patching ``can_show_windows`` directly: the guard consults
``can_show_windows()``, which DERIVES from that one token, so patching the source
exercises the real derivation and cannot pin an impossible pair (a "capable"
token that reports itself invisible). It also makes each test say the same thing
on every runner — Session 0 Windows, a DISPLAY-less Linux CI cell, or an Aqua
desktop. Exactly one test breaks that rule, deliberately and with its reasons
written down: ``test_the_message_reports_the_token_rather_than_the_word_headless``.

F-810 demoted the refusal to a FALLBACK: when the OS can place the launch on a
logged-on user's desktop (``desktop_launch.available()``), the spawn proceeds and
delegation delivers the window. So every test here states a SECOND premise —
whether delegation is on offer — the same way it states its display premise, and
for the same reason: otherwise a Windows developer's console session and a Linux
CI cell would run different branches of the guard.
"""

from types import SimpleNamespace

import pytest

from fakes import FakeBrowserManager, pretend_display_context
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    desktop_launch,
    display_context,
    tool_errors,
)


def _fake_instance(headless: bool) -> SimpleNamespace:
    return SimpleNamespace(
        instance_id="i1",
        state="ready",
        headless=headless,
        viewport={"width": 1920, "height": 1080},
    )


@pytest.fixture()
def doomed_spawn(monkeypatch, patched_server):
    """Server module wired with TRIPWIRES in place of the two collaborators the
    guard must precede. Returns the fake manager so a test can assert it stayed
    untouched.

    Tripwires, not plain fakes, for two reasons: they make "the guard ran first"
    a positive assertion, and they keep a regressed guard from reaching the real
    ``BrowserManager`` and launching Chrome inside the hermetic lane. Their
    messages deliberately avoid every token the message contract asserts on —
    a tripwire that says "F-808" would make the contract test pass for the
    wrong reason.
    """

    async def tripwire_resolve(user_data_dir, **kwargs):
        raise AssertionError("profile selection ran before the visibility guard")

    monkeypatch.setattr(clone_storage, "resolve_profile_selection", tripwire_resolve)
    # The second premise (F-810): no logged-on desktop to hand the launch to, so
    # the refusal is the only remaining branch. Stated, never inherited from the
    # runner — a Windows dev box has a console session and a CI cell does not.
    monkeypatch.setattr(desktop_launch, "available", lambda: False)
    # Unseeded on purpose: FakeBrowserManager's own "seed spawn_instance" error IS
    # the second tripwire (and contains no contract token). A seeded instance here
    # would let a regressed guard spawn "successfully".
    fbm = FakeBrowserManager()
    return patched_server(browser_manager=fbm), fbm


@pytest.fixture()
def spawn_fakes(monkeypatch, patched_server):
    """Install the normal hermetic spawn plumbing (mirrors ``TestSpawnBrowserSeam``
    in ``test_bug_prone_tools.py``): a seeded browser-manager fake plus a profile
    selection that touches no disk. Yields ``(server_module, fake_manager)`` so a
    test can assert what the tool did — or did not — call."""

    def _install(headless: bool = False):
        fbm = FakeBrowserManager(
            spawn_instance=_fake_instance(headless), spawn_diagnostics={}
        )

        async def fake_resolve(user_data_dir, **kwargs):
            return {"user_data_dir": "/fake/dir", "profile_role": "clone"}

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        # Delegation OFF by default here, so a test that proceeds proves it did so
        # on the DISPLAY premise (F-808) and not on an available desktop (F-810).
        monkeypatch.setattr(desktop_launch, "available", lambda: False)
        return patched_server(browser_manager=fbm), fbm

    return _install


async def test_headed_spawn_raises_when_no_window_can_be_shown(
    monkeypatch, call_tool, doomed_spawn
):
    pretend_display_context(monkeypatch, display_context.HEADLESS)
    server_mod, _ = doomed_spawn
    with pytest.raises(tool_errors.ToolError) as err:
        await call_tool(server_mod, "spawn_browser", headless=False)
    msg = str(err.value)
    assert "cannot display a window" in msg
    # The context token VERBATIM, parenthesised. A bare `"headless" in msg` is
    # subsumed by the `headless=True` assert below, so deleting the parenthetical
    # from the message would leave every other assert here green.
    assert f"({display_context.HEADLESS})" in msg
    assert "headless=True" in msg  # the honest alternative
    assert "stealth-chrome-devtools doctor" in msg  # where to see the diagnosis
    assert "F-808" in msg  # the finding, for greppability
    # F-810: the message must say the automatic delivery was TRIED and unavailable,
    # otherwise a user on a machine where it normally works reads a stale reason.
    assert "F-810" in msg
    assert "logged on" in msg
    # Raised on its own terms, NOT re-wrapped by the tool's blanket handler: the
    # guard sits outside the try, so the user reads the diagnosis first.
    assert not msg.startswith("Failed to spawn browser")


async def test_the_message_reports_the_token_rather_than_the_word_headless(
    monkeypatch, call_tool, doomed_spawn
):
    """The message INTERPOLATES ``display_context()``; it does not hardcode the
    word "headless".

    This is the only test in the file that patches ``can_show_windows``
    separately, and it does so to build a pair production cannot reach:
    ``can_show_windows()`` is defined as ``display_context() != HEADLESS``, so a
    firing guard ALWAYS has the HEADLESS token to print. That makes the
    contract "names the actual context token" untestable from a reachable state
    — a hardcoded literal and a correct interpolation are indistinguishable. So
    the pair is forced here on purpose. If display_context.py ever grows a
    second proven-invisible token (say, one that distinguishes a Windows
    service session from an SSH login), this test is what already guarantees
    the user is told WHICH one.
    """
    monkeypatch.setattr(display_context, "can_show_windows", lambda: False)
    pretend_display_context(monkeypatch, "win-session-0-service")
    server_mod, _ = doomed_spawn
    with pytest.raises(tool_errors.ToolError) as err:
        await call_tool(server_mod, "spawn_browser", headless=False)
    assert "(win-session-0-service)" in str(err.value)


async def test_the_guard_precedes_every_side_effect(
    monkeypatch, call_tool, doomed_spawn
):
    """The ordering contract. A spawn that can never be seen must not first
    clone a profile directory onto disk or reach the browser manager — a clone
    made for a doomed spawn is quota the user pays for and nobody uses."""
    pretend_display_context(monkeypatch, display_context.HEADLESS)
    server_mod, fbm = doomed_spawn
    with pytest.raises(tool_errors.ToolError) as err:
        await call_tool(server_mod, "spawn_browser", headless=False)
    # The guard's own words, not a tripwire's: neither collaborator ran.
    assert "cannot display a window" in str(err.value)
    assert fbm.spawn_calls == []


async def test_headless_spawn_is_unaffected_without_a_display(
    monkeypatch, call_tool, spawn_fakes
):
    """CI and scripted use must keep working from a headless context."""
    pretend_display_context(monkeypatch, display_context.HEADLESS)
    server_mod, fbm = spawn_fakes(headless=True)
    result = await call_tool(server_mod, "spawn_browser", headless=True, sandbox=False)
    assert result["state"] == "ready"
    assert len(fbm.spawn_calls) == 1


async def test_unverified_context_does_not_block_headed_spawn(
    monkeypatch, call_tool, spawn_fakes
):
    """UNVERIFIED is treated as capable (task 1's rule): a probe that could not
    classify the platform must not block headed browsing that works today — a
    wrong "headless" is a worse regression than the ghost it prevents."""
    pretend_display_context(monkeypatch, display_context.UNVERIFIED)
    server_mod, fbm = spawn_fakes(headless=False)
    result = await call_tool(server_mod, "spawn_browser", headless=False, sandbox=False)
    assert result["state"] == "ready"
    assert len(fbm.spawn_calls) == 1
    assert fbm.spawn_calls[0].headless is False


async def test_an_available_desktop_launch_turns_the_refusal_off(
    monkeypatch, call_tool, spawn_fakes
):
    """F-810, the whole point: from a context that cannot show a window, a headed
    spawn now PROCEEDS when the OS can place the launch on a logged-on desktop.
    The refusal is the fallback for when it cannot."""
    pretend_display_context(monkeypatch, display_context.HEADLESS)
    server_mod, fbm = spawn_fakes(headless=False)
    monkeypatch.setattr(desktop_launch, "available", lambda: True)
    result = await call_tool(server_mod, "spawn_browser", headless=False, sandbox=False)
    assert result["state"] == "ready"
    assert len(fbm.spawn_calls) == 1
    assert fbm.spawn_calls[0].headless is False


async def test_a_failing_delegation_surfaces_its_own_reason(
    monkeypatch, call_tool, patched_server
):
    """When delegation is on offer but fails, the user must read WHY it failed —
    not the F-808 refusal, which by then would be a lie about the machine."""

    class ExplodingManager(FakeBrowserManager):
        async def spawn_browser(self, options):
            self.spawn_calls.append(options)
            raise RuntimeError(
                "F-810: could not create the desktop-launch task "
                "stealth-mcp-launch-abc (schtasks exit 1)"
            )

    async def fake_resolve(user_data_dir, **kwargs):
        return {"user_data_dir": "/fake/dir", "profile_role": "clone"}

    monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
    pretend_display_context(monkeypatch, display_context.HEADLESS)
    monkeypatch.setattr(desktop_launch, "available", lambda: True)
    fbm = ExplodingManager()
    server_mod = patched_server(browser_manager=fbm)
    with pytest.raises(tool_errors.ToolError) as err:
        await call_tool(server_mod, "spawn_browser", headless=False, sandbox=False)
    msg = str(err.value)
    assert "F-810" in msg
    assert "desktop-launch task" in msg
    # The delegation branch is a real spawn attempt, so it DOES reach the manager
    # (unlike the refusal branch, which must precede every side effect).
    assert len(fbm.spawn_calls) >= 1


async def test_a_named_desktop_does_not_block_headed_spawn(
    monkeypatch, call_tool, spawn_fakes
):
    """The common case: any token that is neither HEADLESS nor UNVERIFIED names a
    real desktop, and a headed spawn there proceeds untouched."""
    pretend_display_context(monkeypatch, "win-session-1")
    server_mod, fbm = spawn_fakes(headless=False)
    result = await call_tool(server_mod, "spawn_browser", headless=False, sandbox=False)
    assert result["state"] == "ready"
    assert len(fbm.spawn_calls) == 1
