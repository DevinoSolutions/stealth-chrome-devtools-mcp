# F-806 — the masked User-Agent keeps advertising a Chrome version the browser no longer has

**Status: FIXED — RE-LANDED 2026-08-09** on branch
`fix/f806-reland-ua-version-skew`, with the condition the revert attached to it
discharged.

The history, because it is the reason this file reads as it does. The fix first
landed on `main` (merge `ca63b77`) and was reverted back out of the 2.0.3 release
by `b628b61`. Reason for the revert: the fix adds
`reconcile_launched_browser_version` — an **unbounded** CDP `Browser.getVersion`
— to *every* spawn, first in the post-launch sequence. The
`integration (Windows/X64)` gate cell failed three times running with three
different spawn-path symptoms (an F-801 cookie race, "Failed to connect to
browser", then a job-level hang producing no pytest report at all), having
passed on 2.0.2. That was suspicion, not proof — it was never reproduced off CI
— but 2.0.3 existed to fix a backend-killing config bug, so the release was
reduced to the smallest diff that does it, and the recorded condition was: bound
that CDP call before re-landing.

**That bound now exists** (see "The CDP read is bounded" below), so the two
commits preserved on `fix/ua-version-skew` (`39927a0`, `b863878`) are re-landed
here unchanged in substance, reconciled against 2.0.3→2.0.6 and the Sentry
batches.
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

### The Windows nuance — a second, independent instance of the same bug

The Windows branch of the probe deliberately does **not** shell out
(`chrome.exe --version` hands the argument to an already-running Chrome and
returns nothing useful — "Opening in existing browser session"). It read the
version out of the **version-named sibling directories** next to `chrome.exe`
instead, taking `max()` over them.

Those directories are not the browser. Chrome's updater stages the new one
*before* it swaps the launcher stub, and the running browser keeps executing the
old build until it is next restarted. So the scan answers with a version Chrome
will not run — and it keeps answering that way for the whole pending-update
window, which on a workstation is days, not milliseconds.

Neither of the two defenses below reaches it. The executable's on-disk identity
is *stable* across that window, so the re-key has nothing to expire; and the
reconciliation cannot repair the instance that triggered it, because a launch
flag is fixed for the life of the process. The result is that **the first spawn
of every fresh backend ships a skewed UA for as long as an update is pending**.

That is the F-806 tell itself, and it survived the first round of the fix. It
was found by an adversarial review of `39927a0` that reproduced it live on the
Windows workstation, on the fix branch:

```
sibling dirs:               ['150.0.7871.186', '151.0.7922.72', ...]
fresh-process probe     ->  151
chrome.exe file version ->  150.0.7871.186

SPAWN #1 navigator.userAgent : ... Chrome/151.0.0.0 Safari/537.36
SPAWN #1 sec-ch-ua brands    : [..., {"brand":"Google Chrome","version":"150"}]
SPAWN #1 Browser.getVersion  : Chrome/150.0.7871.186
```

`tests/test_stealth.py` was green only by ordering: `e2e_helpers.warmup_once`
performs a throwaway real spawn that absorbed the bad UA and reconciled the memo
before any asserting node spawned, so nothing observed the first spawn.

macOS and Linux were genuinely closed by the re-key alone — they shell out to
`<exe> --version`, which reports the binary that will actually run. The fix
below closes Windows the same way: by asking the binary.

## The fix

Three defenses, ordered by what each one can see. The first makes the *answer*
right; the second makes it *fresh*; the third is authoritative and covers what
neither can predict (an upgrade landing between probe and launch, a second
install, a launcher that chose a different binary).

**1 — ask the binary, not its neighbours** (`_windows_file_version`). The
Windows probe now reads `chrome.exe`'s own embedded Win32 file-version resource
(`GetFileVersionInfoW` → `VS_FIXEDFILEINFO`, via the `ctypes` already imported
in `platform_utils`). That is the executable answering for *itself*, which is
precisely what the directory scan could not do; measured on the workstation
during a pending update, the resource read `150.0.7871.186` — the version that
actually launched — where the scan answered `151`. Windows still does not shell
out, for the reason above. The directory scan is **kept as the fallback** for a
binary whose resource is unreadable, so today's behaviour is the floor and no
machine gets a worse answer than it had; a zeroed resource (`0.0.0.0`, a
stripped or repacked binary) is treated as unreadable rather than masked as
`Chrome/0`.

**2 — re-key the memo on the binary's on-disk identity** (`platform_utils.py`).
The `lru_cache` is replaced by an explicit bounded memo keyed on
`(executable_path, _executable_identity(executable))`, where the identity is
`(exe mtime_ns, exe size)`. An in-place upgrade changes at least one of the two,
so the key changes and the stale entry is simply never looked up again. Cost per
spawn on the hit path is **one `stat` call**, and a subprocess is paid only when
the binary has actually changed — the memo is kept, not traded away. `None`
(version unresolvable) is memoized too, so a browser that cannot be probed does
not re-pay for a failed subprocess on every spawn.
`reset_browser_version_memo()` is the one manual invalidation seam (tests, and
ops after an out-of-band swap); routine upgrades need no call.

The identity deliberately does **not** include the parent directory's mtime. The
first round of this fix did, to chase the directory scan — but with defence 1 in
place every branch of the probe answers from the executable itself, and even on
the fallback the directory component bought nothing: re-probing during a pending
update only re-derives the same staged guess, sooner, and swapping the launcher
stub moves the key anyway. Off Windows it was pure cost —
`check_browser_executable()` resolves to `/usr/bin/google-chrome`, and
`/usr/bin` changes mtime on any package install, putting a blocking
`subprocess.run([exe, "--version"], timeout=10)` back on the spawn path. A key
component that no branch justifies is not kept.

**3 — reconcile against the browser that actually launched.**
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

Wiring: `browser_manager._apply_post_launch` now takes the resolved
`browser_executable` and awaits the reconciliation once per spawn, before the
extra-headers / window-size / timezone overrides. It is guarded the way
`window_sizing` guards its measurement — a diagnostic probe must never fail a
spawn, so a failure degrades to leaving the memo as the pre-launch probe left
it and logs why the check could not be made. The guard covers the **write-back**
as well as the CDP call: `_executable_identity` swallows only `OSError` and the
skew warning is unguarded, so a `try` around `tab.send` alone did not deliver
what the docstring promised.

### The CDP read is bounded

This is the condition the 2.0.3 revert attached to the re-land, and it is the
one substantive change this re-land makes to the reverted work.

Defence 3 is the **first await of every spawn**, and a guard that catches
exceptions does nothing about a call that never returns: against a stale or dead
CDP connection `tab.send` simply hangs, and with it the spawn. "A diagnostic
probe must never *fail* a spawn" is the weaker half of the promise; it must not
be able to *wedge* one either.

`server.py`'s `_with_cdp_timeout` is the established wrapper for exactly this,
and it is **not reachable from here**: convention 1 forbids any module under
`embedded/` importing `server` (it double-registers the tools under `runpy`).
So the bound is a local `asyncio.wait_for` inside
`reconcile_launched_browser_version`, on the module's existing
`_VERSION_PROBE_TIMEOUT_SECONDS` — **10s**, the same bound the pre-launch
`subprocess.run([exe, "--version"], timeout=…)` probe already waits for the same
answer about the same binary. One question gets one number; a second constant
beside it would be the second way convention 4 calls a defect, and 30s (the
`_with_cdp_timeout` default) is a long time to hold a spawn for a reading that
is only ever advisory.

On expiry `wait_for` **cancels** the pending send — it is not left running
behind the spawn — and the `TimeoutError` lands in the guard that was already
there, so the outcome is the one every other probe failure already produces: log
it, return `None`, leave the memo exactly as the pre-launch probe set it. The
warning names the bound so an operator reading `TimeoutError` knows how long was
waited. A spawn's worst case therefore grows by at most 10s, and never fails.

Scope is deliberately narrow. The **other** unbounded awaits in
`_apply_post_launch` are untouched, per the round-2 note below: they are
pre-existing, they are not what the revert named, and bounding them is a
separate finding's job. This bounds the call this fix introduced.

## Tests

**Unit — `tests/test_platform_utils.py`** (46 nodes total in the file), five new
classes, no Chrome:

* `TestWindowsProbeReadsTheBinaryNotItsNeighbours` — the version resource wins
  over a newer sibling directory; an **unpatched, real** Windows binary
  (`cmd.exe` copied in as `chrome.exe`, whose file-version resource is the OS
  build, so `platform.version()` supplies the expected major independently of
  the code under test) beats a planted `9999.0.0.0` directory; an unreadable
  resource still falls back to the directory scan; a zeroed resource is treated
  as unreadable. Against the pre-fix probe the real-binary node fails with
  `assert '9999' == '10'` — the staged-update shape, reproduced from disk.
* `TestBrowserVersionMemoFreshness` — an in-place upgrade expires the memo; the
  masked UA follows the upgrade; a **staged version directory alone does not
  move the answer** (the browser still runs the old build; the directory mtime
  is bumped explicitly so the test pins the *key*, not the filesystem's
  timestamp granularity); the identity key is the binary's own bytes and nothing
  else; an unchanged executable is probed exactly once across five calls (the
  memo is still a memo); a changed executable is re-probed; an unresolvable
  probe is memoized too.
* `TestRecordLaunchedBrowserVersion` / `TestReconcileLaunchedBrowserVersion` —
  the write-back, the skew warning, the guard that keeps a failed
  `Browser.getVersion` from failing a spawn, and the guard covering a failing
  write-back too.
* `TestTheReconcileCdpCallIsBounded` — the re-land condition. A `tab.send` that
  **never answers** must still let the reconciliation return: with the bound
  patched to 50ms the call returns `None` promptly, the memo still holds the
  pre-launch probe's answer (nothing was written back from a reading that never
  arrived), and the pending send is observed **cancelled** rather than left
  running behind the spawn. The node's own `asyncio.wait_for(…, timeout=5)` is
  what makes this a terminating RED: against the unbounded code it fails there
  in 5s instead of wedging the suite the way the reverted code wedged the CI
  job — verified red before the bound was written. A second node checks the
  bound is not so tight that a merely slow browser loses its reconciliation (it
  passes pre-fix too, and says so), and the default is asserted positive and
  finite so a regression to `None` cannot quietly restore the unbounded await.

