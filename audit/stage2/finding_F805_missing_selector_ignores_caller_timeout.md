# F-805 — a selector that never resolves costs nodriver's default 10s regardless of the timeout the caller asked for

**Status: OPEN.** Opened by the 2.0.1 SOAK stability work, 2026-07-31.
**Severity: MEDIUM** — nothing hangs and nothing wedges: every call is bounded
and the instance stays healthy afterwards. But a declared parameter has no
effect on the path that matters most, and the resulting cost is ~5x what the
caller asked for. Any "probe for this element, move on if it isn't there"
pattern — the normal way to branch on optional page content — pays ~10.5s per
probe instead of the ~2s it requested.

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
~10.5 s). **It turns RED the moment this is fixed**, which is the intended
signal to close this finding and drop the xfail.

---

## Root cause

`embedded/element_resolution.resolve_element` takes an optional `timeout` and
passes it through to `tab.select`; with `timeout=None` it calls
`tab.select(selector)` — and **nodriver's default `select` timeout is 10
seconds**. Every caller that omits the argument therefore inherits a fixed 10 s
floor for a non-existent selector:

```python
# element_resolution.py
async def _do() -> Element | None:
    if timeout is None:
        return await tab.select(selector)      # <- nodriver default: 10s
    return await tab.select(selector, timeout=timeout)
```

`dom_handler.wait_for_element` is the clearest case, because it *has* the
caller's budget and does not use it:

```python
start_time = time.time()
timeout_seconds = timeout / 1000

while time.time() - start_time < timeout_seconds:
    element = await resolve_element(tab, selector)   # <- no timeout passed
```

The loop condition is only evaluated *between* iterations, so with a 2000 ms
budget the very first iteration blocks for nodriver's full 10 s and the deadline
is checked for the first time long after it has passed. The tool-level guard
above it (`_with_cdp_timeout(..., timeout=max(timeout / 1000 + 5,
CDP_OPERATION_TIMEOUT))`) is a backstop against a true hang, not a bound on this
path, and it does not fire either.

`click_element` reaches the same `resolve_element(tab, selector)` call with no
timeout, which is why it costs the same 10.6 s before reporting the element
missing.

---

## The contained fix (not applied here — this branch is test-only)

Pass the remaining budget down, so the caller's number is the bound:

```python
remaining = timeout_seconds - (time.time() - start_time)
element = await resolve_element(tab, selector, timeout=max(remaining, 0.1))
```

and give the interaction path (`click_element` and its siblings) an explicit,
documented per-resolve timeout instead of inheriting nodriver's default. Both
are inside the one selector-resolution home, so no second resolution path is
introduced.

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
