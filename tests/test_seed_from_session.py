"""F-897 — a new session can be seeded from an existing one, and never from a
live profile.

Five claims, each pinned because each is a place the feature could be a
silence rather than an answer:

1. **Seeding from a CLOSED session copies that session's login state.** The
   thing `--from work` promises is the cookies in `work`, so the pin reads a
   login WITNESS out of the new directory rather than trusting a role word.
2. **A RUNNING source is refused BY NAME, and nothing is created.** This is
   the whole safety argument: `profile_copy.copy_file` answers a locked file
   by skipping it, so a copy of a live profile is a session that looks
   complete and is missing exactly the logins that were asked for, with no
   way to enumerate the gap. The refusal has to name the session (a message
   about a path is not actionable) and it has to happen before a directory
   exists (a half-made session the caller must now clean up is a second harm).
   `default` is the one exception and it is pinned as one, with its reason:
   the product keeps a separate closed copyable form of it.
3. **`seed_from` applies at CREATION and nowhere else.** An existing session
   plus `--from` is an ERROR, not a no-op and not a re-seed. Both alternatives
   were rejected deliberately (see `profile_seed.require_new_session`), so the
   refusal is pinned together with the fact that it NAMES where the existing
   session actually came from.
4. **It is a NAME, through the same gate `session` passes.** Never a path,
   never a reserved or folded word, never a second resolver — and the refusal
   says `seed_from`, because a caller told about `session` edits the wrong
   argument.
5. **The provenance is real and it reaches both surfaces.** The marker records
   the source's NAME, `seed_changed_since` works for a non-default source, and
   the answer a caller reads — `spawn_diagnostics.profile_selection` and the
   `stealthy spawn` block — says so in words. F-896's "the seed a session
   reports is a session you can open" is extended here rather than replaced.
"""

import argparse
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from fakes import held_profile
from stealth_chrome_devtools_mcp import cli, cli_call, cli_render
from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: A byte string no fixture writes, so finding it in a new session is proof
#: the copy came from the session that holds it and not from the seed.
WORK_COOKIE = b"work-session-cookie-jar"

#: The profile-relative file the copy has to carry. It is spelled here and not
#: read out of ``LOGIN_WITNESSES`` on purpose: ``test_profile_seed_truth``
#: forbids a second spelling under the PACKAGE, and a pin that derived its
#: fixture from the list it is checking would compare the code to itself
#: (`fixtures-from-the-same-serializer-cannot-fail`).
COOKIE_JAR = "Default/Network/Cookies"


async def _selection(
    *, session: str | None = None, seed_from: str | None = None
) -> dict:
    """What a spawn resolves to — composed exactly as ``spawn_browser`` does:
    the two gates first (the profile request, then the seed request against
    its answer), then the ONE resolver."""
    landed = clone_storage.require_allowed_user_data_dir(None, session)
    seed = clone_storage.require_allowed_seed_from(seed_from, landed)
    selection = await clone_storage.resolve_profile_selection(landed, seed_from=seed)
    return clone_storage._public_profile_selection(selection)


async def _session_with_a_login(name: str, dirs: dict) -> Path:
    """Create session *name* and put a recognisable login in it."""
    await _selection(session=name)
    directory = dirs["sessions"] / name
    jar = directory / COOKIE_JAR
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(WORK_COOKIE)
    return directory


