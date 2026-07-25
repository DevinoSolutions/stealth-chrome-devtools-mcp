# plan_RELEASE_FIX_D — F-770: the headless User-Agent still advertises `HeadlessChrome`

**Status: EXECUTED.** Human authorized the FIX-plan route 2026-07-25. Branch
`audit/release-fix-d` stacked on `audit/release-4-w4` (PR #47). Merge gate: human —
**the executor never merges.**

> ## D0's measurement (the part that decided the mechanism)
>
> Measured under `headless=True` on all three qualified cells, Chrome 150:
>
> | Vector | Linux/X64 | Windows/X64 | macOS/ARM64 |
> |---|---|---|---|
> | V1 `navigator.userAgent` | **LEAK** | **LEAK** | **LEAK** |
> | V2 HTTP `User-Agent` header the server received | **LEAK** | **LEAK** | **LEAK** |
> | V3 `userAgentData` brands / high entropy / `sec-ch-ua` | clean | clean | clean |
> | V4 `Browser.getVersion().userAgent` | **LEAK** | **LEAK** | **LEAK** |
>
> Two of §2/§3's stated assumptions were wrong, which is exactly what D0 exists
> to catch:
>
> * **V3 does not leak.** The brands are identical headless and headed on every
>   cell, so the UA-CH path (M-B) was never required.
> * **V4 *is* covered by `--user-agent=`.** §3 lists it as not covered; the flag
>   is process-wide and `Browser.getVersion` follows it.
>
> So **M-A alone covers every leaking vector** — chosen, and M-B not implemented.
> `--user-agent-product=` was also measured and is inert in release Chrome.
>
> The frozen reduced-UA platform tokens the mask is built from were confirmed on
> the runners themselves: `Windows NT 10.0; Win64; x64`,
> `Macintosh; Intel Mac OS X 10_15_7` (frozen even on ARM64), `X11; Linux x86_64`.
>
> ## What the fix opened: F-774
>
> A `--user-agent` override makes Chrome blank the high-entropy UA client hints
> (`architecture`, `bitness`, `platformVersion`, `uaFullVersion`,
> `fullVersionList`); `brands`/`mobile`/`platform` — and therefore every
> `sec-ch-ua*` header on the wire — stay correct. Recorded honestly as a strict
> `xfail` and routed: see
> [finding_F774_ua_client_hints_high_entropy_blanked.md](./finding_F774_ua_client_hints_high_entropy_blanked.md).
> §6's scope-limit clause applies: partial fix + honest claim.

**Found by:** plan_RELEASE W4's offline stealth probe (`audit/release-4-w4`, PR #45),
pinned as a strict `xfail` in `tests/test_stealth.py`.
**Severity:** the single most commercially significant open finding. It directly
contradicts the product's headline claim.

---

## 1. The finding (F-770)

The package advertises "undetectable browser automation" and
`Development Status :: 5 - Production/Stable`. Under `headless=True` — a first-class,
documented option — the product's User-Agent still contains the literal token
`HeadlessChrome`:

```
Mozilla/5.0 (...) HeadlessChrome/<major>.0.0.0 Safari/537.36
```

This is the **cheapest, most universally deployed bot check in existence**: one
substring test, server-side, before a single byte of JavaScript runs. A product whose
entire value proposition is evading detection fails the first check anyone writes.

nodriver does not mask the UA token, and the product does not either. The spawn path
(`browser_manager.py:84 _append_user_agent_arg`) applies `--user-agent=` **only when the
caller explicitly supplies one**. The default headless user gets the leak.

### Why the current pin is not a fix

`tests/test_stealth.py` records this honestly and deliberately:

- the signal lives in `XFAIL_SIGNALS` with `finding_id="F-770"`, not in the gating table;
- `test_product_ua_headless_token_pinned_gap` is `@pytest.mark.xfail(strict=True)`.

An xfailed invariant **does not satisfy a release claim** (plan_RELEASE §0.2). W4's
green therefore does not currently support the stealth headline for headless. Either
this plan closes it, or W5's `RELEASE_CONTRACT.md` must qualify the stealth claim to
exclude headless — those are the only two honest outcomes.

---

## 2. Learn from FIX-C: measure before you fix

RELEASE-FIX-C shipped on a hypothesis that was strongly evidenced and **wrong**
(see `plan_RELEASE_FIX_C.md`, correction block). The cost was a CI round-trip and a
plan doc that had to be corrected in place. FIX-D therefore front-loads measurement,
and **D1's mechanism is not chosen until D0 has reported.**

### D0 — characterize the ACTUAL leak surface (tests only, no `src/` edit)

"The UA leaks" is not precise enough to fix. `--user-agent=` and
`Emulation.setUserAgentOverride` cover **different** subsets of the surface, so measure
all four vectors, on each of Linux/X64, Windows/X64, macOS/ARM64, under `headless=True`:

| # | Vector | How to observe | Covered by `--user-agent=`? |
|---|---|---|---|
| V1 | `navigator.userAgent` | probe page | yes |
| V2 | The **HTTP `User-Agent` request header actually sent** | the W1/W4 fixture server records request headers — assert on what the server received, not on what the page claims | yes |
| V3 | `navigator.userAgentData.brands` + `getHighEntropyValues()` | probe page | **no** — UA client-hint brands are built from Chrome's version info and are not affected by the flag |
| V4 | `Browser.getVersion` → `userAgent` (the CDP-level value) | CDP | no |

**Report which vectors actually contain `Headless`.** Do not assume; Chrome's
new-headless behavior has changed across majors and the runner image version is what
matters, not any published summary. If V3 leaks, `--user-agent=` alone is insufficient
and D1 must take the UA-CH path.

D0 lands as a **characterization test** with an F-770 docstring, recording current
behavior per vector. It is committed and pushed so CI reports the measurement on all
three OS cells before D1 is written.

---

## 3. D1 — the fix

**Principle: a default headless spawn must not advertise headless.** Universal
default, applied for every user, with **no new config knob** — a knob that must be
found and enabled is not a fix for a headline claim.

### The two candidate mechanisms

Pick based on D0's measurement; do not implement both.

**M-A — pre-launch `--user-agent=` flag.** Derive the masked UA from the resolved
`browser_executable` and pass it as a launch arg when the caller supplied no explicit
`user_agent`.

- *Pros:* one place; covers V1 and V2 (the HTTP header) for **every** tab, worker, and
  subresource for the whole browser process; survives `new_tab`; needs no per-target
  bookkeeping. Verified: `--user-agent` is **not** on `platform_utils._stealth_blocked_args()`,
  so unlike `--use-mock-keychain` it will actually reach Chrome. Confirm that still holds.
- *Cons:* does not touch V3 (UA-CH brands). Needs the Chrome version **before** launch —
  obtain it from the already-resolved executable (bounded subprocess, cached per
  executable path; a launch-path subprocess on every spawn is a perf regression and
  must not be added naively).

**M-B — `Emulation.setUserAgentOverride` with `userAgentMetadata`.** Apply post-launch
per target.

- *Pros:* the only mechanism that can fix V3, because it sets client-hint brands
  explicitly.
- *Cons:* per-target. Every path that creates a tab must apply it or the override
  leaks on the next tab — that is a second-way hazard and a maintenance trap. If you
  take this path, it gets **one home** that every tab-creating path calls, and a pin
  proving a *newly created* tab is covered (the exact failure mode that made FIX-C's
  `create_hook` re-arm necessary).

**A combination is permitted only if D0 proves it necessary** (e.g. M-A for V1/V2 plus
M-B for V3). If so, state plainly in the completion report that this is two mechanisms
serving two vectors, not two ways to do one thing.

### Hard constraints

- **Do not weaken any probe.** The fix makes the assertion pass; it never edits the
  predicate to accept the leak. `_p_ua_no_headless` is the contract.
- **Do not break the caller's explicit `user_agent`.** An explicit value always wins.
  Pin this.
- **Consistency is itself a tell.** A UA that claims `Chrome/141` while
  `navigator.userAgentData` reports a different major, or while the platform token
  disagrees with the real OS, is a *worse* signal than the honest headless UA — it is a
  mismatch no real browser produces. Whatever you mask, mask coherently, and pin the
  coherence.
- No new runtime dependencies. No `embedded/` module imports `server`. M6-pinned error
  bytes preserved. `tools/check_file_budgets.py` green with **no cap padded**.

---

## 4. D2 — the sensitivity control must survive the fix (read this carefully)

W4's stealth suite is only valid because it proves a **vanilla control is still
detected**. `_collect_probe(base_url, control=True)` builds that control by
monkeypatching:

```python
_bm_mod.merge_browser_args = lambda args: (list(args or []), [])
```

That neutralizes `platform_utils.merge_browser_args` — and **nothing else**.

So: if D1's masking is applied anywhere *outside* `merge_browser_args` (for example in
`_append_user_agent_arg`, in the spawn path, or via a post-launch CDP call), the
**control browser will also receive the masked UA**. The control then stops being
detectable, `vanilla_detected` goes false, and the entire stealth suite silently becomes
vacuous — it would assert that a stealthy browser is stealthy and that a "vanilla"
browser is also stealthy, proving nothing.

