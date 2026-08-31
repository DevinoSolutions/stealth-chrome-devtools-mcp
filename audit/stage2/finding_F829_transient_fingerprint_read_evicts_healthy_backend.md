# F-829 — a transient source-read failure was reported as "source changed", and killed the healthy backend

**Status: RESOLVED** on `fix/F829-fingerprint-transient-oserror`.
**Severity: HIGH** — this is one of the two known ways the product kills its
own healthy shared backend (the other is F-839, handled separately). When it
fires, every Claude Code session on that backend is disconnected at once, and
the log says the source changed, so the operator looks for an edit that never
happened.

---

## Symptom

A healthy backend serving several sessions is terminated during another
session's cold start, and the proxy log reads:

```
backend stale (source changed), evicting
```

No source was edited. The same working tree that "changed" is byte-identical
before and after. Every session on that backend loses its 94 tools at the
moment of the eviction, and a fresh backend is spawned in its place.

## Mechanism

Reuse identity is two keys: the package version and a SHA-256 over the
package's `*.py` source (`singleton._source_fingerprint`, M2 / F-206 — on an
editable install the version is frozen, so only the digest can see an in-place
edit). The gate was:

```python
fp = _source_fingerprint()
if not fp or entry.get("source_fingerprint") != fp:
    return False
```

and the digest function collapsed *every* OS read error into the same value the
gate reads as "no match":

```python
    except OSError:
        return ""
```

So the fingerprint had two states carrying three meanings — a digest, "no
digest", and "I could not read the source" — with the last two spelled
identically. One unreadable `*.py` anywhere under the package therefore
produced `""`, `""` failed the gate, and the caller could not tell a failed
read from an edit.

That verdict is not harmless. `_same_identity_backend_ready` is THE reuse gate,
so a `False` from it means, in order:

1. `_find_running_server` reports no reusable backend, so this session
   cold-starts one;
2. `_start_backend_holding_lock` acquires the lock, re-asks the gate, gets
   `False` again, and reaches `_clear_stale_backend(port)`, which
   `_terminate_backend`s the perfectly healthy backend squatting the port —
   this is the kill;
3. and on the way past it logs `backend stale (source changed), evicting`,
   which is the misattribution.

Two more amplifiers make this a fleet event rather than a nuisance:

* **The read is not rare here.** This repository lives under OneDrive. A file
  being synced is briefly unreadable (`OSError`/`PermissionError` — Windows
  sharing violations are `OSError` subclasses, so they all landed in the same
  `except`), and `_source_fingerprint` reads *every* `*.py` in the package on
  *every* cold-start discovery. One file, one moment, is enough.
* **It also poisoned the record.** `_start_server_process` records the digest
  it computes at spawn time. A backend spawned while the source was unreadable
  recorded `""` — a value no future digest can ever equal — so that backend was
  guaranteed to be evicted by the *next* session, and the failure repeated.

## Fix

Give the fingerprint the third state it always had, and put the one reading of
it in one place.

1. **`singleton._source_fingerprint() -> str | None`.** The hash is retried
   `_FINGERPRINT_ATTEMPTS` (3) times, `_FINGERPRINT_RETRY_SECONDS` (0.05s)
   apart — a sync lock is milliseconds, so the overwhelmingly common case is
   that attempt 2 succeeds and nothing downstream ever sees a failure. Only a
   read that fails all three returns `None` ("unreadable"), and it logs the
   real cause once, at WARNING:

   ```
   source fingerprint unreadable: [Errno 13] Permission denied: ...
   ```

   Never raises; the happy path is byte-identical to before (same digest over
   the same relpath+`\0`+content+`\0` stream).

2. **`backend_registry.fingerprint_mismatch(entry, current)`** is now THE one
   reading of the recorded field, consumed by both comparison sites:

   ```python
   recorded = (entry or {}).get("source_fingerprint", "")
   if recorded is None or current is None:
       return False
   return not current or recorded != current
   ```

   `None` on **either** side is UNREADABLE → unknown → **not** a mismatch, so
   nothing is evicted and no rival backend is cold-started. `""` keeps its
   pre-existing fail-closed meaning (M2's cross-review hardening: `"" == ""`
   must never read as a match), which is also what an ABSENT field decays to —
   a pre-M2 legacy record is still refused exactly as before.

