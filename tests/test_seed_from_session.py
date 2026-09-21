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
   were rejected deliberately (see `profile_source.require_new_session`), so the
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
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from fakes import held_profile
from stealth_chrome_devtools_mcp import cli, cli_call, cli_render
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    profile_seed,
    profile_source,
)
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
    *,
    session: str | None = None,
    seed_from: str | None = None,
    driven=profile_source.NOTHING_DRIVEN,
) -> dict:
    """What a spawn resolves to — composed exactly as ``spawn_browser`` does:
    the two gates first (the profile request, then the seed request against
    its answer), then the ONE resolver.

    ``driven`` is F-898's witness and defaults to the fail-closed one, which is
    what an un-updated caller gets in production too."""
    landed = clone_storage.require_allowed_user_data_dir(None, session)
    seed = clone_storage.require_allowed_seed_from(seed_from, landed, driven=driven)
    selection = await clone_storage.resolve_profile_selection(
        landed, seed_from=seed, driven=driven
    )
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
            profile_source.seed_request(requested)
        assert "seed_from" in str(excinfo.value), (
            "a caller told about `session` when they typed `seed_from` goes "
            "and edits the wrong argument"
        )

    @pytest.mark.parametrize("requested", ["   ", "\t", " \n "])
    def test_a_value_that_names_nothing_is_refused_with_the_one_sentence(
        self, requested, tmp_session_root
    ):
        """F-896's delta rule reaching the third field that takes a NAME.

        The sentence is `_names_nothing`'s and is asserted to be the SAME one
        `session` and `user_data_dir` get, down to its last clause — only the
        field name differs, because that is what tells the caller which
        argument to edit. A per-field variant would be a second way to say one
        thing, and this node is what stops one being written.
        """
        with pytest.raises(ToolError) as excinfo:
            profile_source.seed_request(requested)
        assert str(excinfo.value) == str(
            profile_seed._names_nothing("seed_from", requested)
        )
        with pytest.raises(ToolError) as session_refusal:
            profile_seed.profile_request(requested, None)
        assert str(session_refusal.value) == str(excinfo.value).replace(
            "seed_from", "session", 1
        )

    def test_empty_is_not_given_and_seeds_from_the_default(self, tmp_session_root):
        """`""` is NOT the names-nothing case and must not raise (F-896 delta,
        `2953e8d`): an MCP client is a language model and `""` for an optional
        string is one of its commonest shapes, so refusing it would fail a
        spawn that asked for nothing. Here saying nothing has a right answer
        already — the shared session — which is what an unset `--from` means,
        so `seed_request` answers None and the copy comes from the seed."""
        assert profile_source.seed_request("") is None

    async def test_an_empty_from_still_makes_the_session_from_the_default(
        self, tmp_session_root
    ):
        """The half the reader above cannot see: `""` must not merely parse to
        None, it must reach the resolver as no request at all and leave the
        spawn on today's path."""
        selection = await _selection(session="beta", seed_from="")
        assert selection["seeded_from"] == profile_seed.DEFAULT_SESSION
        assert (tmp_session_root["sessions"] / "beta").exists()

    def test_a_drive_hidden_behind_a_space_is_still_refused(self, tmp_session_root):
        """F-896 delta review N, reached by the third field rather than by one.

        `"C:foo"` is refused and one leading space used to hide the drive from
        the rule that reads it. Here the strip is `require_name`'s and happens
        before `is_bare_name` sees the value, so the space cannot hide
        anything.

        A GUARD, stated as one: it passed before this merge too, because
        `require_name` has always stripped first. It is pinned because the
        rule and the strip live in two functions and nothing else says they
        must stay in that order — `reserved_reason` needed its own `.strip()`
        added for exactly this shape, which is what a missing guard here would
        eventually cost."""
        for requested in (" C:profile", "C:profile "):
            with pytest.raises(ToolError) as excinfo:
                profile_source.seed_request(requested)
            assert "seed_from" in str(excinfo.value)

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
        assert profile_source.seed_request("  work  ") == "work"

    def test_none_stays_none(self):
        assert profile_source.seed_request(None) is None


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
# 7. The tool body itself — the layer every pin above sits one below
# ---------------------------------------------------------------------------


