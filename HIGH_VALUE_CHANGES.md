# High-Value Changes

Prioritized improvement plan, ranked against the tool's actual threat model:
a **single-user, local** automation tool that drives the user's *own* logged-in
browser profiles. The dominant risk is therefore **the tool damaging the user's
own data or running automations** — not external attackers. Every real incident
to date (storage sweep deleting a logged-in business profile, stale-version
backend reuse, orphaned stdio processes) was a reliability failure, not a breach.

Security items are ranked accordingly: the one genuine network exposure is
already fixed, and the remaining "hardening" ideas were evaluated and mostly
rejected (see [Rejected](#rejected--evaluated-and-not-worth-it) — with reasons,
so they don't get re-proposed).

---

## Already shipped (branch `fix/singleton-version-aware-backend`, PR #19)

- Version-gated singleton: a reconnect only reuses a backend of the **same
  version**; the proxy tears down when the backend dies.
- Storage sweep never evicts live, in-flight, or legacy-marked clones;
  legacy markers (no `auto_clean` key) **fail safe** to "keep".
- Stdio streams close on disconnect so the entrypoint actually exits.
- HTTP backend defaults to `127.0.0.1` (was `0.0.0.0`) — the one real
  network-exposure fix. Explicit `--host 0.0.0.0` still works.
- CI runs unit (3.11/3.12/3.13) + real-Chrome integration on **every** PR,
  including a no-mock E2E regression for the over-cap sweep sparing live and
  legacy profiles.

---

## Ranked changes

> **Status (this session):** #1 ✅ shipped · #2 ✅ shipped · #3 🟡 in progress
> (6 subsystems covered, coverage 35 → 40 %, + a real proxy-parse bug fixed) ·
> #5 ✅ mostly (import migration split out) · #4 ⛔ next, needs go-ahead.

### 1. Trash-then-purge sweep (recoverable deletion) — ✅ DONE

**What:** When the storage-cap sweep evicts a clone, `rename` it into a
`.trash/` directory inside the sessions root (instant, same volume) instead of
`rmtree`. Purge trash entries older than ~24h on subsequent sweeps; if disk
pressure demands more space, purge trash first before evicting further clones.

**Why:** The worst incident this project has had was the sweep *irreversibly*
deleting a logged-in business profile. Every guard added since reduces the odds;
this changes the blast radius — a wrong eviction becomes a recoverable event
instead of data loss. Directly protects the "users keep access to what's theirs"
guarantee against the tool itself.

**Effort:** ~half day, TDD-able locally (rename + retention + pressure-purge
are all unit-testable; one integration case in the existing E2E sweep test).

### 2. Coverage gate in CI — ✅ DONE

**Shipped:** `pytest-cov` added; CI unit job runs `--cov-fail-under=38`
(current coverage 40 %). Coverage is opt-in locally so TDD single-file runs stay
fast. Threshold ratchets up as suites land.

**What:** Add `coverage.py` to the unit job with a `fail_under` threshold set
just below current coverage; ratchet it up as suites land.

**Why:** Every bug fixed this cycle lived in untested code. Roughly 70% of the
tool surface has no tests, and today that surface can regress with a green
build. A gate makes the gap visible and stops it growing. Cheapest structural
win on the list.

**Effort:** ~2 hours.

### 3. Test suites for the zero-coverage subsystems — 🟡 IN PROGRESS

**Shipped (round 1):** real, no-mock unit suites for `response_handler`,
`proxy_utils`, `hook_learning_system`, `dynamic_hook_system`,
`network_interceptor`, and `response_stage_hooks` (75 new tests). Per-module
coverage jumped e.g. `dynamic_hook_system` 19 → 48 %, `proxy_utils` 18 → 91 %,
`hook_learning_system` 32 → 95 %. **A latent bug surfaced and was TDD-fixed**:
`parse_proxy_config` accepted `:pass@host` (empty username) because `urlsplit`
yields `""` not `None`, defeating the both-or-neither credential guard.

**Remaining (round 2):** live-browser smoke/contract tests for the ~66 untested
MCP tools (integration job), plus pure-logic coverage for `dom_handler`,
`cdp_function_executor`, the element cloners, and `proxy_forwarder`.

**What:** Real unit suites for `network_interceptor`,
`dynamic_hook_system`, `hook_learning_system`,
`response_handler`; plus smoke/contract tests for the ~66 untested MCP
tools (minimum bar: "returns without exception against a live page" in the
integration job).

**Why:** Same logic as #2 — this is where the next sweep-grade bug is hiding.
The interceptor and hook system mutate live browser sessions, so their failure
mode is the expensive kind.

**Effort:** ~1 day per subsystem; smoke tests ~1 day for the batch.

### 4. Split the `embedded/server.py` god-module — ⛔ NEXT (needs go-ahead)

Now unblocked (coverage safety net exists). Large, review-heavy, and best paired
with the import migration (#5's deferred half) so module boundaries move once.
Hold for explicit go-ahead before starting.

**What:** Break the 4,096-line, 96-tool module into per-domain tool modules
(browser lifecycle, DOM, network, hooks, storage/clones), keeping the public
tool names and behavior identical.

**Why:** This is the file where this cycle's bugs hid. Smaller modules make
#2/#3 tractable and reviews meaningful. Do it *after* coverage exists so the
split is provably behavior-preserving.

**Effort:** ~2–3 days, mechanical but wide.

### 5. Hygiene — ✅ MOSTLY DONE

- ✅ Pin `requests>=2.32.0,<4` (was unbounded).
- ✅ Replace deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`
  (marker-format guard test pins the exact `...Z` output).
- ✅ README notes: recoverable trash, `STEALTH_MCP_CLONE_TRASH_RETENTION_HOURS`,
  and the shared-machine session-root warning.
- ⏸ Convert `embedded/` bare imports to package-relative and drop the `sys.path`
  munging — **deferred to pair with #4** (too entangled with the import scheme to
  do safely in isolation; guard with the entrypoint tests when done).

---

## Rejected — evaluated and not worth it

Kept here so these don't get re-proposed without new evidence.

- **Backend auth token.** The token would live in `~/.stealth-mcp/server.json`,
  inside the user's ACL-protected home dir. Any local process able to read the
  token already runs *as the user* and could read the Chrome profile
  directories and cookies off disk directly. It gates nothing the only viable
  attacker doesn't already have. Network exposure is already closed by the
  loopback default.
- **CORS / drive-by-web hardening.** Verified closed: no CORS middleware exists
  anywhere in the codebase, and MCP streamable-HTTP requires
  `Content-Type: application/json` plus custom `mcp-session-id` / `Accept`
  headers — all preflight-triggering. Browsers never deliver the cross-origin
  request. No action needed.
- **Owner-only permissions on debug/network export files.** Exports land in
  user-owned locations on a single-user machine; on Windows the enclosing ACLs
  already scope access. Worth revisiting only if exports ever move to a shared
  path.
- **Redaction-by-default in network exports.** Would break the primary use
  case (debugging auth flows). If added at all, opt-in.
