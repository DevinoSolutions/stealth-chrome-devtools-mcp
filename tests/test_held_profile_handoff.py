"""F-914 / F-915 / F-920 — a HELD profile hands its cookies over, or refuses.

The finding is the owner's "the master profile was erased / I have to set up
the credentials again", and on disk it is not a deletion: it is SUBSTITUTION.
Ask for the shared session while anything holds it and 2.1.12 hands back a
fresh clone of the seed; ask for a NAMED session while anything holds it and it
hands back ``<name>-2``, seeded from that same seed. Both report success, both
are logged out, and 17 walked directories on the owner's machine — one at
``-22`` — count how often the second one happened for ONE session name.

The rule these pins hold to is the owner's, and it has exactly two outcomes:

1. **We can reach the holder** — it is a browser THIS backend drives — so the
   new session is seeded from the holder's LIVE jar over CDP
   (``cookie_handoff``, the mechanism F-898 built for the source side of the
   same question). The answer says which holder it came from.
2. **We cannot** — another backend's Chrome, or the human's own — so the spawn
   is REFUSED BY NAME. Never a silent substitute, never a silent walk.

Every node is hermetic: no Chrome, no psutil hold beyond the SingletonLock
``fakes.held_profile`` writes, and ``tmp_session_root`` sets
``STEALTH_MCP_BROWSER_SESSION_ROOT`` EXPLICITLY, so nothing here can reach the
real session root or the real ``~/.stealth-mcp``.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeBrowser, FakeBrowserManager, held_profile
from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_reattach,
    clone_storage,
    profile_seed,
    profile_source,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.embedded.tool_sections import browser_management

#: A byte string no fixture writes, so finding it in the walked session is
#: proof the copy came from the HELD directory and not from the shared seed.
HELD_COOKIE = b"held-session-cookie-jar"

#: What a CLOSED leftover from an EARLIER walk holds. Distinct from both
#: ``HELD_COOKIE`` and the seed, so a session carrying it is provably a third
#: identity — neither the holder's jar nor a fresh copy of anything.
STALE_COOKIE = b"stale-walked-to-leftover"

#: The profile-relative jar. Spelled here rather than read out of
#: ``LOGIN_WITNESSES``: a pin that derived its fixture from the list it checks
#: would be comparing the code to itself.
COOKIE_JAR = "Default/Network/Cookies"


def _driving(*profiles: Path):
    """The F-898 witness, answering True for exactly these directories.

    ``profile_seed.same_dir`` and not ``==`` because the product compares that
    way too — a caller names a session and the resolver holds an absolute path.
    """

    def driven(profile: Path) -> bool:
        return any(profile_seed.same_dir(profile, known) for known in profiles)

    return driven


async def _selection(
    *,
    session: str | None = None,
    seed_from: str | None = None,
    driven=profile_source.NOTHING_DRIVEN,
) -> dict:
    """What a spawn resolves to, composed exactly as ``spawn_browser`` does:
    the profile gate, the seed gate against its answer, then the ONE resolver.

    The RAW selection, not ``_public_profile_selection``'s — these pins are
    about an internal instruction (``LIVE_SEED_KEY``) as well as a reported
    field, and the boundary between the two has its own node below.
    """
    landed = clone_storage.require_allowed_user_data_dir(None, session)
    seed = clone_storage.require_allowed_seed_from(seed_from, landed, driven=driven)
    return await clone_storage.resolve_profile_selection(
        landed, seed_from=seed, driven=driven
    )


async def _session_with_a_login(name: str, dirs: dict) -> Path:
    """Create session *name* and put a recognisable login in its jar."""
    await _selection(session=name)
    directory = dirs["sessions"] / name
    jar = directory / COOKIE_JAR
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(HELD_COOKIE)
    return directory


def _sessions_on_disk(dirs: dict) -> set[str]:
    return {child.name for child in dirs["sessions"].iterdir() if child.is_dir()}


# ---------------------------------------------------------------------------
# F-915 — a held NAMED session
# ---------------------------------------------------------------------------


class TestAHeldNamedSession:
    async def test_a_holder_we_drive_hands_its_jar_over(self, tmp_session_root):
        """The caller asked for ``work``'s cookies. A browser we drive has it
        open, so the jar comes out of that browser over CDP — the answer
        carries the live source and names the holder it came from."""
        work = await _session_with_a_login("work", tmp_session_root)
        held_profile(work)

        selection = await _selection(session="work", driven=_driving(work))

        assert selection[clone_storage.LIVE_SEED_KEY] == str(work)
        assert selection["handed_over_from"] == "work"
        assert selection["walked_to"] == selection["user_data_dir"]

    async def test_a_hand_over_never_lands_on_a_leftover_walk_target(
        self, tmp_session_root
    ):
        """A CLOSED ``work-2`` an earlier walk left behind is not somewhere a
        hand-over may land.

        The walk target came from ``_next_available_explicit_dir``, which skips
        a candidate that is BUSY and never one that merely EXISTS — while
        ``resolve_profile_selection`` gates the copy AND the ``LIVE_SEED_KEY``
        stamp on the target not existing. So the leftover was handed back
        verbatim: nothing copied, nothing handed over, and the caller got
        whatever that directory last held. A STALE THIRD identity — neither the
        holder's jar nor a fresh copy of anything — under a warning that says
        its cookies came across. That is F-915's substitution with one extra
        step, and it is not hypothetical: the finding's own 17 leftover
        directories, one at ``-22``, are exactly the population the first walk
        after this lane ships would land on.
        """
        work = await _session_with_a_login("work", tmp_session_root)
        stale = tmp_session_root["sessions"] / "work-2"
        stale_jar = stale / COOKIE_JAR
        stale_jar.parent.mkdir(parents=True, exist_ok=True)
        stale_jar.write_bytes(STALE_COOKIE)
        held_profile(work)

        selection = await _selection(session="work", driven=_driving(work))
        landed = Path(selection["user_data_dir"])

        assert landed != stale, f"the hand-over reused the leftover {stale.name}"
        assert selection[clone_storage.LIVE_SEED_KEY] == str(work)
        assert selection["handed_over_from"] == "work"
        assert (landed / COOKIE_JAR).read_bytes() == HELD_COOKIE
        assert stale_jar.read_bytes() == STALE_COOKIE, "the leftover was written to"

    async def test_the_copy_comes_from_the_held_session_not_the_seed(
        self, tmp_session_root
    ):
        """The half the CDP jar cannot carry — everything on disk Chrome does
        not hold open — comes from the HELD directory. Seeding the walked
        session from the shared seed instead is the whole of F-915: a different
        identity with none of the caller's state."""
        work = await _session_with_a_login("work", tmp_session_root)
        held_profile(work)

        selection = await _selection(session="work", driven=_driving(work))

        walked = Path(selection["user_data_dir"])
        assert walked != work
        assert (walked / COOKIE_JAR).read_bytes() == HELD_COOKIE

    async def test_a_holder_we_cannot_reach_is_refused_by_name(self, tmp_session_root):
        """No CDP connection of ours reaches that browser, so there is no jar
        to hand over and a copy of a live profile carries no cookies at all.
        Walking would hand back a logged-out stranger under the caller's own
        session name."""
        work = await _session_with_a_login("work", tmp_session_root)
        held_profile(work)

        with pytest.raises(ToolError) as refusal:
            await _selection(session="work")

        assert "work" in str(refusal.value)

    async def test_the_refusal_names_the_holder(self, tmp_session_root):
        work = await _session_with_a_login("work", tmp_session_root)
        held_profile(work)

        with pytest.raises(ToolError) as refusal:
            await _selection(session="work")

        hold = clone_storage._profile_hold(work)
        assert hold is not None and hold.pid is not None
        assert str(hold.pid) in str(refusal.value)

    async def test_the_refusal_names_no_path(self, tmp_session_root):
        """F-869/F-877 discipline: a profile path names the operating user, and
        this message reaches the client, the durable log and Sentry at once."""
        work = await _session_with_a_login("work", tmp_session_root)
        held_profile(work)

        with pytest.raises(ToolError) as refusal:
            await _selection(session="work")

        message = str(refusal.value)
        assert str(tmp_session_root["root"]) not in message
        assert str(work) not in message

    async def test_a_refusal_creates_nothing(self, tmp_session_root):
        """A half-made session the caller must now clean up is a second harm —
        ``profile_source.seed_source``'s rule for the source side."""
        work = await _session_with_a_login("work", tmp_session_root)
        held_profile(work)
        before = _sessions_on_disk(tmp_session_root)

        with pytest.raises(ToolError):
            await _selection(session="work")

        assert _sessions_on_disk(tmp_session_root) == before

    async def test_an_unheld_session_is_opened_exactly_as_before(
        self, tmp_session_root
    ):
        """The rule is about a HELD profile only: nothing about an ordinary
        named spawn moves, and it never consults the hand-off at all."""
        work = await _session_with_a_login("work", tmp_session_root)

        selection = await _selection(session="work")

        assert selection["user_data_dir"] == str(work)
        assert "walked_to" not in selection
        assert clone_storage.LIVE_SEED_KEY not in selection


