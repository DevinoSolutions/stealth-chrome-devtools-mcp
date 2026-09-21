# F-898 — seeding a session from a RUNNING source

**Severity** n/a — phase 4 of `design_session_ux.md`, a capability rather than a
defect. **Status** MEASURED (§1–§9, taken at `origin/main` = f18ecc5, 2.1.11,
branch `test/F898-seed-storage-matrix`) and **BUILT** (§10–§12, at
`f397de4` = 2.1.12 + F-897, branch `feat/F898-seed-from-running-source`).

The measurement is kept ahead of the design deliberately: every decision in §10
is answerable only because §2–§4 say what the two mechanisms actually carry, and
a reader who starts at the implementation should be able to see what it was
allowed to promise before it promised it.

The design study (§3.C) offers two mechanisms for seeding a new browser session
from a source whose browser is still up, and refuses to let either be described
to a user until a matrix exists:

* **C1** — a file copy of the live profile. Not a proposal: it is what
  `clone_storage._copy_profile_tree` already does, a size+mtime delta walk run
  twice 0.2 s apart, locked files skipped with a WARNING and no failure.
* **C2** — a CDP hand-off, `Storage.getCookies` on the running source into
  `Storage.setCookies` on the target. Browser-wide, plaintext, cookies only.

## 1. Method

`tests/test_e2e_seed_storage_matrix.py`, integration-marked, three nodes — one
per cell — each self-contained (its own source browser, its own seed, its own
target). No real site, no credential, no network: two loopback fixture origins
this file serves.

**Five mechanisms**, each a different shape of real login:

| mechanism | how it is set | how it is read back |
|---|---|---|
| session cookie (no expiry) | `Set-Cookie`, no `Max-Age` | the fixture server's own `Cookie` header ledger |
| persistent cookie | `Set-Cookie; Max-Age=86400` | same |
| `HttpOnly; Secure; SameSite=None` cookie | same | same — **the page cannot see this one** |
| `Partitioned` (CHIPS) cookie | site B, inside a cross-site iframe under site A | same, on the embed request |
| `localStorage` / `sessionStorage` / IndexedDB / Cache Storage / a service worker | page JS | page JS |

**The cookie oracle is the SERVER**, not `document.cookie`: three of the five
cookies are `HttpOnly`, which is the shape a real session cookie has, so a
JS-side check would report "not carried" for the class that matters most.

**Two IP literals, `127.0.0.1` and `[::1]`, not two ports.** A site is
scheme+host; the port is not part of it. That is not quoted from a spec here, it
is MEASURED: Chrome reports the partition key of the CHIPS cookie as
`{"topLevelSite": "http://127.0.0.1", "hasCrossSiteAncestor": true}` — **no
port**. Two ephemeral ports on one host would have been one site and could not
have partitioned anything, which is why `release_gate_harness`'s existing
`serve_fixture_origin_pair` (two ports, one host) is deliberately not reused.
`localhost` was rejected for a different, measured reason: against a v4-only
listener it cost ~2 s per request on this machine, walking `::1` first.

**Secure contexts without TLS**: `Secure` and `Partitioned` cookies were
ACCEPTED on both `http://127.0.0.1` and `http://[::1]`, and the service worker
registered — both loopback literals are "potentially trustworthy". Their
`sourceScheme` is reported as `NonSecure`, and it round-trips as that.

## 2. The matrix

`carried` / `—` (not carried) / `unstable`. Every cell is the same seeded
source, proven complete before anything is copied (the source row is asserted in
every node, so a cell cannot pass by copying an empty profile).

| mechanism | C1, source RUNNING | C2, source RUNNING | C1, source STOPPED (control) |
|---|---|---|---|
| session cookie | — | **carried** | — |
| persistent cookie | — | **carried** | **carried** |
| `HttpOnly; Secure; SameSite=None` | — | **carried** | **carried** |
| `Partitioned` (CHIPS) | — | **carried**, key intact | **carried** |
| `localStorage` | **unstable** (see §4) | — | **carried** |
| `sessionStorage` | — | — | — |
| IndexedDB | **carried** | — | **carried** |
| Cache Storage | — | — | — |
| service worker registration | — | — | — |

Three readings the table makes, which reasoning alone did not:

1. **C1 from a running source carries no cookie at all.** Not "some". Zero, in
   7 runs of 7. `Default/Network/Cookies` is the SQLite jar and Chrome holds it
   open, so on Windows `_copy_profile_file` hits `WinError 32` on all three of
   its attempts, on both passes of the double walk, and skips the file — the
   target starts with no jar and creates a fresh one. The measurement reads the
   skipped names straight out of the copier's own WARNING:
   `{"Cookies", "Cookies-journal", "Session_<n>"}`.
