"""T-11911: a seedless disposable clone retries a failed launch like a seeded one.

The incident (main push run 41196074989 on ce68003, macOS, both attempts):
Chrome launched but never answered ``/json/version`` within
``browser_connect``'s 30 s patience, and ``spawn_browser`` failed after ONE
attempt. ``_SPAWN_ATTEMPTS`` is 3, but the loop re-raises as soon as
``clone_storage._fallback_profile_selection`` answers None, and for a ``clone``
role that answered None whenever the root had no ``master-snapshot`` and no
configured seed session: every brand-new install and every CI session root.

What is pinned, through the real tool body and the real resolver on a root with
no master, no snapshot and no seed (``tmp_empty_root``):

* an unnamed spawn whose first launch fails is launched AGAIN, onto a fresh
  clone, and a later success is returned;
* when all three launches fail the caller gets all three errors and every failed
  attempt's clone reservation was released (the F-860 / ``_release_clone_dir``
  path, unchanged);
* a NAMED session with no seed behaves as before (retried onto the same
  directory), and the shared session held with no seed still does not retry.

The double is ``fakes.FakeBrowserManager`` with a scripted ``spawn_browser``;
nothing launches Chrome. ``tmp_empty_root`` is requested before
``patched_server`` (the fixture caches Settings at build time).
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from fakes import FakeBrowserManager, FakeChrome
from stealth_chrome_devtools_mcp.embedded import (
    clone_protection,
    clone_storage,
    spawn_leak,
)

FAILURE = "Failed to connect to browser"


@pytest.fixture(autouse=True)
def no_reservations():
    """A spawn that SUCCEEDS keeps its clone protected for the life of the
    instance, so the reservation set is emptied around every node."""
    clone_protection.PROTECTED.clear()
    yield
    clone_protection.PROTECTED.clear()


class ScriptedManager(FakeBrowserManager):
    """``FakeBrowserManager`` whose ``spawn_browser`` follows ``outcomes``: an
    Exception is raised, anything else is returned as the instance."""

    def __init__(self, outcomes: list[object]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self.protected_at_call: list[bool] = []

    async def spawn_browser(self, options):
        self.spawn_calls.append(options)
        self.protected_at_call.append(
            clone_protection.normalize(options.user_data_dir)
            in clone_protection.PROTECTED
        )
        outcome = self.outcomes[len(self.spawn_calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def working_instance() -> SimpleNamespace:
    return SimpleNamespace(
        instance_id="i-ok",
        state="active",
        headless=True,
        viewport={"width": 1920, "height": 1080},
    )


async def spawn(srv, call_tool, **kwargs):
    return await call_tool(srv, "spawn_browser", headless=True, sandbox=False, **kwargs)


async def test_a_seedless_clone_is_retried_after_a_failed_launch(
    tmp_empty_root, patched_server, call_tool
):
    assert not tmp_empty_root["snapshot"].exists()
    manager = ScriptedManager([Exception(FAILURE), working_instance()])
    srv = patched_server(browser_manager=manager)

    result = await spawn(srv, call_tool)

    assert result["instance_id"] == "i-ok"
    assert len(manager.spawn_calls) == 2
    first, second = (Path(o.user_data_dir) for o in manager.spawn_calls)
    assert first != second, "the retry must drive a fresh clone, not the failed one"
    assert all(o.auto_clone is True for o in manager.spawn_calls)


async def test_a_seedless_clone_that_fails_every_launch_reports_all_three(
    tmp_empty_root, patched_server, call_tool
):
    errors = [Exception(f"{FAILURE} {n}") for n in (1, 2, 3)]
    manager = ScriptedManager(errors)
    srv = patched_server(browser_manager=manager)

    with pytest.raises(Exception) as raised:
        await spawn(srv, call_tool)

    assert len(manager.spawn_calls) == 3
    expected = "; ".join(f"Exception: {FAILURE} {n}" for n in (1, 2, 3))
    assert str(raised.value).endswith(expected), str(raised.value)
    directories = {o.user_data_dir for o in manager.spawn_calls}
    assert len(directories) == 3, directories
    assert manager.protected_at_call == [True, True, True]
    assert not clone_storage._PROTECTED_CLONE_DIRS, "a failed attempt kept its clone"


async def test_a_named_session_with_no_seed_is_retried_on_the_same_directory(
    tmp_empty_root, patched_server, call_tool
):
    manager = ScriptedManager([Exception(FAILURE), working_instance()])
    srv = patched_server(browser_manager=manager)

    result = await spawn(srv, call_tool, session="seedless-named")

    assert result["instance_id"] == "i-ok"
    assert len(manager.spawn_calls) == 2
    first, second = (o.user_data_dir for o in manager.spawn_calls)
    assert first == second
    assert all(o.auto_clone is False for o in manager.spawn_calls)


async def test_the_fallback_gives_a_seedless_clone_a_fresh_clone_with_the_retry_names(
    tmp_empty_root,
):
    for attempt, suffix in ((0, "retry"), (1, "seed")):
        selection = await clone_storage._fallback_profile_selection(
            {"profile_role": "clone"}, attempt
        )
        assert selection is not None
        assert selection["profile_role"] == "clone"
        assert Path(selection["user_data_dir"]).name.endswith(suffix), selection
        assert Path(selection["user_data_dir"]).is_dir()
        clone_storage._release_clone_dir(selection["user_data_dir"])


async def test_the_held_shared_session_with_no_seed_still_does_not_retry(
    tmp_empty_root, monkeypatch
):
    monkeypatch.setattr(clone_storage, "_profile_hold", lambda directory: object())
    previous = {
        "profile_role": "default",
        "user_data_dir": str(tmp_empty_root["master"]),
    }

    assert await clone_storage._fallback_profile_selection(previous, 0) is None


def test_the_reap_warning_names_the_tcp_ports_the_browser_listened_on():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert str(port) in spawn_leak._listening(psutil.Process().pid).split(",")


def test_the_reap_warning_says_unknown_when_the_pid_cannot_be_read(monkeypatch):
    def denied(self, kind="inet"):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "net_connections", denied)
    assert spawn_leak._listening(psutil.Process().pid) == "unknown"


@pytest.mark.parametrize(
    ("listening", "alive", "expected"),
    [
        ((9222, 9223), True, "9222,9223"),
        ((), True, "none"),
        ((9222,), False, "unknown"),
    ],
)
def test_the_reap_warning_reads_the_launched_pids_listeners(
    monkeypatch, listening, alive, expected
):
    chrome = FakeChrome(4242, "/profile", 0.0, listening=listening)
    chrome.alive = alive
    monkeypatch.setattr(psutil, "Process", lambda pid: chrome)

    assert spawn_leak._listening(4242) == expected
