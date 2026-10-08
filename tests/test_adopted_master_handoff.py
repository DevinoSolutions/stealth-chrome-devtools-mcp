"""F-950 — a browser THIS backend adopted is ours, not a sibling's.

The owner's report: a clone made today carried every login from before the
backend started and none made since. Measured on the live machine (2026-10-08),
the shared ``default`` browser (pid 38852) was RE-ATTACHED at startup by backend
84488, which re-stamped the browser's record entry with ``owner_pid: 84488`` —
ITSELF. Every later unnamed spawn then asked ``held_by`` about the shared
directory, read that entry, saw a LIVE owner and raised ``Refused`` ("a live
backend of ours already owns it ... Stop that backend first"), about itself.
The clone got the file copy of the stale seed and none of the live jar.

Every node is hermetic: a ``tmp_path`` record, the process table and the CDP
door faked, ``tmp_session_root`` for every directory. The ownership witness is
injected as "only THIS process is a live backend", which is the one fact the
in-process tests that existed before this file could not express: they all
injected a DEAD owner, so the live-owner-is-me branch was never entered.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fakes import FakeBrowser, FakeTab, held_profile
from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_reattach,
    cdp_attach,
    clone_storage,
    cookie_handoff,
    profile_lock,
)
from stealth_chrome_devtools_mcp.embedded import browser_pid_registry as registry
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

#: A pid that is really alive (this one), because ``process_exit.browser_is_alive``
#: asks psutil about a browser whose handle is gone. A made-up pid reads as a
#: dead Chrome and ``list_instances`` discards the instance, which would hide the
#: very predicate these pins are about.
CHROME_PID = os.getpid()
SIBLING = 4_000_001
PORT = 9223


def _cleanup(tmp_path: Path, *, live_backends: set[int]) -> ProcessCleanup:
    """A ``ProcessCleanup`` on a ``tmp_path`` record whose ownership witness
    says exactly *live_backends* are backends of ours that are running."""
    cleanup = ProcessCleanup.__new__(ProcessCleanup)
    cleanup.pid_file = tmp_path / "browser_pids.json"
    cleanup.tracked_pids = set()
    cleanup.browser_processes = {}
    cleanup.orphan_profile_max_age_seconds = 0
    cleanup._init_time = 1700000100.0
    cleanup._owner_backend_alive = lambda pid, _created: pid in live_backends  # type: ignore[method-assign]
    return cleanup


def _argv(directory: str) -> list[str]:
    return [
        "chrome.exe",
        f"--user-data-dir={directory}",
        f"--remote-debugging-port={PORT}",
    ]


def _witnesses(directory: str):
    """The process table: one browser holds *directory*."""
    return (
        patch(
            "stealth_chrome_devtools_mcp.embedded.profile_lock.profile_hold",
            return_value=profile_lock.Hold(
                CHROME_PID, "process", members=(CHROME_PID,)
            ),
        ),
        patch.object(
            browser_cmdline,
            "arguments",
            side_effect=lambda pid: _argv(directory) if pid == CHROME_PID else [],
        ),
        patch.object(browser_cmdline, "debug_port", return_value=PORT),
        patch.object(browser_cmdline, "dead_local_proxy", return_value=None),
        patch.object(browser_cmdline, "is_headless", return_value=False),
        patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
    )


async def _adopt_as_the_startup_pass_does(
    manager: BrowserManager, cleanup: ProcessCleanup, directory: str
) -> str:
    """Re-attach the shared browser through the real adoption, as ``run`` does."""
    browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=FakeTab())
    a, b, c, d, e, f = _witnesses(directory)
    with a, b, c, d, e, f, patch.object(cdp_attach, "attach", return_value=browser):
        held = await browser_reattach.adopt_held_profile(manager, cleanup, directory)
    assert held.instance_id is not None, held.declined
    return held.instance_id


class TestAnEntryOwnedByThisBackendIsOurs:
    """``held_by`` is the second entry point into the one adoption rule, and it
    read a live owner as a sibling without asking WHO the owner was."""

    def _held(self, entries, *, live_backends):
        a, b, c, d, _e, _f = _witnesses(r"C:\profiles\master")
        with a, b, c, d:
            return browser_reattach.held_by(
                r"C:\profiles\master",
                read_entries=lambda: entries,
                owner_alive=lambda pid, _created: pid in live_backends,
                live_pids=lambda _dir: {CHROME_PID},
                new_instance_id="i-new",
            )

    @staticmethod
    def _entry(owner_pid):
        return {
            "pid": CHROME_PID,
            "create_time": 1700000000.0,
            "user_data_dir": r"c:\profiles\master",
            "uses_custom_data_dir": True,
            "auto_clone": False,
            "cdp_port": PORT,
            "timestamp": 0,
            "owner_pid": owner_pid,
            "owner_create_time": None,
        }

    def test_an_entry_this_process_owns_is_adopted_under_its_own_id(self):
        """The re-attach re-stamped the owner to US. Refusing it told the caller
        to stop the backend they were talking to."""
        found = self._held(
            {"i-recorded": self._entry(os.getpid())}, live_backends={os.getpid()}
        )

        assert found is not None
        assert found.instance_id == "i-recorded"
        assert found.pid == CHROME_PID

    def test_an_entry_a_sibling_backend_owns_is_still_refused(self):
        """F-886, kept: two backends driving one Chrome is the harm."""
        with pytest.raises(browser_reattach.Refused, match="live backend of ours"):
            self._held({"i-theirs": self._entry(SIBLING)}, live_backends={SIBLING})

    def test_our_pid_on_a_dead_owner_is_not_a_live_backend_at_all(self):
        """An entry stamped with a pid this process now holds, by a backend that
        is gone (``owner_alive`` says no), is simply orphaned and adoptable."""
        found = self._held({"i-old": self._entry(os.getpid())}, live_backends=set())

        assert found is not None
        assert found.instance_id == "i-old"

    def test_the_identity_witness_decides_whether_the_pid_is_us(self):
        """``held_by_sibling`` must NOT collapse into "same pid means us":
        ``owner_alive`` is asked twice, once as the reapable rule and once as
        the identity check, and a live owner it vouches for the first time but
        not as this process is somebody else's."""
        entry = self._entry(os.getpid())
        answers = iter([True, False])

        assert registry.held_by_sibling(entry, lambda _p, _c: next(answers)) is True

    def test_everyone_else_keeps_the_reapable_rule(self):
        entry = self._entry(SIBLING)
        assert registry.held_by_sibling(entry, lambda p, _c: p == SIBLING) is True
        assert registry.held_by_sibling(entry, lambda _p, _c: False) is False
        assert registry.held_by_sibling({"pid": 1}, lambda _p, _c: True) is False


