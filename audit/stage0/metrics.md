# Stage 0 Metrics — stealth-chrome-devtools-mcp

SHA `2267b83d` verified (HEAD matches pinned_sha; tree clean except self-generated `audit/` and untracked `CODEBASE_AUDIT.md`).

## Tooling: configured vs enforced
- pytest + pytest-asyncio/-timeout/-cov: configured (pyproject test extra) AND enforced (both workflows).
- Coverage gate `--cov-fail-under=39`: lives ONLY as a CI CLI flag in test.yml — not in `pyproject.toml`'s `[tool.coverage.report]`, so local runs never enforce it.
- Linter / type-checker / formatter: NOT configured anywhere, NOT enforced anywhere. `.gitignore` pre-declares `.ruff_cache/` and `.mypy_cache/` — dead intent, never wired up.
- Dependency audit: NOT configured or enforced anywhere (no CI step, no dependabot.yml).
- publish.yml's pre-publish test gate has no coverage flag (weaker than push/PR CI).
- Integration tests run on Python 3.12 only; unit tests run the full 3.11/3.12/3.13 matrix.

## Final exclusions (provisional + validated additions)
.venv/, .git/, __pycache__/, *.egg-info/, dist/, build/, .pytest_cache/, .coverage, htmlcov/, element_clones/, uv.lock, audit/, root pypi_*.png + test_screenshot.png (screenshots).
Added: root scratch-scripts `perf_*.py, probe_*.py, preview_*.py, smoke_*.py, test_server_direct.py` (gitignored, physically present); `CODEBASE_AUDIT.md` (untracked, feeds a different pipeline stage, not source).

## LOC (tracked .py only)
src/stealth_chrome_devtools_mcp: 15,264 | tests: 6,187 | total: 21,451 across 62 files.
`server.py` alone = 4,207 LOC = 27.6% of `src/`, 19.6% of all tracked Python.

## Largest files (top 5 of 15)
1. embedded/server.py — 4207
2. embedded/browser_manager.py — 1335
3. embedded/process_cleanup.py — 1023
4. embedded/cdp_function_executor.py — 842
5. embedded/dom_handler.py — 714

## Churn (top 5 of 15, 68 commits total history)
1. embedded/server.py — 25
2. README.md — 19
3. tests/test_browser_integration.py — 14
4. pyproject.toml — 12
5. .github/workflows/test.yml — 10

## Churn x Size intersection (mechanical, 8 files)
server.py (25c/4207L), process_cleanup.py (8c/1023L), browser_manager.py (7c/1335L), test_browser_integration.py (14c/625L), singleton.py (5c/571L), dom_handler.py (4c/714L), file_based_element_cloner.py (4c/648L), test_cdp_timeout.py (4c/505L).

## Complexity — radon, grade D-or-worse (8 blocks in 7 files)
1. element_cloner.py `extract_element_styles_cdp` — E(36)
2. browser_manager.py `spawn_browser` — E(32)
3. proxy_forwarder.py `_handle_http_request` — E(31)
4. dom_handler.py `query_elements` — D(30)
5. dynamic_hook_system.py `_process_request_hooks` — D(25)
6. dynamic_hook_system.py `_execute_hook_action` — D(23)
7. server.py `_resolve_profile_selection` — D(23)
8. network_interceptor.py `search_requests` — D(22)
Note: proxy_forwarder.py and dynamic_hook_system.py are complexity hotspots absent from both the size and churn top-15s — stable-but-complex.

## Dependency audit — `uv run --with pip-audit pip-audit` (real project env)
34 known vulnerabilities across 9 packages. Direct deps hit: fastmcp==2.11.2 (6 advisories, fixes span 2.13.0→3.2.0, two majors behind), pillow==11.3.0 (6 advisories, fix needs major bump to 12.x), python-dotenv==1.1.1 (1 advisory, patch fix). Transitive deps hit: starlette 1.0.0 (5), pyjwt 2.12.1 (5), python-multipart 0.0.28 (3), cryptography 48.0.0 (1), joserfc 1.6.5 (1), pydantic-settings 2.14.1 (1) — all patch/minor fixes. Own package skipped (not on PyPI).

## Lint — unconfigured `ruff check` probe (baseline only, no config exists)
99 violations, 82 auto-fixable: F401 unused-import 67, F541 f-string-missing-placeholders 15, F841 unused-variable 7, E402 import-not-at-top 6, E741 ambiguous-variable-name 3, F821 undefined-name 1 (only non-cosmetic rule — correctness-adjacent).