2. **A session cookie never survives a FILE copy, even a clean one.** It has no
   expiry, so Chrome does not write it to the jar; no copy of that file can
   contain it. This is the one mechanism where C2 beats a cleanly-stopped C1.
3. **Cache Storage and the service worker are not a race — they are a
   decision.** `Service Worker/` is in `clone_storage._REGENERABLE_PROFILE_NAMES`,
   so the copier is instructed never to carry them, and stopping the source
   changes nothing. A PWA-style login whose token lives in a service worker's
   cache does not survive a clone at all, today, and never has.

## 3. C2 fidelity, field by field

`Storage.getCookies` on the source → `Storage.setCookies` on the target, over a
raw CDP socket (see §5). Every field that exists on both `Network.Cookie` and
`Network.CookieParam` is forwarded verbatim: `name`, `value`, `domain`, `path`,
`secure`, `httpOnly`, `sameSite`, `priority`, `sourceScheme`, `sourcePort`,
`partitionKey`. `size`, `session` and `partitionKeyOpaque` are read-only report
fields with no `CookieParam` counterpart and are dropped.

| question | measured |
|---|---|
| count before / after | 5 / 5 synthetic, in 3 runs of 3 (plus, in 1 of 3, one Chrome-originated cookie — §6) |
| `httpOnly` | round-trips |
| `secure` | round-trips (True on a loopback `http` origin) |
| `sameSite` | round-trips (`None`) |
| `expires` — persistent | round-trips |
| `expires` — session | round-trips **as a session cookie**: CDP reports `-1`, which is a marker and not a date, so the hand-off omits `expires` rather than forwarding it, and the target reports `session: true` |
| `domain` / host-only | round-trips |
| `sourceScheme` / `sourcePort` | round-trip |
| `partitionKey` | round-trips **exactly**, `topLevelSite` and `hasCrossSiteAncestor` both |
| cookies REJECTED by `setCookies` | **none**, in 3 runs of 3 |

The partition key is asserted **positively**, not by equality alone: two cookies
that both lack a key also "match", so the node asserts the source's CHIPS cookie
really has a key naming site A, that its unpartitioned twin — set by the same
response — has none, and that the key is what arrived.

`Storage.setCookies` gives **no per-cookie verdict**. It answers once for the
batch. The measurement therefore re-offers any cookie that did not land, alone,
to tell "Chrome refused this" from "Chrome dropped it silently"; nothing had to
be re-offered in any run.

## 4. Stability, and the one unstable cell

**3 consecutive full-matrix runs, all green**, plus 5 extra runs of the C1-live
cell. Every cell above is identical across every run **except one**:

* **`localStorage` under C1-live**: absent in 6 of 7 runs, present in 1.
  REASONED cause — Blink commits `localStorage` to its LevelDB on a delay rather
  than at the `setItem`, so whether the bytes are on disk when the copy walks
  past depends on how long the rest of the seed took. IndexedDB is stable in the
  same cell because its transaction commits its log at `oncomplete`, which the
  seed awaits.

  **It is deliberately not asserted.** Asserting either answer would be
  asserting a race. And the unstable answer is itself the finding: C1 from a
  live source *may or may not* carry `localStorage`, and cannot say which.

Timing: a full matrix run is ~23 s (six Chrome spawns, three profile copies).

## 5. The blocker found on the way — F-902

C2 could not be measured through the door a shipped C2 would use.
`Storage.getCookies` returns `Network.Cookie`, `nodriver` 0.47 parses it with
`bool(json['sameParty'])`, and **Chrome 153 no longer sends that field** — so
the `KeyError` ends `Connection._listener`, which guards its event path and not
its result path, and every later call on that connection hangs forever. It
reaches `get_cookies`, `clear_cookies(url=…)` and `get_instance_state` in the
shipped product. Written up separately as
`finding_F902_cookie_result_kills_the_cdp_listener.md`; not fixed here.

The measurement drives C2 over a **raw websocket to the browser endpoint** —
Chrome's own JSON in, Chrome's own JSON out, no generated parser in between.
That is not a preference: a measurement may not be conducted through a door it
has just proved broken. It also makes §3 stronger, because every field there is
the wire's rather than a dataclass's idea of the wire.

**Phase 4 cannot ship before F-902 is fixed.** Its first CDP call is the call
that kills the connection.

