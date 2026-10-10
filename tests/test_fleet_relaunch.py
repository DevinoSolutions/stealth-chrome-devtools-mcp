"""F-961 -- the shared ``fleet`` browser comes BACK after it is closed.

Chats share one signed-in ``fleet`` browser, each in its own tab. Nothing one chat
does to it -- an accidental ``close_instance``, a closed window, a crash -- may
cost the others their login: the next ``spawn_browser(session="fleet")``, from any
chat, relaunches it from its persistent profile. Five paths, each pinned where the
decision is made (the real-Chrome journey is ``test_e2e_fleet_relaunch``):

1. **Closed by one chat, spawned by another**: a NEW instance on the SAME
   directory -- no ``already_running``, no ``fleet-2``, no copy, no refusal.
2. **The browser died with nobody closing it**: the dead instance is not handed
   back as ``already_running``, and the lock files Chrome left behind do not
   refuse or walk the spawn.
3. **A chat holding the OLD instance id** is told how to relaunch, in the one
   error convention (``InstanceNotFoundError``), by whichever way the id went.
4. **Two chats ask at once** right after the close: ONE launch. This is the
   defect -- each read "nothing holds it" and each launched, two instances on one
   signed-in profile. ``directory_gate`` is what serializes them.
5. **An advisory session lock held at close time** does not block the relaunch.

Hermetic: no Chrome, no real session root, no real process table.
"""

import asyncio
import contextvars
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fakes import FakeBrowser
from stealth_chrome_devtools_mcp.embedded import (
    browser_reattach,
    clone_storage,
    desktop_launch,
    profile_lock,
    profile_seed,
    session_lease,
    tool_errors,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
)
from stealth_chrome_devtools_mcp.embedded.tool_sections import browser_management

RELAUNCH = 'spawn_browser(session="fleet")'


@pytest.fixture(autouse=True)
def _no_real_teardown(monkeypatch):
    """``close_instance`` and the dead-browser discard reach the process table
    and the on-disk pid record; here they are stubs, and the departed-id memory
    starts empty so no other test's ids leak in."""
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

    for name in (
        "kill_browser_process",
        "finalize_browser_process",
        "cleanup_deferred_profiles",
    ):
        monkeypatch.setattr(process_cleanup, name, MagicMock())
    monkeypatch.setattr(in_memory_storage, "remove_instance", MagicMock())
    monkeypatch.setattr(tool_errors, "_departed", {}, raising=False)
    monkeypatch.setattr(desktop_launch, "can_deliver_headed_window", lambda: True)
    session_lease.reset()
    yield
    session_lease.reset()


async def _fleet(dirs: dict) -> Path:
    """The fleet session, created and with a login in it."""
    landed = clone_storage.require_allowed_user_data_dir(None, "fleet")
    await clone_storage.resolve_profile_selection(landed)
    directory = dirs["sessions"] / "fleet"
    jar = directory / "Default" / "Network" / "Cookies"
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(b"the-login")
    return directory


def _running(manager: BrowserManager, instance_id: str, directory: Path, **browser):
    manager._instances[instance_id] = {
        "browser": FakeBrowser(**browser),
        "instance": BrowserInstance(instance_id=instance_id),
        "options": SimpleNamespace(user_data_dir=str(directory)),
        "tab": SimpleNamespace(),
    }


def _on(manager: BrowserManager, directory: Path) -> list[str]:
    return [
        iid
        for iid, data in manager._instances.items()
        if profile_seed.same_dir(Path(data["options"].user_data_dir), directory)
    ]


