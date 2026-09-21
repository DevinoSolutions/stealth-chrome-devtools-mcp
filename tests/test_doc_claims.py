"""M14 S8 — doc-claim accuracy harness (plan_M14 §4 / §5.3 / §5.4).

The single failure mode of the M14 docs is *docs that lie about the tree*. This
harness is the guard: it fails loudly if a root doc names a module, env var, CLI
verb, or load-bearing symbol the tree does not have, if a tombstoned module comes
back, or if the documented tool count drifts from the live registry. It keeps
DESIGN / CLAUDE / RUNBOOK / CONTRIBUTING + README honest against the code, in CI.

It deliberately does NOT assert the F-403 "uv run fails on the &-path" claim: that
is true only in a checkout whose path contains spaces/`&` (the dev checkout), not on
CI's clean path, so it is verified by the Stage-4 executor in the real checkout, not
pinned here (pinning it would fail on CI).
"""

import itertools
import re
import tomllib
from pathlib import Path
from typing import ClassVar

import pytest

from stealth_chrome_devtools_mcp import cli
from stealth_chrome_devtools_mcp.embedded import server, tool_registry
from stealth_chrome_devtools_mcp.settings import Settings

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "src" / "stealth_chrome_devtools_mcp"
DOCS = ["README.md", "DESIGN.md", "CLAUDE.md", "RUNBOOK.md", "CONTRIBUTING.md"]


def _doc_text() -> str:
    return "\n".join((REPO / d).read_text(encoding="utf-8") for d in DOCS)


class TestDocFilesPresent:
    def test_all_root_docs_exist(self):
        for d in DOCS:
            assert (REPO / d).is_file(), f"{d} is missing"


class TestDocumentedEnvVars:
    # STEALTH_MCP_SESSION_STORAGE_CAP_GB is intentionally documented as the
    # RETIRED pre-A1 name (the README/RUNBOOK migration notes); it is the one
    # STEALTH_MCP_* token the docs may name that is no longer a live field.
    RETIRED: ClassVar[set[str]] = {"STEALTH_MCP_SESSION_STORAGE_CAP_GB"}

    def test_every_documented_stealth_env_var_is_real(self):
        known = Settings._known_env_names()  # upper-cased, incl. legacy aliases
        mentioned = set(re.findall(r"STEALTH_MCP_[A-Z0-9_]+", _doc_text()))
        assert mentioned, "expected the docs to mention STEALTH_MCP_* env vars"
        for name in sorted(mentioned):
            assert name in known or name in self.RETIRED, (
                f"{name} is documented but is not a Settings env var "
                "(rename drift? add a typed field or fix the doc)"
            )

    def test_a1_renamed_cap_var_is_documented(self):
        text = _doc_text()
        assert "STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB" in text
        # the new name must be a live field
        assert (
            "STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB" in Settings._known_env_names()
        )


class TestDocumentedCliVerbs:
    VERBS: ClassVar[list[str]] = [
        "status",
        "profiles",
        "cleanup",
        "doctor",
        "stop",
        "restart",
        "kill-orphans",
        "serve",
        # F-891 — the six verbs that drive the LIVE backend's tool surface.
        "tools",
        "call",
        "ls",
        "spawn",
        "nav",
        "close",
    ]

    @staticmethod
    def _handler_for(verb: str):
        """What `cli.main` would dispatch `verb` to.

        TWO tables since F-891's extraction — the ops verbs' in `cli`, the six
        tool verbs' in `cli_call` beside the bodies they name — looked up in
        the same order `main` uses, so this pin cannot pass for a verb `main`
        itself could not resolve.
        """
        return cli._DISPATCH.get(verb) or cli._cli_call().DISPATCH.get(verb)

    def test_verbs_exist_and_are_documented(self):
        text = _doc_text()
        for verb in self.VERBS:
            assert self._handler_for(verb) is not None, f"{verb} reaches no body"
            assert verb in text, f"{verb} not documented in the root docs"

    def test_every_dispatchable_verb_has_a_parser(self):
        """The other direction: a body no parser offers is unreachable, and the
        split into two tables is exactly how one could come to be."""
        offered = set(cli.build_parser()._subparsers._group_actions[0].choices)
        dispatchable = set(cli._DISPATCH) | set(cli._cli_call().DISPATCH)
        assert dispatchable == offered
        assert set(self.VERBS) == offered
        # The ops table wins SILENTLY on a shared key, and set equality above
        # passes a duplicate — so the disjointness is its own assertion.
        assert not (set(cli._DISPATCH) & set(cli._cli_call().DISPATCH))

    def test_renamed_flag_present_old_absent_in_cli(self):
        parser = cli.build_parser()
        ns = parser.parse_args(["cleanup", "--browser-session-cap-gb", "1.0"])
        assert ns.browser_session_cap_gb == 1.0
        with pytest.raises(SystemExit):  # old flag no longer recognized
            parser.parse_args(["cleanup", "--session-cap-gb", "1.0"])


