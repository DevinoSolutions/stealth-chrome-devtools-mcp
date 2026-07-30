"""Executable documentation examples and claims sync (plan_RELEASE W11, MQ-130).

A user's first five minutes are spent copy-pasting out of `README.md`. A fence
that no longer works is a worse first impression than any internal bug, and no
internal green catches it — nothing else in this suite reads the docs as
*commands*. This module closes that hole in two halves.

**Extract-and-run.** Every fenced block in the scanned docs that carries the ONE
reviewed marker (`<!-- doc-example: runnable -->`, exactly) is parsed into
commands, *statically screened*, and then executed in a bounded throwaway
directory with every state path redirected inside it. The screen is the load-
bearing part: it rejects external URLs, undeclared file writes, secrets,
interactive/blocking commands, and shell-metacharacter ambiguity BEFORE anything
runs, so marking a fence runnable can never turn the doc gate into an arbitrary
shell. Each executed fence is a separate parametrized node whose id records the
exact source file and fence ordinal, so the evidence names what it proved.

**Claims-sync.** The README advertises an install command, two console scripts,
and a tool list. Each is checked against the real thing: `[project]` in
`pyproject.toml` for the distribution name and version, `[project.scripts]` for
the two entry points, the live `SECTION_TOOLS` registry for the served surface,
and W5's `gen_release_contract.tool_rows()` for which of those tools are
*release-qualified*. Both counts in the README are derived here, never typed
twice — and a served-unqualified tool may be listed as served but never
presented as qualified.

Deliberately Chrome-free and network-free: it runs in the unit lane on every
qualified OS cell, which is where a broken README needs to be caught.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from release_gate_harness import _isolated_env, resolve_launcher

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import gen_release_contract as gen  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"

# ---------------------------------------------------------------------------
# The ONE reviewed runnable marker.
# ---------------------------------------------------------------------------
# A fence runs only when the immediately preceding non-blank line is EXACTLY
# this comment. There is deliberately no second spelling, no per-fence options
# and no "runnable-ish" tier: one marker means one reviewed decision, and a
# near-miss silently does nothing rather than half-running.
RUNNABLE_MARKER = "<!-- doc-example: runnable -->"

# Marked fences must also declare a shell info string, so a marked *output*
# sample (RUNBOOK shows several) can never be mistaken for a command list.
ALLOWED_INFO_STRINGS = frozenset({"console"})

# Docs a user copy-pastes from. `docs/` does not exist at this commit; the glob
# is here so a doc added there is scanned the day it lands, not the day someone
# remembers to extend a list.
DOC_SOURCES: tuple[Path, ...] = (
    REPO / "README.md",
    REPO / "RUNBOOK.md",
    REPO / "CONTRIBUTING.md",
    *sorted((REPO / "docs").glob("*.md")),
)


@dataclass(frozen=True)
class Fence:
    """One fenced block, with the evidence identifying it."""

    source: str  # repo-relative POSIX path
    ordinal: int  # 0-based index among ALL fences in that file
    info: str  # the fence info string ("console", "bash", "json", "")
    body: str
    marked: bool

    @property
    def evidence_id(self) -> str:
        return f"{self.source}#fence{self.ordinal}"

    def commands(self) -> list[str]:
        """Non-blank, non-comment lines — one command each."""
        return [
            line.strip()
            for line in self.body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]


_FENCE_RE = re.compile(r"^```([^\s`]*)\s*$")


def parse_fences(text: str, source: str) -> list[Fence]:
    """Every fence in one document, in order, with its marker state."""
    fences: list[Fence] = []
    lines = text.splitlines()
    index = 0
    ordinal = 0
    while index < len(lines):
        opening = _FENCE_RE.match(lines[index])
        if not opening:
            index += 1
            continue
        body: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and not _FENCE_RE.match(lines[cursor]):
            body.append(lines[cursor])
            cursor += 1
        # The marker is the closest preceding non-blank line.
        preceding = ""
        back = index - 1
        while back >= 0 and not lines[back].strip():
            back -= 1
        if back >= 0:
            preceding = lines[back].strip()
        fences.append(
            Fence(
                source=source,
                ordinal=ordinal,
                info=opening.group(1),
                body="\n".join(body),
                marked=preceding == RUNNABLE_MARKER,
            )
        )
        ordinal += 1
        index = cursor + 1
    return fences


def all_fences() -> list[Fence]:
    out: list[Fence] = []
    for path in DOC_SOURCES:
        if not path.is_file():
            continue
        source = path.relative_to(REPO).as_posix()
        out.extend(parse_fences(path.read_text(encoding="utf-8"), source))
    return out


def marked_fences() -> list[Fence]:
    return [fence for fence in all_fences() if fence.marked]


# ---------------------------------------------------------------------------
# The static screen — everything below runs BEFORE any command is executed.
# ---------------------------------------------------------------------------
# Only the ops CLI is executable. It is read-only in the forms allowed below and
# exits on its own.
ALLOWED_EXECUTABLES = frozenset({"stealth-chrome-devtools"})
# Named explicitly so the refusal carries its reason. The MCP launcher defaults
# to stdio: invoking it spawns a detached backend and then blocks reading
# JSON-RPC from stdin, which is exactly the interactive/undeclared-side-effect
# shape this screen exists to keep out of a doc lane.
BLOCKING_EXECUTABLES = {
    "stealth-chrome-devtools-mcp": (
        "speaks MCP over stdin: it spawns a detached backend and blocks on stdin"
    ),
}
# Sub-commands that mutate state, kill processes, or never return.
DENIED_SUBCOMMANDS = frozenset({"serve", "stop", "restart", "kill-orphans"})
# Flags that turn a preview into a mutation, or that write files.
DENIED_FLAGS = frozenset(
    {"--apply", "--force", "-f", "-o", "--output", "--out", "--file", "--write"}
)
# Flags that wait for a human.
INTERACTIVE_FLAGS = frozenset({"-i", "--interactive", "--prompt", "--confirm"})
# Anything the shell would reinterpret: redirection, pipes, substitution, globs,
# quoting, line continuation. A doc example that needs one of these is not
# unambiguous enough to execute.
SHELL_METACHARACTERS = frozenset("|&;<>()$`\\\"'*?[]{}!~")
SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(sentry_dsn|api[-_]?key|apikey|token|password|passwd|secret"
        r"|credential|bearer|authorization)\b"
    ),
    re.compile(r"(?i)\b[a-f0-9]{32,}\b"),  # bare hex blobs (the README's DSN shape)
)
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://([^/\s]+)")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def screen_command(command: str) -> list[str]:
    """Every reason ``command`` must not be executed. Empty list == safe.

    Returns ALL reasons rather than the first, so a rejected fence tells its
    author everything that has to change instead of one round trip per rule.
    """
    problems: list[str] = []

    metachars = sorted(SHELL_METACHARACTERS.intersection(command))
    if metachars:
        problems.append(
            f"shell metacharacter(s) {''.join(metachars)!r}: ambiguous without a shell"
        )
    for pattern in SECRET_PATTERNS:
        match = pattern.search(command)
        if match:
            problems.append(f"looks like a secret: {match.group(0)!r}")
            break
    for match in _URL_RE.finditer(command):
        host = match.group(1).split(":")[0]
        if host not in LOOPBACK_HOSTS:
            problems.append(f"external URL host {host!r}: doc examples stay offline")

    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:  # unbalanced quoting
        problems.append(f"not parseable as argv: {exc}")
        return problems
    if not argv:
        problems.append("empty command")
        return problems

    executable = argv[0]
    if executable in BLOCKING_EXECUTABLES:
        problems.append(f"{executable!r} {BLOCKING_EXECUTABLES[executable]}")
    elif executable not in ALLOWED_EXECUTABLES:
        problems.append(
            f"executable {executable!r} is not in the doc-example allowlist "
            f"{sorted(ALLOWED_EXECUTABLES)}"
        )

    for arg in argv[1:]:
        if arg in DENIED_SUBCOMMANDS:
            problems.append(f"sub-command {arg!r} mutates state or does not return")
        if arg in DENIED_FLAGS:
            problems.append(f"flag {arg!r} mutates state or writes a file")
        if arg in INTERACTIVE_FLAGS:
            problems.append(f"flag {arg!r} waits for a human")
        if arg.startswith("-"):
            continue
        # A doc example is read on every platform, so a path is "absolute" if
        # EITHER convention says so — Path(arg).is_absolute() alone would let
        # C:/foo through the screen on POSIX runners.
        if (
            PureWindowsPath(arg).is_absolute()
            or PurePosixPath(arg).is_absolute()
            or arg.startswith("~")
        ):
            problems.append(f"absolute path {arg!r}: an undeclared write/read target")
        if ".." in PurePosixPath(arg).parts or ".." in PureWindowsPath(arg).parts:
            problems.append(f"path {arg!r} escapes the throwaway directory")
    return problems


# The reviewed inventory. Marked commands and this set must be EQUAL: adding a
# marked fence without adding its command here fails, and so does deleting a
# reviewed example from the docs. That equality is what makes "reviewed" a fact
# rather than a claim.
REVIEWED_COMMANDS = frozenset(
    {
        "stealth-chrome-devtools status",
        "stealth-chrome-devtools profiles",
        "stealth-chrome-devtools cleanup",
        "stealth-chrome-devtools cleanup --browser-session-cap-gb 12",
    }
)

# The default browser-session roots. A doc example that printed one of these
# would mean the throwaway redirect leaked and the command touched the user's
# real, logged-in profile directory.
REAL_SESSION_ROOTS = ("stealth-mcp-browser-sessions",)


class TestRunnableMarker:
    def test_the_marker_has_exactly_one_spelling(self):
        """A near-miss marker must be inert, not "close enough"."""
        near_misses = [
            "<!-- doc-example: runnable-ish -->",
            "<!-- doc-example:runnable -->",
            "<!-- DOC-EXAMPLE: RUNNABLE -->",
            "<!-- runnable -->",
        ]
        for marker in near_misses:
            text = f"{marker}\n```console\nstealth-chrome-devtools status\n```\n"
            assert not parse_fences(text, "synthetic.md")[0].marked, marker
        exact = f"{RUNNABLE_MARKER}\n```console\nstealth-chrome-devtools status\n```\n"
        assert parse_fences(exact, "synthetic.md")[0].marked

    def test_a_marker_separated_by_blank_lines_still_marks_its_fence(self):
        text = f"{RUNNABLE_MARKER}\n\n```console\nstealth-chrome-devtools status\n```\n"
        assert parse_fences(text, "synthetic.md")[0].marked

    def test_the_docs_contain_marked_fences(self):
        """The gate is worthless if nothing is marked; fail loudly if it empties."""
        fences = marked_fences()
        assert fences, (
            f"no fence in {[p.name for p in DOC_SOURCES]} carries {RUNNABLE_MARKER!r} — "
            "the doc-example gate would silently pass while proving nothing"
        )

    def test_marked_fences_declare_an_allowed_info_string(self):
        for fence in marked_fences():
            assert fence.info in ALLOWED_INFO_STRINGS, (
                f"{fence.evidence_id} is marked runnable but its info string is "
                f"{fence.info!r}; expected one of {sorted(ALLOWED_INFO_STRINGS)}"
            )

    def test_marked_commands_equal_the_reviewed_inventory(self):
        marked = {cmd for fence in marked_fences() for cmd in fence.commands()}
        assert marked == REVIEWED_COMMANDS, (
            "the executed doc examples drifted from the reviewed inventory; "
            f"only in docs: {sorted(marked - REVIEWED_COMMANDS)}; "
            f"only in REVIEWED_COMMANDS: {sorted(REVIEWED_COMMANDS - marked)}"
        )


class TestStaticScreen:
    """Negative controls: each rule rejects, and the safe form still passes."""

    def test_the_reviewed_commands_pass_the_screen(self):
        for command in sorted(REVIEWED_COMMANDS):
            assert screen_command(command) == [], command

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("stealth-chrome-devtools status > out.txt", "shell metacharacter"),
            ("stealth-chrome-devtools status | head", "shell metacharacter"),
            ("stealth-chrome-devtools status && rm -rf .", "shell metacharacter"),
            ("stealth-chrome-devtools status $(whoami)", "shell metacharacter"),
            ("pip install stealth-chrome-devtools-mcp", "not in the doc-example"),
            ("curl https://example.com/install.sh", "external URL host"),
            ("stealth-chrome-devtools serve --http", "does not return"),
            ("stealth-chrome-devtools cleanup --apply", "mutates state"),
            ("stealth-chrome-devtools kill-orphans", "does not return"),
            ("stealth-chrome-devtools-mcp --list-sections", "blocks on stdin"),
            ("stealth-chrome-devtools status --interactive", "waits for a human"),
            ("stealth-chrome-devtools cleanup C:/stealth", "absolute path"),
            ("stealth-chrome-devtools cleanup ../../etc", "escapes the throwaway"),
            ("stealth-chrome-devtools login --token abcdef", "looks like a secret"),
        ],
    )
    def test_unsafe_forms_are_rejected_before_execution(self, command, expected):
        problems = screen_command(command)
        assert any(expected in problem for problem in problems), (
            f"{command!r} was screened as {problems}; expected a {expected!r} refusal"
        )

    def test_a_loopback_url_is_not_treated_as_external(self):
        problems = screen_command("stealth-chrome-devtools status http://127.0.0.1:80")
        assert not any("external URL" in problem for problem in problems)

    def test_every_marked_command_is_screened_clean(self):
        for fence in marked_fences():
            for command in fence.commands():
                assert screen_command(command) == [], (
                    f"{fence.evidence_id}: {command!r} -> {screen_command(command)}"
                )


def _throwaway(root: Path) -> tuple[Path, dict[str, str]]:
    """A bounded throwaway workspace plus an env whose every state path is in it."""
    home = root / "home"
    session_root = root / "browser-sessions"
    log_dir = root / "logs"
    clone_dir = root / "clones"
    for path in (home, session_root, log_dir, clone_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = _isolated_env(
        home_dir=home, session_root=session_root, log_dir=log_dir, clone_dir=clone_dir
    )
    return session_root, env


def _executable_commands() -> list[tuple[str, str]]:
    return [
        (fence.evidence_id, command)
        for fence in marked_fences()
        for command in fence.commands()
    ]


@pytest.mark.parametrize(
    ("evidence_id", "command"),
    _executable_commands(),
    ids=[f"{eid}::{cmd}" for eid, cmd in _executable_commands()],
)
def test_documented_example_runs(evidence_id, command, tmp_path):
    """MQ-130: every marked doc example actually runs, in a throwaway directory.

    The node id carries the source file, the fence ordinal, and the exact
    command, so the evidence names what it proved rather than "the docs".
    """
    assert screen_command(command) == [], f"{evidence_id}: refused by the static screen"
    session_root, env = _throwaway(tmp_path)
    argv = shlex.split(command, posix=True)
    launcher = resolve_launcher(name=argv[0])
    proc = subprocess.run(
        [str(launcher), *argv[1:]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{evidence_id}: `{command}` exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr
    # The redirect held: nothing reported a path under the real, logged-in root.
    for real_root in REAL_SESSION_ROOTS:
        assert real_root not in proc.stdout, (
            f"{evidence_id}: `{command}` reported the REAL browser-session root "
            f"({real_root!r}) — the throwaway redirect leaked"
        )
    # ...and every path it did report is inside the throwaway.
    for line in proc.stdout.splitlines():
        if line.startswith(("browser-session root", "clone root")):
            assert str(session_root.parent) in line, (
                f"{evidence_id}: `{command}` reported {line.strip()!r}, which is "
                f"outside the throwaway directory {tmp_path}"
            )


def test_the_runner_would_fail_a_broken_example(tmp_path):
    """Control: the executed-example assertion is sensitive, not decorative.

    A doc example that no longer works must turn this lane red. `sttatus` is what
    a stale README looks like after a verb is renamed — it passes the static
    screen (nothing about it is unsafe) and dies at execution, which is exactly
    where a broken doc should be caught.
    """
    broken = "stealth-chrome-devtools sttatus"
    assert screen_command(broken) == []
    _, env = _throwaway(tmp_path)
    proc = subprocess.run(
        [str(resolve_launcher(name="stealth-chrome-devtools")), "sttatus"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode != 0, (
        "a broken doc example exited 0 — the extract-and-run lane cannot fail"
    )


# ---------------------------------------------------------------------------
# Claims-sync.
# ---------------------------------------------------------------------------
def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _readme() -> str:
    return (REPO / "README.md").read_text(encoding="utf-8")


def _doc_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in DOC_SOURCES if path.is_file()
    )


def served_tools() -> frozenset[str]:
    """The live served surface, via W5's generator API.

    ``tool_rows()`` walks the live ``SECTION_TOOLS`` registry, so this is the
    registry-derived surface reached through the one API plan_RELEASE §2.11
    requires W11 to import — not a second scrape of the same thing.
    """
    return frozenset(row.tool for row in gen.tool_rows())


def served_sections() -> frozenset[str]:
    return frozenset(row.section for row in gen.tool_rows())


def qualified_tools() -> frozenset[str]:
    """The release-qualified subset, from the same ledger-backed rows."""
    return frozenset(
        row.tool for row in gen.tool_rows() if row.state == "release-qualified-success"
    )


_TOOL_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_]*)`")


def advertised_tools(readme: str) -> frozenset[str]:
    """Backticked names in the README's ``## MCP Tools`` table."""
    section = readme.split("## MCP Tools", 1)
    assert len(section) == 2, "README lost its '## MCP Tools' section"
    body = section[1].split("\n## ", 1)[0]
    return frozenset(
        name
        for line in body.splitlines()
        if line.startswith("|")
        for name in _TOOL_TOKEN_RE.findall(line.split("|")[1])
    )


