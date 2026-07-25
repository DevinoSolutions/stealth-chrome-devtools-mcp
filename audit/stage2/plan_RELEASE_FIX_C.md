# plan_RELEASE_FIX_C — F-772: catch-all Fetch interception hangs every navigation on macOS

**Status: AWAITING EXECUTION** — human authorized the FIX-plan route 2026-07-24
(selected "Authorize a FIX plan" over routing macOS as a gap). Merge gate: human.
**Found by:** W2's three-OS release gate (`audit/release-2-w2`, PR #44), macOS/ARM64
cells, rounds 1-8. **Severity:** Tier-A-equivalent, release-blocking for any
"works on macOS" claim.

---

## 1. The finding (F-772)

Over the transport a real user gets (stdio proxy -> detached HTTP backend), **no URL
can be loaded on macOS**. Not the local fixture, not any site. `navigate` burns the
product's 30s internal deadline, the tool ends at its 35s CDP deadline, and the
target server never receives a request.

### Mechanism (established by probe, not inference)

1. `dynamic_hook_system.setup_interception` is called on **every** spawn
   (`browser_manager.py:783`). When an instance has **zero hooks** — the default for
   every user who never creates one — it does **not** skip interception. It builds a
   catch-all pattern pair and enables it (`dynamic_hook_system.py:274-285`):

   ```python
   if not all_patterns:
       all_patterns = [
           RequestPattern(url_pattern="*", request_stage=RequestStage.REQUEST),
           RequestPattern(url_pattern="*", request_stage=RequestStage.RESPONSE),
       ]
   await tab.send(uc.cdp.fetch.enable(patterns=all_patterns))
   ```

2. `Fetch.enable` makes Chrome **pause every request before it leaves the browser**.
   Each one stays paused until something calls `Fetch.continueRequest`. The only
   thing that does is the `Fetch.RequestPaused` handler registered on the next lines.

3. On macOS in the detached backend, **that handler never fires**. The backend log
   for a failing run contains no `_on_request_paused` / "Intercepted request" entry
   at all, on any attempt. Nothing resumes the request, so the navigation hangs.

### Evidence (CI, macOS/ARM64, run 30135028126)

Navigation probe from one live instance, same session, in order:

| target | result |
|---|---|
| `about:blank` | **ok, 1.1s** |
| `http://127.0.0.1:1/` (nothing listening) | **hang, 35.3s** |
| `http://127.0.0.1:<fixture>/index.html` | **hang, 35.2s** |

`about:blank` passing rules out a dead tab, a broken CDP session, and cold start —
it exercises all three. The refused-port row is the decisive one: a connection to a
closed port is answered by the OS with an immediate `ECONNREFUSED` and **cannot
hang**. It hung. Therefore the request never reached the network stack: it is
sitting in the Fetch pause. Independently corroborated: the fixture server recorded
**0 hits** across every failing run.

### Blast radius

- **Ships today.** This is not a test artifact; 1.2.0 has the same code path. A macOS
  user driving this MCP the normal way (stdio proxy -> backend) cannot navigate.
- **Invisible to the whole in-process suite** (the same gap that hid B1): macOS
  in-process integration passes 54 tests, so the defect only appears over the real
  detached-backend wire path.
- **Every platform pays the tax even where it works.** Linux and Windows enable
  catch-all interception too; they merely win the race. Every request there takes an
  extra pause + CDP round-trip for zero benefit when no hooks exist.

## 2. Why no existing test saw it

Same transport gap W1 exists to close, one layer deeper: the `.fn`-seam E2E suite
never runs the detached backend, and the W1 journey only started exercising macOS
when W2 added the macOS cell. Rounds 1-8 of W2 walked it down from "macOS is red"
to the exact CDP frame.

## 3. The fix

**Principle: interception must not be enabled when there is nothing to intercept —
and a paused request must always have an owner that will resume it.**

### C1 — do not intercept when there are no hooks (src: dynamic_hook_system.py)

Replace the zero-hook catch-all fallback with an early return, so a default spawn
never enables Fetch and never pauses a request.

**The regression this must not cause (verified while designing, not assumed):**
`create_hook` does **not** call `setup_interception` (`dynamic_hook_system.py:534`),
and no other caller re-arms it. Interception is established **only at spawn**. Today
the catch-all accidentally covers hooks created later — `_on_request_paused` resolves
hooks dynamically per event. A bare early return would therefore silently break
**spawn -> create_dynamic_hook -> navigate**, which is the entire point of the hook
subsystem.

So C1 is two halves, and neither ships without the other:

1. `setup_interception`: when the computed pattern list is empty, log and return
   without calling `fetch.enable` (idempotent — safe to call again later).
2. `create_hook`: after a hook is registered, **re-arm interception** for each live
   instance the hook applies to, by calling the same `setup_interception` (the one
   home — do not add a second enabling path). This needs the instance's tab, which
   `dynamic_hook_system` does not hold; pass it in from the caller rather than
   importing `server` (**no embedded module imports `server`** — see C1 notes).

If (2) proves to need a wider surface than this plan's budget allows, **STOP and
report** rather than shipping (1) alone.

- LOC: small net change in `dynamic_hook_system.py`; `tools/check_file_budgets.py`
  must stay green and **no cap may be padded**.
- No new error shapes, no tool-surface change, no new deps, no M6-pinned bytes touched.

### C2 — RED-first pins (tests)

Hermetic, no real Chrome (a fake tab recording `tab.send` calls):

- `test_no_hooks_means_no_fetch_enable`: spawn-time setup with zero hooks sends **no**
  `Fetch.enable`. RED today (it sends a catch-all pair).
- `test_hooks_still_intercept`: with one active hook, `Fetch.enable` is sent with that
  hook's pattern. GREEN today; must stay green (proves C1 did not disable the feature).
- `test_hook_created_after_spawn_arms_interception`: spawn with zero hooks, then
  `create_hook`, then assert interception is now armed for that instance. RED today
  **and** RED against a bare early-return — this is the regression guard.

### C3 — prove it on the real wire path (tests, W2 branch)

The W2 macOS transport + integration cells must go green with the nav probe showing
`http` ok. That is the acceptance evidence; a local Windows/Linux pass is not.

## 4. Scope limits — state these, do not paper over them

- **This restores the DEFAULT (zero-hook) path on macOS. It does not fix the handler
  itself.** Why `Fetch.RequestPaused` never dispatches in the detached backend on
  macOS is still unknown. A macOS user who *creates a hook* will re-enable
  interception and, on current evidence, hang again. That residue stays open as
  **F-773** (route: nodriver handler dispatch under the detached backend event loop)
  and **must not** be described as fixed by this plan.
- Therefore: after FIX-C, "navigation works on macOS" is a claim about the default
  configuration only. The hooks-on-macOS path remains unproven and unclaimed.
- Out of scope: the 35s CDP deadline (correct as a bound; it exposed this, it did not
  cause it), and F-771 (`list_tabs` after `close_tab`, pinned in W2).

## 5. Gates

Same as FIX-A/FIX-B: ruff format+check, `ty check --exit-zero-on-warning
src/stealth_chrome_devtools_mcp/` (76-diagnostic baseline), vulture, file budgets,
suppression owners, unit suite green, integration + transport locally, and the W2
three-OS gate green on macOS. `--no-verify` banned. Branch stacked on
`audit/release-2-w2`; PR opened, **never merged by the executor**.