class TestNavMapModules:
    # Every embedded module the CLAUDE.md nav map points at must exist.
    LIVE_EMBEDDED: ClassVar[list[str]] = [
        "browser_manager",
        "singleton",
        "backend_registry",
        "display_context",
        "browser_pid_registry",
        "browser_reattach",
        "browser_cmdline",
        "backend_client",
        "cdp_attach",
        "tool_registry",
        "tool_errors",
        "logging_setup",
        "process_cleanup",
        "models",
        "platform_utils",
        "cdp_element_cloner",
        "file_based_element_cloner",
        "progressive_element_cloner",
        "clone_storage",
        "network_interceptor",
        "dynamic_hook_system",
        "dynamic_hook_ai_interface",
        "hook_learning_system",
        "cdp_function_executor",
        "response_handler",
        "in_memory_storage",
        "debug_logger",
        "element_resolution",
    ]
    LIVE_TOPLEVEL: ClassVar[list[str]] = [
        "cli",
        "server",
        "settings",
        "observability",
        "expected_events",
        "__main__",
    ]
    # Tombstones: the docs say these are GONE; if one comes back the tombstone lies.
    TOMBSTONES: ClassVar[list[str]] = [
        "embedded/element_cloner.py",
        "embedded/comprehensive_element_cloner.py",
        "embedded/persistent_storage.py",
        "embedded/response_stage_hooks.py",
        "env_utils.py",
    ]

    def test_live_modules_exist(self):
        for name in self.LIVE_EMBEDDED:
            assert (PKG / "embedded" / f"{name}.py").is_file(), name
        for name in self.LIVE_TOPLEVEL:
            assert (PKG / f"{name}.py").is_file(), name
        # the browser-side JS payload dir the cloner engine loads from
        assert (PKG / "embedded" / "js").is_dir()

    def test_tombstoned_modules_are_gone(self):
        for rel in self.TOMBSTONES:
            assert not (PKG / rel).exists(), f"{rel} is tombstoned in docs but exists"
            assert not (PKG / "embedded" / rel).exists()


