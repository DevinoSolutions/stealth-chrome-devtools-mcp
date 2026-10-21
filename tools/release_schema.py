"""The ``release-evidence/v1`` vocabulary: schema constants and the required-cell table.

Split out of :mod:`release_evidence` (T-5431); that module re-exports every name
here, so ``import release_evidence`` keeps working. Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "release-evidence/v1"
CLAIMS_SCHEMA = "release-evidence/v1#tool-claims"
AGGREGATE_JOB = "release-gate"
AGGREGATE_CELL = "aggregate"

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
CLAIMS_PATH = TOOLS_DIR / "release_tool_claims.json"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
NUMERIC_RE = re.compile(r"^[0-9]+$")
CELL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PYTHON_RE = re.compile(r"^3\.\d+(\.\d+)?$")
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
NODE_RE = re.compile(r"^[^:]+\.py::.+$")
MQ_RE = re.compile(r"^MQ-\d+$")
# Real GitHub-hosted image ids carry hyphens and dots (`ubuntu24`, `macos15`,
# `win25-vs2026`). The check that matters is that there IS one: a self-hosted
# runner exposes no ImageOS at all, and an empty value still fails.
IMAGE_OS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TERMINAL_OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped"})
WORKFLOW_EVENTS = frozenset(
    {"pull_request", "push", "workflow_dispatch", "schedule", "merge_group", "release"}
)
ARTIFACT_KINDS = frozenset(
    {
        "runner-identity",
        "chrome-identity",
        # F-870: how long THIS runner's Chrome took to open its DevTools
        # endpoint, measured twice, product-free. It rides as an artifact
        # because the record's own keys are a closed set (RECORD_KEYS) and
        # this is evidence ABOUT the machine, not a claim about the release.
        "chrome-cold-start",
        "coverage",
        "junit",
        "build-manifest",
        "package-verify",
        "install-smoke",
    }
)
TOOL_STATES = ("release-qualified-success", "served-unqualified", "not-served")
TRANSPORTS = frozenset({"stdio", "http"})
CLAIMS_KEYS = frozenset(
    {
        "schema",
        "note",
        "qualified",
        "default_note",
        "served_unqualified_notes",
        "not_served",
    }
)

RECORD_KEYS = frozenset(
    {
        "schema",
        "release_sha",
        "workflow",
        "job",
        "runner",
        "python_version",
        "chrome",
        "pytest",
        "artifacts",
        "mq_ids",
    }
)

# plan_RELEASE §2.1/§2.5: the representative journey is ONE node covering many
# tools. It is release evidence that the wire path works; it is explicitly NOT
# per-tool evidence, so no tool row may cite it.
NON_PER_TOOL_NODES = frozenset(
    {"tests/test_e2e_transport.py::test_real_stdio_release_gate_journey"}
)


@dataclass(frozen=True)
class CellSpec:
    """One required job/matrix cell of the release gate.

    ``expects_pytest``/``expects_chrome``/``expects_launched_chrome`` are the
    fail-closed declarations: a cell that owes pytest evidence cannot satisfy the
    aggregate with ``pytest: null``, and a cell that owes a *launched* browser
    cannot satisfy it by merely resolving the executable on disk.
    """

    job: str
    cell: str
    runner_os: str
    runner_arch: str
    python_version: str
    expects_pytest: bool
    expects_chrome: bool
    expects_launched_chrome: bool
    proves: str

    @property
    def key(self) -> str:
        return f"{self.job}/{self.cell}"

    @property
    def label(self) -> str:
        return f"{self.runner_os}/{self.runner_arch}"


# (cell name, runner.os, runner.arch) for the three qualified runners.
LINUX = ("Linux-X64", "Linux", "X64")
WINDOWS = ("Windows-X64", "Windows", "X64")
MACOS = ("macOS-ARM64", "macOS", "ARM64")
ALL_OS = (LINUX, WINDOWS, MACOS)
# The ubuntu-only jobs run whatever ``python3`` the image provides; they pin no
# interpreter, so their record carries the version that actually ran and the
# validator checks its SHAPE rather than an invented equality.
UNPINNED = ""


def _spec(
    job: str,
    cell: tuple[str, str, str],
    python: str,
    flags: str,
    proves: str,
) -> CellSpec:
    """One cell. ``flags``: ``p`` owes pytest, ``c`` Chrome, ``l`` a LAUNCH."""
    return CellSpec(
        job,
        cell[0],
        cell[1],
        cell[2],
        python,
        expects_pytest="p" in flags,
        expects_chrome="c" in flags,
        expects_launched_chrome="l" in flags,
        proves=proves,
    )


def _build_required_cells() -> tuple[CellSpec, ...]:
    """The exact required job/cell key set the aggregate demands.

    Mirrors ``release-gate.yml``; ``tests/test_release_workflows.py`` asserts the
    two never drift apart, so adding a matrix cell without adding its evidence
    edge is a red test rather than a silent hole.
    """
    one = ("default", "Linux", "X64")
    specs: list[CellSpec] = [
        _spec("quality", one, UNPINNED, "", "lint/type/vulture/owner/budget gates"),
        _spec("known-gaps", one, UNPINNED, "", "the declared gaps, in the check list"),
        _spec("build-dist", one, UNPINNED, "", "the ONE build + its hashed manifest"),
        _spec(
            "package-verify",
            one,
            UNPINNED,
            "",
            "downloaded-bytes re-check + three bite proofs",
        ),
    ]
    specs += [
        _spec(
            "unit-tests",
            (f"{cell[0]}-py{py}", cell[1], cell[2]),
            py,
            "p",
            "hermetic unit suite (`-m 'not integration'`)",
        )
        for py in ("3.11", "3.12", "3.13")
        for cell in ALL_OS
    ]
    # 3.14 is one Linux cell, not a column: F-1005's `__annotate__` fix is gated
    # to 3.14 and no other cell executes its pin (T-4804).
    specs.append(
        _spec(
            "unit-tests",
            (f"{LINUX[0]}-py3.14", LINUX[1], LINUX[2]),
            "3.14",
            "p",
            "hermetic unit suite (`-m 'not integration'`)",
        )
    )
    specs += [
        _spec(
            "coverage",
            cell,
            "3.12",
            "p",
            "per-OS coverage floor (no merged report hides a red OS)",
        )
        for cell in ALL_OS
    ]
    specs += [
        _spec(
            "integration",
            cell,
            "3.12",
            "pcl",
            "real-Chrome integration suite + Chrome identity"
            + (" MINUS the transport journey (F-773)" if cell is MACOS else ""),
        )
        for cell in ALL_OS
    ]
    specs += [
        _spec(
            "offline-stealth",
            cell,
            "3.12",
            "pcl",
            "offline stealth predicates and their failing controls",
        )
        for cell in ALL_OS
    ]
    specs += [
        _spec(
            "transport",
            cell,
            "3.12",
            "pcl",
            "real-stdio JSON-RPC journey against real Chrome",
        )
        for cell in (LINUX, WINDOWS)
    ]
    specs += [
        _spec(
            "install-smoke",
            (f"{kind}-{cell[0]}", cell[1], cell[2]),
            "3.12",
            "c" if cell is MACOS else "cl",
            f"clean install of the exact {kind} + "
            + ("handshake only (NO navigation, F-773)" if cell is MACOS else "journey"),
        )
        for kind in ("wheel", "sdist")
        for cell in ALL_OS
    ]
    return tuple(specs)


REQUIRED_CELLS: tuple[CellSpec, ...] = _build_required_cells()
CELLS_BY_KEY: dict[str, CellSpec] = {spec.key: spec for spec in REQUIRED_CELLS}
REQUIRED_KEYS: tuple[str, ...] = tuple(sorted(CELLS_BY_KEY))

# A stdio claim is only believable from a job whose selector runs the real-stdio
# lane. Transport-ness is proved by WHICH JOB executed the node, never by a
# hand-written label on the claim.
TRANSPORT_JOBS = frozenset({"transport"})
