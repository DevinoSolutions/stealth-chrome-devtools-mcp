"""F-896 — the session-first vocabulary, and the one spelling that carries it.

Four claims, pinned here because each of them is a place the old vocabulary
could come back:

1. ``session=`` and ``user_data_dir=`` are ONE request, not two. A name means
   the same directory through either spelling, and passing both with different
   values is refused rather than silently resolved by precedence.
2. ``session`` takes a NAME and refuses a path — under BOTH ``PurePath``
   flavours, because a drive is a Windows concept and reading the host's
   flavour makes a refusal that only fires on one platform (``profile_seed``'s
   F-894 lesson, reached again).
3. ``default`` is the shared session: it selects the master ROLE rather than
   creating ``sessions/default``, while ``master``/``master-snapshot`` stay
   refused.
4. No USER-FACING string says "master" or "snapshot" any more — and the string
   set is DERIVED (the live tool registry, the live parser tree, a real
   resolver answer) rather than listed by hand, because a hand list is a list
   of the places somebody remembered.
"""

import argparse
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from stealth_chrome_devtools_mcp import cli, cli_call
from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

RETIRED_WORDS = ("master", "snapshot")


def _says_retired(text: object) -> list[str]:
    """Which retired word a user-facing string still carries."""
    if not isinstance(text, str):
        return []
    lowered = text.casefold()
    return [word for word in RETIRED_WORDS if word in lowered]


async def _selection(
    *, session: str | None = None, user_data_dir: str | None = None
) -> dict:
    """What a spawn resolves to — composed exactly as ``spawn_browser`` does:
    the ONE gate first (which normalises the two spellings and answers where
    the request lands), then the ONE resolver."""
    requested = clone_storage.require_allowed_user_data_dir(user_data_dir, session)
    selection = await clone_storage.resolve_profile_selection(requested)
    return clone_storage._public_profile_selection(selection)


# ---------------------------------------------------------------------------
# 1. One request, two spellings
# ---------------------------------------------------------------------------


class TestOneRequest:
    async def test_a_name_resolves_identically_through_either_spelling(
        self, tmp_session_root
    ):
        """The deprecated alias must RESOLVE TO the new spelling, never run
        beside it — a second path that merely agrees today is the defect
        convention 4 names."""
        via_session = await _selection(session="acme")
        via_alias = await _selection(user_data_dir="acme")
        assert via_session["user_data_dir"] == via_alias["user_data_dir"]
        assert via_session["profile_role"] == via_alias["profile_role"] == "explicit"

    def test_both_given_and_different_is_refused(self, tmp_session_root):
        with pytest.raises(ToolError) as excinfo:
            clone_storage.require_allowed_user_data_dir("other", "acme")
        message = str(excinfo.value)
        assert "session" in message and "user_data_dir" in message

    def test_both_given_and_equal_is_honoured(self, tmp_session_root):
        """Refusing a caller who said the same thing twice buys nothing."""
        assert clone_storage.require_allowed_user_data_dir("acme", "acme")

    def test_neither_given_is_no_request_at_all(self, tmp_session_root):
        assert clone_storage.require_allowed_user_data_dir(None, None) is None


# ---------------------------------------------------------------------------
# 2. A session is a NAME
# ---------------------------------------------------------------------------


class TestSessionRefusesAPath:
    @pytest.mark.parametrize(
        "given",
        [
            "/tmp/profile",
            "C:\\Users\\me\\profile",
            "sessions/acme",
            "sub\\acme",
            "C:profile",
            "~/profile",
            "",
            "   ",
        ],
    )
    def test_a_path_shaped_session_is_refused(self, given, tmp_session_root):
        with pytest.raises(ToolError) as excinfo:
            clone_storage.require_allowed_user_data_dir(None, given)
        message = str(excinfo.value)
        assert "user_data_dir" in message or "name" in message

    @pytest.mark.parametrize("flavour", [PurePosixPath, PureWindowsPath])
    def test_the_refusal_is_flavour_independent(self, flavour, tmp_session_root):
        """``PurePosixPath("C:profile").drive`` is ``""`` (measured), so a
        drive read through the HOST's flavour makes this rule Windows-only and
        the pin RED on every POSIX cell. Both flavours must agree that the
        product refuses it."""
        assert flavour("C:profile").name  # the string is a name under either
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir(None, "C:profile")

    def test_a_path_is_still_reachable_through_the_alias(self, tmp_session_root):
        """The path door is ``user_data_dir`` / ``stealthy call``, and it stays
        open — which is what makes refusing a path on ``session`` a narrowing
        of one spelling rather than a loss of reach."""
        target = tmp_session_root["sessions"] / "by-path"
        assert clone_storage.require_allowed_user_data_dir(str(target), None)