# ---------------------------------------------------------------------------
# F-914 — a held SHARED session, which is what an unnamed spawn asks for
# ---------------------------------------------------------------------------


class TestTheHeldSharedSession:
    async def test_a_holder_we_drive_hands_the_shared_jar_over(self, tmp_session_root):
        """The shared session is the one a human logs in to, and while it is
        open the seed is never refreshed — so the clone an unnamed spawn gets
        is as old as the last time that window was closed. The live jar is what
        closes that gap, over a connection this process already holds."""
        master = tmp_session_root["master"]
        held_profile(master)

        selection = await _selection(driven=_driving(master))

        assert selection["profile_role"] == "clone"
        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)
        assert selection["handed_over_from"] == profile_seed.DEFAULT_SESSION

    async def test_the_copy_still_comes_from_the_closed_seed(self, tmp_session_root):
        """F-893's argument is untouched: a file copy must come from a
        directory nothing is writing to. The stale seed plus a current jar is
        strictly better than the stale seed, and the ordering is what makes it
        true — the hand-off writes last."""
        master = tmp_session_root["master"]
        held_profile(master)

        selection = await _selection(driven=_driving(master))

        assert selection["clone_source"] == "default-seed"
        assert selection["clone_source_path"] == str(tmp_session_root["snapshot"])

    async def test_a_holder_we_cannot_reach_is_refused_by_name(self, tmp_session_root):
        """2.1.12 answered this with a fresh clone of the seed and said so only
        in ``profile_role`` — a field a caller has to go looking for. That is
        F-914, and it is the whole of the owner's complaint."""
        held_profile(tmp_session_root["master"])

        with pytest.raises(ToolError) as refusal:
            await _selection()

        assert profile_seed.DEFAULT_SESSION in str(refusal.value)

    async def test_the_refusal_names_no_path(self, tmp_session_root):
        master = tmp_session_root["master"]
        held_profile(master)

        with pytest.raises(ToolError) as refusal:
            await _selection()

        message = str(refusal.value)
        assert str(tmp_session_root["root"]) not in message
        assert str(master) not in message

    async def test_an_unheld_shared_session_is_opened_exactly_as_before(
        self, tmp_session_root
    ):
        selection = await _selection()

        assert selection["profile_role"] == profile_seed.DEFAULT_SESSION
        assert selection["user_data_dir"] == str(tmp_session_root["master"])
        assert clone_storage.LIVE_SEED_KEY not in selection

    async def test_orphaned_children_of_a_closed_browser_do_not_refuse(
        self, tmp_session_root, monkeypatch
    ):
        """F-931(b) at the surface F-914 refuses from.

        ``close_instance`` waits for and kills the BROWSER only — a ``--type=``
        child is deliberately never waited on (``process_exit.browser_pid``) —
        so for a window after a close the shared profile's tree still has
        members in it, and under the shipped witness that is enough to refuse
        the very next unnamed spawn about a profile whose browser has gone.

        The release gate showed that refusal one test after a close
        (``integration (Windows/X64)`` run 35689647688) but never said whether
        the pid it named was the browser or a child — so this node is the proof
        and the pids below are deliberately not the gate's.
        """
        master = tmp_session_root["master"]
        children = {4201: 4201, 4202: 4202}
        monkeypatch.setattr(
            clone_storage.process_cleanup,
            "_get_browser_pids_for_profile",
            lambda _dir: set(children),
        )
        monkeypatch.setattr(
            browser_cmdline,
            "arguments",
            lambda pid: (
                ["chrome.exe", f"--user-data-dir={master}", "--type=renderer"]
                if pid in children
                else []
            ),
        )

        selection = await _selection()

        assert selection["profile_role"] == profile_seed.DEFAULT_SESSION
        assert selection["user_data_dir"] == str(master)


