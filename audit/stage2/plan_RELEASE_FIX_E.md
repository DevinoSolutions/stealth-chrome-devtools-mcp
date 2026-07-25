# plan_RELEASE_FIX_E — F-771: `list_tabs` raises a bare `TypeError` after any tab close

**Status: AUTHORIZED, not yet executed.** Human authorized the FIX-plan route
2026-07-25. Branch stacked on `audit/release-fix-c` (PR #46). Merge gate: human —
**the executor never merges.**

**Found by:** plan_RELEASE W2's three-OS transport gate, in
`tests/test_e2e_interaction.py::test_tabs_lifecycle`; pinned there as a
characterization xfail with the mechanism documented inline.
**Severity:** user-facing, ships today in 1.2.0, and breaks a core tool on a completely
ordinary sequence.

---

## 1. The finding (F-771)

`spawn_browser` → `new_tab` → `close_tab` → `list_tabs` raises:

```
TypeError: object Connection can't be used in 'await' expression
```

It is **not** a race and **not** transient. Once a target is discovered rather than
created in-process, the failure is permanent for the life of the browser.

### Mechanism — established from nodriver's source, not inferred

Three facts in nodriver 0.47 compose into the bug:

1. **`Browser.update_targets()` appends raw `Connection` objects.**
   `nodriver/core/browser.py:561-583` — for every target it did not already know about,
   it appends a `Connection(...)`, **not** a `Tab`.

2. **`Browser.tabs` returns them anyway.** `browser.py:137-142`:
   ```python
   @property
   def tabs(self) -> List[tab.Tab]:
       tabs = filter(lambda item: item.type_ == "page", self.targets)
       return list(tabs)
   ```
   It filters on `type_ == "page"` and returns whatever objects matched. The
   `List[tab.Tab]` annotation is **wrong** — a `Connection` with `type_ == "page"` passes
   straight through.

3. **Only `Tab` defines `__await__`.** `nodriver/core/tab.py:1262`. `Connection` does not.

The product then does, in `browser_manager.py::list_tabs`:

```python
await browser.update_targets()

tabs = []
for tab in browser.tabs:
    await tab                     # <-- TypeError for any discovered target
    tabs.append({...})
```

`close_tab` causes the next `update_targets()` to rediscover the surviving targets and
append them as `Connection`s. From that moment `await tab` raises, permanently.

### Blast radius

- Ships in 1.2.0 on every platform. Any user who closes a tab loses `list_tabs`.
- The error is a **bare `TypeError`**, not a `ToolError` — it violates the project's one
  error convention (`DESIGN.md` §9) and reaches the client as an unhandled internal fault
  with no actionable message.
- Invisible to the pre-existing in-process suite; only W2's real-transport journey hit it.

---

## 2. The fix

**Principle: `list_tabs` reads target metadata that `update_targets()` has already
refreshed. It has no reason to await anything.**

`await tab` resolves to `Tab.wait()` (`tab.py:1222`), which blocks on page lifecycle
events (`FrameStoppedLoading`, `FrameNavigated`, `LoadEventFired`, …) with a timeout.
In a metadata-listing loop that is three separate defects at once:

1. **wrong** — it raises for `Connection` objects (the finding);
2. **pointless** — every field the loop reads (`tab.target.target_id`,
   `tab.target.title`, `tab.target.type_`, `tab.url`) was already refreshed by the
   `update_targets()` call immediately above it;
3. **slow** — it serially blocks on lifecycle events *per tab*, so listing N tabs pays N
   waits for data that is already in hand.

So the fix is a **deletion**, not an `isinstance` guard bolted on top. Removing the
`await tab` closes the crash, removes the latency, and leaves one code path rather than
two. Prefer that over any branch that keeps `await` alive for one object type — a type
switch here would be a second way to do one thing (`CLAUDE.md` convention 4).

If, and only if, E0 (below) proves a genuine settle is required for correct data,
the settle belongs **once, outside the loop**, not once per tab — and the plan's LOC and
convention rules still apply.

### The trap: do not trade a loud crash for a silent wrong answer

This is the part to get right. The loop already reads defensively:

```python
"url": getattr(tab, "url", "") or "",
```

On a `Tab`, `url` is a property backed by `target.url`. On a raw `Connection` it may not
exist — in which case `getattr(..., "")` returns **empty string** and `list_tabs` starts
returning tabs with blank URLs instead of raising.

That is strictly worse than the current bug: a `TypeError` is loud and gets fixed; a
silently empty `url` is a **lying success** that a caller acts on. It is exactly the
Tier-A "silent correctness" class this campaign exists to eliminate.

Therefore acceptance requires asserting the **actual URL value**, not just that the call
returns without raising. Same for `title` and `type`. If a discovered target genuinely
cannot supply a real `url`, that is a finding to report — not something to paper over
with a default.

---

## 3. E0 — RED-first pins (tests)

Land these **before** the src edit and demonstrate each is RED for the right reason.
Read the failure text: a pin that fails on a harness `TypeError` rather than the product
`TypeError` is **not** a valid RED.

1. **`test_list_tabs_after_close_tab`** (integration, real Chrome).
   `spawn_browser` → `new_tab` → `close_tab` → `list_tabs`. Asserts the call succeeds,
   the closed tab is absent, the surviving tab is present, **and its `url` equals the
   expected fixture URL** (the anti-silent-regression assertion from §2). RED today with
   `TypeError: object Connection can't be used in 'await' expression`.

2. **`test_list_tabs_metadata_survives_rediscovery`** — after a close, every returned
   record has a non-empty `tab_id`, a `url` matching what was navigated, a real `title`,
   and `type == "page"`. This is the pin that would catch the empty-`url` regression.

3. **A hermetic pin** in `tests/test_browser_manager*.py` using `tests/fakes.py`
   (that file is THE hermetic harness home — do not start a second one): a fake browser
   whose `tabs` yields one awaitable `Tab`-like and one non-awaitable `Connection`-like
   object, proving `list_tabs` handles both. This keeps the guarantee enforced on the
   fast unit lane, not only in the ~15-minute integration lane.
   Note `call_tool(server_mod, name, /, **kwargs)` in `fakes.py` is positional-only by
   design so a tool's own `name` parameter cannot collide.

## 4. E1 — flip the existing characterization pin

`tests/test_e2e_interaction.py::test_tabs_lifecycle` currently contains a bounded poll
that ends in `pytest.xfail("F-771: ...")`. In the same commit as the fix:

- delete the tolerance loop and the `pytest.xfail` branch;
- restore the direct assertion (`new_id not in remaining`);
- keep or trim the explanatory comment block to reflect that F-771 is **closed**, citing
  this plan.

A characterization pin that survives its own fix is dead weight that teaches the next
reader the wrong thing.

---

## 5. Acceptance

1. Both real-Chrome pins green on **all three** W2 cells (Linux/X64, Windows/X64 through
   the gate; macOS/ARM64 remains subject to the declared F-773 navigation gap — if the
   macOS cell cannot run this journey, say so, and do not claim macOS coverage).
2. The hermetic pin green on the unit lane.
3. `test_tabs_lifecycle` passes with **no** xfail.
4. `url`/`title`/`type` asserted by value, not by presence.
5. Prove the fix is load-bearing: restore the `await tab` line alone and show pin #1 goes
   red with the exact `TypeError`.
6. No new error shape. If you touch an error path, it **raises** `ToolError` /
   `InstanceNotFoundError` — never a `{"success": False}` dict (`DESIGN.md` §9).
7. Full local gate green: ruff format+check; `ty check --exit-zero-on-warning
   src/stealth_chrome_devtools_mcp/` at the **76-diagnostic baseline** (a bare `ty check`
   reports 172 — wrong scope, not a regression); vulture; file budgets with **no cap
   padded**; suppression owners; unit suite (~703-705 on `-m "not integration"`).
8. `--no-verify` never used. PR opened, **never merged**.

### Scope limits

- Out of scope: F-773 (macOS navigation), F-770 (headless UA — RELEASE-FIX-D, running in
  parallel), and any other `list_tabs` behavior not named above.
- Do **not** attempt to fix nodriver's `update_targets`/`tabs` annotation upstream or
  vendor a patch. The product must be correct against the pinned dependency as it is.
- If you find sibling call sites that `await` an element of `browser.tabs` and would fail
  the same way, **report them with file:line** — fixing them may be in scope if the
  change stays small and the LOC budget holds, but do not sprawl. Say what you found
  either way.

---

## 6. Gates

Branch `audit/release-fix-e` stacked on `audit/release-fix-c`; PR opened against that
base and held at the human merge gate. Commit messages end with
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