QUALIFIED_CLAIM_RE = re.compile(r"(?i)release-qualified|qualified at the wire")


def tools_claimed_qualified(text: str) -> frozenset[str]:
    """Tool names named on (or right after) a line making a qualification claim.

    This is the check the plan's control targets: listing `get_cookies` as a
    served tool is fine; writing that a served-unqualified tool is
    release-qualified is not.
    """
    lines = text.splitlines()
    claimed: set[str] = set()
    for index, line in enumerate(lines):
        if not QUALIFIED_CLAIM_RE.search(line):
            continue
        window = " ".join(lines[index : index + 2])
        claimed.update(_TOOL_TOKEN_RE.findall(window))
    return frozenset(claimed)


class TestInstallClaims:
    def test_readme_pip_install_names_the_real_distribution(self):
        project = _pyproject()["project"]
        matches = re.findall(r"pip install ([^\s`]+)", _readme())
        assert matches, "README no longer shows a pip install command"
        for spec in matches:
            name = spec.split("[")[0].split("==")[0]
            assert name == project["name"], (
                f"README says `pip install {spec}` but the distribution is "
                f"{project['name']!r}"
            )
            if "==" in spec:
                assert spec.split("==")[1] == project["version"], (
                    f"README pins {spec!r} but pyproject version is "
                    f"{project['version']!r}"
                )

    def test_readme_mcp_config_pins_the_published_name_and_version(self):
        project = _pyproject()["project"]
        pinned = re.findall(r'"([a-z0-9-]+)==([0-9][^"]*)"', _readme())
        assert pinned, "README's MCP config no longer pins a package version"
        for name, version in pinned:
            assert name == project["name"]
            assert version == project["version"]

    def test_both_console_scripts_are_declared_and_documented(self):
        scripts = _pyproject()["project"]["scripts"]
        assert scripts == {
            "stealth-chrome-devtools-mcp": "stealth_chrome_devtools_mcp.server:main",
            "stealth-chrome-devtools": "stealth_chrome_devtools_mcp.cli:main",
        }
        text = _doc_text()
        for name in scripts:
            assert name in text, f"console script {name} is undocumented"

    def test_every_console_script_the_docs_invoke_is_declared(self):
        """No fence may invoke an entry-point name that is not installed."""
        declared = set(_pyproject()["project"]["scripts"])
        for fence in all_fences():
            for command in fence.commands():
                token = command.split()[0]
                if not token.startswith("stealth-chrome-devtools"):
                    continue
                assert token in declared, (
                    f"{fence.evidence_id} invokes {token!r}, which is not a "
                    f"declared console script {sorted(declared)}"
                )

    def test_the_readme_cli_section_documents_the_ops_script_not_the_server(self):
        """The two scripts are one hyphen apart; a swap here is a real support bug."""
        section = _readme().split("## CLI", 1)[1].split("\n## ", 1)[0]
        invoked = {
            command.split()[0]
            for fence in parse_fences(section, "README.md#CLI")
            for command in fence.commands()
        }
        assert invoked == {"stealth-chrome-devtools"}, (
            f"the README CLI section invokes {sorted(invoked)}; the ops verbs "
            "belong to `stealth-chrome-devtools` only"
        )

    def test_the_ops_console_script_resolves_in_this_environment(self):
        launcher = resolve_launcher(name="stealth-chrome-devtools")
        assert launcher.is_absolute() and launcher.is_file()


