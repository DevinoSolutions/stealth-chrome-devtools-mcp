# F-777 — `get_cookies` hangs through the in-process `.fn` seam and poisons the tab's CDP connection

**Status: OPEN.** Opened by RELEASE-5 (W5) when the real-transport cookie node
(PR #52) cleared the `get_cookies` hard block and, in doing so, disproved the
reason the tool had been exempt.
**Severity: MEDIUM for the test harness, NONE for the served product** — the path
a user drives works; the path the E2E suite drives does not.

---

## The finding

`get_cookies` carried a standing `E2E_EXEMPT` entry whose stated reason was that
it "hangs against real Chrome … and poisons the tab's CDP connection so every
later call times out". Measured on the merged tree, same tool and same Chrome,
that reason is **seam-specific, not product-wide**:

| Seam | `Network.getCookies` | `Network.getAllCookies` | next call on that tab |
|---|---|---|---|
| in-process `.fn` (what `tests/test_e2e_*.py` use) | hangs ~30s, never returns | hangs ~30s, never returns | dies with a 10s CDP timeout |
| real stdio transport (what a user gets) | returns correctly | returns correctly | unaffected |

So the exemption was recording a **harness** limitation as if it were a product
defect. The tool now has a passing per-tool success assertion over the real
transport — `tests/test_e2e_transport_cookies.py::test_real_transport_cookie_round_trip`,
which asserts the retrieved VALUE — and `E2E_EXEMPT` is empty.

## Why the blast radius is wider than one tool

The failure is not "one call returns nothing". The tab's CDP connection is left
unusable, so the *next* call on that tab fails too. Any `.fn`-seam test that
touches `get_cookies` therefore corrupts whatever runs after it in the same tab,
and the symptom surfaces on the innocent test rather than on the cause.

Practical rule, and the reason `tests/test_e2e_interaction.py::test_cookies_lifecycle`
asserts through `document.cookie` instead: **no `.fn`-seam test may call
`get_cookies`.** Its coverage lives in the transport lane.

## What this does NOT mean

* It is not a user-facing defect. Nothing a user does goes through the `.fn`
  seam; the transport path is evidenced and qualified in `RELEASE_CONTRACT.md`.
* It does not mean the E2E suite is unsound. It means one tool is unreachable
  from it, and the suite now says so instead of hiding it behind an exemption
  whose reason was wrong.
* It is not the same finding as F-776. F-776 is that *most* tools lack per-tool
  transport evidence; F-777 is that *this* tool cannot be reached in-process at
  all.

## What closing it requires

A cause: whether the hang is in nodriver's `get_all_cookies`/`get_cookies`
wrappers, in how the in-process seam shares the CDP connection, or in the
absence of the transport's connection lifecycle. Until then the rule above is
the mitigation, and it is enforced by the coverage manifest's comment plus the
transport-lane placement.

## Routing

Recorded here and in the generated contract's limitations register. No `src/`
change is proposed: W5 is forbidden from production edits, and the served path is
correct — a fix here is test-infrastructure work that the human schedules.