> **Resolved.** F-902 shipped before this implementation started (`cdp_transport`
> half 2 guards `Transaction.__call__` so a parse failure fails ONE command
> instead of the listener; half 3's `_RETIRED_COOKIE_FIELDS` supplies the
> retired `sameParty` so the reply parses at all). The implementation therefore
> sends its jar through `Connection.send` like every other command in the tree,
> and the raw websocket stays what it always was — a measurement instrument.
> One consequence is carried forward into §10.2: because half 3 SYNTHESISES
> `sameParty`, the hand-off must not forward it.

## 6. What C2 carries that nobody asked it to

`Storage.getCookies` is **browser-wide**. There is no origin filter, no
`urls=`, no partition filter. MEASURED on run 1 of 3: a profile created seconds
earlier had already acquired a Google `NID` cookie from Chrome's own startup
fetches, and C2 carried it into the target along with the five synthetic ones.

On a real seed profile that class is **every site the human is logged into**. A
`--from default` that hands the target the whole jar is a different product from
one that hands it a login, and the difference is not visible at the call site.
The design study's Q3 default (cookies only, no `localStorage`) answers the
mechanisms question and not this one; an origin allow-list is a design decision
phase 4 still owes.

## 7. Verdict

**May C2 be advertised as "carries cookie logins"? — YES, with the sentence
bounded, and NOT before F-902 is fixed.**

* It carries every cookie shape measured, including the two a file copy cannot:
  the session cookie, and every cookie at all while the source is running.
* Fidelity is exact on every field that exists on both types, `partitionKey`
  included. Nothing was refused.
* It is the only one of the three cells that works **without closing the
  seed's browser**, which since F-888 is the state the master/`default` profile
  is permanently in (`design_session_ux.md` §1.6, §2.5). That is the whole
  reason C2 was costed.

**What the tool docstring must say it does NOT carry** — each of these is a real
login for some site, and each was measured absent:

1. `localStorage` — a JWT-in-`localStorage` SPA does not come across.
2. `sessionStorage` — per tab, in memory; nothing carries it, ever.
3. IndexedDB.
4. Cache Storage, and any **service worker** registration — so a PWA-style login
   whose token lives in a worker's cache does not come across (and does not
   survive an ordinary clone either).
5. The `Login Data` / `Web Data` autofill stores.

The voice to write it in is `click_target`'s "deliberately not a did-the-page-
react oracle": name the two facts it decides and refuse the rest out loud.

**And it must say what it DOES carry that was not asked for**: every cookie in
the source browser, for every site (§6).

## 8. The PII rule — pin-worthy

**No cookie NAME and no cookie VALUE may reach a log line, a tool message, a
returned record or Sentry.** Counts, flags, domains-as-shape and per-field
verdicts only.

The names alone are identifying — `NID` names Google, and a seed's jar is a list
of the sites its human uses — and the values are the sessions themselves. This
is `page_storage`'s discipline (F-869: "no message here ever carries a stored
VALUE", because localStorage is where session tokens live), `text_entry`'s (the
typed text never appears, because the field may be a password box) and
`control_state`'s (an option's text is frequently an account number), applied to
the one subsystem whose entire payload is credentials.

The measurement obeys the rule it proposes: `_report` prints this file's own
`f898_*` names and renders everything else as `<N non-synthetic>`, and it prints
every stored value as `<N chars>`. A phase-4 implementation should be pinned the
same way — a test that greps the durable log, the tool return and the Sentry
`before_send` payload for any cookie name it set.

## 9. Residuals

* **Windows only.** Chrome 153.0.8010.50, Windows 11 Pro 26200, 2026-09-21.
  **The Linux and macOS cells are UNMEASURED** until CI runs this file. One
  result is expected to be platform-specific and it is the most important one:
  C1-live's zero cookies is caused by a Windows **share-mode** refusal
  (`WinError 32`), and POSIX has no such refusal — a live SQLite jar will
  probably COPY there, giving a torn or WAL-truncated read instead of a clean
  miss. That is a worse failure than a clean miss, not a better one, and the
  finding's C1 verdict should be re-read once the POSIX cells report.
* **Five mechanisms, not all of them.** `Login Data`, `Web Data` and
  `Trust Tokens` were not measured; the first two are named in §7 as
  not-carried by C2 on the plain ground that C2 sends cookies.
* **One source profile shape.** Every cell seeds a profile created seconds
  earlier. A real seed is gigabytes with tens of thousands of files, and C1's
  copy duration — and therefore its exposure to the race in §4 — scales with
  that. Nothing here measures a large profile.
* **`Storage.setCookies` was never observed to refuse.** The re-offer path that
  would name a refusal is present and exercised but never fired, so it is
  proven to run and not proven to report correctly.

---

# The implementation

## 10. Design

### 10.1 The decision: one witness, three answers