**Real Chrome — `tests/test_stealth.py`** (the existing home for UA facts
measured against a live browser; no parallel file was created), two new nodes:

* `test_f806_masked_ua_major_is_the_launched_browsers_major` — the invariant
  the skew broke, stated between the mask and its subject: the Chrome major in
  `navigator.userAgent` must equal the major in `Browser.getVersion().product`
  for the browser running it. The shared `_collect_ua_facts` fixture now clears
  the version memo after `warmup_once()`, so this measures the **pre-launch
  probe's** answer — the first spawn of a fresh backend — rather than a value
  the warmup spawn already reconciled. That ordering is what kept the file green
  over the Windows defect; it no longer can. The node still cannot fail on a
  machine whose Chrome is not mid-update, which is why the teeth live below and
  in `TestWindowsProbeReadsTheBinaryNotItsNeighbours`.
* `test_f806_a_stale_version_memo_is_repaired_by_the_launched_browser` — the
  teeth for the reconciliation. It forces the state an in-place upgrade produces
  (the pre-launch probe patched to answer with `real_major - 1`) and requires
  three things of a real spawn: (1) the stale major really does reach the wire,
  so the invariant is guarding something reachable; (2) `product` still reports
  the real version despite the wrong `--user-agent=` — the measured fact above,
  re-measured; (3) the memo answers with the **launched** major afterwards even
  though the still-patched probe would answer stale, i.e. the reconciliation
  wrote back and the next spawn masks correctly. The memo is process-global, so
  the node resets it in `finally`.

