# F-927 — the deferred profile cleanup never consults the in-flight clone protection

**Status:** FILED, not fixed
**Found by:** the F-925 audit of `clone_storage`'s sweep protection
**Severity:** data loss / spawn corruption. A deferred delete can `rmtree` a
directory a new spawn is copying its profile into, and the spawn's own sweep is
what fires it.

---

## 1. The two halves that do not meet

`clone_storage` keeps an authoritative in-process reservation set
(`clone_storage.py:115-158`):

```python
_PROTECTED_CLONE_DIRS: set = set()
def _protect_clone_dir(path) -> None: ...
def _clone_dir_is_protected(path) -> bool: ...
```

Its own comment states exactly why it exists: the filesystem liveness
heuristic reports "not running" during the window between a clone being chosen
and its browser attaching, so "a concurrent or startup sweep could delete a
live-but-not-yet-attached clone out from under the spawning browser (a silent,
unlogged session loss)."

`process_cleanup._cleanup_profile_dir` (`process_cleanup.py:411-478`) is a
deleter of exactly those directories, and it consults two witnesses —
`_profile_claimed_by_live_instance` (`:432`) and
`_get_active_browser_profile_dirs` (`:442-447`) — before
`shutil.rmtree(path, ignore_errors=False)` at `:459`. It never consults
`_clone_dir_is_protected`. A census of the whole module finds no reference to
the protected set or to `clone_storage` at all (the single match for
`clone_storage` at `:629` is a comment about a different function).

Both witnesses it *does* consult are liveness witnesses. They are precisely the
ones the protection set exists to compensate for.

## 2. Why it is reachable in-process, with no storage-cap breach

The clone directory name is **deterministic per project**:
`_clone_profile_dir_for_session` builds `<label>-<sha256[:12]>` from the client
session seed, and `_available_clone_dir` returns that base name unchanged
whenever `_dir_unavailable` is false. So instance B, spawned in the same
project after instance A closed, picks the **same directory name A used**.

The sequence, all inside one backend process:

1. A closes. Its profile is tracked but its browser pid is gone, so cleanup is
   deferred (`cleanup_deferred_profiles` skips entries whose pid still exists,
   `process_cleanup.py:816-817`).
2. B spawns. `_available_clone_dir` hands back the same base name, and
   `_protect_clone_dir(clone)` reserves it (`clone_storage.py:959`).
3. B calls `spawn_background_sweep("pre-clone")` (`:932`) — **before** its own
   copy at `:960`. The sweep runs on a worker thread via `asyncio.to_thread`,
   so it is concurrent with the copy.
4. `run_storage_sweep` calls `process_cleanup.cleanup_deferred_profiles()`
   (`:442`), which reaches `_cleanup_profile_for_metadata` →
   `_cleanup_profile_dir` for A's entry — naming the directory B is copying
   into.
5. B's browser has not launched, so `_get_active_browser_profile_dirs` does not
   list it and `_profile_claimed_by_live_instance` does not claim it. The
   protection set says "reserved". Nobody asks. `rmtree` at `:459`.

No storage cap is involved, no eviction, no second process. The spawn that gets
hurt is the one that scheduled the sweep.

## 3. Notes

* `cleanup_deferred_profiles`' docstring is right about its own question — it
  deliberately re-measures ownership at FIRE time rather than trusting a
  sweep-start snapshot (F-834) — which makes the omission sharper, not softer:
  it re-measures the two witnesses that cannot see an unlaunched spawn, and
  does not ask the one that can.
* The natural shape is the one `backend_eviction` already uses: the deleter
  takes the predicate as an argument so `process_cleanup` does not import
  `clone_storage` (the import runs the other way today —
  `clone_storage.py:41` imports `process_cleanup`). `_dir_unavailable` is the
  existing composition of "protected or live" and is the obvious thing to hand
  over.
* Unverified by execution: this is a code-path reading, not a reproduction. The
  window is genuinely narrow (it needs the sweep thread to reach A's entry
  between B's reservation and B's browser attaching), which is consistent with
  it never having been seen and with it being very hard to attribute if it
  were — the symptom is a spawn whose profile directory vanished mid-copy.
