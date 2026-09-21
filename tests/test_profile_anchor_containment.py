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

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: Every shape the review measured, plus the two walks that come back. Both
#: separators are here on purpose: a backslash is a separator on Windows and a
#: filename character on POSIX, so these are ONE list and the assertions below
#: are what has to hold on either host — never a platform branch in the pin.
#: ``sub\..\acme`` is the most instructive member (review N4): it is the one
#: shape carrying a NAME as well as dots, so it means two genuinely different
#: directories on the two hosts — ``sessions/acme`` on Windows, a literal
#: directory called ``sub\..\acme`` on POSIX — and BOTH satisfy the invariant.
DOT_SHAPES = [
    ".",
    "./",
    ".\\",
    "...",
    "..",
    "../..",
    "..\\..",
    "sub/..",
    "sub\\..\\acme",
    "sessions/..",
    "sessions/../..",
    "acme/../..",
]

#: The subset that is a path walk under BOTH flavours, so the refusal itself —
#: not merely the containment — is the same answer on all seven CI cells.
UNIFORM_SHAPES = [".", "./", "..", "../..", "sub/..", "sessions/.."]

#: How THIS host spells a filesystem root, in every separator it has. The list
#: is built per-platform rather than parametrized across both flavours because
#: `D:\` is a root on Windows and an ordinary relative filename on POSIX, so one
#: list would be asserting two different things under one name.
ROOT_SPELLINGS = ["D:\\", "D:/"] if os.name == "nt" else ["/"]


def _roots() -> tuple[Path, Path]:
    """The two directories that HOLD profiles, as the product resolves them."""
    return clone_storage.clone_root_dir(), clone_storage.default_session_root()