class TestLoadBearingSymbols:
    """The specific symbols the docs lean on, by the module/attr the doc names."""

    def test_symbols_resolve(self):
        import importlib

        expected = {
            "embedded.singleton": [
                "_backend_http_ready",
                "_probe_backend_status",
                "_source_fingerprint",
                "_select_backend_port",
                "DEFAULT_PORT",
                "run_stdio_proxy",
                "stop_backend",
                "restart_backend",
            ],
            "embedded.backend_registry": [
                "SCHEMA_VERSION",
                "read_backends",
                "record_backend",
                "forget_entries",
                "clear_record",
                "adoption_candidates",
                "window_capable_first",
                "port_for_context",
                "own_or_first_port",
                "port_conflict",
                "read_record",
                "first_backend",
                "backend_on_port",
                "recorded_int",
                "STATE_DIR",
                "SERVER_STATE_FILE",
                "PORT_FILE",
            ],
            "embedded.build_identity": [
                "version",
                "source_fingerprint",
                "ATTEMPTS",
                "RETRY_SECONDS",
                "UNKNOWN_VERSION",
            ],
            "embedded.display_context": [
                "display_context",
                "can_show_windows",
                "HEADLESS",
                "UNVERIFIED",
            ],
            "embedded.browser_pid_registry": [
                "OWNER_PID",
                "OWNER_CREATE_TIME",
                "RECORD_NAME",
                "read_entries",
                "update_entries",
                "on_persistent_profile",
                "recorded_port",
                "valid_port",
                "owner_identity",
                "claim_browser",
                "release_claim",
            ],
            "embedded.browser_reattach": [
                "adoptable",
                "adoptable_for",
                "held_by",
                "adopt_held_profile",
                "claim",
                "endpoint",
                "reap_recorded",
                "recorded_browser_alive",
                "start",
                "run",
                "Adoptable",
                "Classified",
                "Held",
                "Refused",
                "DEVTOOLS_PORT_FILE",
                "ATTACH_BUDGET_SECONDS",
            ],
            "embedded.cdp_attach": [
                "config_for",
                "attach",
                "attach_reclaiming",
                "close",
                "CDP_HOST",
            ],
            "embedded.browser_cmdline": [
                "arguments",
                "flag_value",
                "debug_port",
                "is_headless",
                "dead_local_proxy",
                "DEBUG_PORT_FLAG",
            ],
            "embedded.backend_eviction": [
                "pid_on_port",
                "terminate",
                "owned_browsers",
                "protected",
                "clear_stale",
            ],
            "embedded.navigation_milestone": [
                "MILESTONES",
                "require",
                "navigate",
                "landing",
                "Progress",
                "LANDING_JS",
                # F-882e: the swap key and its bound, both named in CLAUDE.md.
                "document_swapped",
                "SWAPPED_CODE",
                "SWAPPED_MESSAGE",
                "LANDING_SWAP_RETRIES",
            ],
            "embedded.tool_registry": ["SECTION_TOOLS", "ToolRegistry"],
            "embedded.tool_errors": [
                "ToolError",
                "InstanceNotFoundError",
                "_require_tab",
                "_require_browser",
            ],
            "embedded.logging_setup": [
                "resolve_log_dir",
                "with_correlation_id",
                "CorrelationIdFilter",
            ],
            "embedded.cdp_element_cloner": ["CDPElementCloner", "cdp_element_cloner"],
            "embedded.file_based_element_cloner": ["FileBasedElementCloner"],
            "embedded.clone_storage": ["browser_session_storage_cap_bytes"],
            "embedded.network_interceptor": ["NetworkInterceptor"],
            "embedded.process_cleanup": ["ProcessCleanup"],
            "embedded.in_memory_storage": ["InMemoryStorage", "in_memory_storage"],
            "settings": ["Settings", "get_settings"],
        }
        for mod_suffix, attrs in expected.items():
            mod = importlib.import_module(f"stealth_chrome_devtools_mcp.{mod_suffix}")
            for attr in attrs:
                assert hasattr(mod, attr), (
                    f"{mod_suffix}.{attr} named in docs but missing"
                )

    def test_process_cleanup_activation_seam(self):
        from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

        assert hasattr(ProcessCleanup, "activate")
        assert hasattr(ProcessCleanup, "recover_orphans")


class TestDocumentedToolCount:
    def test_docs_say_94_and_registry_agrees(self):
        registry_total = sum(len(v) for v in server.SECTION_TOOLS.values())
        assert registry_total == 94
        assert tool_registry.SECTION_TOOLS is server.SECTION_TOOLS
        # every root doc that cites a tool count cites 94 (no 90/96/97/99 left)
        text = _doc_text()
        for stale in ("90 tools", "96 tools", "97 tools", "99 tools"):
            assert stale not in text, f"stale tool count '{stale}' still in docs"
        assert "94 tools" in text