F-897 refuses `seed_from=<a session a browser holds>` BY NAME, and §2 says that
refusal was right: a file copy of a running source carries **zero** cookies and
cannot say which file mattered. What changed is that since F-888 the `default`
session's browser is immortal, so "close it first" stopped being a remedy anyone
would take.

The refusal now splits on one question — **do WE drive that browser** — and
`profile_source.seed_source` has three answers rather than two:

| source | answer | kind |
|---|---|---|
| closed | copy it, exactly as 2.1.12 | `explicit-session` |
| running, an instance THIS backend holds | copy what the copier can, then hand the jar over CDP | `explicit-session-live` |
| running, anything else | refuse by name | — |

The witness arrives as a plain predicate (`driven`), on the same terms `held`
already does, so `profile_source` stays a leaf that knows nothing about
browsers. It is **asked only when `held` is true**, so an ordinary spawn pays
nothing for a question it does not need (pinned with a witness that raises).

Row 3 is kept rather than widened, and the refusal now says WHICH half is
missing: there is no CDP connection of ours to ask for the cookies. Row 3 is
also what a naive "if it is running, hand the cookies over" would get wrong —
another backend's browser is reachable on a port we can read out of the record,
and reading a stranger's jar because we could is not a capability this tool
should acquire by accident.

**The default is fail-CLOSED.** `clone_storage`'s two public gates default
`driven` to `profile_source.NOTHING_DRIVEN`, which answers no, so every caller
that does not hand in a witness gets 2.1.12's refusal. Reading a browser's
cookie jar is not something an omitted argument may authorise.

### 10.2 The translation, and the two fields held out of it

`Cookie` → `CookieParam` is a verbatim pass-through over a set **derived** from
nodriver's own two dataclasses (their shared field names), so a CDP bump that
adds or retires a shared field moves it and a pin makes that movement a
decision. Two fields are excluded by name:

* **`expires`** needs a translation and not a copy. CDP reports a session
  cookie's expiry as `-1` — a marker, not a date — so forwarding it is a claim
  about 1969, and OMITTING it is what makes the target treat the cookie as a
  session cookie too (§3). It is the one shape a file copy can never carry at
  all, because a session cookie is never written to the jar on disk.
* **`same_party`** is F-902's fix read from the other side. Chrome 153 does not
  send it; `cdp_transport._RETIRED_COOKIE_FIELDS` SYNTHESISES `false` so the
  reply parses. Forwarding that would write our own invention back into a
  browser.

Everything the derivation drops without being told to is also right: `size`,
`session` and `partitionKeyOpaque` are read-only report fields with no
`CookieParam` counterpart, and `url` is `CookieParam`'s alone — a jar read
carries `domain`/`path`, and inventing a `url` would change which host a cookie
belongs to.

### 10.3 The file copy still runs

Deliberate, and it is the reason the kind is its own word rather than a flag
beside `explicit-session`. §2 says C1-live carries IndexedDB and (unstably)
`localStorage` while carrying no cookies; C2 carries cookies and nothing else.
Running both is strictly more than either, the copier already reports its own
skips, and what ARRIVES is stated in the tool docstring rather than implied.

### 10.4 A failed hand-off is REPORTED, never raised