def _spawn_namespace(**overrides) -> argparse.Namespace:
    base = {
        "session": None,
        "seed_from": None,
        "profile": None,
        "headed": False,
        "headless": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _spawn_parser() -> argparse.ArgumentParser:
    parser = cli.build_parser()
    action = next(
        a for a in parser._actions if isinstance(getattr(a, "choices", None), dict)
    )
    return action.choices["spawn"]


# ---------------------------------------------------------------------------
# 1. Seeding from a closed session copies its login state
# ---------------------------------------------------------------------------


class TestSeedingFromAClosedSession:
    async def test_the_new_session_holds_the_sources_cookies(self, tmp_session_root):
        """The claim `--from work` makes is about cookies, so the pin reads
        the cookie jar. A role word or a marker alone would pass for a copy of
        the shared seed, which is the bug this feature exists to avoid."""
        await _session_with_a_login("work", tmp_session_root)

        selection = await _selection(session="beta", seed_from="work")

        beta = Path(selection["user_data_dir"])
        assert beta == tmp_session_root["sessions"] / "beta"
        assert (beta / COOKIE_JAR).read_bytes() == WORK_COOKIE

    async def test_the_marker_records_the_source_by_name(self, tmp_session_root):
        source = await _session_with_a_login("work", tmp_session_root)

        selection = await _selection(session="beta", seed_from="work")

        marker = profile_seed.read_marker(Path(selection["user_data_dir"]))
        assert marker["seeded_from"] == "work"
        assert marker["source"] == str(source)
        # `explicit` prefix is what `is_named` reads: a session seeded from
        # another session is still a NAMED profile and must never be swept.
        assert marker["source_kind"] == "explicit-session"
        assert marker["auto_clean"] is False

    async def test_omitting_seed_from_is_byte_identical_to_today(
        self, tmp_session_root
    ):
        """F-897 adds a door; it moves nothing that already worked. A session
        created with no `seed_from` still comes from the shared seed and still
        reports `default`."""
        selection = await _selection(session="beta")

        marker = profile_seed.read_marker(Path(selection["user_data_dir"]))
        assert marker["seeded_from"] == profile_seed.DEFAULT_SESSION
        assert marker["source_kind"] == "explicit-default-seed"

    async def test_seed_from_default_is_the_same_as_omitting_it(self, tmp_session_root):
        """`None` and `"default"` are one path, not two that agree today."""
        omitted = profile_seed.read_marker(
            Path((await _selection(session="beta"))["user_data_dir"])
        )
        named = profile_seed.read_marker(
            Path(
                (await _selection(session="gamma", seed_from="default"))[
                    "user_data_dir"
                ]
            )
        )

        for key in ("seeded_from", "source", "source_kind", "auto_clean"):
            assert named[key] == omitted[key], key


# ---------------------------------------------------------------------------
# 2. A running source is refused by name, and nothing is created
# ---------------------------------------------------------------------------


class TestARunningSourceIsRefused:
    async def test_refused_by_name_with_a_remedy(self, tmp_session_root):
        source = await _session_with_a_login("work", tmp_session_root)
        held_profile(source)

        with pytest.raises(ToolError) as excinfo:
            await _selection(session="beta", seed_from="work")

        message = str(excinfo.value)
        assert "'work'" in message, "the refusal must name the SESSION"
        assert "open" in message.casefold()
        assert "close" in message.casefold(), "a refusal owes the caller a remedy"

    async def test_nothing_is_created_on_disk(self, tmp_session_root):
        """A half-made session the caller now has to clean up would be a
        second harm on top of the refusal."""
        source = await _session_with_a_login("work", tmp_session_root)
        held_profile(source)

        with pytest.raises(ToolError):
            await _selection(session="beta", seed_from="work")

        assert not (tmp_session_root["sessions"] / "beta").exists()

    async def test_a_running_default_is_not_refused(self, tmp_session_root):
        """The one exception, and it is a fact about the MECHANISM: the
        product keeps a separate closed copyable form of the shared session,
        so a copy taken from it is a copy of a directory nothing is writing
        to. Every other session has no such form, which is why every other
        session is refused while open."""
        held_profile(tmp_session_root["master"])

        selection = await _selection(session="beta", seed_from="default")

        assert Path(selection["user_data_dir"]).exists()
        assert selection["seeded_from"] == profile_seed.DEFAULT_SESSION

    async def test_a_source_that_does_not_exist_is_refused_naming_it(
        self, tmp_session_root
    ):
        with pytest.raises(ToolError) as excinfo:
            await _selection(session="beta", seed_from="nosuch")

        assert "'nosuch'" in str(excinfo.value)
        assert not (tmp_session_root["sessions"] / "beta").exists()


# ---------------------------------------------------------------------------
# 3. seed_from applies at CREATION and nowhere else
# ---------------------------------------------------------------------------


class TestSeedFromAppliesOnlyAtCreation:
    async def test_an_existing_session_is_refused_not_re_seeded(self, tmp_session_root):
        """A re-seed would overwrite a login a human typed by hand, and a
        silent no-op would tell the caller their session came from `work`
        when it did not. The refusal is the only answer that is neither."""
        await _session_with_a_login("work", tmp_session_root)
        existing = Path((await _selection(session="beta"))["user_data_dir"])
        before = (existing / COOKIE_JAR).exists()

        with pytest.raises(ToolError) as excinfo:
            await _selection(session="beta", seed_from="work")

        message = str(excinfo.value)
        assert "'beta'" in message and "already exists" in message
        # It NAMES where the existing session actually came from, so a caller
        # expecting a fresh copy learns what they have instead.
        assert f"seeded from {profile_seed.DEFAULT_SESSION}" in message
        assert (existing / COOKIE_JAR).exists() is before, "no re-seed happened"

    async def test_with_no_session_at_all_it_is_refused(self, tmp_session_root):
        with pytest.raises(ToolError) as excinfo:
            await _selection(seed_from="work")
        assert "session=" in str(excinfo.value)

    async def test_it_cannot_apply_to_the_shared_session(self, tmp_session_root):
        """`default` is the session every OTHER one is seeded FROM."""
        with pytest.raises(ToolError) as excinfo:
            await _selection(session=profile_seed.DEFAULT_SESSION, seed_from="work")
        assert profile_seed.DEFAULT_SESSION in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. It is a NAME, through the same gate
# ---------------------------------------------------------------------------


class TestSeedFromIsAName:
    @pytest.mark.parametrize(
        "requested",
        [
            "a/b",
            "a\\b",
            "C:\\Users\\me\\profile",
            "C:profile",
            "~/profiles/acme",
            "/tmp/acme",
            ".",
            "..",
        ],
    )
    def test_a_path_shape_is_refused(self, requested, tmp_session_root):
        with pytest.raises(ToolError) as excinfo:
            profile_seed.seed_request(requested)
        assert "seed_from" in str(excinfo.value), (
            "a caller told about `session` when they typed `seed_from` goes "
            "and edits the wrong argument"
        )

    def test_empty_is_refused_and_names_the_default(self, tmp_session_root):
        with pytest.raises(ToolError) as excinfo:
            profile_seed.seed_request("   ")
        message = str(excinfo.value)
        assert "seed_from" in message and profile_seed.DEFAULT_SESSION in message

    def test_the_drive_refusal_reads_one_flavour_on_both_platforms(self):
        """F-894's lesson, reached a third time: a drive is a Windows concept
        and ``PurePosixPath("C:x").drive`` is ``""``, so a refusal that read
        the HOST's flavour would fire on Windows only. `C:profile` is not
        absolute under EITHER flavour, which is what makes the refusal the
        same everywhere."""
        assert PureWindowsPath("C:profile").drive == "C:"
        assert PurePosixPath("C:profile").drive == ""
        assert not PureWindowsPath("C:profile").is_absolute()
        assert not PurePosixPath("C:profile").is_absolute()

    @pytest.mark.parametrize(
        "requested", ["master", "master-snapshot", "default.", "master."]
    )
    async def test_reserved_and_folded_names_are_refused(
        self, requested, tmp_session_root
    ):
        """The SAME gate `session` passes — `profile_seed.require_allowed` —
        so a word the product owns cannot be reached through the seed door
        either. `default.` is F-896's S1 fold: Windows strips a trailing dot,
        so it would reach the shared directory under a different string."""
        with pytest.raises(ToolError):
            await _selection(session="beta", seed_from=requested)
        assert not (tmp_session_root["sessions"] / "beta").exists()

    async def test_surrounding_whitespace_is_stripped_not_folded(
        self, tmp_session_root
    ):
        """`"default "` is NOT the fold case and must not be refused: the
        whitespace is noise around a name and ``require_name`` removes it
        before any rule sees it, so this is the shared session spelled
        untidily — exactly what ``session=" default "`` already means
        (F-896 review N5). The fold refusal is for a character the FILESYSTEM
        removes, which is why ``default.`` above raises and this does not."""
        selection = await _selection(session="beta", seed_from="  default  ")
        assert selection["seeded_from"] == profile_seed.DEFAULT_SESSION

    def test_whitespace_around_a_name_is_not_part_of_it(self, tmp_session_root):
        assert profile_seed.seed_request("  work  ") == "work"

    def test_none_stays_none(self):
        assert profile_seed.seed_request(None) is None


# ---------------------------------------------------------------------------
# 5. Provenance, and the two surfaces that report it
# ---------------------------------------------------------------------------


class TestProvenanceReachesTheCaller:
    async def test_diagnostics_carry_the_source_name(self, tmp_session_root):
        await _session_with_a_login("work", tmp_session_root)

        selection = await _selection(session="beta", seed_from="work")

        assert selection["seeded_from"] == "work"
        assert selection["seeded_at"]

    async def test_the_seed_it_reports_is_a_session_you_can_open(
        self, tmp_session_root
    ):
        """F-896's pin, extended rather than replaced: the word in
        `seeded_from` has to be something `session=` accepts, and with F-897
        that word is now any session's name."""
        await _session_with_a_login("work", tmp_session_root)
        selection = await _selection(session="beta", seed_from="work")

        reported = selection["seeded_from"]
        assert profile_seed.profile_request(reported, None) == reported
        reopened = await _selection(session=reported)
        assert Path(reopened["user_data_dir"]) == tmp_session_root["sessions"] / "work"

    async def test_seed_changed_since_works_for_a_non_default_source(
        self, tmp_session_root
    ):
        """`seed_changed_since` reads the LOGIN_WITNESSES off the marker's
        recorded `source` PATH, so it has to work for a source that is a
        session rather than the shared seed — that path was only ever
        exercised for the seed before."""
        source = await _session_with_a_login("work", tmp_session_root)
        selection = await _selection(session="beta", seed_from="work")
        beta = Path(selection["user_data_dir"])
        assert profile_seed.provenance(beta)["seed_changed_since"] is False

        jar = source / COOKIE_JAR
        later = jar.stat().st_mtime + 3600
        os.utime(jar, (later, later))

        assert profile_seed.provenance(beta)["seed_changed_since"] is True

    async def test_the_spawn_block_says_where_the_session_came_from(
        self, tmp_session_root
    ):
        await _session_with_a_login("work", tmp_session_root)
        selection = await _selection(session="beta", seed_from="work")

        lines = cli_render.spawn_lines(
            {
                "instance_id": "abc",
                "spawn_diagnostics": {"profile_selection": selection},
            }
        )

        assert any("seeded from work" in line for line in lines)

    async def test_it_says_nothing_about_a_profile_that_is_nobodys_copy(
        self, tmp_session_root
    ):
        """The shared session has no marker, and a line claiming an unknown
        seed for it is a category error — `cli._seed_line`'s review m6, the
        same rule reached from the spawn side."""
        selection = await _selection(session=profile_seed.DEFAULT_SESSION)

        lines = cli_render.spawn_lines(
            {
                "instance_id": "abc",
                "spawn_diagnostics": {"profile_selection": selection},
            }
        )

        assert not any("seeded" in line for line in lines)

    def test_the_sentence_has_one_home(self):
        """Both surfaces phrase it with ``profile_seed.seed_sentence``, so a
        fourth marker field cannot appear in one listing and not the other."""
        fields = {
            "seeded_from": "work",
            "seeded_at": "2026-09-21T10:00:00Z",
            "seed_changed_since": True,
        }
        sentence = profile_seed.seed_sentence(fields)
        assert sentence == cli._seed_line({"role": "explicit", **fields})
        assert "SEED CHANGED SINCE" in sentence


# ---------------------------------------------------------------------------
# 6. The CLI holds no second opinion
# ---------------------------------------------------------------------------


class TestStealthySpawnFrom:
    def test_from_is_sent_as_seed_from(self):
        arguments = cli_call._spawn_arguments(_spawn_namespace(seed_from="work"))
        assert arguments == {"seed_from": "work"}

    def test_it_is_passed_through_untouched(self):
        """A value the BACKEND will refuse still leaves the CLI verbatim: the
        rule has one home and a CLI-side pre-check would be a second answer
        that goes stale between the check and the spawn."""
        arguments = cli_call._spawn_arguments(_spawn_namespace(seed_from="a/b"))
        assert arguments == {"seed_from": "a/b"}

    def test_it_is_documented_unlike_profile(self):
        spawn = _spawn_parser()
        action = next(a for a in spawn._actions if a.dest == "seed_from")
        assert "--from" in action.option_strings
        assert action.help and action.help is not argparse.SUPPRESS

    def test_no_from_sends_no_seed_from(self):
        assert cli_call._spawn_arguments(_spawn_namespace()) == {}


# ---------------------------------------------------------------------------
# 7. The vocabulary survives the new door
# ---------------------------------------------------------------------------


class TestNoRefusalSaysTheOldWords:
    """F-896 retired `master` and `snapshot` from every user-facing string; a
    new parameter with five new refusals is exactly where they come back."""

    async def _refusals(self, tmp_session_root) -> list[str]:
        source = await _session_with_a_login("work", tmp_session_root)
        await _selection(session="beta")
        held_profile(source)
        messages = []
        for kwargs in (
            {"session": "beta", "seed_from": "work"},  # target exists
            {"session": "gamma", "seed_from": "nosuch"},  # no such source
            {"seed_from": "work"},  # no session
            {"session": profile_seed.DEFAULT_SESSION, "seed_from": "work"},  # shared
            {"session": "delta", "seed_from": "master"},  # reserved
        ):
            with pytest.raises(ToolError) as excinfo:
                await _selection(**kwargs)
            messages.append(str(excinfo.value))
        with pytest.raises(ToolError) as excinfo:
            await _selection(session="epsilon", seed_from="work")  # source is held
        messages.append(str(excinfo.value))
        return messages

    async def test_every_seed_from_refusal_is_clean(self, tmp_session_root):
        for message in await self._refusals(tmp_session_root):
            said = [
                word
                for word in ("master", "snapshot")
                # the reserved-name refusal quotes the caller's own word back,
                # which is not the product teaching a vocabulary (F-896's own
                # exemption, same reason).
                if word in message.casefold().replace("'master'", "")
            ]
            assert not said, f"{said} in: {message}"