# ---------------------------------------------------------------------------
# F-920 — the `live-default-fallback` branch, whose comment claimed a live
# copy carries cookies
# ---------------------------------------------------------------------------


class TestTheNoSeedFallback:
    async def test_a_live_copy_is_never_taken_silently(self, tmp_session_root):
        """With no seed yet and the shared session open, the only copy
        available is of the live directory — which ``profile_copy.copy_file``'s
        own docstring says drops whatever Chrome holds open, with no way to
        enumerate the gap. The shipped comment asserted the opposite."""
        shutil.rmtree(tmp_session_root["snapshot"])
        held_profile(tmp_session_root["master"])

        with pytest.raises(ToolError):
            await _selection()

    async def test_a_driven_holder_makes_the_live_copy_honest(self, tmp_session_root):
        """Same branch, holder reachable: the copy still runs and still loses
        whatever is locked, and the cookies arrive over CDP instead."""
        master = tmp_session_root["master"]
        shutil.rmtree(tmp_session_root["snapshot"])
        held_profile(master)

        selection = await _selection(driven=_driving(master))

        assert selection["clone_source"] == "live-default-fallback"
        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)


# ---------------------------------------------------------------------------
# F-931 — the two spellings of the shared session must ask the SAME question
# ---------------------------------------------------------------------------