class TestAnAdoptedMasterIsOurs:
    async def test_the_adoption_re_stamps_the_record_with_this_backend(
        self, tmp_session_root, tmp_path
    ):
        """The premise the live log shows, kept as a node so the cause cannot
        drift from it: after adoption the record names US as owner."""
        master = str(tmp_session_root["master"])
        cleanup = _cleanup(tmp_path, live_backends={os.getpid()})
        manager = BrowserManager()

        instance_id = await _adopt_as_the_startup_pass_does(manager, cleanup, master)

        entry = registry.read_entries(cleanup.pid_file)[instance_id]
        assert entry["owner_pid"] == os.getpid()

    async def test_an_adopted_master_is_reported_as_the_default_session(
        self, tmp_session_root, tmp_path
    ):
        """``close_instance`` reads this role to decide whether closing the
        browser refreshes the seed. It was `explicit` for every adopted browser,
        the shared one included."""
        master = str(tmp_session_root["master"])
        cleanup = _cleanup(tmp_path, live_backends={os.getpid()})
        manager = BrowserManager()

        instance_id = await _adopt_as_the_startup_pass_does(manager, cleanup, master)

        selection = manager._spawn_diagnostics[instance_id]["profile_selection"]
        assert selection["profile_role"] == "default"

    async def test_an_adopted_named_profile_stays_explicit(
        self, tmp_session_root, tmp_path
    ):
        named = str(tmp_session_root["sessions"] / "work")
        cleanup = _cleanup(tmp_path, live_backends={os.getpid()})
        manager = BrowserManager()

        instance_id = await _adopt_as_the_startup_pass_does(manager, cleanup, named)

        selection = manager._spawn_diagnostics[instance_id]["profile_selection"]
        assert selection["profile_role"] == "explicit"

    async def test_closing_an_adopted_master_refreshes_the_seed(
        self, tmp_session_root, tmp_path, call_tool, patched_server, monkeypatch
    ):
        """Through the real ``close_instance`` tool: the refresh is asked for."""
        master = str(tmp_session_root["master"])
        cleanup = _cleanup(tmp_path, live_backends={os.getpid()})
        manager = BrowserManager()
        instance_id = await _adopt_as_the_startup_pass_does(manager, cleanup, master)
        asked: list[str] = []

        def refresh(reason):
            asked.append(reason)
            return {"seed_refreshed": True}

        async def closed(_instance_id):
            return True

        monkeypatch.setattr(clone_storage, "_refresh_master_snapshot_if_safe", refresh)
        monkeypatch.setattr(manager, "close_instance", closed)
        srv = patched_server(browser_manager=manager)

        answer = await call_tool(srv, "close_instance", instance_id=instance_id)

        assert asked == ["after-default-close"]
        assert answer["seed_refreshed"] is True

    async def test_the_driven_snapshot_sees_the_adopted_master(
        self, tmp_session_root, tmp_path
    ):
        """The witness the resolver's hand-off is gated on, over the lowercased
        path the record stores."""
        master = tmp_session_root["master"]
        cleanup = _cleanup(tmp_path, live_backends={os.getpid()})
        manager = BrowserManager()
        instance_id = await _adopt_as_the_startup_pass_does(
            manager, cleanup, str(master).lower()
        )

        driven = await cookie_handoff.driven_profiles(manager)

        assert driven.instance(master) == instance_id


