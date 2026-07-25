"""The generated release contract may not overclaim (plan_RELEASE W5).

`RELEASE_CONTRACT.md` is the document a reader trusts when they push blind, so
these tests police the two ways it could lie:

* **drift** — the file on disk no longer matches what the generator produces
  from the ledger, the claim ledger, and the live registry;
* **overclaim** — a number or sentence in it asserts more than the evidence
  supports (a 94-qualified-tool statement, an unqualified "works on Linux,
  Windows and macOS", an HTTP claim inherited from stdio evidence, an upgrade
  claim W14 never made, or `get_cookies` presented as qualified).

They are hermetic: they read the repository, never CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import gen_release_contract as gen  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)
import release_evidence as re_mod  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)


@pytest.fixture(scope="module")
def contract() -> str:
    return gen.CONTRACT_PATH.read_text(encoding="utf-8")


def test_the_contract_is_regenerated_not_edited():
    """CI fails on drift: the document is output, never a hand-maintained file."""
    assert gen.check_contract() == [], (
        "RELEASE_CONTRACT.md is stale — regenerate it with "
        "`uv run python tools/gen_release_contract.py --write` in the SAME "
        "commit as whatever changed the ledger, claims, or registry"
    )


def test_regeneration_is_reproducible():
    assert gen.render_contract() == gen.render_contract()


def test_the_tool_table_covers_every_served_tool_exactly_once():
    rows = gen.tool_rows()
    names = [row.tool for row in rows]
    assert sorted(names) == sorted(re_mod.registry_tool_names())
    assert len(set(names)) == len(names)


def test_every_served_unqualified_row_carries_a_tracking_id_and_impact():
    for row in gen.tool_rows():
        if row.state != "served-unqualified":
            continue
        assert row.tracking_id, f"{row.tool} has no tracking id"
        assert row.impact, f"{row.tool} has no user impact"


def test_the_headline_count_is_derived_not_typed(contract: str):
    counts = re_mod.tool_surface(re_mod.load_claims())
    served = counts["served_total"]
    qualified = counts["release_qualified"]
    assert f"qualifies {qualified} of the {served} served MCP tools" in contract
    assert qualified + counts["served_unqualified"] == served


def test_no_ninety_four_qualified_tool_statement_exists(contract: str):
    """The plan's hard block: 94 may not be claimed by exemption or by counting.

    A tool becomes qualified only through a verified claim row, so this test is
    what makes the count fall out of evidence rather than out of a sentence.
    """
    served = re_mod.tool_surface(re_mod.load_claims())["served_total"]
    qualified = re_mod.tool_surface(re_mod.load_claims())["release_qualified"]
    for phrase in (
        f"{served} release-qualified",
        f"all {served} tools",
        f"{served} qualified tools",
    ):
        assert phrase not in contract, f"contract overclaims: {phrase!r}"
    if qualified != served:
        assert f"qualifies {served} of the {served}" not in contract


def test_get_cookies_is_never_presented_as_qualified(contract: str):
    """plan_RELEASE §2.5 option (b): the exclusion must be VISIBLE, not implied."""
    row = next(r for r in gen.tool_rows() if r.tool == "get_cookies")
    claimed = {str(c.get("tool", "")) for c in re_mod.claim_rows(re_mod.load_claims())}
    if "get_cookies" not in claimed:
        assert row.state == "served-unqualified"
        assert "get_cookies" in contract
        assert row.tracking_id, "the excluded tool must carry a tracking id"
    else:
        assert row.state == "release-qualified-success"
        assert "stdio" in row.tier


def test_macos_transport_is_named_as_excluded_not_covered(contract: str):
    assert "F-773" in contract
    assert "excluded" in contract.lower()
    transport_cells = {
        spec.label for spec in gen.matrix_rows() if spec.job == "transport"
    }
    assert "macOS/ARM64" not in transport_cells


def test_the_os_family_claim_is_always_qualified(contract: str):
    """'Linux, Windows and macOS' may never appear unqualified."""
    lowered = contract.lower()
    assert "ubuntu x64" in lowered
    assert "windows x64" in lowered
    assert "macos arm64" in lowered
    for banned in (
        "works on linux, windows and macos",
        "supported on linux, windows, and macos",
        "all platforms",
        "any platform",
    ):
        assert banned not in lowered, f"unqualified OS claim: {banned!r}"


def test_http_is_described_but_never_qualified(contract: str):
    assert "unauthenticated" in contract.lower()
    assert "no HTTP claim is derived from stdio evidence" in contract
    for claim in re_mod.claim_rows(re_mod.load_claims()):
        assert claim.get("transport") != "http", (
            "an HTTP claim needs live HTTP acceptance evidence, which the gate "
            "does not produce"
        )


def test_no_upgrade_or_rollback_claim_is_made(contract: str):
    assert "## 4. Upgrade qualification" in contract
    assert "**None.**" in contract
    assert "W14 has not run" in contract


def test_undetectability_is_refused(contract: str):
    lowered = contract.lower()
    assert "not claimed undetectable" in lowered or (
        "not promise universal undetectability" in lowered
    )
    assert "f-774" in lowered


def test_the_limitations_register_names_every_required_area(contract: str):
    """§2.5 enumerates what the register must contain; each id must appear."""
    required = (
        "E8-1",
        "E8-2",
        "E8-3",
        "E8-4",
        "E7-1",
        "E7-6",
        "F-181",
        "F-165",
        "close-path flake",
        "F-773",
        "F-774",
        "F-776",
        "native IME",
        "HTTP transport",
        "code-execution surface",
        "missing interaction surface",
        "live public web",
    )
    for ident in required:
        assert ident in contract, f"limitations register omits {ident!r}"


def test_the_register_names_w7s_exact_public_surface_exclusions(contract: str):
    for exclusion in (
        "stale live handles",
        "frame-targeted",
        "redirect chains",
        "truncation",
        "downloads",
        "SSE/WS",
        "shadow-root",
    ):
        assert exclusion in contract, f"W7 exclusion missing: {exclusion!r}"


def test_every_unrun_workstream_is_declared(contract: str):
    for workstream in (
        "W6",
        "W7",
        "W8",
        "W9",
        "W10",
        "W11",
        "W12",
        "W13",
        "W14",
        "W15",
        "W16",
    ):
        assert workstream in contract, f"{workstream} is not declared in the register"


def test_the_matrix_table_matches_the_ledgers_required_cells(contract: str):
    for spec in gen.matrix_rows():
        assert f"| `{spec.job}` | `{spec.cell}` |" in contract, (
            f"required cell {spec.key} is missing from the contract matrix"
        )


def test_the_contract_says_it_is_generated(contract: str):
    assert contract.startswith("<!-- GENERATED by tools/gen_release_contract.py")


def test_the_readme_style_docs_are_not_a_second_contract():
    """The contract has ONE home; a second copy would be a second truth."""
    duplicates = [
        path.name
        for path in gen.REPO_ROOT.glob("*.md")
        if path.name != "RELEASE_CONTRACT.md"
        and "release-qualified-success" in path.read_text(encoding="utf-8")
    ]
    assert duplicates == [], f"a second release contract exists in {duplicates}"