class TestAnUnnamedSpawnAsksTheSameQuestion:
    """``spawn_browser()`` and ``spawn_browser(session="default")`` name one
    profile, and until F-931 only the second one could re-attach to it.

    ``require_allowed_user_data_dir`` answers ``None`` when nothing was named,
    and the F-888 re-attach was gated on that answer — so the call the owner
    makes, and every integration test makes, went straight to the resolver,
    where F-914 refuses a holder we do not drive. The named spelling of the
    same directory was adopted. Two spellings of one profile with two outcomes
    is convention 4's "second way", and the one that loses is the default.

    The re-attach therefore has to be asked about the directory the SELECTION
    will land on, which for an unnamed spawn is the shared session (F-834 /
    F-896). These nodes drive the real tool and watch which directory the
    re-attach is handed, because a gate that answers correctly and a body that
    asks about something else would leave every pin above green.
    """

    def _manager(self) -> FakeBrowserManager:
        return FakeBrowserManager(
            spawn_instance=SimpleNamespace(
                instance_id="i1",
                state="active",
                headless=True,
                viewport={"width": 800, "height": 600},
            ),
            spawn_diagnostics={},
        )

    async def _spawn(
        self, call_tool, patched_server, monkeypatch, *, held, **kwargs
    ) -> tuple[list[str], list[str | None]]:
        """Drive ``spawn_browser`` with the re-attach and the resolver faked,
        and answer (directories the re-attach was asked about, directories the
        resolver was asked about)."""
        asked: list[str] = []
        resolved: list[str | None] = []

        async def fake_adopt(_manager, _cleanup, user_data_dir, **_):
            asked.append(user_data_dir)
            return held

        async def fake_resolve(user_data_dir, **_):
            resolved.append(user_data_dir)
            return {
                "user_data_dir": str(user_data_dir or "/shared"),
                "profile_role": "explicit" if user_data_dir else "clone",
                "clone_source": None,
            }

        async def fake_adopted(instance_id, _block_resources):
            return {"instance_id": instance_id, "reattached": True}

        monkeypatch.setattr(browser_reattach, "adopt_held_profile", fake_adopt)
        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        monkeypatch.setattr(
            browser_management, "_adopted_instance_record", fake_adopted
        )
        srv = patched_server(browser_manager=self._manager())
        await call_tool(srv, "spawn_browser", headless=True, sandbox=False, **kwargs)
        return asked, resolved

    @pytest.mark.parametrize(
        "kwargs", [{}, {"session": "default"}], ids=["unnamed", "default"]
    )
    async def test_both_spellings_ask_the_reattach_about_the_shared_directory(
        self, call_tool, patched_server, monkeypatch, tmp_session_root, kwargs
    ):
        asked, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            held=browser_reattach.Held(),
            **kwargs,
        )

        assert asked == [str(tmp_session_root["master"])]

    @pytest.mark.parametrize(
        "kwargs", [{}, {"session": "default"}], ids=["unnamed", "default"]
    )
    async def test_both_spellings_adopt_the_holder_instead_of_selecting(
        self, call_tool, patched_server, monkeypatch, kwargs
    ):
        """An adoption short-circuits selection, which is the whole point: the
        resolver is where F-914's refusal and F-871's walk live, and a browser
        we just re-attached to needs neither."""
        _, resolved = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            held=browser_reattach.Held(instance_id="i-adopted"),
            **kwargs,
        )

        assert resolved == []


