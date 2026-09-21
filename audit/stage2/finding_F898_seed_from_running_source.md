# F-898 — seeding a session from a RUNNING source: the measurement

**Severity** n/a — this is the GATE on phase 4 of `design_session_ux.md`, not a
defect. **Ref** `origin/main` = f18ecc5 (2.1.11). **Branch**
`test/F898-seed-storage-matrix`. **Status** MEASURED; verdict in §7.

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
