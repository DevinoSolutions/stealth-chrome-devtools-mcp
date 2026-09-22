"""F-921's counting half, away from the parser that prints it.

`persistent_profile_risk.assess` answers "which tracked browsers keep their
logins, and which of those profiles is open right now". It exists as a leaf
because `kill-orphans --force` skips F-888's persistent-profile spare and is
the one verb left that can end a human's logged-in Chrome, so the count has to
be checkable without building a CLI parser or capturing stdout.

Pure: entries are dicts, the hold predicate is injected, nothing is read from
disk and nothing is probed.
"""

from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import persistent_profile_risk
from stealth_chrome_devtools_mcp.embedded.browser_pid_registry import normalize_entries

NOTHING_OPEN = "nothing is open"
EVERYTHING_OPEN = "everything is open"


def _entries(*specs):
    """Entries as the record hands them over — through `normalize_entries`, so
    `user_data_dir` arrives normalized exactly as `read_entries` would give it
    and this test cannot pass on a shape production never sees."""
    return normalize_entries(
        {
            f"i{index}": {
                "pid": 100 + index,
                "create_time": 1.0,
                "user_data_dir": str(directory),
                "uses_custom_data_dir": True,
                "auto_clone": auto_clone,
            }
            for index, (directory, auto_clone) in enumerate(specs)
        }
    )


def _never_open(_path: Path) -> bool:
    return False


def _always_open(_path: Path) -> bool:
    return True


class TestAssess:
    def test_an_empty_record_is_zero_and_no_names(self):
        """The case that made the CLI warn about nothing: with no record at
        all there is no login to lose, and the caller needs to be able to SEE
        that rather than infer it from a zero."""
        risk = persistent_profile_risk.assess({}, is_open=_always_open)
        assert risk.tracked == 0
        assert risk.open_names == ()

    def test_auto_clones_are_not_persistent(self, tmp_path):
        """`on_persistent_profile` is the predicate `--force` skips, so a
        disposable clone must not appear here — its whole contract is that it
        dies with its browser."""
        risk = persistent_profile_risk.assess(
            _entries((tmp_path / "sess-auto", True)), is_open=_always_open
        )
        assert risk.tracked == 0
        assert risk.open_names == ()

    def test_persistent_entries_are_counted(self, tmp_path):
        risk = persistent_profile_risk.assess(
            _entries((tmp_path / "github", False), (tmp_path / "master", False)),
            is_open=_never_open,
        )
        assert risk.tracked == 2
        assert risk.open_names == ()

    def test_two_entries_on_one_directory_count_once(self, tmp_path):
        """The reap kills by `user_data_dir`
        (`process_cleanup._kill_processes_for_metadata`), so two entries on one
        profile end one profile. A count of ENTRIES said "2" about one."""
        shared = tmp_path / "master"
        risk = persistent_profile_risk.assess(
            _entries((shared, False), (shared, False)), is_open=_always_open
        )
        assert risk.tracked == 1
        assert risk.open_names == ("master",)

    def test_open_names_are_sorted_basenames_only(self, tmp_path):
        """Names and counts, never a path — a caller PRINTS this and a path
        names the operating user (F-869/F-877)."""
        risk = persistent_profile_risk.assess(
            _entries((tmp_path / "zulu", False), (tmp_path / "alpha", False)),
            is_open=_always_open,
        )
        assert risk.open_names == ("alpha", "zulu")
        assert str(tmp_path) not in "".join(risk.open_names)

    def test_the_hold_predicate_is_the_callers(self, tmp_path):
        """Injected on `backend_liveness`'s pattern: answering it needs
        `profile_lock` through `clone_storage`'s adapter, and importing that
        would make this module a node in the lifecycle graph. The pin is that
        the answer follows the predicate and nothing else — both directories
        exist, so a presence test (F-871) would answer the same either way."""
        asked: list[str] = []

        def only_master(path: Path) -> bool:
            asked.append(path.name)
            return path.name == "master"

        risk = persistent_profile_risk.assess(
            _entries((tmp_path / "github", False), (tmp_path / "master", False)),
            is_open=only_master,
        )
        assert risk.tracked == 2
        assert risk.open_names == ("master",)
        assert sorted(asked) == ["github", "master"]

    def test_an_entry_naming_no_directory_is_skipped(self):
        """A legacy entry recorded as a bare pid carries `user_data_dir: None`
        (`normalize_entries`), and there is no directory to warn about."""
        risk = persistent_profile_risk.assess(
            normalize_entries({"legacy": 4242}), is_open=_always_open
        )
        assert risk.tracked == 0

    def test_it_is_a_leaf_that_reads_no_record(self):
        """A leaf by construction: the record arrives as ENTRIES, like
        `backend_liveness.survey`, so one read can serve several questions and
        this module can never disagree with the reaper about which file it
        read. The import pin is the claim its own docstring makes — that it
        imports `browser_pid_registry` (itself a leaf) and stdlib, and so can
        never become a node in the lifecycle graph."""
        import ast
        import inspect

        signature = inspect.signature(persistent_profile_risk.assess)
        assert list(signature.parameters) == ["entries", "is_open"]
        source = Path(persistent_profile_risk.__file__).read_text(encoding="utf-8")
        ours = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("stealth_chrome_devtools_mcp")
        }
        assert ours == {"stealth_chrome_devtools_mcp.embedded.browser_pid_registry"}
        # It is handed entries; reading the record is the caller's, through the
        # reaper's own `_load_tracked_pids`.
        assert "read_entries" not in source
