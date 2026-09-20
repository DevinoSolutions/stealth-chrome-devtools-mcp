# F-895 — a session could not say which seed it came from, or how old it is

**Severity** MEDIUM (visibility). **Ref** `origin/main` = 56596e8 (2.1.10).
**Shipped in** `fix/F892-F895-snapshot-truth`. This is option (E) of
`design_session_ux.md` §3 — the phase the study rates highest value per line,
because every other story in that document is a silent surprise until the state
is sayable.

## 1. What shipped

The clone marker recorded `source` (an absolute path), `source_kind` and
`created_at` (56596e8 `clone_storage.py:720-731`). Nothing read them back for a
caller. `spawn_diagnostics.profile_selection` carried the role and the path;
`stealth-chrome-devtools profiles` printed name, role, size and in-use.

So a session that had been frozen since August looked exactly like one created
this morning, and the question an operator actually has — *is my login in
there, or is this copy older than the login?* — had no answer anywhere in the
product.

`MEASURED 2026-09-20`: 27 named profiles, all `source_kind:
explicit-master-snapshot`, the oldest `created_at 2026-08-04`.

## 2. The fix

**The marker** gains two keys, written beside the three legacy ones (never
replacing them, so a 2.1.10 reader still finds what it looks for):

* `seeded_from` — the seed by NAME (`master-snapshot` today). A name, not a
  path, because it is the word the coming session vocabulary will rename and
  the word a user can say.
* `seeded_at` — when this copy was taken.

**`profile_seed.provenance`** reads them back as three fields, and computes
`seed_changed_since` by asking `LOGIN_WITNESSES` — F-892's list, the SAME one,
never a second — whether the seed has taken a login write since `seeded_at`.

**Two consumers, one answer.** `spawn_diagnostics.profile_selection` (stamped
in `_public_profile_selection`, the one site every role passes through) and the
`profiles` CLI verb (`_collect_profiles` merges the same dict; `_seed_line`
formats it). They read the same marker, so they cannot disagree.

## 3. Unknown is never "fresh"

A marker carrying neither key is a legacy marker and reads `seeded_from:
"unknown"`, `seeded_at: None`, `seed_changed_since: None`. **`created_at` is
deliberately NOT substituted for `seeded_at`**: a timestamp that says when the
directory was made is not a claim about which seed it was made from, and the
whole point of the field is to stop a frozen profile reading as up to date.
The CLI prints `seeded from unknown (when: unknown)` and never
`SEED CHANGED SINCE`, which is only printed when the answer is True — False is
the ordinary case and would be noise.

## 4. No golden moved

The brief anticipated a SOFT golden update for the diagnostics shape. There is
none to update: no file under `tests/goldens/` carries `profile_selection` or
`clone_source` (checked), and `tool_surface.json` describes tool SIGNATURES,
which are unchanged — no tool gained or lost a parameter. `tests/goldens/` and
`test_mcp_protocol_surface.py` are green untouched.

## 5. Privacy

Sizes, mtimes, roles, a seed NAME and an ISO timestamp. No cookie name, no
cookie value, no profile content is read, printed or logged by anything added
here — `newest_login_write` calls `stat()` and never `open()`.

## 6. What the CLI does NOT print (review m6)

The master and the snapshot ARE the seed, so asking what seeded them is a
category error — they carry no marker and reported `seeded from unknown` about
themselves, on exactly the two rows an operator reads first. `_seed_line`
returns `""` for those two roles and the verb skips the line. An **unmarked
session** directory still says unknown: there the answer is genuinely not
known, which is the thing worth printing.

`seeded_from` ships `"master-snapshot"` today, which is the word the session
vocabulary (F-896) will rename to `default` — `seed_name`'s docstring says so
rather than calling it "the word a user can say", which overstated phase 1
(review m7). The field existing is part of what gives that rename one place to
land.

## 7. Residuals

* **It reports; it does not propagate.** A login in one session still does not
  reach another. That is deliberate (`design_session_ux.md` §3.D: default
  write-back silently merges two identities), and the remedy is the study's
  phase 3 `--from`, not this one.
* **`seed_changed_since` is an mtime comparison, so it over-reports.** Chrome
  rewrites the cookie jar for expiry pruning and for any page's `Set-Cookie`,
  not only for a login. A `True` means "the seed has moved on", not "there is a
  new login in it". Over-reporting is the direction that cannot hide one.
* **It says nothing about a seed that was refreshed but copied nothing.** That
  is F-893's report, in the refresh's own result, not in the marker.
* **Second-precision stamps.** `seeded_at` is truncated to the second and the
  comparison is `int(mtime) > seeded_at`, so a login landing inside the same
  second as the copy reads as not-changed. The alternative — every fresh clone
  reporting itself already stale — is worse and much more common.
* **Nothing back-fills the 27 existing profiles.** They read `unknown` forever,
  which is true: we do not know.