3. **The eviction diagnostic** in `_start_backend_holding_lock` asks the same
   predicate, so `backend stale (source changed), evicting` can now only be
   printed when two *known* digests genuinely differ. An unreadable digest
   never borrows that line; its own WARNING names the real cause.

It lives in `backend_registry` rather than in a second helper inside
`singleton` because that module is already THE home for the `server.json`
record and its per-field normalizers (`recorded_int` is the same shape of
thing): singleton computes the current digest, the registry says what a
recorded one means. One home, per CLAUDE.md convention 4.

### The record-time decision (deliberate)

`_write_server_state` / `record_backend` now accept `str | None` and **record
the sentinel** as JSON `null` when the source could not be hashed at spawn.
Considered and rejected: refusing to spawn (a read hiccup must not block a
backend), and recording `""` (that is the poisoned-record bug above — an empty
digest contradicts every future one, so the next session evicts). Recording
`null` means the entry says "unknown", `fingerprint_mismatch` reads it as
unknown, and the backend survives on the version key plus a live `initialize`
until something re-records it. The cost is bounded and named: for that one
backend, an in-place source edit is invisible to the digest key until it is
respawned, exactly as it is for a pre-M2 record.

## Cost

Three reads instead of one, only on the failing path (worst case ~0.1s of
sleeping before a WARNING). Discovery's happy path is unchanged. The relaxation
is scoped to one of the two identity keys: a version mismatch still evicts
immediately, and a *known* digest mismatch still evicts immediately — an
upgrade or a real source edit takes effect exactly as before.

## Tests

`tests/test_fingerprint_unreadable.py` (11 pins, hermetic — no Chrome, no real
backend, state paths redirected to `tmp_path`):

* `test_persistent_oserror_yields_none_not_empty_string` — the sentinel is
  `None`, not `""`. RED before the fix: `assert '' is None`.
* `test_read_failure_is_retried_before_giving_up` — ≥2 read attempts.
* `test_transient_failure_that_heals_yields_a_real_digest` — attempt 2 wins.
* `test_readable_source_still_yields_a_digest` — happy path unchanged.
* `test_unreadable_current_fingerprint_reuses_healthy_backend` — THE fix: an
  unreadable digest + a healthy same-version backend → reused. RED before:
  `assert None == <port>`.
* `test_recorded_sentinel_fingerprint_reuses_healthy_backend` — a recorded
  `null` is unknown too, so the defect cannot move to record time.
* `test_version_mismatch_still_evicts_when_fingerprint_unknown` — unknown
  relaxes exactly one key.
* `test_genuine_source_change_still_evicts` — regression guard (M2/F-206).
* `test_legacy_record_without_a_fingerprint_still_evicts` — an absent field is
  not a sentinel.
* `test_unreadable_warning_names_the_real_cause` — WARNING, contains the OS
  error, and does NOT say "source changed".
* `test_eviction_line_is_not_logged_when_the_fingerprint_is_unknown` — the
  misattribution itself. RED before: the line was logged.

Kept green alongside: `test_singleton_version_aware.py` (including
`test_empty_fingerprint_never_matches`, the `""` fail-closed pin),
`test_singleton_cold_start_patience.py`, `test_watchdog_busy_vs_dead.py`
(F-820), `test_watchdog_app_level.py`, `test_proxy_backend_death.py`,
`test_backend_registry.py`, `test_find_running_server_app_probe.py`,
`test_singleton_display_routing.py`, `test_singleton_stop_restart.py`,
`test_doc_claims.py` — 156 passed.

## Residuals — NOT fixed here

1. **F-839** is the other way a healthy backend gets killed; it is handled
   separately and is untouched by this change.
2. **`_source_fingerprint` hashes the whole package on every cold-start
   discovery.** Now that a failed read is survivable, the remaining exposure is
   cost, not correctness (~1 MB read+hash per proxy start). Caching it by
   (mtime, size) is a separate, optional optimisation.
3. **A backend recorded with `null`** keeps the version key only until it is
   respawned. Nothing re-records the digest in place; if that ever matters, the
   fix is a re-record after a successful hash, not a change to the comparison.
4. **`backend-boot.log` is still unrotated** (F-820 residual 2), unchanged.

## LOC budget

`singleton.py` was at 1000/1000 — zero headroom. The change is net **−1**
(999): the three-state reading moved into `backend_registry.py`
(453 → 475/1000), and the prose in the functions this change touches was
compressed to pay for the retry loop. No cap was raised and nothing was padded.