# ---------------------------------------------------------------------------
# 3. `default` is the shared session
# ---------------------------------------------------------------------------


class TestDefaultIsTheSharedSession:
    async def test_default_selects_the_shared_profile(self, tmp_session_root):
        selection = await _selection(session="default")
        assert selection["profile_role"] == "default"
        assert Path(selection["user_data_dir"]) == tmp_session_root["master"]

    async def test_default_is_the_same_answer_as_naming_no_session(
        self, tmp_session_root
    ):
        named = await _selection(session="default")
        unnamed = await _selection()
        assert named["user_data_dir"] == unnamed["user_data_dir"]
        assert named["profile_role"] == unnamed["profile_role"]

    def test_default_never_creates_a_sessions_directory(self, tmp_session_root):
        clone_storage.require_allowed_user_data_dir(None, "default")
        assert not (tmp_session_root["sessions"] / "default").exists()

    def test_a_spelling_that_would_create_one_is_refused(self, tmp_session_root):
        """``sessions/default`` is F-894's trap wearing the new word."""
        with pytest.raises(ToolError, match="default"):
            clone_storage.require_allowed_user_data_dir("sessions/default", None)

    @pytest.mark.parametrize("name", ["master", "master-snapshot"])
    def test_the_two_mechanism_names_stay_refused(self, name, tmp_session_root):
        with pytest.raises(ToolError, match="reserved"):
            clone_storage.require_allowed_user_data_dir(name, None)

    def test_default_is_no_longer_refused_as_a_name(self):
        assert "default" not in profile_seed.RESERVED_NAMES
        assert profile_seed.DEFAULT_SESSION == "default"


# ---------------------------------------------------------------------------
# 4. No user-facing string says the old words — derived, never listed
# ---------------------------------------------------------------------------