# ---------------------------------------------------------------------------
# F-923 — CHANGELOG PLACEMENT, which `TestChangelogIntegrity` did not guard
# ---------------------------------------------------------------------------
#: A release heading names its version FIRST. Two shapes exist in this file and
#: the regex takes both without a special case: ``## 2.1.12``, and the legacy
#: terminal catch-all ``## 1.2.0 and earlier``, whose version is still the
#: leading token. Deriving the version from the heading rather than listing the
#: headings is what lets a new release join with no edit here.
_RELEASE_HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+)(?: .*)?$")

#: The one heading that is not a release. Compared by EQUALITY, never by
#: prefix: ``## Unreleased notes`` is a section someone invented and should be
#: reported, not silently accepted as the Unreleased block.
_UNRELEASED = "## Unreleased"


def _headings(text: str) -> list[str]:
    """Every ``## `` heading, in file order."""
    return [line.rstrip() for line in text.splitlines() if line.startswith("## ")]


def _unreleased_problem(text: str) -> str | None:
    """AT MOST one ``## Unreleased``, and if present it LEADS.

    "At most", not "exactly", and that is measured rather than preferred: a
    release commit renames ``## Unreleased`` to ``## <version>`` and adds no
    replacement, so on 2.1.11 (8ae59ce), 2.1.12 (031318c) and 2.1.13 (9bb948c)
    the file legitimately carries NO Unreleased heading at all. A node
    demanding one would go red on every release commit, which is how a gate
    gets disabled in a week.
    """
    headings = _headings(text)
    if not headings:
        return "CHANGELOG has no `## ` headings at all"
    found = [i for i, h in enumerate(headings) if h == _UNRELEASED]
    if len(found) > 1:
        return (
            f"CHANGELOG states `{_UNRELEASED}` {len(found)} times "
            f"(at heading positions {found}); there is one queue, not several"
        )
    if found and found[0] != 0:
        return (
            f"`{_UNRELEASED}` is not the first heading — it sits below "
            f"{headings[found[0] - 1]!r}. An entry under a SHIPPED heading is a "
            f"false statement about a released artefact"
        )
    return None


def _ordering_problem(text: str) -> str | None:
    """Every non-Unreleased ``## `` heading is a release, and they DESCEND.

    Strictly: an equal pair is a release heading stated twice, which is how a
    hand-resolved merge duplicates a section.
    """
    problems = []
    versions: list[tuple[tuple[int, int, int], str]] = []
    for heading in _headings(text):
        if heading == _UNRELEASED:
            continue
        match = _RELEASE_HEADING.match(heading)
        if match is None:
            problems.append(f"{heading!r} is neither `{_UNRELEASED}` nor a release")
            continue
        versions.append((tuple(int(p) for p in match.groups()), heading))
    if problems:
        return "; ".join(problems)
    for (earlier, first), (later, second) in itertools.pairwise(versions):
        if earlier <= later:
            return (
                f"release headings are not in descending order: {first!r} is "
                f"followed by {second!r}"
            )
    return None


def _version_sync_problem(text: str, version: str) -> str | None:
    """``pyproject.toml``'s version IS the top release heading.

    MEASURED true in all three states this repo produces, which is the whole
    reason it is safe to assert: on a release commit (the bump and the heading
    rename are ONE commit — 9bb948c changes one line of each), on an ordinary
    commit after it (pyproject 2.1.12, top release heading ``## 2.1.12``, with
    ``## Unreleased`` above), and on a feature branch that has merged main.

    The `or is absent from the CHANGELOG entirely` arm this was first drafted
    with is deliberately NOT here: this repo never produces that state, and an
    alternative that nothing can reach is a hole in the pin rather than
    tolerance — it would accept a release heading silently deleted.
    """
    top = next(
        (h for h in _headings(text) if _RELEASE_HEADING.match(h)),
        None,
    )
    if top is None:
        return "CHANGELOG carries no release heading at all"
    if top != f"## {version}":
        return (
            f"pyproject.toml is version {version!r} but the top release heading "
            f"is {top!r}; the bump and the heading must move together"
        )
    return None


def _pyproject_version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]