class TestAnUnnamedSpawnIsNeverHandedOurOwnBrowser:
    """F-931's own regression, found by the release gate (PR #166,
    ``integration (macOS/ARM64)``, four tests of one shape): once an unnamed
    spawn asked the re-attach about the shared session, a SECOND unnamed spawn
    was handed the first one's running instance. ``test_close_one_keeps_others``
    spawned ``a`` and ``b``, got one browser twice, closed ``a`` and lost ``b``.

    The two spellings do NOT mean the same thing about a browser this backend
    already drives. ``session="default"`` names a directory, and the browser
    already open on it is the answer (F-888). A spawn that names nothing asks
    for a browser of its OWN — the fleet contract F-834 and F-914 build on — so
    for a holder we drive it is the resolver's job: a fresh copy with the
    holder's jar handed over. What F-931 adds for an unnamed spawn is only the
    holder we do NOT drive, the stranded one.

    Two shapes reach "ours", and both are driven here through the REAL
    ``adopt_held_profile`` and the real ``BrowserManager`` — only ``held_by``
    (the psutil walk), the resolver and the launch are faked: in-process, the
    record's owner is not a backend process, so ``held_by`` answers a candidate
    naming our own instance; in a real backend the owner IS one, so ``held_by``
    raises ``Refused`` about ourselves. And a sibling spawn IN FLIGHT has a
    Chrome on that directory before it has an instance, so no instance table
    can see it — adopting it would register one Chrome twice.
    """

    def _manager(self, master: Path, *, drives_master: bool, in_flight: int = 0):
        manager = BrowserManager()
        if drives_master:
            manager._instances["i-ours"] = {
                "browser": FakeBrowser(alive=True),
                "instance": BrowserInstance(instance_id="i-ours"),
                "options": SimpleNamespace(user_data_dir=str(master)),
            }
        manager._spawns_in_flight = in_flight
        return manager

    async def _spawn(  # noqa: PLR0913  PERMANENT(each keyword is one of the fakes this node composes — one per seam the spawn crosses)
        self,
        call_tool,
        patched_server,
        monkeypatch,
        manager,
        *,
        holder,
        **kwargs,
    ) -> tuple[dict, list, list]:
        """Drive ``spawn_browser``; answer (its reply, directories the resolver
        was asked about, adoptions the re-attach attempted)."""
        resolved: list = []
        adoptions: list = []

        def fake_held_by(*_args, **_kwargs):
            if isinstance(holder, Exception):
                raise holder
            return holder

        async def fake_adopt_one(_manager, _cleanup, candidate, **_):
            adoptions.append(candidate.instance_id)
            return candidate.instance_id

        async def fake_resolve(user_data_dir, **_):
            resolved.append(user_data_dir)
            return {
                "user_data_dir": "/a-fresh-copy",
                "profile_role": "clone",
                "clone_source": None,
            }

        async def fake_launch(_options):
            return SimpleNamespace(
                instance_id="i-new", state="active", headless=True, viewport={}
            )

        async def no_tab(_instance_id):
            return None

        async def diagnostics(_instance_id):
            return {}

        async def fake_adopted(instance_id, _block_resources):
            return {"instance_id": instance_id, "reattached": True}

        monkeypatch.setattr(browser_reattach, "held_by", fake_held_by)
        monkeypatch.setattr(browser_reattach, "_adopt_one", fake_adopt_one)
        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        monkeypatch.setattr(
            browser_management, "_adopted_instance_record", fake_adopted
        )
        monkeypatch.setattr(manager, "spawn_browser", fake_launch)
        monkeypatch.setattr(manager, "get_tab", no_tab)
        monkeypatch.setattr(manager, "get_spawn_diagnostics", diagnostics)
        srv = patched_server(browser_manager=manager)
        answer = await call_tool(
            srv, "spawn_browser", headless=True, sandbox=False, **kwargs
        )
        return answer, resolved, adoptions

    @staticmethod
    def _candidate(instance_id: str, master: Path) -> browser_reattach.Adoptable:
        return browser_reattach.Adoptable(
            instance_id=instance_id, pid=4321, user_data_dir=str(master), port=9223
        )

    async def test_our_own_browser_on_the_shared_session_is_not_the_answer(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """The CI shape: in-process, ``held_by`` names our own instance."""
        master = tmp_session_root["master"]
        answer, resolved, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(master, drives_master=True),
            holder=self._candidate("i-ours", master),
        )

        assert answer["instance_id"] == "i-new", (
            "an unnamed spawn must get a browser of its own, never the one "
            "this backend already drives on the shared session"
        )
        assert resolved == [None]

    async def test_a_refusal_about_ourselves_is_not_reported_as_a_decline(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """The production shape: the record's owner is this live backend, so
        ``held_by`` refuses — and its remedy is "stop that backend", which read
        about ourselves on every unnamed spawn is advice to kill the caller's
        own session. Nothing was declined: the resolver hands the jar over."""
        master = tmp_session_root["master"]
        answer, resolved, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(master, drives_master=True),
            holder=browser_reattach.Refused("a live backend of ours already owns it"),
        )

        assert answer["instance_id"] == "i-new"
        assert resolved == [None]
        assert "reattach_declined" not in answer["spawn_diagnostics"]

    async def test_a_sibling_spawn_in_flight_is_never_adopted(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """Its Chrome is up before its instance is registered, so the instance
        table cannot see it; the in-flight count can."""
        master = tmp_session_root["master"]
        answer, resolved, adoptions = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(master, drives_master=False, in_flight=1),
            holder=self._candidate("i-sibling", master),
        )

        assert adoptions == [], "one Chrome registered as two instances"
        assert answer["instance_id"] == "i-new"
        assert resolved == [None]

    async def test_a_stranded_holder_is_still_re_attached(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """What F-931 is FOR, kept: a browser no spawn of ours is launching
        and no instance of ours drives is adopted, not refused."""
        master = tmp_session_root["master"]
        answer, resolved, adoptions = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(master, drives_master=False),
            holder=self._candidate("i-stranded", master),
        )

        assert adoptions == ["i-stranded"]
        assert answer["instance_id"] == "i-stranded"
        assert resolved == []

    async def test_a_refusal_about_a_stranger_is_still_reported(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        master = tmp_session_root["master"]
        answer, _, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(master, drives_master=False),
            holder=browser_reattach.Refused("a sibling backend owns it"),
        )

        assert answer["spawn_diagnostics"]["reattach_declined"] == (
            "a sibling backend owns it"
        )

    async def test_the_named_spelling_keeps_f888s_answer(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """``session="default"`` names the directory, so the browser already
        open on it IS what was asked for — unchanged by this fix."""
        master = tmp_session_root["master"]
        answer, resolved, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(master, drives_master=True),
            holder=self._candidate("i-ours", master),
            session="default",
        )

        assert answer["instance_id"] == "i-ours"
        assert resolved == []