Measured on the Windows workstation, 2026-07-31: masked UA major **150**,
`Browser.getVersion().product` `Chrome/150.0.7871.186` → major **150**; under
the forced-stale spawn the UA advertised **149** while `product` still reported
**150**, and the memo read back **150** after launch.

## Review round 2 (2026-08-01)

An adversarial Opus review of `39927a0` reproduced the defect live on the
Windows workstation, on the fix branch — see "The Windows nuance" above. Four
things came out of it; three are fixed here.

1. **The Windows directory scan** (confirmed) — fixed by defence 1.
2. **The prose overclaimed** (confirmed) — this Residual called a days-long
   Windows window "one spawn wide … only when an upgrade lands between probe and
   launch", and the CHANGELOG headline was false on Windows as shipped. Both
   rewritten to match what the code now does.
3. **The parent-directory mtime** (judgement) — removed, not merely gated on
   Windows: with defence 1 no branch justifies it. See defence 2.
4. **The `reconcile_launched_browser_version` guard** (confirmed) — widened to
   cover the write-back, as its docstring already promised.

A fifth observation — `reconcile_launched_browser_version` has no CDP timeout —
was deliberately **not** fixed in round 2. Its neighbours in
`_apply_post_launch` are equally unbounded and `_with_cdp_timeout` lives in
`server.py`, which `embedded/` may not import; a fix was left to a finding of
its own that covers all of them.

