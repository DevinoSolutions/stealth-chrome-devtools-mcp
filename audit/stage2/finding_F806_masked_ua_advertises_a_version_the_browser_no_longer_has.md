# F-806 — the masked User-Agent keeps advertising a Chrome version the browser no longer has

**Status: RESOLVED** on branch `fix/ua-version-skew` (2.0.2 stabilization).
**Severity: MEDIUM-HIGH (stealth)** — the mask exists to remove one tell and
this replaces it with a sharper one. A UA reading `Chrome/150` on a browser
whose own `sec-ch-ua` says `151` is not a version anyone's browser reports; it
is a mismatch no real Chrome can produce, and `platform_utils`' own docstring
already named that outcome ("masking the token while claiming a version
sec-ch-ua contradicts would replace one tell with a worse one") before the code
could produce it.

---

## The finding

CI run **30607810053**, the **macOS** cell, on 2.0.1 — the stealth gate's
coherence predicate `_p_ua_major_matches_client_hints` failed:

```
navigator.userAgent   Mozilla/5.0 (Macintosh; ...) Chrome/150.0.0.0 Safari/537.36
sec-ch-ua             "Chromium";v="151", "Google Chrome";v="151", ...
```

The product code was byte-identical to the run before it, which had passed. The
runner's Chrome had moved from 150 to 151 in between; nothing in the repo had
moved at all. That is the signature of state carried across the version change
rather than of a code defect, and it is what pointed at the memo.

## What was actually happening

Three individually reasonable facts, jointly wrong:

1. **The mask is built before launch.** `build_reduced_user_agent(executable)`
   asks the executable what version it is, then renders
   `Chrome/{major}.0.0.0` into a `--user-agent=` launch arg. It has to run
   pre-launch — it *is* a launch argument.
2. **That probe was memoized on the executable's PATH.** It was an
   `@lru_cache(maxsize=8)` on `resolve_browser_major_version(executable)`. The
   cache is not optional: the probe costs a subprocess (macOS/Linux) and it runs
   on the spawn path, so an uncached probe is a per-spawn subprocess.
3. **Chrome updates in place.** The path does not change across an upgrade —
   only the bytes behind it do. A path-keyed cache therefore cannot see an
   upgrade at all.

The backend is a long-lived singleton shared by every Claude Code session, so
step 2's cache lives for as long as the machine keeps the backend up — days.
The first spawn after boot fixes the version the mask will claim *forever*, and
Chrome's updater silently invalidates it underneath. Every vector the mask
reaches (page UA, the wire `User-Agent` header, `Browser.getVersion().userAgent`)
carried the stale major, while every vector it does **not** reach — the
low-entropy `sec-ch-ua*` client hints, which Chrome generates from its own
build — carried the truth. The two disagree, and the disagreement is machine-
checkable by any anti-bot vendor.

CI made this visible because a runner image gets a fresh Chrome and a fresh
process on a schedule of its own. On a developer workstation the same skew is
*more* likely and less visible: the backend outlives many Chrome updates.

### The Windows nuance

The Windows branch of the probe deliberately does **not** shell out
(`chrome.exe --version` hands the argument to an already-running Chrome and
returns nothing useful — "Opening in existing browser session"). It reads the
version out of the **version-named sibling directory** next to `chrome.exe`
instead. Chrome's updater lands that new directory *before* it swaps the
launcher stub, so there is a window in which the executable's own `mtime`/size
are unchanged and the answer has already changed. That is why the freshness key
below includes the **parent directory's** mtime and not just the file's.

## The fix

Two defenses, because they fail in different directions: the first is cheap and
covers every spawn; the second is authoritative and covers what the first
cannot see (an upgrade that lands between probe and launch, a second install, a
launcher that chose a different binary).

**1 — re-key the memo on the binary's on-disk identity** (`platform_utils.py`).
The `lru_cache` is replaced by an explicit bounded memo keyed on
`(executable_path, _executable_identity(executable))`, where the identity is
`(exe mtime_ns, exe size, parent-dir mtime_ns)`. An in-place upgrade changes at
least one of the three, so the key changes and the stale entry is simply never
looked up again. Cost per spawn on the hit path is **two `stat` calls**, and a
subprocess is paid only when the binary has actually changed — the memo is
kept, not traded away. `None` (version unresolvable) is memoized too, so a
browser that cannot be probed does not re-pay for a failed subprocess on every
spawn. `reset_browser_version_memo()` is the one manual invalidation seam
(tests, and ops after an out-of-band swap); routine upgrades need no call.

**2 — reconcile against the browser that actually launched.**
`reconcile_launched_browser_version(tab, executable)` reads CDP
`Browser.getVersion` immediately after launch and
`record_launched_browser_major_version(executable, product)` writes the result
into the memo, so a disagreement corrects every later spawn. The
disagreement is *logged*, not silently repaired: the instance already running
kept the flag it launched with, and that instance is advertising a
contradictory UA right now — an operator should see it.

This rests on one measured fact, not on an assumption:
**`Browser.getVersion()`'s `product` field is NOT rewritten by
`--user-agent=`.** Measured on Chrome 150 — launching with
`--user-agent=…Chrome/1.0.0.0…` still reported
`product: "Chrome/150.0.7871.186"` while the same call's `userAgent` field
carried the override. `product` is therefore the one post-launch reading that
reports the binary rather than the mask, which is what makes it usable as the
yardstick. The regression test re-measures it on every run rather than citing
this paragraph (see Tests, node 3).

Wiring: `browser_manager._apply_post_launch_setup` now takes the resolved
`browser_executable` and awaits the reconciliation once per spawn, before the
extra-headers / window-size / timezone overrides. It is guarded the way
`window_sizing` guards its measurement — a diagnostic probe must never fail a
spawn, so a failure degrades to leaving the memo as the pre-launch probe left
it and logs why the check could not be made.

## Tests

**Unit — `tests/test_platform_utils.py`** (38 nodes total in the file), three
new classes, no Chrome:

* `TestBrowserVersionMemoFreshness` — an in-place upgrade expires the memo; the
  masked UA follows the upgrade; a **new version directory alone** expires it
  (the Windows nuance above, with the directory mtime bumped explicitly so the
  test pins the *key* rather than the filesystem's timestamp granularity); an
  unchanged executable is probed exactly once across five calls (the memo is
  still a memo); a changed executable is re-probed; an unresolvable probe is
  memoized too.
* `TestRecordLaunchedBrowserVersion` / `TestReconcileLaunchedBrowserVersion` —
  the write-back, the skew warning, and the guard that keeps a failed
  `Browser.getVersion` from failing a spawn.

**Real Chrome — `tests/test_stealth.py`** (the existing home for UA facts
measured against a live browser; no parallel file was created), two new nodes:

* `test_f806_masked_ua_major_is_the_launched_browsers_major` — the invariant
  the skew broke, stated between the mask and its subject: the Chrome major in
  `navigator.userAgent` must equal the major in `Browser.getVersion().product`
  for the browser running it.
* `test_f806_a_stale_version_memo_is_repaired_by_the_launched_browser` — the
  teeth. The node above cannot fail on a machine whose Chrome did not update
  mid-run, so this one forces the state an in-place upgrade produces (the
  pre-launch probe patched to answer with `real_major - 1`) and requires three
  things of a real spawn: (1) the stale major really does reach the wire, so
  the invariant is guarding something reachable; (2) `product` still reports the
  real version despite the wrong `--user-agent=` — the measured fact above,
  re-measured; (3) the memo answers with the **launched** major afterwards even
  though the still-patched probe would answer stale, i.e. the reconciliation
  wrote back and the next spawn masks correctly. The memo is process-global, so
  the node resets it in `finally`.

Measured on the Windows workstation, 2026-07-31: masked UA major **150**,
`Browser.getVersion().product` `Chrome/150.0.7871.186` → major **150**; under
the forced-stale spawn the UA advertised **149** while `product` still reported
**150**, and the memo read back **150** after launch.

## Residual

The reconciliation cannot repair the instance that triggered it — a launch flag
is fixed for the life of the process, so a browser that launched under a stale
mask keeps advertising it until it is closed. Repairing it would need
`Emulation.setUserAgentOverride`, which is per-target and would reintroduce the
later-tab gap F-770's launch-flag mechanism exists to avoid. The window is one
spawn wide and only opens when an upgrade lands between probe and launch;
widening the fix to cover it was judged the wrong trade.

F-774 (Chrome blanks the *high-entropy* UA client hints whenever a
`--user-agent` override is active) is unchanged by this work and remains an
open, strictly-xfailed gap.
