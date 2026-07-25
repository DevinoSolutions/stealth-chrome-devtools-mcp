# F-774 — a `--user-agent` override blanks Chrome's high-entropy UA client hints

**Status: OPEN.** Opened by RELEASE-FIX-D (the F-770 fix), measured not inferred,
pinned as a strict `xfail` in `tests/test_stealth.py` (`XFAIL_SIGNALS`,
`test_product_ua_client_hints_high_entropy_pinned_gap`).
**Severity: LOW** — strictly smaller and strictly more expensive to exploit than
the `HeadlessChrome` token it replaced, but it is a new tell and is recorded as one.

---

## The finding

RELEASE-FIX-D closes F-770 by supplying a masked default `--user-agent=` launch
flag. Whenever that override is active, Chrome **blanks the high-entropy UA
client hints it cannot derive from the override string**.

Measured on Chrome 150.0.7871.186, headless, same binary, with and without the mask:

| `getHighEntropyValues()` field | unmasked | masked |
|---|---|---|
| `architecture` | `"x86"` | `""` |
| `bitness` | `"64"` | `""` |
| `platformVersion` | `"19.0.0"` | `""` |
| `uaFullVersion` | `"150.0.7871.186"` | `""` |
| `fullVersionList` | 3 brands | `[]` |
| `brands` | 3 brands | **unchanged** |
| `mobile` | `false` | **unchanged** |
| `platform` | `"Windows"` | **unchanged** |

## What is and is not affected

- **The wire is unaffected.** The hints Chrome actually sends by default —
  `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform` — are the low-entropy set,
  and all three remain populated and coherent with the masked User-Agent (the
  gating `ua_major_matches_client_hints` signal asserts that coherence).
- **Only JS matters.** The blanked fields are reachable solely through
  `navigator.userAgentData.getHighEntropyValues()`, which a site must call
  explicitly. Compare with what it replaced: `HeadlessChrome` in the User-Agent is
  one server-side substring test, run before a byte of JavaScript executes.

## Why FIX-D did not fix it

The only mechanism that restores these values is
`Emulation.setUserAgentOverride`'s `userAgentMetadata`, which is **per-target**:
every path that creates a tab would have to apply it, and a tab the page itself
opens (`window.open`, `target=_blank`) would not be covered — the exact
second-way/maintenance hazard the launch-flag mechanism was chosen to avoid, and
the failure mode that made RELEASE-FIX-C's `create_hook` re-arm necessary. It is a
different mechanism with its own design decision, deliberately out of FIX-D's scope.

Any fix must also solve where the metadata comes from: once `--user-agent` is set
at launch, the real values are *already* blanked, so they cannot simply be read
back and re-applied — they would have to be captured before the override exists,
or constructed, which is the guessing FIX-D's coherence rule forbids.

## Routing

- Recorded as a strict `xfail`, so a future fix (or a Chrome behaviour change)
  XPASSes and turns the suite red, forcing a review. It is never resolved by
  relaxing `strict`.
- **W5's `RELEASE_CONTRACT.md` must qualify the stealth claim accordingly:**
  headless no longer advertises `HeadlessChrome` on any vector that reaches a
  site, and the low-entropy client hints are coherent; the high-entropy client
  hints are blank under the mask.
