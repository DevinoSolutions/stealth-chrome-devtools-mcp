"""F-901 — a profile request may not reach a directory that HOLDS profiles.

``anchor`` decides where a relative ``user_data_dir`` / ``session`` request
lands. It has always had an ``inside(anchored, roots.clones)`` call, but that
call is a DISAMBIGUATION — "did the caller already write the ``sessions/``
prefix?" — and never a guard: whatever the other branch composes is returned
unchecked. So a request made of dots walked straight out of it (measured on
2.1.11 and on 2953e8d):

    user_data_dir="."       -> <root>/sessions          THE CLONE ROOT
    user_data_dir="./"      -> <root>/sessions          THE CLONE ROOT
    user_data_dir=".\\"     -> <root>/sessions          (Windows folds it)
    user_data_dir="..."     -> <root>/sessions/...      resolves to THE CLONE ROOT
    user_data_dir=".."      -> <root>/sessions/..       THE BROWSER-SESSION ROOT
    user_data_dir="../.."   -> <root>/sessions/../..    above both

Chrome would then be handed the directory that holds every session — or the one
that holds every session AND the shared profile AND its seed — as its own
user-data-dir, and write its profile files among them. No refusal could see it
either: ``Path("..").name`` is ``""``, so the fold, reserved-name, ``default``
and seed clauses all pass it through.

``session=`` refused every one of these already (they are paths, and a session
is a name), so this was also the two spellings disagreeing about one request —
the asymmetry ``profile_request`` exists to remove.

**The rule these pin is flavour-uniform even where the filesystem is not**: a
relative request must land STRICTLY INSIDE the clone root. What each host folds
away differs — Windows drops a trailing dot or space from a component, POSIX
keeps it — so ``...`` is refused here and is an ordinary (if odd) directory
name there. That is the invariant every node below asserts: refused, or inside.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: Every shape the review measured, plus the two walks that come back. Both
#: separators are here on purpose: a backslash is a separator on Windows and a
#: filename character on POSIX, so these are ONE list and the assertions below
#: are what has to hold on either host — never a platform branch in the pin.
DOT_SHAPES = [
    ".",
    "./",
    ".\\",
    "...",
    "..",
    "../..",
    "..\\..",
    "sub/..",
    "sessions/..",
    "sessions/../..",
    "acme/../..",
]

#: The subset that is a path walk under BOTH flavours, so the refusal itself —
#: not merely the containment — is the same answer on all seven CI cells.
UNIFORM_SHAPES = [".", "./", "..", "../..", "sub/..", "sessions/.."]


def _roots() -> tuple[Path, Path]:
    """The two directories that HOLD profiles, as the product resolves them."""
    return clone_storage.clone_root_dir(), clone_storage.default_session_root()


class TestADotRequestNeverReachesADirectoryThatHoldsProfiles:
    @pytest.mark.parametrize("given", DOT_SHAPES)
    def test_the_landing_is_refused_or_strictly_inside_the_clone_root(
        self, given, tmp_session_root
    ):
        """THE invariant, and the one thing that has to be true on every host.

        Either answer is acceptable — a refusal, or a directory of the caller's
        own under the clone root — because which spellings the FILESYSTEM folds
        away is the host's business. What may never happen is the third answer
        2.1.11 gave: a landing ON, or ABOVE, a directory that holds profiles.
        """
        clones, session_root = _roots()
        try:
            landed = Path(clone_storage.require_allowed_user_data_dir(given, None))
        except ToolError:
            return
        assert not profile_seed.same_dir(landed, clones), (given, landed)
        assert not profile_seed.same_dir(landed, session_root), (given, landed)
        assert clone_storage._is_relative_to(landed, clones), (given, landed)

    @pytest.mark.parametrize("given", UNIFORM_SHAPES)
    def test_a_walk_out_of_the_clone_root_is_refused_in_words(
        self, given, tmp_session_root
    ):
        """And the refusal has to SAY something a caller can act on, because
        these arrive from a client that meant a session name."""
        with pytest.raises(ToolError) as excinfo:
            clone_storage.require_allowed_user_data_dir(given, None)
        message = str(excinfo.value)
        assert "session" in message or "name" in message, message

    @pytest.mark.parametrize("given", UNIFORM_SHAPES)
    def test_both_spellings_answer_the_same_way(self, given, tmp_session_root):
        """``session=`` refused all of these before this finding and the alias
        honoured them — the asymmetry ``profile_request`` exists to remove,
        wearing dots instead of whitespace."""
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir(None, given)
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir(given, None)

    @pytest.mark.parametrize("flavour", [PurePosixPath, PureWindowsPath])
    def test_the_walk_is_relative_under_both_flavours(self, flavour, tmp_session_root):
        """What gates the rule is ``Path.is_absolute()`` — the flavour ``anchor``
        anchors with — and ``..`` is relative under both, which is what makes
        the refusal identical on six POSIX cells and one Windows box."""
        assert not flavour("..").is_absolute()
        assert not flavour("../..").is_absolute()
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir("..", None)


class TestTheRootsAreNotProfiles:
    def test_the_clone_root_is_refused_by_absolute_path(self, tmp_session_root):
        """The same hazard by the other door. An absolute path is otherwise the
        caller's own business (F-896 §2.1), and it still is — but these two
        directories are not profiles, they are where profiles live, and a
        browser opened on one writes its own profile files among them."""
        clones, _ = _roots()
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir(str(clones), None)

    def test_the_browser_session_root_is_refused_by_absolute_path(
        self, tmp_session_root
    ):
        _, session_root = _roots()
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir(str(session_root), None)

    def test_an_ordinary_absolute_path_is_still_the_callers_own(
        self, tmp_path, tmp_session_root
    ):
        """The refusal above is two equalities and nothing wider: a directory
        that merely lives near the roots, or anywhere else, is untouched."""
        target = tmp_path / "work" / "profile"
        assert clone_storage.require_allowed_user_data_dir(str(target), None) == str(
            target
        )


class TestAWalkThatStaysInside:
    def test_it_lands_on_the_directory_it_normalises_to(self, tmp_session_root):
        """``sub/../acme`` is not refused, and that is a decision rather than an
        oversight: after normalisation it IS ``acme`` — the same directory, the
        same marker, nothing second created — so the product answers with the
        canonical form and there is one directory with one answer, which is what
        convention 4 asks for. Refusing it would buy nothing and cost a caller
        who composed a path from parts.
        """
        landed = clone_storage.require_allowed_user_data_dir("sub/../acme", None)
        plain = clone_storage.require_allowed_user_data_dir("acme", None)
        assert landed == plain

    def test_an_ordinary_name_is_untouched(self, tmp_session_root):
        clones, _ = _roots()
        landed = Path(clone_storage.require_allowed_user_data_dir("acme", None))
        assert landed == clones / "acme"

    def test_the_sessions_prefix_still_means_the_same_directory(self, tmp_session_root):
        """`anchor`'s disambiguation is what this finding leaves alone: a caller
        who writes the prefix gets `sessions/acme`, not `sessions/sessions/acme`."""
        clones, _ = _roots()
        landed = Path(
            clone_storage.require_allowed_user_data_dir("sessions/acme", None)
        )
        assert landed == clones / "acme"