**That deferral did not survive contact with CI.** The reds that got this fix
reverted out of 2.0.3 were all on the spawn path, one of them a job-level hang,
and this call was the one await whose *position* the fix had changed. Being
right that the neighbours are equally unbounded was not the same as being right
to ship a new unbounded await ahead of them. It is bounded as of the re-land —
see "The CDP read is bounded" above — using a local `asyncio.wait_for` rather
than the forbidden `server` import. The neighbours remain unbounded and remain a
separate finding; that part of the round-2 judgement stands.

By the time this round was implemented the workstation's Chrome had finished
updating (sibling dirs `['151.0.7922.72', …]`, file version `151.0.7922.72`), so
the two readings now agree and the live skew could not be re-observed — which is
the pending-update window closing, exactly as described. The disagreement stands
as measured by the review.

## Residual

The reconciliation cannot repair the instance that triggered it — a launch flag
is fixed for the life of the process, so a browser that launched under a stale
mask keeps advertising it until it is closed. Repairing it would need
`Emulation.setUserAgentOverride`, which is per-target and would reintroduce the
later-tab gap F-770's launch-flag mechanism exists to avoid.

With the probe now reading the binary itself, the remaining window is genuinely
narrow: it takes an upgrade that completes — stub swapped — in the interval
between this spawn's probe and this spawn's launch, and it costs exactly the one
instance. Every later spawn is corrected by the write-back. Widening the fix to
cover that instance was judged the wrong trade. The wide window the first round
left open on Windows — days, one skewed browser per backend — is closed, not
narrowed.

The bounded CDP read costs a spawn **up to 10s** in the one case where the
connection is dead but the launch otherwise succeeded, and in that case the
reconciliation is skipped, so the mask keeps the pre-launch probe's answer and
the next spawn tries again. That is the price of the bound and it is deliberate:
the alternative the revert measured is an unbounded hang. The remaining
unbounded awaits in `_apply_post_launch` (extra headers, window sizing, timezone
override) are **not** addressed here and are the subject of their own finding —
this one bounds only the call it introduced.

F-774 (Chrome blanks the *high-entropy* UA client hints whenever a
`--user-agent` override is active) is unchanged by this work and remains an
open, strictly-xfailed gap.

The CI-environment half of that remaining window — a runner whose Chrome the
image's own updater replaces *between* two of the gate's measurements, which no
product change can close — is
[F-819](./finding_F819_ci_image_chrome_updates_mid_run.md).