The new session exists, its browser is running, and everything a file copy could
carry is in it — what failed is an augmentation. Raising would take a working
session away from the caller AND leave a directory on disk that a retry then
refuses as "already exists" (`require_new_session`'s fourth refusal), i.e. the
worst of both. So the answer carries `seeded_via: "copy"` plus a shape-only
`cookie_handoff_error`, and a WARNING goes to the ring. `seeded_via` appears
ONLY on this path: on every other spawn `seeded_from` already says everything
there is about where the session came from.

### 10.5 The PII rule, as built

§8's rule, enforced in three places:

* **`cookie_handoff` emits counts, a CDP METHOD and an exception TYPE.** Its one
  exception class carries a message this module wrote; anything else is reported
  by type alone, because the text of a `Storage.setCookies` failure is Chrome's
  answer to a command whose parameters WERE the jar.
* **The warning deliberately does NOT pass `error=exc`.** That forwards
  `exc_info`, and a traceback is a log line. This is the one place in the tree
  where F-869's convenience is declined, and it is declined because the payload
  is credentials rather than page shape.
* **The source DIRECTORY never reaches a caller.** It rides an internal
  selection key (`clone_storage.LIVE_SEED_KEY`) dropped at
  `_public_profile_selection` — the one line between what the resolver decided
  and what is reported.

### 10.6 Where the code went, and what paid for it

`embedded/cookie_handoff.py` is the new home: the jar read and write, the
translation, the `Driven` snapshot and the failure phrasing.
`profile_source.seed_source` owns the DECISION; `clone_storage` binds the two
witnesses and marks the selection it permitted; `spawn_browser` takes ONE
snapshot before its pre-flight, hands the same predicate to both asks, and
drives the hand-off after the target launches.

`clone_storage.py` stood at exactly **1000/1000**, a cap that ratchets DOWN
only, so this is paid for by an extraction rather than a raise: the
regenerable-profile TRIM (`dir_size_bytes`, `regenerable_dirs`,
`regenerable_size`, `trim_regenerable`) moved to `profile_copy`, beside the
`REGENERABLE_NAMES` list it reads. Those two paths read one list in opposite
directions — a copy EXCLUDES these names, a trim DELETES them — and they lived
one import apart from drifting. **1000 → 993.**

## 11. What the pins catch, and two defects they caught

`tests/test_cookie_handoff.py`, 31 nodes, hermetic. RED evidence was taken by
mutating the product and re-running (bytecode writing disabled, so no stale
`.pyc` can flatter a revert):

| mutation | node that failed |
|---|---|
| a live source we DO drive is refused anyway | `test_b_a_running_source_we_drive_is_seeded_under_its_own_kind` |
| the `-1` expiry marker is forwarded | `test_a_a_session_cookies_minus_one_expiry_is_omitted` |
| `same_party` is carried back | `test_d_same_party_is_never_written_back`, `test_g_the_carried_set_is_the_measured_one` |
| a non-`HandoffError` reports its TEXT | `test_b2_…reports_its_type_alone`, `test_b3_…never_reaches_the_answer` |

**The translation class is deliberately not driven through a double.** A test
double answers a canned value, so a fixture built from objects of ours would
compare the mapping to itself; every case starts from Chrome 153's own measured
WIRE JSON through `Cookie.from_json` and asserts on `CookieParam.to_json`. That
is `fixtures-from-the-same-serializer-cannot-fail` applied to the one place here
where a wrong field is silent.

Two defects the process caught in the first draft, both worth recording because
neither was visible to a reading:

1. **The reported CDP method was a guess.** It was stamped by the caller before
   the call, so a failed READ was reported as `Storage.setCookies`. A diagnostic
   pointing at the wrong half of a mechanism is worse than none. The method is
   now read off the step that actually failed.
2. **The witness read a field that does not exist, and every pin passed.**
   `driven_profiles` reached for `instance.user_data_dir`; `BrowserInstance` has
   no such field (it is `BrowserOptions`', kept under the entry's `"options"`),
   so `getattr` answered None for every real instance and the hand-off would
   never have fired on a live backend — silently. The pins passed because the
   DOUBLE offered the attribute the product does not have, which is
   `mocked-fakes-can-encode-the-bug` reached through the double's own
   convenience. `ty` is what caught it. `fakes.FakeBrowserManager` now offers the
   real `get_instance` surface with a `profiles=` seed, and the absence is
   pinned.

## 12. Residuals

Everything in §9 still stands. In addition:

* **An origin allow-list is still owed** (§6). The hand-off carries the WHOLE
  jar — every site the source session is logged into — which is what a file copy
  of a closed source already does, and is why it ships as the default rather
  than behind a flag. A `carry_origins=` opt-in is the named later step; it
  needs a decision about what a partitioned cookie's key means under a filter,
  which is why it is not a half-hour change.
* **`localStorage` is not carried, and a JWT-in-`localStorage` SPA therefore
  does not come across.** Per the design study's Q3 default. `DOMStorage` can
  add per-origin storage but only for origins you can NAME, so it is blocked
  behind the same allow-list decision.
* **POSIX is unmeasured for the hand-off**, as it is for the matrix. Nothing in
  the mechanism is platform-specific (it is two CDP commands), but the FILE half
  beside it is: §9 predicts a POSIX C1-live copies a torn jar rather than none,
  which would make `seeded_via: "cdp-cookies"` arrive on top of cookies that are
  present-but-stale rather than absent. The hand-off writes after the copy, so
  the jar wins either way; this is a note about what the copier's own warnings
  will look like, not about correctness.
* **A source that closes between the pre-flight and the hand-off** loses the new
  session its cookies and says so. Re-deriving the snapshot at the moment of use
  is what makes that a reported degradation rather than a wrong answer; nothing
  holds the source open, and holding it would be a worse promise than the one
  being made.
* **`Storage.setCookies` still gives no per-cookie verdict.** The implementation
  reports what it SENT and what the target's jar held afterwards, and does not
  claim the two are the same number — Chrome's own startup fetches put cookies
  in a seconds-old profile (§6).