class TestASpawnWhileWeDriveTheAdoptedMaster:
    """The production state: this backend adopted the shared browser at startup,
    the record names it as owner and the owner is alive."""

    async def _adopted(self, tmp_session_root, tmp_path):
        master = tmp_session_root["master"]
        cleanup = _cleanup(tmp_path, live_backends={os.getpid()})
        manager = BrowserManager()
        instance_id = await _adopt_as_the_startup_pass_does(
            manager, cleanup, str(master)
        )
        held_profile(master)
        return master, cleanup, manager, instance_id

    async def test_an_unnamed_spawn_is_handed_the_live_jar_not_told_to_stop(
        self, tmp_session_root, tmp_path
    ):
        """End to end through the two real halves the spawn path composes: the
        re-attach question, then the resolver with the SAME driven witness. The
        answer is a clone seeded from the master's LIVE jar, and nothing was
        declined."""
        master, cleanup, manager, _ = await self._adopted(tmp_session_root, tmp_path)

        a, b, c, d, e, f = _witnesses(str(master))
        with a, b, c, d, e, f:
            held = await browser_reattach.adopt_held_profile(
                manager, cleanup, str(master), reuse_ours=False
            )
        driven = await cookie_handoff.driven_profiles(manager)
        selection = await clone_storage.resolve_profile_selection(
            None, seed_from=None, driven=driven.holds
        )

        assert held.declined is None
        assert held.instance_id is None
        assert selection["profile_role"] == "clone"
        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)

    async def test_a_spawn_naming_the_shared_session_gets_the_running_browser(
        self, tmp_session_root, tmp_path
    ):
        """``session="default"`` names a directory, and the browser already open
        on it is the answer (F-888). It was a decline about ourselves."""
        master, cleanup, manager, instance_id = await self._adopted(
            tmp_session_root, tmp_path
        )

        a, b, c, d, e, f = _witnesses(str(master))
        with a, b, c, d, e, f:
            held = await browser_reattach.adopt_held_profile(
                manager, cleanup, str(master)
            )

        assert held.declined is None
        assert held.instance_id == instance_id

    async def test_a_sibling_owned_master_is_still_never_adopted(
        self, tmp_session_root, tmp_path
    ):
        """F-886 through the same entry point: the record's live owner is some
        OTHER backend, so the spawn is told, and nothing is adopted."""
        master = tmp_session_root["master"]
        cleanup = _cleanup(tmp_path, live_backends={SIBLING})
        registry.update_entries(
            cleanup.pid_file,
            lambda recorded: {
                **recorded,
                "i-theirs": registry.with_owner(
                    registry.new_entry(
                        CHROME_PID,
                        create_time=None,
                        user_data_dir=str(master),
                        uses_custom_data_dir=True,
                        auto_clone=False,
                        cdp_port=PORT,
                    ),
                    SIBLING,
                    None,
                ),
            },
        )
        held_profile(master)

        a, b, c, d, e, f = _witnesses(str(master))
        with a, b, c, d, e, f:
            held = await browser_reattach.adopt_held_profile(
                BrowserManager(), cleanup, str(master)
            )

        assert held.instance_id is None
        assert held.declined is not None
        assert "live backend of ours" in held.declined
