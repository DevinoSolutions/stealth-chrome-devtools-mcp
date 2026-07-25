# F-773 — macOS: Chrome under the detached backend cannot complete ANY network navigation

**Status: OPEN, cause unknown, routed.** Not fixed, not reproduced outside CI.
**Found by:** plan_RELEASE W2's three-OS release gate (`audit/release-2-w2`, PR #44),
macOS/ARM64 cells, across 11 CI rounds 2026-07-24/25.
**Severity:** blocks any "works on macOS" claim for the navigation path. Whether it
also blocks *real users* is **unknown** — see §5, and do not state otherwise.

---

## 1. Symptom

Over the transport a real user gets (stdio proxy → detached HTTP backend), on
macOS/ARM64 **no URL can be loaded**. `navigate` exceeds the product's 30s internal
deadline, `_replace_main_tab` recovers, the tool ends at its 35s CDP deadline. The
target server never receives a request.

Linux/X64 and Windows/X64 run the identical journey green.

## 2. The probe that bounds it

One live instance, one session, three navigations in order (harness
`_cold_start_warmup` nav probe, recorded in the failure output):

| target | macOS/ARM64 | meaning |
|---|---|---|
| `about:blank` | **ok, 1.1s** | the tab, the CDP session, and `Page.navigate` all work |
| `http://127.0.0.1:1/` — nothing listening | **hang, 35.3s** | **the decisive row** |
| `http://127.0.0.1:<fixture>/index.html` | hang, 35.2s | matches the real journey |

**Why the refused-port row is decisive:** a TCP connection to a closed port on
loopback is answered by the OS with an immediate `ECONNREFUSED`. It *cannot* hang.
Since it hung, the request never reached the network stack at all. Corroborated
independently: the fixture server (which records every request line it serves)
logged **0 hits** on every failing run.

So the failure is *upstream of the network*, inside the browser, and it is specific
to navigations that require a network request — `about:blank`, which does not, is
unaffected while using the same CDP command.

## 3. Elimination table

Each row was excluded by an experiment, not by argument.

| Hypothesis | Excluded by |
|---|---|
| Chrome cold start / slow first launch | `about:blank` returns in 1.1s on the same instance; a warmup spawn+navigate runs first; retries with backoff made no difference |
| Dead tab / broken CDP session | `about:blank` uses the same `Page.navigate` on the same tab and succeeds; `Emulation.setDeviceMetricsOverride` and hook setup also succeed |
| Fixture server unreachable / wrong bind | a **closed port** hangs identically; fixture binds literal `127.0.0.1`, asserted |
| Catch-all Fetch interception pausing requests | RELEASE-FIX-C shipped; backend log confirms `leaving Fetch interception disabled`; hang **unchanged**. Hypothesis disproved — see §6 |
| Chrome profile / `HOME` location (macOS sandboxed `/private/var/folders` temp) | workspace moved to `RUNNER_TEMP` (`/Users/runner/work/_temp/gate-*`, confirmed in the log); hang **unchanged** |
| Chrome keychain block (`--use-mock-keychain`) | flag is stripped by the product's own stealth filter ("Playwright default") and never reached Chrome; test was void, flag removed |
| macOS or Chrome broken generally | on the **same runner, same job, same Chrome**, 53 in-process integration tests navigate the same fixture successfully |
| Chrome's network service failed to start | live process snapshot during the stall shows `--type=utility --utility-sub-type=network.mojom.NetworkService` running (two browsers, each with one) |
| Chrome crashed or logged an error | Chrome's own log (`--enable-logging --log-file=…`, flags verified not on the stealth blocklist) is **silent** for the entire navigation window |
| `MachPortRendezvousServer` child-process failures | present in Chrome's log but timestamped `081558.05`, ~0.5s before teardown capture and ~2 min AFTER the navigations; message is "parent died?" — teardown noise, not cause |

**What remains:** the detached backend process itself. Chrome launched *by the
detached backend* cannot complete a network navigation; Chrome launched *in-process*
on the same machine can. No instrument available from outside the process
distinguishes the cause further.

## 4. Reproduction

- **Reliable** on `macos-latest` (ARM64) GitHub-hosted runners, every run, 11/11.
- **Never** on Windows/X64 (local dev machine and CI) or Ubuntu/X64 CI.
- CI job: `release-gate / transport (macOS/ARM64)`; test
  `tests/test_e2e_transport.py::test_real_stdio_release_gate_journey`.
- Evidence lands in the failure text automatically: nav probe, fixture hit list,
  Chrome process snapshot, Chrome's log, and the backend's own logs.

## 5. What is NOT established (read before quoting this finding)

- **Whether real macOS users are affected.** Every data point comes from GitHub's
  hosted macOS runners. A hosted runner differs from a normal Mac in ways that
  plausibly matter here (no logged-in window server session, different bootstrap/
  launchd context for a detached process, CI sandboxing). This may be a
  runner-environment artifact.
- **The mechanism.** Excluded a great deal; proved nothing positive.
- Therefore: the gate must **not** claim macOS navigation works, and the release
  notes must **not** claim macOS is broken. Both would be unevidenced.

## 6. Relationship to RELEASE-FIX-C

FIX-C was written on the hypothesis that catch-all Fetch interception was pausing
every request. CI disproved it: with interception provably disabled the hang was
byte-identical. FIX-C is retained on its independent merits (a default spawn no
longer pauses every request on any platform; interception, previously armed only at
spawn, is now re-armed when a hook is created — with a pin that fails if that half
is removed). **It is not a macOS fix and must not be described as one.**

## 7. Next step that would actually settle it

Run the journey on a **real Mac** (not a hosted runner):

```bash
uv sync --extra test --extra dev
uv run python -m pytest tests/test_e2e_transport.py -m transport -v
```

- **Passes** → hosted-runner artifact. Keep the macOS transport cell excluded with
  this finding cited, and the product is fine for macOS users.
- **Fails** → a real product defect on macOS: the stdio proxy always spawns a
  detached backend, so navigation is broken for every macOS user. That would be
  release-blocking for a macOS claim and needs its own FIX plan.

Until one of those runs, F-773 stays open and macOS navigation stays unclaimed.
