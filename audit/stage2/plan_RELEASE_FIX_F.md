# plan_RELEASE_FIX_F — F-775: the F-771 family, where the bare `TypeError` is *swallowed*

**Status: AUTHORIZED, not yet executed.** Branch stacked on `audit/release-fix-e`
(PR #49). Merge gate: human — **the executor never merges.**

**Found by:** RELEASE-FIX-E while closing F-771, which required auditing every call
site that awaits an element of `browser.tabs`. Confirmed independently against the
source before this plan was written.
**Severity:** the `get_navigation_tab` site is a **silent-correctness / lying-success**
defect — the Tier-A class this whole campaign exists to eliminate. It ships in 1.2.0.

---

## 1. Why this is worse than F-771

F-771 was *loud*: `list_tabs` raised a bare `TypeError` and the user knew something
broke. The three remaining sites in the same family are **quiet**, because each one is
wrapped in a broad `except Exception` that turns the `TypeError` into a plausible-looking
fallback. A user sees no error at all — just wrong behavior.

The shared mechanism is unchanged from F-771 (nodriver 0.47):
`Browser.update_targets()` appends raw `Connection` objects
(`nodriver/core/browser.py:561-583`), `Browser.tabs` returns them despite its
`List[Tab]` annotation (`browser.py:137-142`), and only `Tab` defines `__await__`
(`tab.py:1262`) or the ~50 `Tab`-only methods. `Connection.__getattr__` delegates to
`self.target` (a `TargetInfo`), which is why *attribute reads* still work — and why
*method calls* fail with a confusing `AttributeError` naming `TargetInfo`.

### F-775a — `get_navigation_tab`: silent tab abandonment + tab leak (HIGHEST VALUE)

`browser_manager.py:1093-1104`:

```python
for candidate_tab in browser.tabs:
    if self._get_tab_target_id(candidate_tab) == tracked_target_id:
        await candidate_tab          # <-- TypeError for a rediscovered target
        return candidate_tab

if browser.tabs:
    fallback_tab = browser.tabs[0]
    await fallback_tab               # <-- same
    ...
except Exception as error:           # <-- swallows it as a "tab health check" warning
    ...
return await self._replace_main_tab(..., close_existing=False)
```

The `await` is a *liveness check* on a tab the code has **already correctly found by
target id**. When it raises, the handler concludes the tracked tab is "missing or
invalid" — which is false; it was found — and calls `_replace_main_tab`.

Consequences, all invisible to the user:

1. **The user's navigation lands in a different tab** than the one they were tracking.
2. **`close_existing=False` leaks the abandoned tab**, which stays open forever.
3. It happens on **every navigation after any `close_tab`**, and worsens with each one.
4. `NAVIGATION_RECYCLE_THRESHOLD` accounting is distorted by the spurious replacements.

### F-775b — `close_tab` cannot close a rediscovered tab

`browser_manager.py:1391-1396`: `await target_tab.close()`. `Connection` has no
`close()`; the `__getattr__` delegation surfaces
`AttributeError: 'TargetInfo' object has no attribute 'close'`, the broad `except`
catches it, and the method **returns `False` for a tab that is perfectly closeable**.
FIX-E observed this live. A user cannot close such a tab through the tool at all.

### F-775c — `switch_to_tab`

`browser_manager.py:~1346`: `bring_to_front()` is `Tab`-only, so switching returns
`False`; and on the success path it would store a `Connection` as the instance's main
tab, seeding F-775a.

### F-775d — `close_instance` teardown

`browser_manager.py:~865`: same missing `close()`. Cosmetic relative to the others
(teardown proceeds regardless), but fix it in the same pass if it costs nothing.

---

## 2. The fix

**Principle: never `await` an element of `browser.tabs`, and never call a `Tab`-only
method on one. Address targets by id through CDP, which works for every object type.**

- **F-775a:** delete the `await candidate_tab` / `await fallback_tab` liveness checks.
  The tab was located by target id from a just-refreshed `update_targets()`; the `await`
  adds no information and costs up to 0.5s per tab (`Tab.wait()` blocks on page
  lifecycle events). This mirrors FIX-E's deletion — same reasoning, same shape.
  **If** a genuine liveness check is required, it must be one that works for both types
  (e.g. a bounded CDP round-trip on the target id) and must live in **one** home.
- **F-775b/d:** close by target id through CDP rather than the `Tab`-only convenience
  method. Determine the exact call from the pinned nodriver
  (`cdp.target.close_target(target_id=...)` sent on the browser connection is the
  likely form) — **verify against the installed source, do not guess**.
- **F-775c:** same treatment; and do not store a rediscovered object as the main tab
  without the same guarantees the spawn tab has. If storing it is unsafe, say so and
  route it rather than inventing a conversion.

### The trap that defines this plan

**Do not widen the broad `except Exception` blocks, and do not narrow them into
silence.** They are what hid these defects. Either the underlying call cannot raise
`TypeError`/`AttributeError` any more (the fix), or the failure is surfaced through the
project's error convention — `raise ToolError` / `InstanceNotFoundError`, never a
`{"success": False}` dict (`DESIGN.md` §9). A defect that is merely re-swallowed more
tidily is **not fixed**.

---

## 3. RED-first pins

Local Chrome does **not** naturally reproduce this family (FIX-E established that: every
`browser.tabs` entry stays a `Tab` on Windows). So RED evidence must come from the two
tiers FIX-E proved out — reuse its approach rather than inventing a third:

1. **Hermetic tier** — extend `tests/fakes.py` (THE hermetic harness home; never start a
   second one) with the non-awaitable, `close()`-less discovered-target fake FIX-E added.
   Pin each of F-775a/b/c against it. These must be RED before the fix with the exact
   `TypeError`/`AttributeError`, and GREEN after.
2. **Real-Chrome forced-rediscovery probe** — FIX-E forced nodriver's rediscovery path
   against real Chrome (drop a target, let `update_targets()` re-append it) to produce a
   genuine `Connection`. Reuse that technique.

**F-775a needs a behavioral pin, not just a no-raise pin.** Assert that after a
`close_tab`, a subsequent navigation uses **the same tracked tab id** as before — i.e.
that `_replace_main_tab` was *not* called and no extra tab was leaked. A pin that only
proves "no exception" would pass against the current silent-fallback behavior and prove
nothing. Assert tab identity and the total tab count.

Prove each fix load-bearing: restore the single removed line and show the specific pin
goes red with the exact original error.

---

## 4. Acceptance

1. All pins green on the unit lane and on the three-OS gate cells that can run them
   (macOS integration works; macOS *transport* remains blocked by the declared F-773 gap
   — do not claim macOS transport coverage).
2. F-775a proven by **tab identity and count**, not by absence of an exception.
3. No `except Exception` block widened; no error silently swallowed; the error
   convention honored.
4. `python tools/check_file_budgets.py` green. `browser_manager.py` was at **1531/1532
   LOC** after FIX-E — you have essentially **one line of headroom**. If the fix needs
   more, the correct move is to make the change a net deletion (it should be), **not** to
   pad the cap. Padding is a standing prohibition. If you genuinely cannot fit, STOP and
   report rather than raising the cap.
5. Full local gate: ruff format+check; `ty check --exit-zero-on-warning
   src/stealth_chrome_devtools_mcp/` at the **76-diagnostic baseline** (a bare `ty check`
   reports 172 — wrong scope, not a regression); vulture; suppression owners; unit lane.
6. `--no-verify` never used. PR opened, **never merged**.

### Also in scope, if cheap; route it if not

FIX-E observed a macOS-only flake where **`close_tab` returned `True` while the target
survived a full 10s poll** — a lying success, distinct from F-775b's lying failure.
Investigate only far enough to characterize it. If it is not a quick, certain fix,
**pin and route it as its own finding** rather than growing this plan.

### Out of scope

F-773 (macOS navigation), F-770/F-774 (UA masking — RELEASE-FIX-D), W3's packaging work,
and any nodriver upstream change. The product must be correct against the pinned
dependency as it is.

---

## 5. Gates

Branch `audit/release-fix-f` stacked on `audit/release-fix-e`; PR opened against that
base and held at the human merge gate. Commit messages end with
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
