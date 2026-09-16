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
  record is adopted rather than evicted. A DOWNGRADE to ≤2.1.8 reads a v3 record
  as no backends and cold-starts once — the same cost as any first run, not a
  kill.
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
  own backend, because the protection is identity-gated and our own identity is
  never protected.
* A WEDGED backend of our own is still evicted. Protecting it would turn a wedge
  into a permanent one.
* The F-820 strike policy, the F-843 bridge witness, `proxy_selfheal`'s single
  heal path and `backend_liveness`'s deadness rule are untouched.

**LOC.** `singleton.py` 985 → 998 against a hard 1000 cap. The eviction cluster
(`_backend_pid_on_port`, `_terminate_backend`, `_clear_stale_backend`) moved to
the new leaf and four thin bindings stayed, because the suite patches those
names and a binding resolves its collaborators at call time. No cap was raised;
`backend_eviction.py` is 269 LOC under the 1000 default.

---

## 5. Tests

**Hermetic — `tests/test_backend_eviction.py` (26 nodes, 1.1 s).** Every probe
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

**Record — `tests/test_backend_registry.py::TestTwoIdentitiesOneContext` (9
nodes).** A foreign identity or a different version on the same context is kept;
our own identity respawned on a new port supersedes; same-port supersede is
unchanged; F-829's unreadable digest supersedes like our own; the file is a v3
list; a v2 keyed record still reads with the key authoritative; a v3 entry with
no context reads UNVERIFIED. Plus `test_forget_backend_is_gone`.

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
   entry.** The supersede comparison goes through `fingerprint_mismatch`, so
   F-829's "unknown is not a contradiction" applies: if OUR digest is unreadable
   at record time, an entry we would otherwise treat as a stranger's is dropped.
   The backend itself is untouched and keeps serving — only its discoverability
   is lost until it re-records. Chosen over the alternative (never supersede
   under an unknown digest), which would accumulate an entry per respawn on
   exactly the OneDrive-sync machine F-829 was written for.

5. **Not measured on POSIX.** Every number here is Windows. The POSIX kill path
   additionally runs the terminated backend's own `atexit` teardown, so before
   the fix a browser would die there *with* its backend rather than 4 s later by
   orphan recovery — a different route to the same loss, and the fix cuts both
   off at the same decision. The CI gate's Linux cells run `S5` and will say so.

6. **Two identities on one desktop now means two backends, permanently, while
   both have browsers.** That is the intended trade and it is not free: twice
   the Chrome-supervision memory, two entries in `doctor`, and a `status`
   summary that names only the first. `cli`'s `others` line already reports
   sibling display contexts; it compares on display context, so two entries
   under ONE context are not called out there. Worth a follow-up if operators
   find the status output confusing.
