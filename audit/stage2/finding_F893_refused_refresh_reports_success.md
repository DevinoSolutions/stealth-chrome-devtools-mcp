# F-893 — a snapshot refresh that copied nothing reported success

**Severity** HIGH (silent). **Ref** `origin/main` = 56596e8 (2.1.10).
**Fixed in** `fix/F892-F895-snapshot-truth`.

## 1. What shipped

`_copy_profile_tree` returned early, with no answer, when the TARGET directory
was held by a live browser (56596e8 `clone_storage.py:712-714`):

```python
    if target.exists():
        if _profile_has_running_browser(target):
            return
        _rmtree_robust(target)
```

and `_refresh_master_snapshot_if_safe` then set success unconditionally
(`:763`):

```python
        _copy_profile_tree(master, snapshot, default_session_root(), ...)
        result["snapshot_refreshed"] = True
```

Refusing the copy is RIGHT — deleting and rewriting a directory a Chrome is
writing into would be the harm. Reporting it as a refresh is not. The caller,
the `close_instance` return and `spawn_diagnostics` were all told the seed now
carried the master's logins.

This is convention 2's spirit from the other side: not a `{"success": False}`
where an exception belongs, but a `True` that is not one.

## 2. The measurement

The precondition is not hypothetical — it was live on this machine today.

* `MEASURED ~14:00 UTC 2026-09-20` (the design study, `design_session_ux.md`
  §2.3): a Chrome running with
  `--user-data-dir=C:\stealth-mcp-browser-sessions\master-snapshot`, pid 84708,
  `--remote-debugging-port=20048`.
* `MEASURED 17:32 UTC 2026-09-20` (this finding): that process is **gone**, and
  the artefacts it left are the durable evidence —

| file | master | master-snapshot |
|---|---|---|
| `Default/Network/Cookies` mtime | 17:27:53 | **17:28:00** (7 s newer) |
| `Local State` size | 90,406 B | **133,607 B** |

`_copy_profile_delta` uses `shutil.copy2`, which preserves the SOURCE mtime, so
a snapshot copied from that master can be neither newer than it nor 43 KB
larger in a file the master also has. The divergence can only have been written
by a browser running on the snapshot directory itself.

Nothing in the product puts one there; something passed that path as a
`user_data_dir`, which is why F-894 also closes that door.

## 3. The fix

`_copy_profile_tree` now returns `str | None`: `None` when the copy ran,
`TARGET_IN_USE` when it was refused. `_refresh_master_snapshot_if_safe` reports
`snapshot_refreshed: False` + `snapshot_error: SNAPSHOT_IN_USE`
(`"snapshot-in-use"`). The existing `"master-in-use"` arm — the SOURCE being
live, decided before the copy is attempted at all — is untouched.

**A return value, not an exception**: of the three callers only the refresh can
reach the branch (the other two copy into a directory they have just found
free), so an exception would be two `except` clauses written to be ignored.

## 4. The pins

`tests/test_profile_seed_truth.py::TestRefusedRefreshIsNotSuccess` — target
held → `snapshot_refreshed is False` and the named error; source held → the
unchanged `"master-in-use"`; an unobstructed refresh → still `True` with no
error key; and `_copy_profile_tree` itself returning `TARGET_IN_USE` with no
marker written. Mutation-checked.

## 5. Residuals

* **The refusal is reported, not repaired.** A browser on the snapshot still
  blocks every refresh for as long as it runs. That is deliberate — never kill
  a window somebody may be using — and F-894 stops the product from being the
  way one gets there. A browser already on the snapshot when this ships keeps
  blocking until its operator closes it.
* **`master-in-use` is now the dominant refusal**, and since F-888 made the
  master browser survive its backend it can hold forever
  (`design_session_ux.md` §1.6). Nothing in this PR addresses that; phase 1 of
  the design study reports it, and it is the strongest argument for seeding
  from a RUNNING source (option C2).
* **A partially-copied snapshot still reports success.** `_copy_profile_delta`
  skips individually locked files with a WARNING and no failure, so
  `snapshot_refreshed: True` means "the copy ran", not "every byte moved". That
  is the pre-existing C1 behaviour and is out of scope here; it is named
  because the fix makes the True more load-bearing than it was.