class TestChangelogIntegrity:
    """The CHANGELOG is a statement about shipped artefacts; a merge must not
    leave it half-said. Release 2.1.8's branch found ``<<<<<<< HEAD`` /
    ``=======`` / ``>>>>>>> origin/main`` committed to ``main`` by two fix
    merges (the F-875 and F-878 branches), wrapping the whole 2.1.7 section,
    and nothing in the gate read the file. Two invariants, both cheap: no
    conflict marker survives in a root doc, and no ``### `` heading is stated
    twice (a section auto-placed by a clean merge AND re-added by hand reads as
    two fixes).

    **F-923 adds the third question those two never asked: PLACEMENT.** Both
    nodes above were green throughout the incident that motivated this class —
    and a green ``test_doc_claims`` has been read since as placement being
    verified, which it never was. See
    ``audit/stage2/finding_F923_changelog_placement_is_unguarded.md``, whose §6
    is explicit that these structural nodes still do NOT catch the exact
    failure that started it: after a bad merge the file is
    byte-indistinguishable from a legitimate release commit, so that shape
    needs a base ref and lives in CONTRIBUTING as a procedure instead."""

    MARKER = re.compile(r"^(<<<<<<< |=======$|>>>>>>> )", re.MULTILINE)

    def test_no_conflict_markers_in_root_docs(self):
        for d in ["CHANGELOG.md", *DOCS]:
            text = (REPO / d).read_text(encoding="utf-8")
            hit = self.MARKER.search(text)
            assert hit is None, (
                f"{d} carries a merge conflict marker at offset {hit.start()}: "
                f"{hit.group(0)!r}"
            )

    def test_changelog_states_each_section_once(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = [ln.strip() for ln in text.splitlines() if ln.startswith("### ")]
        assert headings, "expected ### sections in CHANGELOG.md"
        dupes = sorted({h for h in headings if headings.count(h) > 1})
        assert not dupes, f"CHANGELOG.md states these sections more than once: {dupes}"

    # -- F-923: PLACEMENT ---------------------------------------------------
    #: Deliberately mis-shaped CHANGELOGs, each naming the real mistake it
    #: models. They are fixture STRINGS: nothing here writes the real file,
    #: because a test that mutates `CHANGELOG.md` to prove it can fail is one
    #: interrupted run away from committing the mis-shaped copy.
    SHIPPED_HEADINGS: ClassVar[str] = (
        "# Changelog\n\n## 2.1.13\n\nfix A\n\n## 2.1.12\n\nfix B\n"
    )
    #: The mistake the POST-RELEASE merge procedure can make: main leads with
    #: `## 2.1.13` and carries no queue, so a branch has to CREATE one — and
    #: creating it in the wrong place puts a queue below a shipped heading.
    #: Deliberately NOT named for the 2.1.7 incident: that one left no
    #: Unreleased heading at all and is accepted here, which is the point
    #: `SHIPPED_HEADINGS` and the finding's §6 both make.
    QUEUE_PLACED_BELOW_A_RELEASE: ClassVar[str] = (
        "# Changelog\n\n## 2.1.13\n\nfix A\n\n## Unreleased\n\nmine\n\n## 2.1.12\n"
    )
    TWO_QUEUES: ClassVar[str] = (
        "# Changelog\n\n## Unreleased\n\nmine\n\n## Unreleased\n\nalso mine\n\n"
        "## 2.1.12\n"
    )
    DUPLICATED_RELEASE: ClassVar[str] = (
        "# Changelog\n\n## Unreleased\n\nmine\n\n## 2.1.12\n\nfix A\n\n## 2.1.12\n"
    )
    ASCENDING: ClassVar[str] = (
        "# Changelog\n\n## Unreleased\n\nmine\n\n## 2.1.11\n\nfix A\n\n## 2.1.12\n"
    )
    INVENTED_SECTION: ClassVar[str] = (
        "# Changelog\n\n## Unreleased\n\nmine\n\n## Next up\n\nsoon\n\n## 2.1.12\n"
    )

    def test_the_real_changelog_has_one_queue_and_it_leads(self):
        """F-923. An entry under a SHIPPED heading is a false statement about a
        released artefact, and nothing in the gate read placement until now."""
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        assert _unreleased_problem(text) is None, _unreleased_problem(text)

    def test_the_real_changelog_releases_descend(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        assert _ordering_problem(text) is None, _ordering_problem(text)

    def test_the_real_changelog_agrees_with_pyproject(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        version = _pyproject_version()
        assert _version_sync_problem(text, version) is None, _version_sync_problem(
            text, version
        )

    def test_a_release_commit_is_accepted_with_no_unreleased_heading(self):
        """The cry-wolf case, pinned so nobody "fixes" the rule into demanding
        one.

        MEASURED on 8ae59ce (2.1.11), 031318c (2.1.12) and 9bb948c (2.1.13):
        the release commit RENAMES `## Unreleased` and adds no replacement, so
        a rule reading "exactly one" would be red on every release.
        """
        assert _unreleased_problem(self.SHIPPED_HEADINGS) is None
        assert _ordering_problem(self.SHIPPED_HEADINGS) is None
        assert _version_sync_problem(self.SHIPPED_HEADINGS, "2.1.13") is None

    @pytest.mark.parametrize(
        "fixture",
        [
            pytest.param(
                "QUEUE_PLACED_BELOW_A_RELEASE", id="queue-below-a-shipped-heading"
            ),
            pytest.param("TWO_QUEUES", id="two-unreleased-sections"),
        ],
    )
    def test_a_misplaced_queue_is_caught(self, fixture):
        """The RED half. A pin for a gate must first be able to fail."""
        problem = _unreleased_problem(getattr(self, fixture))
        assert problem is not None, f"{fixture} was accepted"
        assert "Unreleased" in problem

    def test_the_incident_shape_is_not_caught_and_that_is_stated(self):
        """The limit of these three nodes, pinned so it cannot be forgotten.

        The 2.1.7 incident absorbed four entries INTO the shipped section and
        left no ``## Unreleased`` behind. The resulting file is
        byte-indistinguishable from a legitimate release commit — same
        headings, same order, same pyproject agreement — so all three rules
        accept it, and no rule reading only this file could do otherwise.

        This node exists so that a future reader who assumes F-923 closed the
        incident is contradicted by a test rather than by a paragraph. The
        control for THAT shape needs a base ref and is the
        ``git diff origin/main --numstat -- CHANGELOG.md`` procedure in
        CONTRIBUTING.md, not a node here.
        """
        absorbed = self.SHIPPED_HEADINGS.replace("fix A\n", "fix A\n\nmine\n")
        assert _unreleased_problem(absorbed) is None
        assert _ordering_problem(absorbed) is None
        assert _version_sync_problem(absorbed, "2.1.13") is None

    @pytest.mark.parametrize(
        "fixture",
        [
            pytest.param("DUPLICATED_RELEASE", id="release-heading-stated-twice"),
            pytest.param("ASCENDING", id="releases-out-of-order"),
            pytest.param("INVENTED_SECTION", id="a-heading-that-is-neither"),
        ],
    )
    def test_a_malformed_release_heading_is_caught(self, fixture):
        assert _ordering_problem(getattr(self, fixture)) is not None, (
            f"{fixture} was accepted"
        )

    def test_a_bump_without_its_heading_is_caught(self):
        """The two halves of a release move together or not at all."""
        assert _version_sync_problem(self.SHIPPED_HEADINGS, "2.1.14") is not None

    def test_the_legacy_catch_all_heading_is_not_a_special_case(self):
        """`## 1.2.0 and earlier` is a real heading in this file.

        It parses as 1.2.0 through the same regex every other release uses, so
        it neither needs an exemption nor gets one — which is what keeps the
        ordering rule derived rather than a list of known headings.
        """
        assert _RELEASE_HEADING.match("## 1.2.0 and earlier")
        assert (
            _ordering_problem(
                "# Changelog\n\n## Unreleased\n\nx\n\n## 2.1.12\n\ny\n\n"
                "## 1.2.0 and earlier\n\nz\n"
            )
            is None
        )