class TestToolClaims:
    def test_advertised_tools_are_a_subset_of_the_live_registry(self):
        advertised = advertised_tools(_readme())
        assert advertised, "README's MCP Tools table advertises nothing"
        missing = advertised - served_tools()
        assert not missing, (
            f"README advertises tool(s) the server does not serve: {sorted(missing)}"
        )

    def test_the_readme_served_count_is_derived_from_the_registry(self):
        total = len(served_tools())
        sections = len(served_sections())
        assert f"**{total} tools** across {sections} sections" in _readme(), (
            f"README's served-tool claim does not match the live registry "
            f"({total} tools across {sections} sections)"
        )

    def test_the_readme_qualified_count_matches_the_w5_ledger(self):
        qualified = len(qualified_tools())
        total = len(served_tools())
        assert f"{qualified} of those {total} are" in _readme(), (
            f"README must state the derived release-qualified count "
            f"({qualified} of {total}); W5's ledger is the only source for it"
        )

    def test_no_served_unqualified_tool_is_advertised_as_qualified(self):
        overclaimed = tools_claimed_qualified(_readme()) - qualified_tools()
        assert not overclaimed, (
            "the README presents served-unqualified tool(s) as release-qualified: "
            f"{sorted(overclaimed)} (see RELEASE_CONTRACT.md §5)"
        )

    def test_the_overclaim_check_catches_a_planted_claim(self):
        """Negative control for the rule above — the plan's required control.

        `spawn_browser` is served-unqualified in the ledger; a doc that called it
        release-qualified must fail, and the check must find it.
        """
        assert "spawn_browser" not in qualified_tools()
        planted = "Every tool below, including `spawn_browser`, is release-qualified.\n"
        assert tools_claimed_qualified(planted) - qualified_tools() == {"spawn_browser"}

    def test_the_overclaim_check_allows_a_genuinely_qualified_tool(self):
        qualified = sorted(qualified_tools())
        assert qualified, "W5's ledger qualifies nothing — claims-sync has no anchor"
        allowed = f"`{qualified[0]}` is release-qualified over the stdio transport.\n"
        assert not tools_claimed_qualified(allowed) - qualified_tools()


