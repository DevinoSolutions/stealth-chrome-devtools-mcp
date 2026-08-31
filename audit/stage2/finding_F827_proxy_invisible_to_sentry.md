# F-827 — the component that decides to disconnect was the one component that reported nothing

**Status: RESOLVED** on `fix/F827-proxy-sentry` (stacked on
`fix/F838-proxy-self-heal`, which is stacked on `fix/F829-fingerprint-transient-oserror`).
**Severity: HIGH (observability)** — no user-visible misbehaviour of its own,
but it is the reason four separate disconnect defects (F-820, F-829, F-838,
F-839) had to be reconstructed by hand from local log files on one machine,
and the reason nobody else's disconnects were ever seen at all.

---

## Symptom

The Sentry project receives events from the **backend** and from the **ops
CLI**, and nothing else. Every proxy-side decision — condemning a backend as
dead, evicting one for a source change, healing onto a replacement, giving up
and tearing the session down — happens in a process that has never called
`sentry_init()`.

The 2026-08-30 disconnect waves are the case in point. Four waves of proxies
tore down inside the same second (05:12 ×7, 05:19 ×30, 05:28 ×3, 05:34 ×10)
while the shared backend kept serving. The only reason that is known at all is
that the maintainer read `~/.stealth-mcp/logs/proxy-*.log` on the affected
machine. For the third-party installs on PyPI (memory: at least four other
machines on 2.0.3), the same waves produced exactly zero telemetry.

## Mechanism

`sentry_init()` had exactly two callers:

| Role | Call site |
|---|---|
| backend | `embedded/server.py::main()` (line ~3397), after the argument parse |
| ops CLI | `cli.py::main()` (line 635), first statement |

The stdio proxy has neither. The thin entrypoint,
`src/stealth_chrome_devtools_mcp/server.py::main()`, reads:

```python
if known.transport == "stdio" and not known.standalone:
    port = ensure_server_running(port=known.singleton_port)
    if port is not None:
        run_stdio_proxy(port)
        return                    # <-- the proxy's whole life is above this line

runpy.run_path(str(EMBEDDED_DIR / "server.py"), run_name="__main__")
```

The `return` is the defect. Everything a proxy process ever does happens in the
stdio branch, and the branch returns before the `runpy` load that would have
brought `embedded/server.py`'s own `sentry_init()` into the process. So the
proxy — the component that owns the liveness watchdog, the eviction decision
and (since F-838) the heal loop — was structurally unreachable by error
reporting.

Two consequences worth naming separately:

1. **Nothing was reported, not even crashes.** `LoggingIntegration` ships every
   ERROR record as an event once Sentry is initialized. In the proxy it never
   is, so even `_logger.exception("backend cold start failed")` and
   `_logger.error("backend did not become ready within %.0fs")` went to a local
   file and no further.
2. **The F-829 misattribution shipped nothing either.** F-829 was deliberately
   sequenced *before* this fix so that once proxies do report, they report the
   corrected verdict — an unreadable digest is "unknown", not "you edited the
   source". Without that ordering the first thing this fix would have delivered
   is a stream of false eviction reports.

## Fix

### 1. Where `sentry_init()` runs — the stdio branch, and only the stdio branch

The init is wired **inside** the `if known.transport == "stdio" and not
known.standalone:` block, not at the top of `main()`.

That placement is structural, not stylistic. `main()` falls through to
`runpy.run_path(embedded/server.py, run_name="__main__")`, and that module runs
its own `sentry_init()`. An init at the top of `main()` would therefore run
**twice in one process** on the backend path. This repository has already paid
for a runpy double-load once — the tool registry accumulated 282 = 3 × 94
registrations when a module-global mutation was moved into a shared module — so
"it would only be a duplicate init, Sentry is idempotent-ish" is not a defence
we get to use here. Putting the call where the backend path cannot reach it
makes the double-init impossible rather than merely unlikely, and
`tests/test_proxy_sentry_reporting.py::TestInitPlacement` pins both halves:
the stdio branch wires it, and `--transport http` / `--standalone` (called
twice, to simulate the double load) wire nothing.

### 2. Why it runs off-thread — the measured init cost

Measured in this worktree's venv, three cold processes:

```
import=0.211s  init=2.321s  total=2.532s
import=0.196s  init=1.582s  total=1.778s
import=0.194s  init=1.354s  total=1.548s
```

**~1.5–2.5 s**, dominated by `sentry_sdk.init` (integration discovery), not by
the import. That is not a cost the proxy can pay inline:

* The proxy exists to answer the client's `initialize` **locally and
  instantly** — that is the entire point of `ensure_server_running` being
  non-blocking (it returns a port immediately and spawns the cold start on a
  daemon thread) and of `_proxy_streams` answering `initialize` itself. Two
  seconds added ahead of that lands on **every session start**, against Claude
  Code's 30 s connection timeout, on a machine that routinely runs fleets of
  sessions.
