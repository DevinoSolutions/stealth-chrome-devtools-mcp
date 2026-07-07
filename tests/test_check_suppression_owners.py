"""Self-tests for tools/check_suppression_owners.py (audit findings G-1, G-2).

A prior version of the gate validated ZERO per-file-ignore entries: it only
looked at lines that both contained ``=`` and ended in ``]``, which no real
entry does. These tests pin the parser against tagged/untagged fixtures so an
inert check fails its own suite, and confirm the live repo passes its own gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parent.parent / "tools" / "check_suppression_owners.py"
)
_spec = importlib.util.spec_from_file_location("check_suppression_owners", _MOD_PATH)
assert _spec and _spec.loader
cso = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cso)

# Build the suppression marker without writing the literal token, so this test
# file does not itself trip the repo-wide inline-suppression scan.
_MARK = "# " + "noqa"


def test_per_file_ignore_untagged_multiline_is_flagged():
    text = "\n".join(
        [
            "[tool.ruff.lint.per-file-ignores]",
            '"tests/**" = [',
            '    "S101", "E402",',
            "]",
        ]
    )
    violations = cso.per_file_ignore_violations(text)
    assert len(violations) == 1
    assert "tests/**" in violations[0]


def test_per_file_ignore_tagged_multiline_passes():
    text = "\n".join(
        [
            "[tool.ruff.lint.per-file-ignores]",
            '"tests/**" = [',
            '    "S101",',
            "]  # PERMANENT(pytest idioms)",
        ]
    )
    assert cso.per_file_ignore_violations(text) == []


def test_per_file_ignore_untagged_singleline_is_flagged():
    text = "\n".join(
        [
            "[tool.ruff.lint.per-file-ignores]",
            '"tools/**" = ["T20"]',
        ]
    )
    assert len(cso.per_file_ignore_violations(text)) == 1


def test_per_file_ignore_tagged_singleline_passes():
    text = "\n".join(
        [
            "[tool.ruff.lint.per-file-ignores]",
            '"tools/**" = ["T20"]  # plan_M4ph1',
        ]
    )
    assert cso.per_file_ignore_violations(text) == []


def test_per_file_ignore_stops_at_next_table():
    # An untagged entry in a DIFFERENT table must not be scanned.
    text = "\n".join(
        [
            "[tool.ruff.lint.per-file-ignores]",
            '"tools/**" = ["T20"]  # PERMANENT(cli)',
            "",
            "[tool.other]",
            '"not-an-ignore" = ["x"]',
        ]
    )
    assert cso.per_file_ignore_violations(text) == []


def test_inline_suppression_untagged_is_flagged():
    text = f"x = 1  {_MARK}: E501\n"
    assert len(cso.inline_noqa_violations(text, "demo.py")) == 1


def test_inline_suppression_tagged_passes():
    text = f"x = 1  {_MARK}: E501  PERMANENT(demo)\n"
    assert cso.inline_noqa_violations(text, "demo.py") == []


def test_live_repo_passes_its_own_gate():
    # Guards against a real untagged suppression sneaking in anywhere in the repo.
    assert cso.check_inline_noqas() == []
    assert cso.check_pyproject_per_file_ignores() == []