# ---------------------------------------------------------------------------
# The retry door — `_fallback_profile_selection` must answer the same way
# ---------------------------------------------------------------------------


class TestTheRetryDoor:
    async def _shared_selection(self, dirs: dict) -> dict:
        return {
            "user_data_dir": str(dirs["master"]),
            "profile_role": profile_seed.DEFAULT_SESSION,
            "clone_source": None,
        }

    async def test_a_retry_onto_a_held_shared_session_refuses_too(
        self, tmp_session_root
    ):
        """F-834 widened this function to all three roles, so it is the second
        door onto F-914: left alone it answers a held shared session with the
        very snapshot clone the resolver now refuses."""
        held_profile(tmp_session_root["master"])
        previous = await self._shared_selection(tmp_session_root)

        with pytest.raises(ToolError):
            await clone_storage._fallback_profile_selection(previous, 0)

    async def test_a_retry_hands_the_jar_over_when_we_drive_the_holder(
        self, tmp_session_root
    ):
        master = tmp_session_root["master"]
        held_profile(master)
        previous = await self._shared_selection(tmp_session_root)

        selection = await clone_storage._fallback_profile_selection(
            previous, 0, driven=_driving(master)
        )

        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)

    async def test_a_retry_keeps_a_clone_hand_over(self, tmp_session_root):
        """The previous attempt was already handing a jar over; the retry
        clones again and must not drop it, or the finding reappears one spawn
        failure later."""
        master = tmp_session_root["master"]
        previous = {
            "user_data_dir": str(tmp_session_root["sessions"] / "gone"),
            "profile_role": "clone",
            "clone_source": "default-seed",
            clone_storage.LIVE_SEED_KEY: str(master),
        }

        selection = await clone_storage._fallback_profile_selection(
            previous, 0, driven=_driving(master)
        )

        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)

    async def test_a_retry_with_no_hand_over_does_not_invent_one(
        self, tmp_session_root
    ):
        """The other half of ``still_driven_source``, and the reachable one: an
        attempt that was making no hand-off must not acquire one on the retry.

        This node replaces a pin that drove ``_fallback_profile_selection``
        with no ``driven=`` at all to watch a closed source be dropped. That
        call shape does not exist in production — ``spawn_browser`` takes ONE
        immutable ``cookie_handoff.Driven`` and hands it to the first selection
        and to every fallback, and ``LIVE_SEED_KEY`` is only stamped on a path
        that same object already approved — so the drop it asserted cannot
        happen, and the node passed against the unfixed product. Where a source
        that stopped being ours IS observed is one browser launch later, in
        ``_seed_cookies_over_cdp``, pinned in ``tests/test_cookie_handoff.py``
        (``TestEveryReasonAHandOffCanReport``).
        """
        other = tmp_session_root["sessions"] / "elsewhere"
        previous = {
            "user_data_dir": str(tmp_session_root["sessions"] / "gone"),
            "profile_role": "clone",
            "clone_source": "default-seed",
        }

        selection = await clone_storage._fallback_profile_selection(
            previous, 0, driven=_driving(other)
        )

        assert clone_storage.LIVE_SEED_KEY not in selection
        assert clone_storage.HANDED_OVER_KEY not in selection


# ---------------------------------------------------------------------------
# The two questions that must not become one
# ---------------------------------------------------------------------------


class TestTheBoundaries:
    async def test_seed_from_onto_a_held_target_is_still_the_older_refusal(
        self, tmp_session_root
    ):
        """A held session EXISTS, and ``seed_from`` applies at creation only —
        so the two live sources can never both be set, and the message a caller
        gets is about the flag they typed."""
        work = await _session_with_a_login("work", tmp_session_root)
        await _session_with_a_login("other", tmp_session_root)
        held_profile(work)

        with pytest.raises(ToolError) as refusal:
            await _selection(session="work", seed_from="other")

        assert "already exists" in str(refusal.value)

    async def test_the_live_source_stays_internal_and_the_holder_is_public(
        self, tmp_session_root
    ):
        """``LIVE_SEED_KEY`` is an instruction to this process and is dropped
        at the one line between the resolver and its caller; the HOLDER is a
        word the caller reads, so it survives."""
        master = tmp_session_root["master"]
        held_profile(master)

        selection = await _selection(driven=_driving(master))
        public = clone_storage._public_profile_selection(selection)

        assert clone_storage.LIVE_SEED_KEY not in public
        assert public["handed_over_from"] == profile_seed.DEFAULT_SESSION