class TestNoUserFacingStringSaysTheOldWords:
    async def test_the_served_tool_surface_is_clean(self):
        """Every tool description and every parameter description FastMCP
        serves, read off the live registry rather than off the source."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        try:
            from dump_tool_surface import _surface
        finally:
            sys.path.pop(0)

        offenders: list[str] = []
        for name, record in (await _surface()).items():
            offenders.extend(
                f"{name}.description says {found!r}"
                for found in _says_retired(record.get("description"))
            )
            schema = record.get("input_schema") or {}
            for param, spec in (schema.get("properties") or {}).items():
                if isinstance(spec, dict):
                    offenders.extend(
                        f"{name}.{param} says {found!r}"
                        for found in _says_retired(spec.get("description"))
                    )
        assert not offenders, offenders

    def test_the_cli_parser_tree_is_clean(self):
        """Walked off the real parser, so a flag added tomorrow is covered."""

        def walk(parser: argparse.ArgumentParser, path: str) -> list[str]:
            found: list[str] = []
            for field in ("description", "epilog"):
                found.extend(
                    f"{path}.{field} says {word!r}"
                    for word in _says_retired(getattr(parser, field, None))
                )
            for action in parser._actions:
                if action.help is not argparse.SUPPRESS:
                    found.extend(
                        f"{path} {action.dest} help says {word!r}"
                        for word in _says_retired(action.help)
                    )
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict):
                    for name, sub in choices.items():
                        if isinstance(sub, argparse.ArgumentParser):
                            found.extend(walk(sub, f"{path} {name}"))
            return found

        assert not walk(cli.build_parser(), "stealthy")

    async def test_every_role_reports_itself_without_the_old_words(
        self, tmp_session_root
    ):
        """The three roles' real ``profile_selection`` payloads — keys AND
        values — produced by the resolver, not transcribed.

        **A value that is an absolute PATH is exempt, and that exemption is
        the cost of this change rather than a hole in it.** The two directories
        are still called ``master`` and ``master-snapshot`` on disk: renaming
        them would move every existing installation's profiles and invalidate
        every configured ``BROWSER_MASTER_USER_DATA_DIR``, for a word that
        appears in a path an operator reads and never types. So an operator can
        still SEE "master" in ``spawn_diagnostics``; what they can no longer be
        TOLD is a role, a reason, an error or a seed named after it. Everything
        this release authors is checked; only what the filesystem already held
        is skipped, and F-896 §6 records it as the residual it is.
        """
        offenders: list[str] = []
        selections = [
            await _selection(),  # the shared session
            await _selection(session="acme"),  # a named session
            await _selection(session="default"),
        ]
        for selection in selections:
            for key, value in selection.items():
                offenders.extend(
                    f"key {key!r} says {word!r}" for word in _says_retired(key)
                )
                if isinstance(value, str) and Path(value).is_absolute():
                    continue  # an on-disk path, not a word this release authors
                offenders.extend(
                    f"{key}={value!r} says {word!r}" for word in _says_retired(value)
                )
        assert not offenders, offenders

    async def test_the_seed_a_session_reports_is_a_session_you_can_open(
        self, tmp_session_root
    ):
        """The exemption above must not be a door: ``seeded_from`` is a NAME,
        never a path, so it is checked without it — and the name it answers has
        to be one a caller can actually pass to ``session=``."""
        named = await _selection(session="acme")
        assert named["seeded_from"] == profile_seed.DEFAULT_SESSION
        assert clone_storage.require_allowed_user_data_dir(
            None, named["seeded_from"]
        ), "the seed a session names must itself be openable by that name"

    def test_the_reserved_refusal_says_the_word_only_because_you_did(
        self, tmp_session_root
    ):
        """The one message that must still contain ``master``: it is quoting
        the name the CALLER typed. Everything this product AUTHORS around that
        echo has to be clean, so the echo is removed before the check."""
        with pytest.raises(ToolError) as excinfo:
            clone_storage.require_allowed_user_data_dir("master", None)
        authored = str(excinfo.value).replace("'master'", "").replace("master", "", 1)
        assert not _says_retired(authored), authored


# ---------------------------------------------------------------------------
# 5. The CLI surface
# ---------------------------------------------------------------------------


def _spawn_namespace(**overrides) -> argparse.Namespace:
    base = {"session": None, "profile": None, "headed": False, "headless": False}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestStealthySpawnFlags:
    def test_session_is_sent_as_the_tools_session_argument(self):
        arguments = cli_call._spawn_arguments(_spawn_namespace(session="acme"))
        assert arguments == {"session": "acme"}

    def test_profile_still_works_and_says_it_is_deprecated(self, capsys):
        arguments = cli_call._spawn_arguments(_spawn_namespace(profile="acme"))
        assert arguments == {"user_data_dir": "acme"}
        warning = capsys.readouterr().err
        assert "--session" in warning and "deprecated" in warning.casefold()

    def test_profile_is_accepted_but_undocumented(self):
        spawn = _spawn_parser()
        profile = next(a for a in spawn._actions if a.dest == "profile")
        assert profile.help is argparse.SUPPRESS

    def test_session_is_the_documented_spelling(self):
        spawn = _spawn_parser()
        session = next(a for a in spawn._actions if a.dest == "session")
        assert session.help and session.help is not argparse.SUPPRESS

    def test_both_flags_are_left_for_the_tool_to_refuse(self):
        """The conflict rule has ONE home and it is not here: the CLI passes
        both on and the backend raises, so the two surfaces cannot disagree."""
        arguments = cli_call._spawn_arguments(
            _spawn_namespace(session="a", profile="b")
        )
        assert arguments["session"] == "a"
        assert arguments["user_data_dir"] == "b"


def _spawn_parser() -> argparse.ArgumentParser:
    parser = cli.build_parser()
    action = next(
        a for a in parser._actions if isinstance(getattr(a, "choices", None), dict)
    )
    return action.choices["spawn"]
