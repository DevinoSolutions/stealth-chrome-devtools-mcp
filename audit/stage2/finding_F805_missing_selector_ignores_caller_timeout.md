# F-805 — a selector that never resolves costs nodriver's default 10s regardless of the timeout the caller asked for

**Status: OPEN, HALF FIXED.** Opened by the 2.0.1 SOAK stability work,
2026-07-31. **F-884 (2026-09-16) fixed the `wait_for_element` half** as a side
effect of moving the wait into `element_resolution`; the interaction half is
untouched and this finding stays open for it. See "What F-884 changed" below
before reading anything past it — the root-cause code quoted here no longer
exists.

**Severity: MEDIUM** — nothing hangs and nothing wedges: every call is bounded
and the instance stays healthy afterwards. But a declared parameter has no
effect on the path that matters most, and the resulting cost is ~5x what the
caller asked for. Any "probe for this element, move on if it isn't there"
pattern — the normal way to branch on optional page content — pays ~10.5s per
probe instead of the ~2s it requested. **The probe pattern is the half F-884
fixed**: `wait_for_element(timeout=2000)` now costs ~2.03 s. What is left is
every tool that has no `timeout` parameter to honour in the first place.

---

## What is proven

`tests/test_soak_stability.py` drives the real installed launcher over real
stdio against real headless Chrome and measures every operation individually.
Two of its operations target a selector that cannot exist
(`#soak-never-exists`), on a page that is fully loaded and queryable both before
and after:

| operation | asked for | observed |
|---|---|---|
| `wait_for_element(selector, timeout=2000)` | 2.0 s | **10.719 s** |
| `click_element(selector)` | (no parameter) | **10.608 s** |

Measured on Windows/X64, local fixture page over `http://127.0.0.1`, warm
Chrome. The soak node records these in its result under
`journey["missing_selector"]` and prints them on pass, so the numbers are
reproduced on every run rather than quoted from one.

The characterization node
`test_missing_selector_calls_honour_the_caller_timeout` is `xfail(strict=True)`
against the honest bound (6.0 s — halfway between a fixed ~2 s and the observed
~10.5 s). It asserts **both** rows, so it turns RED (XPASS) only when BOTH are
honest — which is why the half fix below did not flip it, and why it is still
the signal to close this finding and drop the xfail.

---

## What F-884 changed (2026-09-16), and what it did not

F-884 serialised DOM queries per tab and, on review, moved the WAIT out of
nodriver and into `element_resolution._wait_for`. Two consequences land here:

* **`wait_for_element` is fixed.** `dom_handler.wait_for_element` now passes
  `timeout=0` — exactly one query — because its own 0.5 s loop IS the wait, so
  the caller's budget is the only deadline on the path. Re-measured by the F-884
  review: a 2000 ms request costs **2.03 s**, down from 10.5 s. The "no timeout
  passed" snippet quoted under *Root cause* below is gone.
* **The interaction half is not.** `resolve_element`'s `timeout=None` no longer
  means "nodriver's `tab.select` default"; it means `_DEFAULT_WAIT_SECONDS`,
  which is deliberately set to nodriver's own 10 s so that a caller who passed
  nothing waits exactly as long as it always did. The number, the path and the
  symptom are therefore unchanged for every call site that passes no timeout:
  `click_element`, `type_text`, `paste_text`, `select_option`, `upload_file`,
  `get_page_content`'s iframe walk and `cdp_element_cloner`. One of those —
  `click_element` — is the second row of the table above, still ~10.5 s.

So the remaining defect is narrower and better stated than when this was
opened: **it is no longer "a declared `timeout` is ignored"** (no tool ignores
one now) **but "a tool with no `timeout` parameter inherits a 10 s floor for a
selector that will never resolve."** The fix is to give the interaction path an
explicit, documented budget rather than the default, and/or to expose the
parameter the tools do not have. It is one edit in one home either way, because
F-884 made `element_resolution` the only place a wait is spent.

---

## Root cause

**As measured in 2026-07.** Both code blocks in this section describe the tree
as it was; F-884 replaced both. They are kept because the second one is the
argument for why the first row of the table read 10.7 s rather than 2 s, and
that history is the reason the xfail exists.

`embedded/element_resolution.resolve_element` took an optional `timeout` and
passed it through to `tab.select`; with `timeout=None` it called
`tab.select(selector)` — and **nodriver's default `select` timeout is 10
seconds**. Every caller that omitted the argument therefore inherited a fixed
10 s floor for a non-existent selector:

```python
# element_resolution.py  (pre-F-884; tab.select is no longer called anywhere)
async def _do() -> Element | None:
    if timeout is None:
        return await tab.select(selector)      # <- nodriver default: 10s
    return await tab.select(selector, timeout=timeout)
```

`dom_handler.wait_for_element` was the clearest case, because it *had* the
caller's budget and did not use it:

```python
# pre-F-884; the call now reads resolve_element(tab, selector, timeout=0)
start_time = time.time()
timeout_seconds = timeout / 1000

while time.time() - start_time < timeout_seconds:
    element = await resolve_element(tab, selector)   # <- no timeout passed
```

The loop condition is only evaluated *between* iterations, so with a 2000 ms
budget the very first iteration blocked for nodriver's full 10 s and the
deadline was checked for the first time long after it had passed. The
tool-level guard above it (`_with_cdp_timeout(..., timeout=max(timeout / 1000 +
5, CDP_OPERATION_TIMEOUT))`) is a backstop against a true hang, not a bound on
this path, and it did not fire either.

`click_element` reaches the same `resolve_element(tab, selector)` call with no
timeout, which is why it costs the same 10.6 s before reporting the element
missing. **That sentence is still true at HEAD** — only the 10 s now comes from
`element_resolution._DEFAULT_WAIT_SECONDS` rather than from `tab.select`.

---

## The contained fix (half of it landed with F-884)

Pass the remaining budget down, so the caller's number is the bound:

```python
remaining = timeout_seconds - (time.time() - start_time)
element = await resolve_element(tab, selector, timeout=max(remaining, 0.1))
```

**Done, differently and better, by F-884**: `wait_for_element` passes
`timeout=0` and owns the deadline itself, so there is no remaining-budget
arithmetic to get wrong and no nested wait to out-wait.

The other half is unchanged: give the interaction path (`click_element` and its
siblings) an explicit, documented per-resolve timeout instead of inheriting the
default. It is inside the one selector-resolution home, so no second resolution
path is introduced — more so since F-884, which made that home the only place
any wait is spent and gave `resolve_elements` the same `timeout` parameter its
three siblings have.

This branch deliberately does not apply it: the 2.0.1 soak mandate allows a src
edit only for a hang or a wedge, and this is neither — it is bounded, it is
slow, and it is honest about *nothing*, so it is characterized and dated instead
of quietly patched alongside a test-coverage change.

---

## Adjacent observations (NOT this finding)

The same soak run recorded two reply shapes that belong to the in-flight
success-flag work, not here, and the soak deliberately asserts neither:

* `navigate("https://definitely-not-a-real-host.invalid/")` returns
  `{"success": True, "title": ..., "url": ...}` — a DNS failure reported as a
  successful navigation. Bounded (~1.1 s) and the instance stays healthy.
* `execute_script` on a throwing script returns
  `{"success": True, "result": ..., "error": ...}` — a success flag alongside a
  populated error field.

Both are logged by the soak on every run (`[soak] unresolvable-host replies:` /
`[soak] throwing-script replies:`) so a change in either shape is visible
without a test having pinned it.