This is a test-invalidation trap of exactly the kind §8.1 warns about, and it will not
announce itself. D2 must therefore, in the same commit as D1:

1. Route the masking through a seam the control genuinely disables, **or** extend the
   control to disable the new seam explicitly.
2. Assert `control_outcomes["vanilla_detected"] is True` **still holds after the fix**,
   and that the control's UA **does** contain `Headless` while the product's does not.
   That differential is the proof the fix is real and the test is still sensitive.

If you cannot make the control fail while the product passes, **STOP and report** — do
not ship a fix whose test cannot tell the two apart.

---

## 5. D3 — flip the pin honestly

- Move `ua_no_headless_token` from `XFAIL_SIGNALS` into the gating `SIGNALS` table.
- **Delete** `test_product_ua_headless_token_pinned_gap`. Do not leave it as a
  non-strict xfail. (Its `strict=True` is self-policing: once the fix lands it XPASSes
  and turns the suite red, which is the intended tripwire — resolve it by deleting the
  now-redundant test, never by relaxing `strict`.)
- Update the F-770 comment block above `XFAIL_SIGNALS` to record the closure, or remove
  the block if it becomes empty.
- Any doc that describes headless stealth as a known gap gets updated in the same commit.

---

## 6. Acceptance — what "fixed" means