class TestRenamedKnobsAreNotResurrected:
    """The 1.x rename has no back-compat alias, so a stale doc is a live trap."""

    OLD_ENV = "STEALTH_MCP_SESSION_STORAGE_CAP_GB"
    OLD_FLAG = "--session-cap-gb"

    RETIREMENT_WORDS = re.compile(
        r"(?i)renamed|previously|migration|no longer|old name|retired|1\.x"
    )

    def test_no_doc_presents_the_old_env_var_as_current(self):
        """Naming it is fine — naming it without saying it is dead is the trap."""
        for path in DOC_SOURCES:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if self.OLD_ENV not in line:
                    continue
                # The retirement wording may wrap; look at the sentence around it.
                window = " ".join(lines[max(0, number - 3) : number + 2])
                assert self.RETIREMENT_WORDS.search(window), (
                    f"{path.name}:{number} names the retired {self.OLD_ENV} without "
                    "marking it retired; it is silently ignored at runtime"
                )

    def test_no_doc_shows_the_old_cli_flag_in_a_command(self):
        for fence in all_fences():
            for command in fence.commands():
                assert self.OLD_FLAG not in command, (
                    f"{fence.evidence_id} still uses {self.OLD_FLAG}, which the CLI "
                    "now rejects as an unknown flag"
                )
