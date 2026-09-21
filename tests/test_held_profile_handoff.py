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

import pytest

from fakes import held_profile
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    profile_seed,
    profile_source,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: A byte string no fixture writes, so finding it in the walked session is
#: proof the copy came from the HELD directory and not from the shared seed.
HELD_COOKIE = b"held-session-cookie-jar"

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
