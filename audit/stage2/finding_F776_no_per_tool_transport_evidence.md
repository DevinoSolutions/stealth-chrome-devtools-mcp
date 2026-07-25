# F-776 — almost no served tool has per-tool real-transport success evidence

**Status: OPEN, narrowed once.** Opened by RELEASE-5 (W5) while generating
`RELEASE_CONTRACT.md`. Narrowed the same day by PR #52
(`tests/test_e2e_transport_cookies.py::test_real_transport_cookie_round_trip`),
which qualifies `set_cookie`, `get_cookies` and `clear_cookies` — the first and,
at this SHA, only per-tool transport evidence in the tree.
Not a product defect: an **evidence** gap, and the reason the generated contract
qualifies the number of tools it does.
**Severity: HIGH for claims, none for behaviour** — nothing here says a tool is
broken. It says the gate cannot prove, per tool, that the user-visible outcome
happens over the transport the user actually uses.

---

## The finding

plan_RELEASE §2.5 defines `release-qualified-success` for a tool as a row naming
"the precise user outcome asserted, fully-qualified passing node, required
transport, fixture or site shape, and required OS cells", each present as
current-run success evidence — and then rules out the substitutes:

> A schema/type/non-null assertion, `.fn`-only call, **representative journey**,
> error-only test, exemption, or characterization cannot satisfy a transport,
> site-shape, manual-QA, or cross-OS success claim.

Applied to the tree at `audit/release-integration` (`ff35ae3`):

| Evidence tier that exists | Where | Why it cannot qualify a tool |
|---|---|---|
| the real-stdio journey | `tests/test_e2e_transport.py::test_real_stdio_release_gate_journey` | it is *the* representative journey (§2.1), explicitly disqualified as per-tool evidence |
| in-process E2E | `tests/test_e2e_*.py`, the `E2E_COVERED` manifest | drives tools through the `.fn` seam — a `.fn`-only call cannot satisfy a transport claim |
| in-memory client | `tests/test_mcp_protocol_surface.py` | an in-memory FastMCP client, not the wire |
| **per-tool transport** | `tests/test_e2e_transport_cookies.py::test_real_transport_cookie_round_trip` | **this one qualifies** — `set_cookie`/`get_cookies`/`clear_cookies`, on Linux/X64 + Windows/X64 |

So at this SHA exactly three of the served tools have per-tool transport success
evidence, and the `get_cookies` hard block turned out to be the visible tip of a
general gap rather than a lone exception: the same bar applied to every other
tool leaves it unqualified. The cookie node is also the template for closing the
rest — a dedicated collected node, one harness, an asserted user outcome.

## What this does NOT mean

* It does not mean the tools do not work. Most are exercised against real Chrome
  every run, and the transport lane proves the wire path itself works on two OS
  cells.
* It does not mean the E2E suite is worthless. It is real regression evidence; it
  is simply the wrong *kind* of evidence for a per-tool transport claim.
* It does not narrow the served surface. All registry tools remain served; the
  contract lists each as `served-unqualified` with this tracking id.

## What closing it requires

Per-tool assertions in the `transport` lane (real launcher, real stdio, real
Chrome) that name a user outcome and assert it — exactly the shape PR #52
landed for the cookie tools. Each such node, once it passes on the required
cells, is added to `tools/release_tool_claims.json` and the `release-evidence`
job re-verifies it against that run's ledger; the contract's count then moves on
its own, with no prose to update.

Two bounds constrain any such claim before it is written:

* `transport` runs on **Linux/X64 and Windows/X64 only** — macOS/ARM64 is excluded
  under F-773, so a per-tool stdio claim can be qualified on **two** cells, never
  three;
* a claim citing the representative journey node is rejected by the ledger, by
  design (`NON_PER_TOOL_NODES`).

## Routing

Recorded here and in the generated contract's limitations register. It is a
plan-level scope question (how much per-tool transport coverage a release
requires), not something RELEASE-5 may fix by relabelling: W5 is forbidden from
`src/` edits, and adding per-tool transport tests is new acceptance surface that
belongs to a plan step the human authorizes.