class _Spawns:
    """``spawn_browser`` with the launch and the process table replaced, and
    everything that DECIDES -- the gate, the resolver, ``adopt_held_profile``,
    the answer -- real. ``launches`` is every browser started."""

    def __init__(self, monkeypatch, call_tool, patched_server, manager, fleet):
        self.manager = manager
        self.fleet = fleet
        self.launches: list[str] = []
        self.walked: list[object] = []
        self._call_tool = call_tool
        self._srv = patched_server(browser_manager=manager)
        real_resolve = clone_storage.resolve_profile_selection

        def held_by(user_data_dir, **_k):
            # The process table: a browser holds the directory while the manager
            # has one that is alive there.
            if not profile_seed.same_dir(Path(user_data_dir), fleet):
                return None
            for iid in _on(manager, fleet):
                if manager._instances[iid]["browser"]._process.poll() is None:
                    return browser_reattach.Adoptable(
                        instance_id=iid, pid=4242, user_data_dir=str(fleet), port=9
                    )
            return None

        async def resolve(user_data_dir, **kwargs):
            selection = await real_resolve(user_data_dir, **kwargs)
            self.walked.append(selection.get("walked_to"))
            return selection

        async def launch(options):
            await asyncio.sleep(0.05)  # a Chrome takes time; that window is the bug
            instance_id = f"i-{len(self.launches) + 1}"
            self.launches.append(instance_id)
            _running(manager, instance_id, Path(options.user_data_dir))
            return SimpleNamespace(
                instance_id=instance_id, state="active", headless=True, viewport={}
            )

        async def no_tab(_instance_id):
            return None

        async def diagnostics(_instance_id):
            return {}

        async def adopted(instance_id, _block_resources):
            return {"instance_id": instance_id, "reattached": True, "headless": True}

        monkeypatch.setattr(browser_reattach, "held_by", held_by)
        monkeypatch.setattr(clone_storage, "resolve_profile_selection", resolve)
        monkeypatch.setattr(browser_management, "_adopted_instance_record", adopted)
        monkeypatch.setattr(manager, "spawn_browser", launch)
        monkeypatch.setattr(manager, "get_tab", no_tab)
        monkeypatch.setattr(manager, "get_spawn_diagnostics", diagnostics)

    async def spawn(self, **kwargs) -> dict:
        kwargs.setdefault("session", "fleet")
        return await self._call_tool(
            self._srv, "spawn_browser", headless=True, sandbox=False, **kwargs
        )

    async def call(self, name: str, **kwargs):
        return await self._call_tool(self._srv, name, **kwargs)


@pytest.fixture()
def spawns(monkeypatch, call_tool, patched_server, tmp_session_root):
    def build(manager: BrowserManager, fleet: Path) -> _Spawns:
        return _Spawns(monkeypatch, call_tool, patched_server, manager, fleet)

    return build


# ---------------------------------------------------------------------------
# 1. closed by one chat, spawned by another
# ---------------------------------------------------------------------------