class TestSpawnBrowserTakesASeed:
    """The composition above is what ``spawn_browser`` is supposed to do; these
    drive the REAL tool, because a gate that answers correctly and a body that
    never calls it would leave every pin above green."""

    async def _seed_handed_to_the_resolver(
        self, call_tool, patched_server, monkeypatch, **kwargs
    ):
        from types import SimpleNamespace

        from fakes import FakeBrowserManager

        seen: list[str | None] = []

        async def fake_resolve(user_data_dir, *, seed_from=None, **_):
            seen.append(seed_from)
            return {
                "user_data_dir": user_data_dir or "/shared",
                "profile_role": "explicit" if user_data_dir else "default",
                "clone_source": None,
            }

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        srv = patched_server(
            browser_manager=FakeBrowserManager(
                spawn_instance=SimpleNamespace(
                    instance_id="i1",
                    state="active",
                    headless=True,
                    viewport={"width": 800, "height": 600},
                ),
                spawn_diagnostics={},
            )
        )
        await call_tool(srv, "spawn_browser", headless=True, sandbox=False, **kwargs)
        return seen[0]

    async def test_seed_from_reaches_the_resolver(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """The source has to EXIST for this node to be about what it says it is
        about. It did not until the review's S1 fix, which moved the
        source-exists check into the pre-flight — so this node was reaching the
        resolver with a name nothing backed, and only the resolver's absence
        (it is patched out here) kept it green."""
        await _session_with_a_login("work", tmp_session_root)

        seed = await self._seed_handed_to_the_resolver(
            call_tool, patched_server, monkeypatch, session="beta", seed_from="  work  "
        )
        assert seed == "work", "the tool hands the resolver the NORMALISED name"

    async def test_no_seed_from_reaches_the_resolver_as_none(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """A GUARD, not a change driver — it passed before the feature too,
        because the resolver had no such keyword and the double's own default
        answered. It is here so a later default cannot start sending one."""
        assert (
            await self._seed_handed_to_the_resolver(
                call_tool, patched_server, monkeypatch, session="beta"
            )
            is None
        )

    async def test_an_existing_session_is_refused_at_the_tool(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """And it has to be refused HERE, in front of the F-888 re-attach: a
        session whose browser is still running is a session that EXISTS, so
        without this guard `--from` would be silently dropped by an adoption in
        exactly the case a caller most wants to be told about."""
        await _selection(session="beta")

        with pytest.raises(ToolError, match="already exists"):
            await self._seed_handed_to_the_resolver(
                call_tool, patched_server, monkeypatch, session="beta", seed_from="work"
            )

    async def test_a_path_shaped_seed_from_is_refused_at_the_tool(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        with pytest.raises(ToolError, match="seed_from"):
            await self._seed_handed_to_the_resolver(
                call_tool, patched_server, monkeypatch, session="beta", seed_from="a/b"
            )


# ---------------------------------------------------------------------------
# 7b. Every refusal reaches the caller as a REFUSAL (review S1, M1)
# ---------------------------------------------------------------------------


WRAPPED = "Failed to spawn browser"


async def _spawn_refusal(call_tool, patched_server, **kwargs) -> str:
    """Drive the REAL tool through the REAL resolver and return the message.

    Deliberately NOT `TestSpawnBrowserTakesASeed`'s harness, which patches
    `resolve_profile_selection` away: three of these refusals are raised from
    INSIDE the resolver, so a double in its place is exactly the blind spot
    review S1 found. The manager is still a fake, because every node here
    refuses before a browser is reached.
    """
    from types import SimpleNamespace

    from fakes import FakeBrowserManager

    srv = patched_server(
        browser_manager=FakeBrowserManager(
            spawn_instance=SimpleNamespace(
                instance_id="i1",
                state="active",
                headless=True,
                viewport={"width": 800, "height": 600},
            ),
            spawn_diagnostics={},
        )
    )
    with pytest.raises(ToolError) as excinfo:
        await call_tool(srv, "spawn_browser", headless=True, sandbox=False, **kwargs)
    return str(excinfo.value)


class TestEverySeedRefusalIsACallerRefusal:
    """`spawn_browser`'s handler re-labels anything raised inside its `try` as
    `Failed to spawn browser: …` — the wrong sentence for a request we declined
    to act on, and the thing `browser_management.py`'s own comment above the
    pre-flight forbids (F-894 review M1).

    Two of the five refusals were already in the pre-flight and arrived clean.
    The other three — a source that is open, a source that does not exist, and
    a source naming a reserved word — were raised from inside
    `profile_source.seed_source`, which runs under the resolver INSIDE the try,
    so a caller who typed `--from work` while `work` was open was told a spawn
    had FAILED about a spawn that never started.

    Every node here drives the real tool and asserts the same one thing, which
    is why the assertion is a helper rather than five spellings of it.
    """

    def _assert_unwrapped(self, message: str, *, names: str) -> None:
        assert not message.startswith(WRAPPED), (
            f"a caller-input refusal was re-labelled as a failed spawn: {message!r}"
        )
        assert names in message, (
            f"the refusal has to name what was refused: {message!r}"
        )

    async def test_a_running_source_refuses_without_the_failure_label(
        self, call_tool, patched_server, tmp_session_root
    ):
        source = await _session_with_a_login("work", tmp_session_root)
        held_profile(source)

        message = await _spawn_refusal(
            call_tool, patched_server, session="beta", seed_from="work"
        )
        self._assert_unwrapped(message, names="work")
        assert not (tmp_session_root["sessions"] / "beta").exists(), (
            "and still nothing on disk — the pre-flight must refuse in FRONT "
            "of the copy, not merely earlier than the handler"
        )

    async def test_a_missing_source_refuses_without_the_failure_label(
        self, call_tool, patched_server, tmp_session_root
    ):
        message = await _spawn_refusal(
            call_tool, patched_server, session="beta", seed_from="nope"
        )
        self._assert_unwrapped(message, names="nope")

    @pytest.mark.parametrize("requested", ["master", "master-snapshot"])
    async def test_a_reserved_source_refuses_without_the_failure_label(
        self, requested, call_tool, patched_server, tmp_session_root
    ):
        message = await _spawn_refusal(
            call_tool, patched_server, session="beta", seed_from=requested
        )
        self._assert_unwrapped(message, names=requested)

    async def test_an_existing_target_refuses_without_the_failure_label(
        self, call_tool, patched_server, tmp_session_root
    ):
        """Already in the pre-flight; pinned in the same class so the five are
        read together and a later move of one is visible against the rest."""
        await _selection(session="beta")
        message = await _spawn_refusal(
            call_tool, patched_server, session="beta", seed_from="work"
        )
        self._assert_unwrapped(message, names="beta")

    async def test_a_path_shaped_source_refuses_without_the_failure_label(
        self, call_tool, patched_server, tmp_session_root
    ):
        message = await _spawn_refusal(
            call_tool, patched_server, session="beta", seed_from="a/b"
        )
        self._assert_unwrapped(message, names="seed_from")


class TestSeedFromNeedsASessionToApplyTo:
    """Review M1 — the silent drop.

    `seed_from` was refused for three shapes and never for the one that
    matters most: a `user_data_dir` landing OUTSIDE the clone root. The
    resolver only seeds a directory it is about to CREATE under the session
    root, so for any other target the request passed every gate and was then
    never used — no copy, no marker, `seeded_from: unknown`, and not one word
    to the caller. That is this finding's own stated commitment inverted, and
    it is reachable from the documented path door (`--arg user_data_dir=…`)
    and from the still-accepted `stealthy spawn --profile`.
    """

    def _outside(self, tmp_session_root) -> Path:
        return tmp_session_root["sessions"].parent / "elsewhere" / "profile"

    async def test_an_out_of_root_target_is_refused_at_the_tool(
        self, call_tool, patched_server, tmp_session_root
    ):
        outside = self._outside(tmp_session_root)
        await _session_with_a_login("work", tmp_session_root)
        message = await _spawn_refusal(
            call_tool,
            patched_server,
            user_data_dir=str(outside),
            seed_from="work",
        )
        assert not message.startswith(WRAPPED)
        assert "seed_from" in message
        assert not outside.exists(), (
            "a refused request creates nothing — the same promise the running-"
            "source refusal makes"
        )

    async def test_the_gate_refuses_it_and_not_only_the_tool(self, tmp_session_root):
        """The rule belongs to the gate that already owns 'is there a NEW
        session for this to apply to', so it holds for the resolver's other
        callers too and not only for the one body that happens to ask first."""
        outside = self._outside(tmp_session_root)
        with pytest.raises(ToolError, match="seed_from"):
            clone_storage.require_allowed_seed_from("work", str(outside))

    async def test_a_target_inside_the_root_still_passes(self, tmp_session_root):
        """The refusal is about WHERE the target lands, never about how it was
        spelled: an absolute path to a session that does not exist yet, under
        the clone root, is a session being created and keeps working."""
        await _session_with_a_login("work", tmp_session_root)
        inside = tmp_session_root["sessions"] / "beta"
        assert clone_storage.require_allowed_seed_from("work", str(inside)) == "work"

    def test_the_no_session_refusal_names_both_spellings(self):
        """Review N4. The CLI prints the backend's message verbatim — one home
        for the rule — so the one sentence has to serve a caller who typed
        `session=` at the tool AND one who typed `--session` at the shell.
        Naming only the tool's spelling sends a `stealthy spawn --from work`
        user looking for an argument they did not type; a second message home
        keyed on who asked would be the defect this file keeps closing."""
        with pytest.raises(ToolError) as excinfo:
            profile_source.require_new_session(
                "work", None, shared=False, inside_root=False
            )
        message = str(excinfo.value)
        assert "session=" in message and "--session" in message

    def test_every_refusal_here_names_both_spellings(self, tmp_session_root):
        """All four, not just the one N4 named: a caller meets whichever
        refusal their request earns, and a remedy spelled for the wrong
        surface is no better in the other three."""
        target = tmp_session_root["sessions"] / "beta"
        cases = [
            (None, {"shared": False, "inside_root": False}),
            (target, {"shared": True, "inside_root": True}),
            (
                tmp_session_root["sessions"].parent / "elsewhere",
                {"shared": False, "inside_root": False},
            ),
        ]
        for requested_target, flags in cases:
            with pytest.raises(ToolError) as excinfo:
                profile_source.require_new_session("work", requested_target, **flags)
            assert "--session" in str(excinfo.value), str(excinfo.value)

        target.mkdir(parents=True)
        with pytest.raises(ToolError) as excinfo:
            profile_source.require_new_session(
                "work", target, shared=False, inside_root=True
            )
        assert "--session" in str(excinfo.value), str(excinfo.value)


# ---------------------------------------------------------------------------
# 7d. A source that is GONE must not read as a source that is unchanged
# ---------------------------------------------------------------------------


class TestAnUnreadableSourceSaysSo:
    """Review N1. `seed_changed_since` is None whenever the recorded source has
    no login witness to read — which a DELETED directory guarantees — and the
    sentence printed `SEED CHANGED SINCE` only for True, so a vanished source
    rendered byte-identical to a fresh one.

    Pre-existing in F-895's shape and made ordinary by F-897: until now the
    recorded source was always the product's own seed, and it is now a
    directory a caller can delete or rename at will.

    The word is a lowercase parenthetical and not a shouty one on purpose.
    `SEED CHANGED SINCE` is an ALERT — act on this, your copy is behind — while
    None is a caveat about what could be read, and giving the two the same
    register would teach a reader to ignore both.
    """

    def test_none_is_not_silence(self):
        unchanged = profile_seed.seed_sentence(
            {
                "seeded_from": "work",
                "seeded_at": "2026-01-01T00:00:00Z",
                "seed_changed_since": False,
            }
        )
        unreadable = profile_seed.seed_sentence(
            {
                "seeded_from": "work",
                "seeded_at": "2026-01-01T00:00:00Z",
                "seed_changed_since": None,
            }
        )
        assert unreadable != unchanged, (
            "a source that could not be read must not render identically to "
            "one that was read and had not moved"
        )
        assert unchanged == "seeded from work at 2026-01-01T00:00:00Z", (
            "and the ordinary case stays exactly as quiet as it was"
        )

    async def test_a_deleted_source_reaches_the_sentence_that_way(
        self, tmp_session_root
    ):
        """End to end through the real marker, because the defect was a
        COMPOSITION: `changed_since` was already answering None honestly and
        the phrasing threw that answer away. Seed `beta` from `work`, delete
        `work`, and read what a caller would be told about `beta`."""
        source = await _session_with_a_login("work", tmp_session_root)
        await _selection(session="beta", seed_from="work")

        fresh = profile_seed.seed_sentence(
            profile_seed.provenance(tmp_session_root["sessions"] / "beta")
        )
        shutil.rmtree(source)
        gone = profile_seed.seed_sentence(
            profile_seed.provenance(tmp_session_root["sessions"] / "beta")
        )

        assert "work" in fresh and "work" in gone, (
            "the NAME it was seeded from is a fact about the copy and does not "
            "stop being true when the source is deleted"
        )
        assert gone != fresh, (
            "but 'I cannot read that source any more' is not the same answer "
            "as 'that source has not moved'"
        )

    def test_changed_since_is_none_once_the_source_is_gone(self, tmp_session_root):
        directory = tmp_session_root["sessions"] / "gone"
        directory.mkdir(parents=True)
        assert profile_seed.changed_since(directory, "2026-01-01T00:00:00Z") is None


# ---------------------------------------------------------------------------
# 8. The vocabulary survives the new door
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


# ---------------------------------------------------------------------------
# 9. What the source ask COSTS (review N2; memo review S)
# ---------------------------------------------------------------------------


class TestTheSourceAskIsPaidForTwice:
    """`_profile_hold` is a full psutil cmdline walk, and on a machine that has
    been running a Chrome fleet that is hundreds of processes. Review S1 bought
    an unwrapped refusal by asking the SOURCE question in the pre-flight and
    throwing the answer away — and that gate is itself asked twice (the tool's,
    then the resolver's own), so one seeded spawn walked the table three times
    for one source where an unseeded named spawn walks it once.

    The RESOLVER's own gate call is the one that pays nothing: it runs inside
    `spawn_browser`'s try, where a source refusal is re-labelled anyway, and
    `_seed_source_for_copy` raises the same sentences one statement later with
    no `await` between. So it passes `check_source=False`, and the walk is paid
    for exactly where its answer is used — the pre-flight's, and the read
    before the copy. A 1 s memo bought the same saving and was REPLACED by the
    flag: no process-global state, no clock, no reset hook, and no remembered
    answer that can go stale.

    Counted at the COMPOSITION rather than through `call_tool`: the cost is a
    property of the gate and the resolver, and the tool path adds F-888's own
    `profile_lock.profile_hold` for the TARGET, which is a different question
    on a different witness and would make the number say less, not more.
    """

    def _counted(self, monkeypatch) -> list[str]:
        seen: list[str] = []
        real = clone_storage._profile_hold

        def counting(profile_dir):
            seen.append(Path(profile_dir).name)
            return real(profile_dir)

        monkeypatch.setattr(clone_storage, "_profile_hold", counting)
        return seen

    async def test_a_seeded_spawn_walks_the_table_twice_for_its_source(
        self, monkeypatch, tmp_session_root
    ):
        await _session_with_a_login("work", tmp_session_root)
        seen = self._counted(monkeypatch)

        await _selection(session="beta", seed_from="work")

        assert seen.count("work") == 2, (
            "the pre-flight's ask and the authoritative read before the copy, "
            f"and not the resolver's own gate call — got {seen}"
        )
        assert seen.count("beta") == 1, f"the target is asked about once: {seen}"

    async def test_an_unseeded_named_spawn_is_unchanged(
        self, monkeypatch, tmp_session_root
    ):
        """A GUARD: the flag must not cost — or save — anything on the spawn
        that never writes `seed_from`.

        It survived F-898 with its number intact, but not for free and not by
        accident. F-898 gave `default` a hand-off, which needs to know whether
        the shared session is running AND whether we drive it — and asked in
        that order, every named spawn paid a psutil walk of the whole process
        table to discover that it is not ours. `_default_source` asks the
        SNAPSHOT first (a lookup this spawn already has) and the walk only
        behind it, so the cost lands on the spawn that can actually receive a
        hand-off and nowhere else. This node is what keeps that order honest:
        put the walk first and it reads `["beta", "master"]`.
        """
        seen = self._counted(monkeypatch)

        await _selection(session="beta")

        assert seen == ["beta"], (
            "the TARGET only — F-898's `default` witness must short-circuit on "
            f"the free `driven` lookup before it walks the process table: {seen}"
        )

    async def test_a_spawn_that_can_receive_a_handoff_does_pay_the_walk(
        self, monkeypatch, tmp_session_root
    ):
        """The other half of the node above, and it is not decoration.

        A short-circuit that never reaches `held` would pass that assertion by
        answering the question WRONG — the shared session's liveness would stop
        being consulted at all. So this pins the conjunction from the other
        side: once `driven` says yes, the walk happens, exactly once.
        """
        seen = self._counted(monkeypatch)

        await _selection(session="gamma", driven=lambda _path: True)

        assert seen == ["gamma", "master"], (
            f"the target, then the shared session ONCE, and only here: {seen}"
        )

    async def test_the_resolvers_gate_call_skips_the_walk_the_preflight_makes(
        self, monkeypatch, tmp_session_root
    ):
        """The replacement for the memo, and the whole of it: ONE parameter,
        False at ONE call site. Asked the way `spawn_browser` asks it, the
        source question is put; asked the way the resolver asks it, it is not.
        """
        await _session_with_a_login("work", tmp_session_root)
        landed = clone_storage.require_allowed_user_data_dir(None, "beta")
        seen = self._counted(monkeypatch)

        clone_storage.require_allowed_seed_from("work", landed)
        assert seen.count("work") == 1, f"the pre-flight asks: {seen}"

        clone_storage.require_allowed_seed_from("work", landed, check_source=False)
        assert seen.count("work") == 1, f"the resolver's own call does not: {seen}"

    async def test_nothing_remembers_an_answer_between_asks(
        self, monkeypatch, tmp_session_root
    ):
        """No memo survives: every ask that is MADE walks. That is what makes
        the pre-copy read the statement it claims to be, and it is why a source
        closed a moment ago can never be refused from a remembered positive."""
        await _session_with_a_login("work", tmp_session_root)
        seen = self._counted(monkeypatch)

        clone_storage._seed_source("work")
        clone_storage._seed_source("work")
        assert seen.count("work") == 2, f"each ask is its own walk: {seen}"

        clone_storage._seed_source_for_copy("work")
        assert seen.count("work") == 3, f"the pre-copy read is fresh: {seen}"
