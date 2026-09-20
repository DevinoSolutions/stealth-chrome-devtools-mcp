# F-892 — the snapshot staleness witness stats a file that does not exist

**Severity** HIGH (silent). **Ref** `origin/main` = 56596e8 (2.1.10).
**Fixed in** `fix/F892-F895-snapshot-truth`.

## 1. What shipped

`clone_storage._snapshot_needs_refresh` (56596e8 `clone_storage.py:785`) decided
whether the master snapshot was behind the master by stating exactly three
files:

```python
for rel in ("Default/Cookies", "Default/Login Data", "Default/Web Data"):
```

That function is the whole of the `pre-clone-stale` trigger — the one of the
three refresh triggers that fires before a NEW session is copied, i.e. the one
that decides whether a session starts logged in.

## 2. The measurement

`MEASURED 2026-09-20`, read-only, names and sizes only, on
`C:\stealth-mcp-browser-sessions`:

| path | master | master-snapshot |
|---|---|---|
| `Default/Cookies` | **ABSENT** | **ABSENT** |
| `Default/Network/Cookies` | 524,288 B | 524,288 B |
| `Default/Login Data` | 40,960 B | 40,960 B |
| `Default/Web Data` | 196,608 B | 196,608 B |

Chrome moved the cookie jar to `Default/Network/` in version 96. The local
profile is Chrome 153. `Default/Cookies` has therefore never existed in this
profile, and `src.exists()` skipped it on every call since the code was written.

## 3. The consequence

A login that writes only cookies is invisible to the refresh. That is the
common case, not an edge case: Google SSO, Amazon Seller Central and any SPA
that never offers to save a password write cookies and nothing else. `Login
Data` moves only when Chrome saves a password; `Web Data` only on autofill. So
for the most frequent login shape, trigger 3 was dead code and every new
session was seeded from whatever the last password-or-autofill event had left.

## 4. The fix

The list is now `profile_seed.LOGIN_WITNESSES`, led by
`Default/Network/Cookies`, with the pre-96 path **kept** beside it (a profile
carried over from an older Chrome still has one; an absent file costs one
`stat`). `_snapshot_needs_refresh` asks `profile_seed.newest_login_write` and
compares it to the snapshot marker's mtime, exactly as before.

The list is spelled in ONE place and nowhere else *including docstrings* — a
pin walks every `*.py` under the package and fails on a second spelling,
because a stale prose copy is how the original claim survived four years.

## 5. The pins

`tests/test_profile_seed_truth.py::TestLoginWitnesses`. The fixture is built
from `REAL_PROFILE_LAYOUT`, the measured relative paths above, **never from
`_copy_profile_tree`'s own output** (memory: a fixture taken from the layer the
code reads compares the normalizer to itself).

`_settle()` matters as much as the assertion: the fixture writes `Login Data`
and `Web Data`, which the SHIPPED list already sees, so without settling the
whole tree into the past the pin passed *before* the fix and proved nothing.
The RED was verified with the settle guard in place:
`assert _snapshot_needs_refresh() is False` (fixture settled) then a newer
`Default/Network/Cookies` alone → shipped code answered `False`.

Mutation-checked: dropping `Default/Network/Cookies` from the list turns the
class RED again.

## 6. Residuals

* **Nothing back-fills the 27 named profiles already on disk.** They were
  seeded when they were created and stay frozen; F-895 makes that visible,
  nothing here changes it.
* **The witness is an mtime, not a diff.** Chrome rewrites `Network/Cookies`
  for reasons other than a login (expiry pruning, a `Set-Cookie` from any
  page), so the detector over-reports. Over-reporting costs one snapshot
  refresh, which is the direction that cannot lose a login.
* **A refresh this now correctly requests can still be refused** — by
  `master-in-use` (the master's browser is up, which since F-888 is the default
  state) or, until F-893 in this same PR, silently. F-892 makes the ASK
  correct; whether the refresh then runs is F-893's and §1.6 of
  `design_session_ux.md`'s.
* **A SECOND documented refresh window is unreachable, and this PR does not
  touch it.** `_clone_needs_refresh` (`clone_storage.py:724`) and
  `_profile_refresh_days` have **no callers anywhere in `src/` or `tests/`**
  (grepped), yet `README.md:333` advertises `BROWSER_PROFILE_REFRESH_DAYS`
  (default 7) as "Refresh copies after N days". Nothing refreshes a copy after
  N days; the knob is inert. It was left alone deliberately — deleting the dead
  reader would make the README claim more obviously false without deciding
  whether the feature should exist, and that decision (wire it up, or remove
  the knob and its four documentation sites) is a change of its own. Named
  here because `_clone_needs_refresh` also spells the marker filename a second
  time, which is the same one-home defect this finding is about. `vulture` does
  not flag it — the known name-matching blind spot.
* **`Service Worker` is still excluded from the copy**, so a PWA-style login
  whose token lives in a service-worker cache does not survive a clone at all,
  however fresh the seed is. Out of scope here; named because "the seed is
  fresh" must not be read as "your login carries".
