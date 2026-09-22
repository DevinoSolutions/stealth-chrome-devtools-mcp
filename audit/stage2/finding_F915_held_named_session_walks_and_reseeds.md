# F-915 — a held NAMED session is walked to `<name>-N` and silently re-seeded

**Severity: top**, and it is the same harm as F-914 reached through the other
door. This is the most COUNTABLE form of the owner's "I have to set up the
credentials again": seventeen directories on disk, one of them at `-22`.

## 1. The defect

`clone_storage.resolve_profile_selection` (2.1.12, `clone_storage.py:877-891`):

```python
hold = _profile_hold(explicit)
if hold is not None:
    requested, explicit = explicit, _next_available_explicit_dir(explicit)
    walk = {"requested_user_data_dir": ..., "walked_to": ..., "walk_reason": ...}
...
if not explicit.exists() and _is_relative_to(explicit, clone_root):
    seed = _seed_source_for_copy(seed_from, driven)
    _copy_profile_tree(seed.path, explicit, clone_root, seed.kind)
```

The caller asked for `session=X`. A live browser holds `X`, so they get `X-2`,
**seeded from the SHARED seed** — a different identity, with none of the cookies
or logins `X` exists to hold. The spawn reports success.

F-871 made this REPORTED (`requested_user_data_dir` / `walked_to` /
`walk_reason` in `spawn_diagnostics.profile_selection`, plus a `warning` that
leads with it). It did not make it stop, and reporting is not the same as being
given what you asked for: the caller named that session precisely because they
wanted its logins.

## 2. Measured on the owner's machine, 2026-09-21

Seventeen directories under `C:\stealth-mcp-browser-sessions\sessions` carry a
numeric walk suffix, among them:

```
amind-72d4743e01f1-41364-14
amind-72d4743e01f1-41364-18
amind-72d4743e01f1-41364-22
superbooks-7cf1e3db6477-190004-7
superbooks-7cf1e3db6477-190004-9
OpenRouterFree-bd2402c3ad2b-47424-9
```

Each suffix is one walk. `-22` means it happened at least twenty-two times for
ONE session name. Every one of those directories was seeded from
`master-snapshot`, which — see F-914 §2 — is frozen at the last clean close of
the shared session while that session is open.

## 3. Why the same hold witness applies

Identical to F-914 §3: `_profile_hold` resolves an unreadable answer toward HELD
on Windows, which is right for "may I take this directory" and was silently also
deciding "may I hand back a different one".

## 4. The rule

The owner's ruling (F-914 §4) applies unchanged — hand over the holder's jar, or
refuse by name. What differs is only which directory is asked about.

## 5. The fix

The walk is now CONDITIONAL, and `profile_target.hand_over_or_refuse` is the
same one home F-914's shared branch asks:

* **driven** -> the walk happens exactly as F-871 shaped it, three fields and
  all, but the copy source is the **HOLDER'S OWN DIRECTORY** rather than the
  shared seed, and the holder's jar is handed over afterwards over CDP. A named
  session has no closed copyable form of its own (per-session seeds were
  declined in F-898 §10.1), so the holder IS the only source there is; the file
  half loses whatever Chrome holds open and the cookies arrive through
  `cookie_handoff`, which is the mechanism F-898 built for exactly this.
  `seeded_via` and `handed_over_from` say so.
* **not driven** -> `ToolError`, session and holder pid, no path, nothing
  created.

`profile_source.LIVE_SESSION_KIND` is the word recorded in the new session's
marker, so `stealthy profiles` reports where it really came from.

**The tool's `warning` changed with the rule.** It said the walked directory
held "none of the cookies or logins the requested one holds" — true of F-871's
unconditional walk and FALSE of the only walk that survives. It now says what is
still true: a different directory, its cookies handed over, and that anything
the original keeps outside its cookie jar did not come across.

## 6. What this costs, and what happens to the seventeen

**The seventeen walked directories are LEFT ALONE.** They are not deleted, not
merged and not renamed. Three reasons: some may hold a login the owner typed by
hand into a walked directory without knowing it was a walk; deleting a profile
directory is the class of act this whole finding family exists to stop; and the
owner has not authorised removing any housekeeping residue. They remain visible
in `stealthy profiles`, which lists every directory under the clone root
unfiltered, and each carries a marker saying it was seeded from `default`. An
operator who wants the space back can remove them by hand. What the fix
guarantees is that **no eighteenth one is created**.

**A spawn onto a held session we do not drive now fails** where it used to
succeed with a stranger. That is the ruling applied literally. The two remedies
are in the message: close that browser and spawn again to get that session, or
pass `session=<a free name>` for a new one.

**The re-attach decline path changed meaning with it** (F-888). `spawn_browser`
asks `browser_reattach.adopt_held_profile` in FRONT of selection, and all three
of its refusals describe a directory held by a browser this backend does not
drive: a live sibling backend's, a process tree with no identifiable browser in
it, and a holder no CDP endpoint could be recovered for. Each used to be followed
by a spawn onto `<name>-N`; all three now end in this finding's refusal instead.
Three sentences said otherwise and were corrected in the same commit —
`spawn_browser`'s own `session` description, which is a **SOFT tool-surface
golden update** (one line in `tests/goldens/tool_surface.json`, regenerated with
`tools/dump_tool_surface.py --write`); the no-endpoint refusal's tail, "and a new
browser was started instead", which the spawn handler glues onto a message
reading "Nothing was created"; and RUNBOOK's "Recover a stranded login". A
`user_data_dir` PATH outside the clone root is unaffected — it was never walked
and is not refused — and that is the one case where a decline is still followed
by an ordinary spawn.

**A walk still changes identity when it DOES happen**, and the new session
diverges from the holder from that moment. The cookie jar is the one thing
carried across; localStorage, IndexedDB, service workers and Cache Storage are
not (F-898 measured this and the glossary's *cookie hand-off* row states it).
That is why the `warning` survives rather than being deleted — a walk is still
worth announcing, it just no longer announces a lie.

**`test_profile_lock.py::test_a_live_lock_walks_and_the_answer_says_why` is a
SOFT golden update**, not a deletion: the three fields it pins are unchanged and
a `driven` witness is added, with the refusal half pinned in the node beside it.
