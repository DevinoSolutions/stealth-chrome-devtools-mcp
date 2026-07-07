# Phase-2 Re-Audit Report (post-convergence, 2026-07-06)

**Scope:** after the 2.5-gates quality workstream landed and merged to `main`, re-verify
the 99-finding ledger against the changed tree and audit the gates-era code the original
audit never saw. Method: quote-delta script narrowed 99 → 38 findings whose anchors moved
(the other 61 are byte-for-byte unchanged, verified not assumed); 5 agents (1 Opus, 4 Sonnet)
re-verdicted the 38 + a handful of adjacent same-file findings (39 total); 1 Opus agent swept
the new code. Fable (orchestrator) independently re-verified load-bearing claims in source.

## Headline
- **No regressions introduced by the gates.** Every still-valid finding is a *pre-existing*
  defect the approved Stage-3 plans already own. Nothing the gates changed made anything worse.
- **3 findings genuinely CLOSED by the gates** (env-parsing duplication), 1 partially closed.
- **6 NEW findings in gates-era code** (1 High, 1 Medium, 4 Low) — the High is a broken
  governance gate.
- Both Criticals (F-301/F-501, wedged-backend liveness) remain **live** — Fable-verified in source.

## Old-findings re-verdict (39 re-checked; 60 unchanged-by-construction)
| Verdict | N | IDs |
|---|---|---|
| STILL_VALID | 35 | F-301, F-501 (Critical); F-180, F-304, F-601, F-612, F-764 (High); F-103, F-106, F-122, F-124, F-163, F-164, F-182, F-184, F-204, F-205, F-207, F-208, F-305, F-307, F-404, F-508, F-606, F-607, F-608, F-702, F-703, F-741, F-742, F-743, F-744, F-745, F-746, F-760 |
| FIXED_BY_GATES | 3 | F-602, F-720, F-763 (all env-parsing dedup, closed by settings.py) |
| PARTIALLY_FIXED | 1 | F-762 (state-root idiom half-closed; STATE_DIR still duplicated) |

Fable second-pass (independently re-verified in source, all matched the agents):
- **F-301/F-501 STILL_VALID** — `singleton.py:81` `_server_is_healthy` is still a bare
  `socket.create_connection(...timeout=2)`; passes the instant the port accepts a TCP connection,
  never issues an MCP request. Wedged-but-listening backend passes forever. Both Criticals live.
- **F-602/F-720/F-763 FIXED** — grep of all `src/` for `os.getenv|os.environ`: only 2 hits, both
  inside `settings.py` (canonical, owner-tagged) + 1 pre-import *write* in `cli.py`. `server.py`
  and `process_cleanup.py` are clean. ruff TID251 bans the pattern repo-wide → regression-proof.
- **F-764 STILL_VALID** — `debug_logger.py:391` re-acquires a plain non-reentrant `Lock` from a
  nested call; M3-A1's Lock→RLock fix has not run. (This is the anchor of the FIRST Stage-3 plan.)

The 60 unchanged findings retain their Stage-1 verdicts (anchors proven identical by script).

## NEW findings in gates-era code (authoritative detail: NEW_FINDINGS_sweep.json)
| ID | Sev | File | One-line |
|---|---|---|---|
| G-1 | **High** | tools/check_suppression_owners.py | Per-file-ignore owner-tag check is INERT — validates 0 entries; the untagged `tests/**` block passes. The owner-tag governance the gates↔Stage-3 bridge relies on enforces nothing. |
| G-2 | Medium | tools/check_suppression_owners.py | Inline-noqa scan walks only `src/`; untagged noqas in `tests/` (`test_settings.py:45 # noqa: PT011`, 2× E402) escape the "repo-wide" discipline. |
| G-3 | Low | server.py (shim) | stdio-proxy early-return entrypoint calls neither `get_settings()` nor `sentry_init()` — client-side proxy failures never reach Sentry (backend boot IS covered). |
| G-4 | Low | package.json | husky pinned `^9` (floating), inconsistent with the exact `==` pins on every Python tool + SHA-pinned Actions. |
| G-5 | Low | .husky/pre-commit + pre-push | 8-line venv-python resolver duplicated verbatim across both hooks (dedup lens). Both correctly fail closed + quote the `&`-containing path. |
| G-6 | Low | settings.py | Loud-rejection strictness only guards the `STEALTH_MCP_` prefix; a typo'd legacy unprefixed var (BROWSER_*/CDP_*) set in the real env is silently ignored. `.env`-file typos ARE caught by `extra="forbid"`. |

Fable second-pass: **G-1 and G-2 verified in source** (per-file-ignores structure confirmed
inert; untagged tests/ noqas confirmed present). G-3..G-6 accepted on the agent's quotes (Low,
self-evident from the cited lines).

## Confirmed closures (expected, now proven)
F-720 + F-763 (divergent truthy-set env parsing) and F-602 (12 scattered getenv sites) are the
dedup-lens findings the settings.py migration was predicted to close. Verified closed with a
zero-match grep + the TID251 ban. This retroactively justifies the cross-review note that
SUPERSEDES plan M11a's `env_utils.py` step — that home now exists as `settings.py`.
