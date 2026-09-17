# F-886 — a cold start's eviction closes another session's browsers and bricks its session

**Severity**: HIGH — the two symptoms the operator actually reports ("my browsers
randomly closed", "the MCP server disconnected mid-session"), from one cause,
reproducible 7/7.

**Status**: FIXED on `fix/F886-eviction-kills-sibling-browsers`.

---

## 1. The mechanism

One stdio proxy per Claude Code session bridges to one shared HTTP backend per
display context. On a cold start, `singleton._start_backend_holding_lock` asks
the reuse gate whether the backend recorded on its port is ours; when the answer
is no it calls `_clear_stale_backend` → `_terminate_backend` and spawns its own.

The only thing that decision consulted was IDENTITY.
`backend_registry.fingerprint_mismatch` answers *"these two source digests
differ"* — never *"mine is newer"*, and never *"that one is busy"*. It is
symmetric: there is no version order and no record-time order in the comparison.
So two clients running the same released version off different source bytes each
read the other as stale, and whichever started second killed the one that was
already working.

That is not a hypothetical fleet. It is a `uvx @latest` session beside a
`uv tool` install, an editable checkout beside either, or (as previously
recorded in this repo's memory) an unreleased `main` installed into the uv tool
while `uvx` sessions kept running — which produced five eviction waves and
killed every browser on the machine.

**What the eviction then destroyed, and by which of two possible mechanisms.**
The finding's author could not isolate whether the browser died *with* its
terminated backend or was reaped afterwards by the replacement. It is the
second, and the measurement is decisive:

| event | t (s, from the first proxy's start) |
|---|---|
| incumbent backend A recorded | 7.54 |
| **backend A terminated** | **9.11** |
| replacement B recorded | 11.20 |
| variant proxy finished its handshake | 13.38 |
| **browser of session A died** | **13.54** |

The browser outlived its own backend by **4.43 seconds**, and backend B's log
carries the line that killed it:

```
backend-123528.log  INFO  stealth.backend: process_cleanup.recovery: Killed 1 orphaned browser processes
backend-123528.log  INFO  stealth.backend: startup job 'orphans' finished 0.6s after serving began
proxy-120632.log    INFO  stealth.proxy:   backend stale (source changed), evicting
```

`browser_pid_registry` stamps the OWNING BACKEND's pid on every entry, and
`process_cleanup._recover_orphaned_processes` reaps an entry whose owner is not
a live backend of ours. An owner we ourselves terminated two seconds ago is
indistinguishable from one that crashed last week. **So the fix has to be at the
eviction; a change to the reaper would be treating the symptom.** (On POSIX a
`SIGTERM`'d backend would additionally run its own `atexit` teardown, so the
browser dies there too — by a different route, from the same decision.)

**Why the surviving session never learned.** Both of the proxy's death witnesses
are PORT-scoped, and the replacement binds the SAME port:
`backend_watchdog.watch_liveness` probes the port and gets an answer, so the
three strikes needed to open a confirmation never accumulate (observed maximum:
"probe failed 2/3", then reset); the streamable-HTTP bridge is per-request, so
nothing "breaks" for `proxy_selfheal._confirm_bridge_verdict`. What actually
died is the MCP SESSION, and nothing watches that. `proxy_selfheal`'s entire
recovery is therefore unreachable on this path, and the client's later calls
answered `{"code": 32600, "message": "Session terminated"}` (5 of 6 runs; 1 of 6
kept answering over a backend that no longer had its browser).

**A second route into the same kill, and why a caller cannot opt out.** The
mixed-install fleet is not the only way to arrive at `_clear_stale_backend` with
someone else's live backend on the port. `singleton._select_backend_port`
PREFERS the port recorded for our display context over the `--singleton-port`
the caller asked for — `target = preferred if recorded is None else recorded` —
so ANY process whose source fingerprint differs from the recorded one resolves
to the live backend's port, whatever port it named on its command line. The
team lead reproduced this from a different direction while I was working:
`tests/test_singleton_fast_handshake.py::TestEntrypointExitsOnDisconnect`
spawns the real stdio entrypoint without redirecting `HOME`, resolves to the
developer's live backend on 52554 (several sessions attached) and evicts it on
fingerprint mismatch — a test worktree's fingerprint being, by construction,
not the installed one. Same `_clear_stale_backend` → `_terminate_backend` call,
same consequence for everybody's browsers, reached by a plain test run rather
than a mixed install. The invariant the operator actually needs is therefore
not "mixed installs must converge" but **"a process that did not start this
backend must not be able to terminate it while another session is live on
it"**, and that is what the chosen rule states. Under it that test's spawn now
steps aside onto a fresh port (the live backend has browsers, so it is
protected) instead of killing it — and a test that cannot reach the real record
at all is still the right fix for the test, which is F-885's.

**The schema's part in it.** `server.json` v2 held ONE entry per display
context. Two clients on one desktop competed for that one slot, so even without
a kill, recording either one erased the other — and an erased proxy can no
longer confirm its own backend through `_same_identity_backend_ready`. The slot
is why "step aside instead of killing" was not expressible before this change.

---

## 2. Evidence

All runs: Windows 11, Python 3.13, the `S5` fleet in
`tests/test_e2e_lifecycle_resilience.py` — two proxies driving the same
installed console launcher, one of them on a `PYTHONPATH` copy of the package
differing by one byte, against one isolated `HOME`, 60 s.

### Before (2.1.8)

| run | waves | timeline `(s, backend pid)` | loser's browser | loser's session at end |
|---|---|---|---|---|
| 1-6 (finding) | 1 | `[(4.9,A),(11.0,B)]`, `[(4.8,A),(10.5,B)]`, `[(4.8,A),(10.5,B)]`, `[(5.1,A),(10.7,B)]`, `[(4.8,A),(10.6,B)]`, `[(7.6,A),(17.1,B)]` | dead 6/6 | `Session terminated` 5/6 |
| 7 (this branch, baseline) | 1 | `[(7.4,A),(16.3,B)]` | dead | `Session terminated` |
| 8 (instrumented, 0.25 s poll) | 1 | see §1 table | dead at +4.43 s, by orphan recovery | n/a |

Incident lines in every before-run: exactly one,
`backend stale (source changed), evicting`.

### After

| run | waves | stepped aside | browsers alive | sessions served | incidents |
|---|---|---|---|---|---|
| 1 | 0 | 1 | both | both `ok` | none |

`S5` wall time 61.4 s; the two nodes plus the vocabulary pin, 99.3 s.
Proxy WARNING-or-worse output for the whole run: empty.

---

## 3. The rule chosen, and the two rejected

### Chosen — **a backend that is still SERVING is never evicted**

A backend is PROTECTED when all four hold: there is a recorded entry; its
identity is NOT ours (so we would not adopt it); its recorded pid is a live
backend of ours; and it still owns at least one live browser. A protected
backend is never terminated and never bound over — the arriving client spawns
its own on a fresh port and `server.json` (now schema v3, a list) records both.

Home: `embedded/backend_eviction.py`, which owns the DECISION and the ACT
together, because the defect is precisely a kill with no rule in front of it.
Two consumers, both thin bindings in `singleton`: `_select_backend_port` (the
bind site, which normally steps around the port before the lock is even taken)
and `_clear_stale_backend` (the kill site, which is the moment that does the
damage and therefore keeps its own guard).

Why this rule and not a weaker or stronger one:

* It is not conditioned on version order, so it also answers the case the team
  lead flagged: a genuinely NEWER client arriving does not get to close an older
  session's browsers. It gets its own backend.
* It cannot ping-pong. Neither side can kill the other, and each has a record
  entry its own next session will adopt.
* "Owns a live browser", not "is alive". Protecting every live backend would
  accumulate one backend per source edit forever with nothing in the tree
  allowed to reclaim one; and browsers are the only state a backend holds that
  reconnecting cannot rebuild. It also keeps the issue-#14 upgrade flow exactly
  as it was in the case that flow actually happens in — edit source with no
  browser open, the idle stale backend is still evicted and replaced.
* Live browsers are a LOCAL question. `browser_pids.json` answers it with no
  round trip and no new endpoint, which is what lets the rule be asked from the
  cold-start path, where there is no backend to ask.

### Rejected — **session-scoped liveness alone**

Feed an invalid-session answer for the proxy's own `mcp-session-id` into
`watch_liveness`, so the existing heal path fires. Rejected AS THE FIX, though
not as an idea (§6 keeps it):

* it does not save a single browser. The heal re-bridges the session onto the
  replacement, whose `BrowserManager` never knew the old instances; the browsers
  are already reaped by then.
* On its own it makes things WORSE. The healing proxy calls
  `ensure_server_running`, whose reuse gate rejects the replacement's identity,
  so it cold-starts — and under the pre-F-886 rule that cold start evicts the
  replacement. That is the unbounded eviction war S5a currently does not see,
  manufactured deliberately. The chosen rule bounds it only where the winner
  holds browsers; where NEITHER side does — which is exactly the case this idea
  would serve — the war is still constructible. §6 residual 1 carries the
  counter-example and the two design decisions that would close it.

### Rejected — **ordered eviction (version, then a record-time stamp)**

Let only a strictly-newer client evict. Rejected:

* it does not address the user's complaint. With two clients at the SAME version
  and different source bytes — the measured case, and the one in this repo's
  memory — "newer" has to fall through to the record-time stamp, and the
  arriving client is always later than the running one. The rule then permits
  exactly the eviction that is doing the harm.
* Where it does bite (a truly newer version arriving), it still kills the older
  session's browsers. The team lead named this explicitly, and it is right: an
  ordering rule decides WHO wins, and the complaint is that anybody wins.
* It needs a monotonic stamp in the record and a comparison whose ties are
  arbitrary, to buy a property the chosen rule gets for free.

---

## 4. Blast radius

**Behaviour that changes.**

* A cold start that finds a stranger's serving backend on its preferred port now
  spawns beside it instead of terminating it — whichever route brought it there:
  a mixed install, an editable checkout, a test worktree that reached the real
  record (the second witness in §1), or a `restart`/cold start from a process
  whose `--singleton-port` was overridden by the recorded-port preference. One extra backend process (~70 MB)
  per extra source identity on a machine, for as long as its session's browsers
  live. Logged at INFO, once, naming the port and the browser count.
* `server.json` is written as schema v3. v2 and v1 still READ, with v2's key
  still authoritative for the display context, so an upgrading user's existing
  record is adopted rather than evicted.
* **A DOWNGRADE to ≤2.1.8 DOES kill.** An earlier draft of this section claimed
  it "cold-starts once — the same cost as any first run, not a kill". That is
  false, and the review measured it against `origin/main`'s own
  `backend_registry` loaded beside this branch's:

  ```
  v3 on disk:  {"schema": 3, "backends": [{"port": 19222, ...}]}
  OLD backends_in:      []
  OLD port_for_context: None
  OLD first_backend:    None
  OLD backend_on_port:  None
  ```

  An empty record is not a harmless cold start. 2.1.8's `_clear_stale_backend`
  asks `_same_identity_backend_ready(port)`, which reads `backend_on_port` →
  `None` → not reusable → `_terminate_backend(port)`, and that resolves its
  victim from the **socket** (`_backend_pid_on_port`), never from the record. So
  an un-upgraded proxy terminates the v3 backend on its port, and being
  un-upgraded it carries no protection rule.

  In fairness the same kill would have happened under v2 — the versions differ,
  so the identity gate fails either way — so this is an accuracy correction, not
  a new kill class. It matters for the release all the same: this machine runs a
  `uv tool` install beside `uvx @latest` sessions, so until **every** install on
  a machine is ≥ 2.1.9 the old side wins every race while the new side politely
  steps aside, and the operator sees the fix "not working". Said in the CHANGELOG
  and in RUNBOOK's "Two backends on one desktop".

  Not taken: the reviewer's cheap option of also emitting the v1 flat keys beside
  `schema: 3` so an old reader sees one entry. It would make a ≤2.1.8 client
  apply its own identity gate instead of reading an empty record, but it writes a
  second representation of the same fact into one file — the defect this codebase
  calls "a second way" — and it buys nothing for the case that actually hurts,
  where the old client's identity gate fails anyway.
* `record_backend` supersedes by port (unchanged) and by (display context,
  identity) instead of by display context alone. Our own respawn on a new port
  still replaces our own entry, so nothing accumulates.
* `backend_registry.forget_backend` is DELETED. `forget_entries` was already the
  entry-precise sibling, and "drop this whole display context" is no longer a
  statement anyone means. `singleton.stop_backend` now forgets the entry it
  stopped; a sibling identity on the same desktop survives a `stop`.

**Behaviour that deliberately does NOT change.**

* `stop` and `restart` are ungated. `backend_eviction.terminate` applies no rule
  of its own: the operator asking is the authority the rule otherwise supplies,
  and `stop`'s docstring has always said terminating every live browser session
  is the verb's purpose. `restart` in particular still lands on and replaces its
  own backend — **but that did not follow from the protection being
  identity-gated, and the first version of this fix broke it.** The review
  measured `restart` on the very desktop this fix creates:

  ```
  port_for_context      -> 40123        (the stranger's)
  own_or_first_port     -> 40123
  bindable_port called  -> {'target': 40123, 'force_new': True}
  restart_backend()     -> ('responsive', None)
  calls                 -> [('kill', 99999), ('start', 99999)]
  OUR backend on 40200 terminated? False
  ```

  `port_for_context` answered the context's FIRST entry, which on a two-identity
  desktop is the stranger's by construction — it was recorded first, which is
  exactly why we stepped aside from it. Selection then stepped aside from it
  again and `restart` spawned a THIRD backend on an OS-assigned port, leaving our
  own (possibly wedged) backend running; supersede-by-identity then dropped its
  record entry, so `doctor`, `status` and `cleanup` stopped seeing it too and no
  verb reached it at all.

  The gate was never enough: selection has to CHOOSE on identity, not merely test
  on it. `port_for_context` and `own_or_first_port` now take a required `matches`
  predicate and answer OUR entry on that context when one exists, falling back to
  the first entry otherwise; both callers hand down
  `singleton._identity_matches`, the same predicate the protection rule is given,
  so the restart seed and the port selection cannot ask different questions.
  Pinned by `TestRestartOnATwoIdentityDesktop`, whose third node is the control:
  a context holding only a stranger must still step aside.
* A WEDGED backend of our own is still evicted. Protecting it would turn a wedge
  into a permanent one.
* The F-820 strike policy, the F-843 bridge witness, `proxy_selfheal`'s single
  heal path and `backend_liveness`'s deadness rule are untouched.

**LOC.** `singleton.py` 985 → 999 against a hard 1000 cap, across two rounds and
with no cap raised in either. First the eviction cluster (`_backend_pid_on_port`,
`_terminate_backend`, `_clear_stale_backend`) moved into `backend_eviction.py`,
leaving four thin bindings because the suite patches those names and a binding
resolves its collaborators at call time. Then the review's F1 and F4 fixes put it
over again, so the build-identity pair (`_server_version`, `_source_fingerprint`)
moved into `build_identity.py` — deliberately paired with
`backend_registry.fingerprint_mismatch`, their one reader, so the producer and the
reader of a source digest sit one import apart. The remainder was paid back by
collapsing prose that restated what is written elsewhere: the
"wrapper, not a re-export" argument appeared four times in this one file and is
now stated once, at `_probe_port`, with pointers from the other three.
`backend_eviction.py` is 321 LOC and `build_identity.py` 99, both far under the
1000 default.

---

## 5. Tests

**Hermetic — `tests/test_backend_eviction.py` (31 nodes, 1.2 s).** Every probe
injected; nothing touches the process table, a socket or the real state dir.

* `TestOwnedBrowsers` — only this owner's still-running browsers; a non-int
  owner, an absent record and a legacy unowned entry each protect nothing.
* `TestProtected` — the four conditions, each the only thing that flips the
  verdict; in particular *our own identity is never protected* (the wedge) and
  *a live backend with no live browser is evictable* (the #14 upgrade flow).
* `TestClearStale` / `TestSteppingAside` — the three answers, the WARNING line
  with its spared count, and the INFO line that explains a second backend.
* `TestTerminateHonoursTheInjectedResolver` — the leaf asks the CALLER's
  `pid_on_port`, so a patch of `singleton._backend_pid_on_port` still decides
  what dies.
* `TestBindSite` / `TestKillSite` — a serving stranger forces a fresh port and
  spawns nothing; an idle stranger keeps the port and is evicted; our own
  serving backend keeps the port so `restart` can replace it.
* `TestStopForgetsOneEntry` — a sibling identity survives a `stop`.
* `TestRestartOnATwoIdentityDesktop` (review F1) — on a context holding the
  stranger FIRST and ours second, selection targets ours and `restart`
  terminates and respawns on ours, not on a third OS-assigned port. Its third
  node is the control: a context holding only a stranger must still step aside,
  and it was green before and after the fix.
* `TestTheReportNamesTheDecisionTaken` (review F4) — a refusal reports
  "eviction refused (still serving)" with the spared count and writes no
  "evicting" line anywhere; a real eviction still reports byte-identically to
  what F-827 always shipped.

**Record — `tests/test_backend_registry.py::TestTwoIdentitiesOneContext` (9
nodes).** A foreign identity or a different version on the same context is kept;
our own identity respawned on a new port supersedes; same-port supersede is
unchanged; F-829's unreadable digest supersedes like our own; the file is a v3
list; a v2 keyed record still reads with the key authoritative; a v3 entry with
no context reads UNVERIFIED. Plus `test_forget_backend_is_gone`, and in
`TestPortForContext` / `TestOwnOrFirstPort` the identity preference and its
first-entry fallback (review F1) — every pre-existing pin there now passes
`matches=_nothing_is_ours`, the single-build machine, so it still asserts exactly
what it always asserted.

**Status — `tests/test_cli_status_wedged.py` (review F5).** A sibling under our
OWN display context is still an "other", named `context:port`, and the entry
being reported on is not listed as one.

**Real fleet — `tests/test_e2e_lifecycle_resilience.py`.** `S5a` flipped from
"converges to at most one wave" to **zero waves, zero lifecycle incidents, at
least one step-aside line, both sessions `ok`**. `S5b`'s `xfail(strict)` is
removed and it passes. The step-aside string joined the vocabulary pin, so a
reworded log line turns that node red rather than making S5a vacuous.

**Harness.** `release_gate_harness._backend_pid_from_state` reads v3; the new
`_backend_pids_from_state` is what `gate_workspace` teardown uses, so a
workspace that ran two identities terminates both.

---

## 6. Residuals

1. **A session with NO browser open is still bricked silently, and
   session-scoped liveness is STILL not safe to add on its own.** The rule
   protects browsers, not sessions. A proxy whose backend is evicted while it
   holds no browser still gets `Session terminated` on its next call, with no
   condemnation, heal or teardown in its log.

   The SIGNAL is clean and worth recording, because it was the uncertain half.
   `mcp.client.streamable_http` synthesises `{"code": 32600, "message":
   "Session terminated"}` at exactly ONE site
   (`_send_session_terminated_error`), reached only from `status_code == 404`
   on a POST carrying a session id — i.e. only from "the backend does not know
   my session". A backend TOOL cannot produce it (tool failures come back as
   `result.isError`, not a JSON-RPC error), and the bridge already sees the
   frame in `_proxy_streams.from_backend`. Pinning that literal against the
   SDK's source is the same discipline `LIFECYCLE_INCIDENTS` already uses for
   the product's own log lines.

   **What is NOT resolved is the consequence, and I was wrong about it in an
   earlier draft of this document.** I claimed F-886 made this safe. It does
   not, and the counter-example is constructible without measurement. Session A
   holds no browser, so it is not protected and B binds A's port;
   `record_backend` supersedes by port, so A's entry is gone. A notices through
   the new signal and heals via `ensure_server_running`. `_find_running_server`
   finds only B's entry, whose identity differs, so it does not adopt; the cold
   start selects B's port; and B — which has just started and may hold no
   browser either — is therefore NOT protected. A evicts B. B's proxy, equally
   browser-less, notices and evicts back. That is the unbounded eviction war,
   one level down from where it was.

   So this residual needs ONE of two further things before the signal can be
   wired up, and both are real design decisions rather than plumbing: widen the
   protection from "owns a live browser" to "has a live client session" (which
   needs the backend to answer that question, so it is no longer the LOCAL read
   that lets the rule be asked from a cold-start path), or make the heal ADOPT
   the replacement instead of cold-starting against it (which means relaxing
   the identity gate for a heal, i.e. running a session against source it did
   not start — the thing issue #14 exists to prevent). Neither is a follow-up
   to do quietly, and neither removes any browser loss, which is why this
   branch stops here and says so.

2. **A stepped-around backend outlives its usefulness.** When the last proxy of
   a stranded identity exits, its backend keeps running with no client. Nothing
   reclaims it: `backend_liveness.forget_dead` only forgets DEAD records, and
   `cleanup --apply` only writes the record. Bounded by the number of distinct
   source identities a machine runs, and an operator can `stop`/`kill-orphans`,
   but there is no automatic reclamation. A refcount, or an idle-with-no-browsers
   self-exit, is the shape — both need measurement first.

3. **A recycled browser pid over-protects.** `owned_browsers` asks liveness of
   the browser pid alone, not of the `(pid, create_time)` pair
   `browser_pid_registry.is_reapable` compares. A pid recycled onto an unrelated
   process spares a backend that could have been evicted: one extra backend on
   one extra port. The opposite mistake closes a browser, so the cheap check is
   deliberate — but the expensive one is right there and could be threaded
   through if the extra backend ever matters.

4. **`record_backend` under an unreadable digest may supersede a stranger's
   entry — and that costs the PROTECTION, not just discoverability.** The
   supersede comparison goes through `fingerprint_mismatch`, so F-829's "unknown
   is not a contradiction" applies: if OUR digest is unreadable at record time,
   an entry we would otherwise treat as a stranger's is dropped. Measured by the
   review:

   ```
   before: [{"port": 1, "source_fingerprint": "AAA"}]
   after recording port 2 with a None digest:
           [{"port": 2, "source_fingerprint": null}]
   ```

   An earlier draft of this residual said only *discoverability* is lost. That
   understates it. `backend_eviction.protected` opens with
   `if entry is None ... return []`, so an erased entry removes that backend's
   protection **entirely** — the record IS the protection, and the next cold
   start on its port is free to kill it. The backend keeps serving until then,
   and re-records itself on its next write, so the window is real but narrow.

   Still chosen over the alternative (never supersede under an unknown digest),
   which would accumulate an entry per respawn on exactly the OneDrive-sync
   machine F-829 was written for. The reachable trigger is the same transient
   read failure that finding documents, which is why the retry in
   `build_identity.source_fingerprint` exists at all.

5. **Not measured on POSIX.** Every number here is Windows. The POSIX kill path
   additionally runs the terminated backend's own `atexit` teardown, so before
   the fix a browser would die there *with* its backend rather than 4 s later by
   orphan recovery — a different route to the same loss, and the fix cuts both
   off at the same decision. The CI gate's Linux cells run `S5` and will say so.

6. **Two identities on one desktop now means two backends, permanently, while
   both have browsers.** That is the intended trade and it is not free: twice
   the Chrome-supervision memory and two entries in `doctor`. The `status`
   summary half of this is FIXED rather than deferred (review F5): `cli`'s
   `others` line compared entries on display context alone, so on exactly this
   machine neither entry was ever an "other" and the summary read "this is all
   there is". It compares on the (context, port) pair now and labels each other
   `context:port`, because two entries under one context are otherwise
   indistinguishable. What remains is the memory, which is the trade itself.

7. **The second route into the same kill is mitigated, not closed.**
   `_select_backend_port` still reads `target = preferred if recorded is None
   else recorded`, so a process that names a port on its command line still
   resolves to the recorded one, and a developer's live backend **with no
   browser open** is still evictable by any test run that reaches the real
   `HOME`. The protection narrows the blast radius; it does not remove the
   route. What actually contains it today is F-885's `HOME` isolation and the
   harness port deny-list in `tests/release_gate_harness.py`, neither of which
   is this branch's work. Recorded here so the brief's "fixed and pinned" is not
   read as satisfied.