All of the following, or the plan is not done:

1. On **all three** W2 cells (Linux/X64, Windows/X64, macOS/ARM64), under
   `headless=True`, every vector D0 found leaking now passes, asserted as a **gating**
   signal — no xfail, no skip, no characterization.
2. The V2 assertion is made against **what the fixture server actually received**, not
   only what the page reports. A page-only assertion can be satisfied by a page-level
   override while the real HTTP header still leaks.
3. `vanilla_detected is True` still holds; the control's UA still contains `Headless`.
4. An explicit caller-supplied `user_agent` still wins, pinned.
5. A **newly created tab** (`new_tab`, not just the spawn tab) is covered — pinned.
6. Full local gate green: ruff format+check; `ty check --exit-zero-on-warning
   src/stealth_chrome_devtools_mcp/` at the **76-diagnostic baseline** (a bare `ty check`
   reports 172 because it takes the wrong scope — that is the wrong command, not a
   regression); vulture; file budgets; suppression owners; unit suite (~703-705 on
   `-m "not integration"`); integration locally.
7. `--no-verify` never used. PR opened, **never merged**.

### Scope limits — state these, do not paper over them

- This closes the **UA vector** for headless. It does not make the product
  "undetectable"; W4's other signals and the §8 residual wall stand unchanged.
- Headed mode is unaffected (it never leaked this token).
- If D0 shows V3 (UA-CH) leaking and M-B proves out of budget, shipping M-A alone is
  **permitted only if** the UA-CH residue is recorded as a new finding, routed, and W5's
  contract qualifies the claim. Partial fix + honest claim is acceptable; partial fix
  presented as complete is not.

---

## 7. Gates

Same as FIX-A/B/C. Branch `audit/release-fix-d` stacked on `audit/release-4-w4`;
PR opened against that base and held at the human merge gate. Commit messages end with
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