class TestAnotherChatClosedIt:
    async def test_the_next_spawn_relaunches_it_on_the_same_directory(
        self, spawns, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        _running(manager, "i-old", fleet)
        s = spawns(manager, fleet)

        assert await manager.close_instance("i-old") is True
        answer = await s.spawn()

        assert answer["instance_id"] == "i-1", "a NEW instance"
        assert "already_running" not in answer
        selection = answer["spawn_diagnostics"]["profile_selection"]
        assert profile_seed.same_dir(Path(selection["user_data_dir"]), fleet)
        assert not [k for k in ("walked_to", "walk_reason") if k in selection]
        assert s.walked == [None]
        assert not (tmp_session_root["sessions"] / "fleet-2").exists()
        assert (fleet / "Default" / "Network" / "Cookies").read_bytes() == b"the-login"
        assert "seed_warning" not in answer, "no copy was made"

    async def test_a_later_spawn_is_handed_the_relaunched_browser(
        self, spawns, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        s = spawns(manager, fleet)

        first = await s.spawn()
        second = await s.spawn()

        assert second["instance_id"] == first["instance_id"]
        assert second["already_running"] is True
        assert s.launches == ["i-1"]


# ---------------------------------------------------------------------------
# 2. the browser died with nobody closing it
# ---------------------------------------------------------------------------


class TestTheBrowserDiedOnItsOwn:
    async def test_the_dead_instance_is_not_handed_back(self, spawns, tmp_session_root):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        _running(manager, "i-dead", fleet, alive=False)
        s = spawns(manager, fleet)

        answer = await s.spawn()

        assert answer["instance_id"] == "i-1"
        assert "already_running" not in answer
        assert _on(manager, fleet) == ["i-1"], "the corpse was not discarded"
        assert s.walked == [None]
        assert not (tmp_session_root["sessions"] / "fleet-2").exists()

    async def test_the_lock_files_a_death_leaves_do_not_refuse_or_walk_it(
        self, tmp_session_root
    ):
        """What Chrome leaves in a killed profile: ``lockfile`` on Windows (the
        kernel normally deletes it; a power loss does not), and on POSIX a
        ``SingletonLock`` symlink to ``<host>-<pid>`` of a pid that is gone, and
        a dangling ``SingletonSocket``. None is a holder: the process table is
        the witness, and the lock only counts while its pid lives."""
        fleet = await _fleet(tmp_session_root)
        (fleet / "lockfile").write_bytes(b"")
        if os.name != "nt":
            dead = 2**22 + 12345  # beyond any default pid_max
            (fleet / profile_lock.LOCK_NAME).symlink_to(f"somehost-{dead}")
            (fleet / "SingletonSocket").symlink_to(str(fleet.parent / "gone" / "S"))

        assert profile_lock.profile_hold(fleet, lambda *_a, **_k: set()) is None
        selection = await clone_storage.resolve_profile_selection(
            clone_storage.require_allowed_user_data_dir(None, "fleet")
        )
        assert profile_seed.same_dir(Path(selection["user_data_dir"]), fleet)
        assert "walked_to" not in selection
        assert not (tmp_session_root["sessions"] / "fleet-2").exists()


# ---------------------------------------------------------------------------
# 3. the chat holding the OLD instance id
# ---------------------------------------------------------------------------


class TestTheOldInstanceIdSaysHowToGetBack:
    async def test_after_a_close_by_another_chat(self, spawns, tmp_session_root):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        _running(manager, "i-old", fleet)
        s = spawns(manager, fleet)
        await manager.close_instance("i-old")

        with pytest.raises(InstanceNotFoundError) as error:
            await s.call("navigate", instance_id="i-old", url="https://example.test/")

        text = str(error.value)
        assert text.startswith("Instance not found: i-old")
        assert RELAUNCH in text
        assert "login" in text

    async def test_after_the_browser_exited(self, spawns, tmp_session_root):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        _running(manager, "i-dead", fleet, alive=False)
        s = spawns(manager, fleet)
        await manager.list_instances()  # the discard every liveness check does

        with pytest.raises(InstanceNotFoundError) as error:
            await s.call("navigate", instance_id="i-dead", url="https://example.test/")

        assert RELAUNCH in str(error.value)

    def test_it_is_still_the_one_error_convention(self):
        assert issubclass(InstanceNotFoundError, ToolError)
        assert isinstance(tool_errors.instance_not_found("x"), InstanceNotFoundError)

    def test_an_id_never_held_keeps_the_bare_message(self):
        assert str(tool_errors.instance_not_found("typo")) == "Instance not found: typo"

    async def test_only_a_named_session_is_offered_a_relaunch(self, tmp_session_root):
        """A disposable clone is gone for good and the shared profile is nobody's
        to ask for by name (a chat wanting a browser calls ``spawn_browser()``):
        both keep the bare message."""
        auto = tmp_session_root["sessions"] / "sess-0123456789ab"
        auto.mkdir()
        profile_seed.write_marker(
            auto,
            source=tmp_session_root["master"],
            source_kind="master-snapshot",
            seeded_from="default",
        )
        assert profile_seed.is_auto(auto)
        for iid, directory in {
            "i-auto": auto,
            "i-master": clone_storage.master_profile_dir(),
            "i-path": tmp_session_root["sessions"].parent / "elsewhere",
        }.items():
            tool_errors.remember_departed(
                iid, {"options": SimpleNamespace(user_data_dir=str(directory))}
            )
            assert str(tool_errors.instance_not_found(iid)) == (
                f"Instance not found: {iid}"
            )

    async def test_a_named_session_gets_its_own_name(self, tmp_session_root):
        directory = tmp_session_root["sessions"] / "my-shop"
        directory.mkdir()
        tool_errors.remember_departed(
            "i-1", {"options": SimpleNamespace(user_data_dir=str(directory))}
        )

        assert 'spawn_browser(session="my-shop")' in str(
            tool_errors.instance_not_found("i-1")
        )

    async def test_the_memory_is_bounded_oldest_first(self, tmp_session_root):
        directory = str(tmp_session_root["sessions"] / "fleet")
        data = {"options": SimpleNamespace(user_data_dir=directory)}
        for n in range(tool_errors._DEPARTED_KEPT + 5):
            tool_errors.remember_departed(f"i-{n}", data)

        assert len(tool_errors._departed) == tool_errors._DEPARTED_KEPT
        assert "i-0" not in tool_errors._departed
        assert f"i-{tool_errors._DEPARTED_KEPT + 4}" in tool_errors._departed

    def test_an_instance_without_options_is_simply_not_remembered(self):
        tool_errors.remember_departed("i-bare", {})
        tool_errors.remember_departed("i-none", {"options": SimpleNamespace()})

        assert tool_errors._departed == {}


# ---------------------------------------------------------------------------
# 4. two chats ask at once
# ---------------------------------------------------------------------------


class TestTwoChatsAskAtOnce:
    async def test_one_browser_is_launched(self, spawns, tmp_session_root):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        s = spawns(manager, fleet)

        first, second = await asyncio.gather(s.spawn(), s.spawn())

        assert s.launches == ["i-1"], f"launched {s.launches}"
        assert first["instance_id"] == second["instance_id"] == "i-1"
        assert sorted(bool(a.get("already_running")) for a in (first, second)) == [
            False,
            True,
        ]
        assert _on(manager, fleet) == ["i-1"]
        assert not (tmp_session_root["sessions"] / "fleet-2").exists()

    async def test_after_a_close_too(self, spawns, tmp_session_root):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        _running(manager, "i-old", fleet)
        s = spawns(manager, fleet)
        await manager.close_instance("i-old")

        answers = await asyncio.gather(s.spawn(), s.spawn(), s.spawn())

        assert len(s.launches) == 1
        assert len({a["instance_id"] for a in answers}) == 1

    async def test_a_failed_launch_does_not_wedge_the_gate(
        self, spawns, tmp_session_root, monkeypatch
    ):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        s = spawns(manager, fleet)
        real_launch = manager.spawn_browser
        attempts = []

        async def flaky(options):
            attempts.append(1)
            if len(attempts) <= 3:  # the whole retry budget of the first spawn
                raise RuntimeError("chrome would not start")
            return await real_launch(options)

        monkeypatch.setattr(manager, "spawn_browser", flaky)

        with pytest.raises(ToolError, match="Failed to spawn browser"):
            await asyncio.wait_for(s.spawn(), 10)
        again = await asyncio.wait_for(s.spawn(), 10)

        assert again["instance_id"] == "i-1"

    async def test_spawns_of_different_sessions_do_not_wait_on_each_other(
        self, spawns, tmp_session_root, monkeypatch
    ):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        s = spawns(manager, fleet)
        started = asyncio.Event()
        release = asyncio.Event()
        real_launch = manager.spawn_browser

        async def slow(options):
            if profile_seed.same_dir(Path(options.user_data_dir), fleet):
                started.set()
                await release.wait()
            return await real_launch(options)

        monkeypatch.setattr(manager, "spawn_browser", slow)
        first = asyncio.create_task(s.spawn())
        await started.wait()

        second = await asyncio.wait_for(s.spawn(session="other"), 10)
        release.set()
        await first

        assert profile_seed.same_dir(
            Path(second["spawn_diagnostics"]["profile_selection"]["user_data_dir"]),
            tmp_session_root["sessions"] / "other",
        )


def _another_chat(coro):
    """*coro* as its own task in a context that holds nothing, which is what a
    second chat's tool call is. (A plain ``create_task`` copies the CALLER's
    context, and a gate's holders pass their hold on to the tasks they start.)"""
    return asyncio.create_task(coro, context=contextvars.Context())


class TestTheGate:
    @pytest.fixture()
    def gate(self):
        from stealth_chrome_devtools_mcp.embedded import directory_gate

        return directory_gate

    async def test_two_holders_of_one_directory_take_turns(self, gate, tmp_path):
        order: list[str] = []

        async def holder(name: str) -> None:
            held = await gate.hold(str(tmp_path))
            order.append(f"{name}+")
            await asyncio.sleep(0.02)
            order.append(f"{name}-")
            held.release()

        await asyncio.gather(holder("a"), holder("b"))

        assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])

    def test_one_directory_has_one_gate_whatever_its_spelling(self, gate, tmp_path):
        assert gate.gate_for(str(tmp_path)) is gate.gate_for(str(tmp_path) + os.sep)
        assert gate.gate_for(str(tmp_path)) is gate.gate_for(str(tmp_path / "x" / ".."))
        assert gate.gate_for(str(tmp_path)) is not gate.gate_for(str(tmp_path / "x"))

    async def test_a_spawn_naming_nothing_waits_for_nobody(self, gate):
        first = await gate.hold(None)
        second = await asyncio.wait_for(gate.hold(None), 1)
        first.release()
        second.release()

    async def test_the_holder_may_ask_again(self, gate, tmp_path):
        """``spawn_browser`` holds it across the launch and
        ``adopt_held_profile`` asks inside that: the same context, not a deadlock."""
        outer = await gate.hold(str(tmp_path))
        async with gate.gate_for(str(tmp_path)):
            pass
        waiter = _another_chat(gate.hold(str(tmp_path)))
        await asyncio.sleep(0.05)
        assert not waiter.done(), "the inner release let the outer hold go"
        outer.release()
        (await asyncio.wait_for(waiter, 1)).release()

    async def test_a_task_the_holder_starts_inherits_the_hold(self, gate, tmp_path):
        outer = await gate.hold(str(tmp_path))

        async def inside() -> None:
            async with gate.gate_for(str(tmp_path)):
                pass

        await asyncio.wait_for(asyncio.create_task(inside()), 1)
        outer.release()
        (await asyncio.wait_for(gate.hold(str(tmp_path)), 1)).release()

    async def test_a_cancelled_waiter_leaves_the_gate_usable(self, gate, tmp_path):
        held = await gate.hold(str(tmp_path))
        waiter = _another_chat(gate.hold(str(tmp_path)))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        held.release()

        (await asyncio.wait_for(gate.hold(str(tmp_path)), 1)).release()


# ---------------------------------------------------------------------------
# 5. an advisory lock held at close time
# ---------------------------------------------------------------------------


class TestAnAdvisoryLockDoesNotBlockTheRelaunch:
    async def test_it_relaunches_and_the_lock_is_still_the_holders(
        self, spawns, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        manager = BrowserManager()
        _running(manager, "i-old", fleet)
        s = spawns(manager, fleet)
        await s.call("acquire_session_lock", owner="chat-a", lease_seconds=300)
        await manager.close_instance("i-old")

        answer = await s.spawn()
        status = await s.call("get_session_lock_status")

        assert answer["instance_id"] == "i-1"
        assert "already_running" not in answer
        # The lock is on the session NAME, not on a browser: it outlives the
        # close and the relaunch, and says whose turn it still is.
        assert status["locked"] is True
        assert status["holder"] == "chat-a"
        # ...and the browser handed to the NEXT asker carries it, as before.
        again = await s.spawn()
        assert again["already_running"] is True
        assert again["session_lock"]["holder"] == "chat-a"