* There is no "after the fast handshake, before the blocking serve loop" seam
  to use instead. The handshake is answered *inside* the serve loop
  (`run_stdio_proxy` → `_bridge` → `_proxy_streams` → `pump_client`), so any
  placement in `main()` is necessarily before it.

So the init is started on a **daemon thread** and the caller does not wait for
it — the same pattern, for the same reason, that `ensure_server_running`
already uses for the backend cold start. The thread is daemonic so a wedged
init can never hold the proxy process open.

**The residual, stated:** a failure inside the proxy's first ~2 s is not
reported. That window covers backend discovery, which is the least interesting
part of the proxy's life, and the alternative was a 2 s regression on every
session start. `sentry_sdk.capture_message` on an uninitialized client is a
silent no-op, not a queue, so an event in that window is dropped rather than
delayed.

### 3. The logging bootstrap moves with it

`configure_logging("proxy")` was called by `run_stdio_proxy`, i.e. *after*
`ensure_server_running` had already started the cold-start daemon thread — so
that thread's log records (including `backend cold start failed` and `source
fingerprint unreadable`) raced the handler install. It is now called in the
same branch, before `ensure_server_running`. It is idempotent (`if
logger.handlers: return`), so `run_stdio_proxy`'s call is unchanged and simply
returns early.

### 4. The four transitions

`observability.capture_lifecycle(message, *, level, **fields)` is the one new
surface, and it lives in `observability.py` because that is THE Sentry home —
the module that owns the DSN, `before_send`, and the PII scrubber. `**fields`
become one `proxy` **context** on the event, which means they pass through
`_scrub_event` exactly like everything else; nothing here decides what an event
looks like.

| # | Event | Site | Level | Fields |
|---|---|---|---|---|
| a | `proxy: backend condemned` | `proxy_selfheal._one_generation.monitor`, the instant `watch(port)` returns | warning | `port`, `strike_seconds` |
| b | `proxy: backend healed` | `proxy_selfheal.drive`, after a successful `heal_backend` | info | `old_port`, `new_port`, `generation`, `consecutive_heals` |
| c | `proxy: teardown after failed heal` | `proxy_selfheal.drive`, both give-up exits | **error** | `port`, `generation`, `reason` (`unhealable` / `flapping`), `consecutive_heals` |
| d | `proxy: backend evicted (source changed)` | `singleton._start_backend_holding_lock`, beside its INFO log | warning | `port` |

Notes on the choices:

* **(a) `strike_seconds`** is measured from the moment the generation was
  *armed* (the backend genuinely answered an `initialize`) to the moment the
  watchdog returned. `_watch_backend_liveness` returns for exactly one reason —
  F-820's three strikes *plus* the `_same_identity_backend_ready` confirmation
  — so its return **is** the condemnation, and the elapsed time is what dates
  the verdict against the backend's own logs.
* **(b) is INFO on purpose.** It is the denominator: "how often does stealth
  still disconnect" is meaningless without "how often did healing work".
* **(c) is ONE event with a `reason`, not two.** `drive` gives up on two axes
  (this recovery failed; recoveries keep failing) but the user experiences one
  thing — the MCP server went away. After F-838 this is the *only* remaining
  path that ends a session, which is why it is the only ERROR of the four.
* **(d)** rides beside the existing INFO log and reports only a *genuine* edit
  — version matches, digests differ. F-829's unreadable digest is `None` and
  `backend_registry.fingerprint_mismatch` reads it as unknown, so it produces
  no event; a test pins that explicitly.
* **F-829's `source fingerprint unreadable` WARNING** was *not* given its own
  capture. It did not need one: with `sentry_init()` finally running in the
  proxy, `LoggingIntegration` records every WARNING as a breadcrumb, so it now
  travels attached to whatever event follows it — which is exactly the context
  in which it is worth reading. Giving it a standalone event would have cost
  `singleton.py` a line it does not have (see below) for strictly less
  information.

Every capture **piggybacks**: no log line changed level or text. In particular
`tests/test_watchdog_busy_vs_dead.py:185` still pins
`WARNING … confirmed unusable`, and `test_singleton_version_aware.py:598` still
pins exactly one `backend stale (source changed), evicting`.

### 5. Never raising, twice over

* `capture_lifecycle` wraps *everything* — including the `get_settings()` read
  and the `sentry_sdk` import — in one `try`, returns `False` on any failure,
  and logs at DEBUG (an ERROR raised while shipping an event is how a reporting
  loop starts). Reporting disabled via `STEALTH_MCP_NO_ERROR_REPORTING` is an
  immediate `False` that never reaches the SDK.
* `proxy_selfheal._report` guards it *again*, locally. The promise is not the
  point: a module whose whole job is to be the backstop after everything else
  failed cannot have its control flow depend on a telemetry call behaving. A
  test pins that a `capture_lifecycle` which raises leaves `drive`'s heal loop
  byte-identical in behaviour.

## Files and budgets

| File | Before | After | Cap |
|---|---|---|---|
| `src/stealth_chrome_devtools_mcp/server.py` | 61 | 101 | 1000 |
| `src/stealth_chrome_devtools_mcp/observability.py` | 424 | 464 | 1000 |
| `src/stealth_chrome_devtools_mcp/embedded/proxy_selfheal.py` | 282 | 359 | 1000 |
| `src/stealth_chrome_devtools_mcp/embedded/singleton.py` | 999 | **999** | 1000 |

`singleton.py` had one line of headroom and is left with the same one. The
import of `capture_lifecycle` costs a line; it is paid for by the eviction
site, whose wrapped three-line conditional collapses to two once the digest is
read into a local first, and by re-flowing (not deleting) the M2-3 rationale
comment from seven filled-to-68-columns lines to six filled-to-88. The capture
call itself is therefore free. No cap was raised and nothing was padded.

The one semantic delta at that site: `_source_fingerprint()` is now evaluated
before the version comparison rather than after it, so it runs even when the
recorded version differs. That path is the one that is about to pay for a 30 s
backend cold start; a ~1 MB hash is noise, and the comment says so.

## Wiring choice: direct import, not an injected callable

`proxy_selfheal` and `singleton` both reach `observability` by direct import.
The precedent is already in the tree: `embedded/server.py:63` is
`from stealth_chrome_devtools_mcp.observability import sentry_init`. The banned
edge under `embedded/` is importing **`server`** (double registration under
runpy), not importing a top-level leaf; `observability` imports only
`settings`, so there is no cycle and no new dependency on the proxy's import
path (`logging_setup` already pulls `settings`).

`proxy_selfheal` imports it *lazily and by module attribute* inside `_report`,
which keeps the leaf importable without the reporting stack and gives tests one
spy seam. `singleton` binds it at import because that file cannot afford a
lazy-import line; tests patch `singleton.capture_lifecycle` directly, which is
the idiom that file's existing tests already use everywhere.

An injected reporting callable was considered and rejected: the transitions are
reported from three functions across two modules, so injection would mean
threading a parameter through `drive`, `_one_generation` and
`_start_backend_holding_lock`'s thread target — more surface, more lines
(`singleton` has none), and a second way to answer a question `observability`
already answers.

## Tests

`tests/test_proxy_sentry_reporting.py` — 16 nodes, hermetic (no DSN, no
network, no real backend, no real `~/.stealth-mcp`, no Chrome; the Sentry seam
is always a spy):

* `TestInitPlacement` — the bootstrap calls `sentry_init` exactly once; it does
  not block (a deliberately stuck init leaves the caller free, on a daemon
  thread); the stdio branch's ordering is `configure_logging` → reporting →
  `ensure_server_running` → `run_stdio_proxy` and never reaches `runpy`;
  `--transport http` and `--standalone`, run twice each, init nothing.
* `TestCondemnationIsReported`, `TestHealOutcomesAreReported`,
  `TestEvictionIsReported` — one capture per transition with the expected
  fields, plus the two negatives that matter: a generation that merely *ended*
  reports nothing, and an unreadable fingerprint is never reported as an
  eviction.
* `TestCaptureSeamContract` — disabled reporting never reaches the SDK; an SDK
  that throws yields `False` instead of raising; the fields arrive as one
  scrubbable `proxy` context; a raising capture seam leaves the heal loop's
  control flow untouched.

Regression lanes run green at HEAD: `test_proxy_selfheal`,
`test_proxy_backend_death`, `test_watchdog_busy_vs_dead`,
`test_watchdog_app_level`, `test_fingerprint_unreadable`,
`test_singleton_cold_start_patience`, `test_singleton_version_aware`,
`test_observability*`, `test_singleton_*`, `test_startup_herd`,
`test_server_entrypoint`, `test_cli*`, `test_no_silent_excepts`,
`test_check_suppression_owners`, `test_doc_claims`, `test_release_contract`
(604 nodes total).

## Residual risks

1. **The first ~2 s of a proxy are still unreported** (see §2). Deliberate.
2. **Event volume.** Four new message events, all on transitions that should be
   rare. The teardown (c) is the one that fires on a real incident; if the
   disconnect waves recur, expect one per affected proxy, which is the signal,
   not noise. `capture_lifecycle` respects `STEALTH_MCP_NO_ERROR_REPORTING`
   like everything else, and no new knob was added.
3. **PII.** The fields are integers and one fixed `reason` string. They still
   go through `_scrub_event`, so the general contract holds; there is no new
   path by which a path, a header or a credential can reach an event.
4. **`AsyncioIntegration` warns when no event loop is running.** It did so for
   the backend already (its `sentry_init()` also runs before `mcp.run()`), and
   it now does so for the proxy's init thread too. The warning goes to stderr,
   which is not the MCP protocol channel. Not a regression, but it is the one
   new line a user may notice on stderr.