def _link_dir(link: Path, target: Path) -> bool:
    """Make *link* a directory link to *target*, or report that this host won't.

    A **junction** is the Windows shape an operator actually has: `mklink /J`
    needs neither administrator nor Developer Mode, while `os.symlink` on a
    directory needs one of them — so a pin written on `os.symlink` would skip
    on the very platform the regression was measured on. On POSIX a symlink is
    the same configuration. False means the platform refused to set it up, and
    the caller SKIPS with that as its reason rather than asserting about a link
    that is not there.
    """
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        shell = shutil.which("cmd")
        if shell is None:
            return False
        made = subprocess.run(
            [shell, "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        return made.returncode == 0
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


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
        """A guard, and the plainest one there is: the request every caller
        actually makes has to answer the directory it always did, or the rule
        above has been bought with the feature (review N1 — its two siblings
        said so in words and this one did not)."""
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


class TestASessionDirectoryMayBeALink:
    """A session directory that is a junction/symlink to storage elsewhere is a
    configuration 2.1.11 supported, and the containment rule must not take it
    away (review S1).

    It is why the WALK test is LEXICAL. `inside` — `clone_storage`'s
    `_is_relative_to` — resolves both sides, so asking it "did this land inside
    the clone root" reads a linked session directory as a walk OUT of it and
    refuses a request that has been working, through both spellings, with a
    message naming a path visibly inside the root. Every escape F-901 closes is
    folded by `normpath` BEFORE the check, so the lexical question closes the
    finding unchanged — and the two equalities one clause above it keep
    resolving, which is what still catches a link pointing AT a root and a
    component the OS folds away.
    """

    def test_a_linked_session_directory_still_opens(self, tmp_path, tmp_session_root):
        clones, _ = _roots()
        if not _link_dir(clones / "acme", tmp_path / "bigdisk" / "acme"):
            pytest.skip("this host will not create a directory link unprivileged")
        assert clone_storage.require_allowed_user_data_dir("acme", None) == str(
            clones / "acme"
        )

    def test_a_linked_session_directory_opens_through_either_spelling(
        self, tmp_path, tmp_session_root
    ):
        """The two spellings agreed on this before the finding and have to keep
        agreeing after it — the asymmetry is what `profile_request` exists to
        remove, and a rule that refused only one of them would put it back."""
        clones, _ = _roots()
        if not _link_dir(clones / "acme", tmp_path / "bigdisk" / "acme"):
            pytest.skip("this host will not create a directory link unprivileged")
        alias = clone_storage.require_allowed_user_data_dir("acme", None)
        named = clone_storage.require_allowed_user_data_dir(None, "acme")
        assert alias == named == str(clones / "acme")

    def test_a_nested_linked_session_directory_still_opens(
        self, tmp_path, tmp_session_root
    ):
        clones, _ = _roots()
        (clones / "team").mkdir(parents=True, exist_ok=True)
        if not _link_dir(clones / "team" / "acme", tmp_path / "bigdisk" / "acme"):
            pytest.skip("this host will not create a directory link unprivileged")
        assert clone_storage.require_allowed_user_data_dir("team/acme", None) == str(
            clones / "team" / "acme"
        )

    def test_a_link_pointing_at_the_clone_root_is_still_refused(self, tmp_session_root):
        """The guard on the clause that stays RESOLVING, and the reason it has
        to: lexically this lands inside the clone root, and what it opens is the
        clone root itself."""
        clones, _ = _roots()
        if not _link_dir(clones / "self", clones):
            pytest.skip("this host will not create a directory link unprivileged")
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir("self", None)

    def test_a_refusal_names_the_directory_it_really_opens(self, tmp_session_root):
        """And it must not name a path that contradicts it. The shipped message
        said `'self' names a directory profiles are KEPT in (<clones>\\self)` —
        a path visibly INSIDE the root, reported by the one sentence explaining
        that it is the root. The refusal reads the resolved target when the two
        differ (review S1)."""
        clones, _ = _roots()
        if not _link_dir(clones / "self", clones):
            pytest.skip("this host will not create a directory link unprivileged")
        with pytest.raises(ToolError) as excinfo:
            clone_storage.require_allowed_user_data_dir("self", None)
        message = str(excinfo.value)
        # The clone root's own path is a PREFIX of the link's, so one occurrence
        # is only the link being named — which is the self-contradicting half.
        # The second is the directory the request really opens.
        assert message.count(str(clones)) >= 2, message

    @pytest.mark.skipif(
        os.name != "nt",
        reason="`...` is an ordinary directory name on POSIX; only Windows "
        "folds the component away onto the clone root (finding §2.5)",
    )
    def test_a_component_the_filesystem_folds_away_is_still_refused(
        self, tmp_session_root
    ):
        """The other guard on the resolving clause. `normpath` leaves `...`
        alone, so nothing lexical can see this one — it is caught because
        `same_dir` resolves BOTH sides and Windows lands `<clones>/...` on
        `<clones>`."""
        with pytest.raises(ToolError):
            clone_storage.require_allowed_user_data_dir("...", None)


class TestACloneRootAtAFilesystemRoot:
    """`_inside_lexically` has to survive a clone root with no parent above it
    (review N1).

    `normpath` KEEPS the trailing separator on a filesystem root — `D:\\` stays
    `D:\\`, `/` stays `/` — so appending one more doubled it and matched
    nothing: an operator whose `STEALTH_MCP_BROWSER_SESSION_ROOT` sits at a
    drive root had EVERY relative request refused with "walks out of the
    session storage". No shipped configuration does this, which is why it is a
    nit and not an outage; it is pinned because the doubling is invisible by
    reading and the resolving twin answers the opposite.

    These call the predicate directly. It is pure and touches no filesystem, so
    the drive need not exist — and going through the gate would need the whole
    session root moved to `D:\\`, which is a fixture that cannot run on a CI
    box and would be measuring the fixture rather than the rule.
    """

    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_a_session_under_a_root_clone_root_is_inside_it(self, spelling):
        root = Path(spelling)
        assert profile_seed._inside_lexically(root / "acme", root), spelling
        assert profile_seed._inside_lexically(root / "sub" / "acme", root), spelling

    @pytest.mark.parametrize("spelling", ROOT_SPELLINGS)
    def test_the_root_itself_is_not_inside_itself(self, spelling):
        """Still STRICTLY inside: landing ON the clone root is the clause above
        this one, and it has its own message."""
        root = Path(spelling)
        assert not profile_seed._inside_lexically(root, root), spelling

    def test_an_ordinary_clone_root_is_unchanged(self, tmp_path):
        """The guard on the fix: stripping the separator before adding one back
        is the identity for every parent that is not a filesystem root, and the
        prefix trap a bare `startswith` would open stays shut."""
        clones = tmp_path / "sessions"
        assert profile_seed._inside_lexically(clones / "acme", clones)
        assert not profile_seed._inside_lexically(clones, clones)
        assert not profile_seed._inside_lexically(
            tmp_path / "sessions2" / "acme", clones
        )
