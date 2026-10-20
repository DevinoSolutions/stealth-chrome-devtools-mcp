# F-999 — the rotation guard's per-session `Fetch.enable` aborts a clone page's CORS POST; block with `Network.setBlockedURLs` instead

**Severity:** Medium (a release-gate journey step, MQ-118's CORS preflight echo, failed on macOS roughly 1 run in 10
with a guard-armed clone; the same interception sits under every real clone page, so a page's own cross-origin POST can
be aborted for a user too). Tracked as T-2750.
**Files:** `embedded/google_rotation_guard.py` (`_configure`, `_send`, `_handle`, `_settle`; the `Fetch.requestPaused`
branch is gone), `NAVMAP.md`, `CHANGELOG.md`. Tests: `tests/test_google_rotation_guard.py`,
`tests/test_e2e_google_rotation_guard.py`.
**Branch:** `fix/F999-rotation-guard-network-block`. The nine diagnostic rounds ran on `fix/F999-cors-preflight-echo`
(PR #238, DO NOT MERGE, closed by this PR; its `tests/test_f999_diag.py` is deliberately not carried over).

---

## 1. The symptom

The release-gate journey step that has a page `fetch()` a cross-origin JSON endpoint with a `POST` saw, on a fraction
of runs, a preflight that completed (`OPTIONS` answered) and then a POST that never reached the server: the page got
`net::ERR_ABORTED`, the fixture ledger read `OPTIONS` without `POST`. Windows never showed it. Linux showed it at about
1-3% per run, macOS at 5-12% per run.

## 2. What nine diagnostic rounds established

Rounds 1-6 (raw CDP `Network` events, a server accept/request trace, Chrome timestamps, a net-log of the failing
requests, a guard-armed vs guard-disabled A/B, 200 runs per arm, then interleaved arms with a guard-connection target
trace) narrowed it to the F-939 rotation guard: the guard-disabled arm never failed.
Rounds 7-9 isolate the mechanism. "Bad" is `echo != 200` or a ledger other than `OPTIONS, POST, GET-blocked`.

| round | platform | arms (bad / runs) |
|---|---|---|
| 7 | Linux + macOS | NOGUARD 0/400, GUARD 11/400, GUARD skipping `chrome-extension` targets 12/400 |
| 8 | Linux | NOGUARD 0/150, GUARD 4/150, NOFETCH 0/150, CONN 0/150, BLOCKNET 0/150 |
| 9 | macOS | NOGUARD 0/216, **GUARD 25/216**, NOFETCH 0/216, BLOCKNET 0/216 |

Arms: GUARD is the guard as shipped. NOFETCH is the guard with everything intact (browser-level connection,
`Target.setAutoAttach{waitForDebuggerOnStart, flatten}`, per-session attach and resume) except that `Fetch.enable` is
never sent. CONN opens the guard's connection and sends nothing. BLOCKNET sends `Network.enable` and
`Network.setBlockedURLs` per session instead of `Fetch.enable`. NOGUARD is no guard.

Fisher exact, two-sided: GUARD vs NOGUARD 25/216 vs 0/216 p=2.9e-08; NOFETCH vs GUARD p=2.9e-08; BLOCKNET vs GUARD
p=2.9e-08; NOFETCH and BLOCKNET vs NOGUARD p=1.0. **Pooled with Linux round 8** (GUARD 4/150, NOFETCH 0/150, BLOCKNET
0/150): GUARD 29/366 vs 0/366 for NOFETCH and for BLOCKNET, p=2.1e-09. Round 9's GUARD failures were spread over 16 of
its 18 chunks (0-3 each), so it is not a chunk or ordering artefact.

What the arms say:

- **`Fetch.enable` is the cause.** Everything the guard does except sending it (NOFETCH) is clean: 0/366 pooled.
- **A bare second connection is not** (CONN 0/150, Linux only; not re-tested on macOS).
- **The extension-service-worker crash is a symptom.** `Inspector.targetCrashed` on `chrome-extension://…/service_worker.js`
  appears only with `Fetch.enable` (GUARD 33/216 macOS runs, 0 in the other three arms), and 8 of the 25 bad GUARD runs
  had no crash. Round 7's arm that skipped `Fetch.enable` on extension targets alone kept the full failure rate, so the
  harmful interception is the one on the page, not on the extension.

## 3. Why `Network.setBlockedURLs`, and a probe bug that nearly hid it

Round 8's BLOCKNET probe reported the rotation URL "not blocked" (0/75). Round 9 logged the full event params and found
that was the diagnostic's bug: a blocked request through `Network.setBlockedURLs` fails with
`blockedReason: "inspector"` and an EMPTY `errorText`, where the `Fetch` path reported
`net::ERR_BLOCKED_BY_CLIENT.Inspector`; the probe matched on `errorText` and dropped the empty field. With the right
check, 108/108 BLOCKNET probes were blocked (`loadingFailed` 9 microseconds after `requestWillBeSent`), the same as
GUARD's 108/108. BLOCKNET therefore both blocks the rotation URL and has 0/366 bad runs.

## 4. The change

`_configure` now sends, per attached session and before resuming it: `Network.enable`, `Network.setBlockedURLs`, then
the re-auto-attach, then `Runtime.runIfWaitingForDebugger` (in `finally`, as before, so a refused command never leaves a
target frozen). The URL patterns are the same `*://host/path*` set as before, so both `http` and `https` are covered, the
`RotateCookies` / `rotateCookies` / `RotateBoundCookies` / `rotateBoundCookies` spellings and both hosts. The
`Fetch.requestPaused` handler is deleted; the guard no longer reacts to any event (it drops the `Network.*` stream
that `Network.enable` makes Chrome send).

`Network.enable` is sent with `maxTotalBufferSize`, `maxResourceBufferSize` and `maxPostDataSize` set to 0 so the
agent buffers no response bodies for a connection that never asks for one. **That setting was not in the measured BLOCKNET
arm** (which sent `Network.enable` bare); it is a deliberate deviation chosen to keep the cost of the fix down, and the
three armed macOS runs requested in the PR are what confirm it.

The refused-command accounting that existed for `Fetch.enable` (a clone left unguarded on a target is a warning, shape
only) now covers `Network.enable` and `Network.setBlockedURLs` (`_BLOCKING_METHODS`), and a target that detaches first
still does not count as a refusal (F-957).

### CDP semantics argued, not measured

- **Redirects.** `Network.setBlockedURLs` is checked on every request the target's network stack issues for a page,
  each hop of a redirect included. CI measured it: a `fetch` redirected by a 302 into a pattern URL is blocked on a
  clone and served on the master, on Linux, macOS and Windows.
- **Navigations: NOT blocked, a behaviour change.** My first version of this argument said a subframe navigation is
  "a request in the frame's own target" and so blocked. The first CI run on this PR disproved it on all three OSes
  (`assert 'served' == 'blocked'`): an `<iframe src="/RotateCookiesPage?origin=x">` inside a clone loads (a
  same-origin document) rather than showing Chrome's blocked-page error. `Network.setBlockedURLs` fails the requests
  a page makes; it does not fail a document navigation, which the `Fetch.enable` interception it replaces did. The
  e2e now pins that outcome (`"served"`) with its reason, so a later Chrome that starts blocking it will show up
  rather than pass silently. Whether that matters depends on whether a rotation can complete from the
  `RotateCookiesPage` document alone; the rotation Google performs from it is the `POST /RotateCookies` the page then
  makes, which IS blocked (the page's own request, in the iframe's attached session), but that is the published
  behaviour of that page, not something measured here (no Google request was made). See section 7.
- **Dedicated workers.** They are separate targets; the guard's `Target.setAutoAttach` already reaches them and
  configures each before it runs. The e2e asserts a worker-issued fetch is blocked on a clone.
- **What is given up.** The guard only ever failed matching requests with `BlockedByClient` (the removed
  `Fetch.requestPaused` handler); it never fulfilled or modified one. The one thing the interception did that
  `Network.setBlockedURLs` does not is failing a frame's document navigation to a pattern URL (above).

None of the three has been run against real Chrome by this change. They are CI-only assertions in
`test_e2e_google_rotation_guard.py`.

## 5. Consumers of the error text

Census of `ERR_BLOCKED_BY_CLIENT` / `BlockedByClient` / `errorText` / `blockedReason` over `src/` and `tests/` (grep
of the claim itself, with `rtk proxy grep`): no code matches on the blocked outcome. The hits are
`navigation_milestone.py:401`, `tests/test_e2e_extra_headers.py:128` and `tests/test_extra_headers_cdp.py:182`, all
comments about a different feature (a `Referer` extra header that Chrome cancels), and the guard's own removed
`Fetch.failRequest{errorReason: "BlockedByClient"}`. The guard e2e asserts the block by what the fixture server saw
(master served, clone not), not by error text, so nothing needed to also accept `blockedReason == "inspector"`; the
hermetic tests that name the new shape use it only as documentation of the wire.

## 6. Tests and the mutation check

`tests/test_google_rotation_guard.py` (60 tests, hermetic; the expected wire messages are built from nodriver's own
`cdp.network` constructors, no bare `MagicMock`): one pin per URL form on the wire (13 positive, 8 negative), `Fetch.*`
is never sent, the order block, auto-attach, resume, a network event provokes no command, and the refused-command
accounting for both commands (still attached warns, detached first does not, an answered command does not, a refused
auto-attach is not a block refusal).

**Method (out of tree, labelled):** source mutation, not runtime rebinding. A `git write-tree` tree of the staged change
was exported with `git archive` into a scratch directory, the editable-install finder was stripped and
`PYTHONDONTWRITEBYTECODE=1` set, and `run_export.py` asserts the imported module path is the export. One mutation at a
time was applied to `google_rotation_guard.py`, the file test run, and the source restored (baseline 60 passed before
and after). All 16 mutants fail RED:

| mutant | result |
|---|---|
| M1 also send `Fetch.enable` | 2 failed |
| M2 no `Network.setBlockedURLs` | 29 failed |
| M3 drop the youtube host | 7 failed |
| M3b drop the lower-case path spellings | 6 failed |
| M3c drop the trailing `*` | 7 failed |
| M3d `https` only | 3 failed |
| M4 wrong param key (`urlPatterns`) | 23 failed |
| M5 `Network.enable` without the buffer limits | 1 failed |
| M6a / M6b accounting covers only one of the two commands | 3 failed each |
| M6c accounting also counts auto-attach | 1 failed |
| M6d never warn | 4 failed |
| M7a resume before the block | 1 failed |
| M7b auto-attach before the block | 1 failed |
| M8 react to a `Network.requestWillBeSent` | 1 failed (see below) |
| M9 detach keeps the block ids | 4 failed |
| M10 never resume | 4 failed |

M8 first stayed GREEN: the "event provokes no command" pin only compared the log length before and after a sleep, so a
reaction logged before the snapshot was not seen. The pin now asserts the set of every method sent in the whole
conversation, and M8 then went RED.

## 7. Open risks

1. **A document navigation to a pattern URL is no longer failed** (measured on CI, section 4). If a rotation can
   complete from the `RotateCookiesPage` document without the later `POST /RotateCookies`, the guard would not stop
   it. Resolving that needs a throwaway Google account (never the operator's), which this change does not touch.
   Redirect and dedicated-worker blocking passed on CI on all three OSes.
2. `Network.enable` makes Chrome stream every network event of every attached session to the guard's connection, where
   they are parsed and dropped. The cost is unmeasured.
3. The zero buffer limits (section 4) deviate from the measured BLOCKNET arm.
4. nodriver's `set_blocked_ur_ls(urls=…)` uses the `urls` field, which newer protocol versions deprecate in favour of
   `urlPatterns`; a Chrome that stopped honouring `urls` would be a refused command (a warning), not a silent one.
5. Whether `Network.setBlockedURLs` works without `Network.enable` was not tested; it would remove risk 2 if it did.
6. Confirmation that the flake is gone is statistical: the PR asks for three armed macOS CI runs (GUARD was 25/216,
   about 11.6% per run, so three runs of the journey are not proof, the 216-run diag is).
