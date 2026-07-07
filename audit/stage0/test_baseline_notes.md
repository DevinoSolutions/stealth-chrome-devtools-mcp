# Test Baseline Notes (Stage 0)

- **Verdict: GREEN, not flaky.** Two CI-flag-identical runs: 402 passed / 0 failed / 24 deselected, 65.13s and 65.66s. Coverage 40.86% / 40.89% (nondeterministic ±0.03pp, both above the 39% gate).
- **`uv run` is broken in this checkout**: `uv run pytest` → "Failed to canonicalize script path". Root cause: repo lives under `...\CUSTOM MCPs & PRODUCTIVITY\...` (`&` + spaces + OneDrive). Every doc/CI instruction that says `uv run` fails verbatim on this machine. Onboarding/operability evidence.
- **24 tests are marker-deselected** (`-m "not integration"`): the real-browser integration suite only runs in CI's separate job (Chrome + Xvfb, py3.12 only).
- **Coverage margin is thin**: gate 39 vs actual ~40.9 — one moderately-sized untested addition flips CI red. Gate lives only in the CI CLI flag, not in pyproject (local runs never enforce it).
- **Zero-coverage entry point**: `src/stealth_chrome_devtools_mcp/server.py` (24 stmts, 0%) — the launcher wrapper is fully untested; `embedded/singleton.py` at 66%.
- One warning per run (not a storm). Orchestrator ran the baseline directly after the delegated agent stalled twice with no output.
