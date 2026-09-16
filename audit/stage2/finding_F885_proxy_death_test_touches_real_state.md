# F-885 — proxy/backend-death tests ran against the developer's live `~/.stealth-mcp` record

**Status:** fixed on `fix/F885-proxy-death-test-isolation`
**Opened by:** team-lead task assignment, 2026-09-16 — a routine test-hygiene audit that surfaced a live-environment hazard mid-sweep.
**Source at:** `main` = `6ca0ae9`
**Severity:** the originally-named test is LOW (log pollution only, measured). The sweep found a SECOND instance of the same shape that is CRITICAL: on a developer machine with a live shared backend, one unpatched test could evict (kill) it and close every browser it was serving.

---

## 1. The hazard

`backend_registry.STATE_DIR = Path.home() / ".stealth-mcp"` (`backend_registry.py:74`)
is computed once, at import time, from `Path.home()`. There is no
`STEALTH_MCP_*` env override for it — by design, per that module's docstring,
this is the one path every recorded-backend consumer shares. A test that spawns
a real proxy or backend process, or drives one in-process, without redirecting
`Path.home()`/`singleton.STATE_DIR` therefore reads and can write the same
`server.json` and log files a real, currently-running Claude Code session's
backend uses.

`tests/test_proxy_backend_death.py::TestProxyExitsOnBackendDeath::
test_proxy_returns_when_backend_dies_and_cannot_be_healed` did exactly this: it
runs `_proxy_streams` IN-PROCESS in the pytest process (no HOME redirect there
helps — the proxy code runs in THIS process) and spawns a real HTTP backend
subprocess with only `STEALTH_MCP_BROWSER_SESSION_ROOT` overridden.

## 2. The before/after measurement

Real state, captured before any change, before running anything:

```
server.json mtime_ns: 1789576057326196500
server.json sha256:   99a741fee8207329cff4637abbb95d7a26f426287fd5e71840f63fa21acc6f10
server.json content:  {"schema": 2, "backends": {"win-session-1": {"port": 52554,
                       "version": "2.1.8", "pid": 5812,
                       "source_fingerprint": "9e78957...", "display_context": "win-session-1"}}}
newest ~/.stealth-mcp/logs/backend-*.log: backend-5812.log, backend-boot.log, backend-143924.log, ...
```

Ran the UNPATCHED test (`pytest tests/test_proxy_backend_death.py::
TestProxyExitsOnBackendDeath -v -s`): **1 passed**. Re-measured immediately
after:

```
server.json mtime_ns: 1789576057326196500   (unchanged — this run never collided
server.json sha256:   99a741fee8...          with the real backend's port, so the
                                              in-process read of _same_identity_
                                              backend_ready never matched an entry)
newest logs: backend-5812.log, backend-154036.log, backend-154036-fault.log, ...
                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                        NEW — written by this run's subprocess-spawned backend
                        straight into the developer's real ~/.stealth-mcp/logs
```

That is the concrete "moved" evidence: `server.json` happened not to change
this run (the ephemeral port never matched the recorded port, and
`heal_backend` was mocked out before `ensure_server_running`/eviction could be
reached), but the backend subprocess unconditionally wrote its boot and fault
logs into the real directory. That the record itself survived unscathed here
is a matter of port-number luck, not a property the test enforces — which is
the defect.

After the fix (`singleton.STATE_DIR`/`PORT_FILE`/`SERVER_STATE_FILE`
monkeypatched to a `tmp_path` for the in-process proxy, `_isolated_env`-built
HOME/USERPROFILE/LOCALAPPDATA/APPDATA/log dir for the subprocess), ran the same
test twice more:

```
server.json mtime_ns: 1789576057326196500   (identical)
server.json sha256:   99a741fee8...          (identical)
newest logs: backend-5812.log, backend-154036.log, backend-154036-fault.log, ...
                                              (identical — no new entries; the
                                               fixed run's logs landed in the
                                               isolated tmp_path instead)
```

Test still passes, and now runs in ~3s instead of ~12s (no real cold-start env
resolution against the developer's actual settings).

## 3. The fix

`tests/test_proxy_backend_death.py`:
- `_proxy_streams` runs in-process; its death-confirmation step
  (`confirm_alive=_same_identity_backend_ready`, wired in `singleton._proxy_streams`)
  reads `singleton.SERVER_STATE_FILE` regardless of whether `heal_backend` is
  mocked (F-843's discriminator runs on every death signal, not only on a
  successful heal). Isolated with the same three `monkeypatch.setattr` calls
  `test_singleton_version_aware.py`'s `isolated_state` fixture uses — the
  established idiom, not a new one.
- The subprocess-spawned backend is a separate process; a monkeypatch in the
  test process cannot reach it. Isolated with `release_gate_harness._isolated_env`
  (already the one mechanism `gate_workspace` and several other e2e modules
  use for exactly this), redirecting HOME/USERPROFILE/LOCALAPPDATA/APPDATA plus
  the session/log/clone dirs into a throwaway workspace under `tmp_path`.

## 4. The sweep

Searched `tests/` for the same shape: anything that spawns a real backend/proxy
process, drives one in-process, or calls a CLI verb, and checked whether it
redirects `Path.home()`/`singleton.STATE_DIR` before doing so.

**Already isolated correctly (no change needed):**
- `test_wire_semantics.py`, `test_startup_herd.py`, `test_soak_stability.py`,
  `test_e2e_transport.py`, `test_e2e_transport_cookies.py`, `test_doc_examples.py`
  — all route through `release_gate_harness.gate_workspace` /
  `run_release_gate_journey` / `_isolated_env`, which already redirect HOME.
- `test_singleton_stop_restart.py`,
  `test_singleton_port_fallback.py`, `test_backend_spawn_no_redirector.py`,
  `test_find_running_server_app_probe.py`, `test_fingerprint_unreadable.py`,
  `test_probe_backend_status.py`, `test_proxy_sentry_reporting.py`,
  `test_singleton_backend_logging.py` — all use the `isolated_state` idiom
  (in-process only, no subprocess spawn in the affected tests).
- **CORRECTED (F-885b):** `test_singleton_version_aware.py` was wrongly
  cleared by this row. Most of the file is `isolated_state`, in-process only —
  but `TestStaleBackendEvictionEndToEnd::
  test_clear_stale_backend_terminates_real_backend` spawns a REAL
  `--transport http` backend subprocess with `env = dict(os.environ)` plus
  only `STEALTH_MCP_BROWSER_SESSION_ROOT` set — byte-for-byte the same
  unisolated-subprocess shape as §3/§4's `test_proxy_backend_death.py` fix,
  missed because the file-level `isolated_state` fixture (which this
  particular test does not even use) made the whole file look covered. See
  F-885b for the fix and the measured before/after.
- `test_singleton_cold_start_patience.py` — same idiom (not modified, per
  explicit instruction not to touch this file).
- `test_sigbreak_immunity.py` — has its own local `_isolated_env` (HOME +
  USERPROFILE) for its subprocess.
- `test_clean_shutdown_noise.py` — already sets `env["HOME"]` before spawning.
- `test_backend_escapes_client_job.py` — the helper subprocess it drives
  patches `backend_registry.STATE_DIR` to a tmp dir as the first thing it does
  (already documented in its own module docstring: "Everything the spawn would
  write to `~/.stealth-mcp` is diverted inside the helper").
- `test_backend_launch.py::test_the_real_state_dir_leaves_room` deliberately
  points `backend_registry.STATE_DIR` at the real home, but only to measure a
  **string length** (`_scheduler_plan` composes a command and returns; it
  performs no I/O against that path) — not a hazard.
- `test_proxy_selfheal.py::TestTheHerdSerializesOnTheColdStartLock` patches
  `STATE_DIR`/`LOCK_FILE` and fakes `_start_server_process` — no real spawn.
- `test_cli.py` — `stop`/`restart`/`kill-orphans` tests all patch
  `singleton.stop_backend` / `singleton.restart_backend` /
  `singleton._probe_backend_status` / the orphan reaper directly; none reaches
  real I/O.
- `test_packaging.py`, `test_release_evidence.py`,
  `test_chrome_cold_start_probe.py`, `test_doc_claims.py`,
  `test_resolve_chrome_freeze.py`, `test_display_context.py` — subprocess/mock
  usage unrelated to the backend registry (`uv build`, spawning a throwaway
  `pytest` module, `tasklist`, static introspection, or a pure stand-in
  double).
- **CORRECTED (F-885b):** `test_tool_registry.py` was wrongly cleared by this
  row on the theory that `--list-sections` "exits before serving." It does —
  but `embedded/server.py`'s `__main__` calls
  `bootstrap_backend_process_logging()` as its FIRST statement, six lines
  before the `--list-sections` branch is even reached, so the process still
  writes a `backend process starting` line and a `-fault.log` and still runs
  `prune_old_logs` against the real `~/.stealth-mcp/logs/` before exiting.
  `test_list_sections_printed_total_matches_registry` ran with NO `env=`
  override at all. See F-885b for the fix and the measured before/after.

**Fixed in this change (same shape as the original finding):**
- `tests/test_proxy_backend_death.py::TestProxyExitsOnBackendDeath::
  test_proxy_returns_when_backend_dies_and_cannot_be_healed` — see §3.
- `tests/test_singleton_fast_handshake.py::TestFastHandshakeEndToEnd::
  test_initialize_local_then_tools_list_forwarded` — spawns a `--transport
  http` backend with only `STEALTH_MCP_BROWSER_SESSION_ROOT` set; same
  log-pollution hazard. Fixed with the same `_isolated_env`-based env.
- `tests/test_singleton_fast_handshake.py::TestProxyExitsOnClientDisconnect::
  test_proxy_returns_when_client_stream_closes` — same shape, same fix.
- `tests/test_singleton_fast_handshake.py::TestEntrypointExitsOnDisconnect::
  test_stdio_entrypoint_exits_when_stdin_closes` — **the critical one.** This
  spawns the REAL stdio entrypoint (`python -m stealth_chrome_devtools_mcp
  --singleton-port <port>`, no `--transport` flag) with only
  `STEALTH_MCP_BROWSER_SESSION_ROOT`/`STEALTH_MCP_NO_AUTO_RECOVERY` set. That
  entrypoint calls `ensure_server_running()` for real. Traced the consequence
  through `singleton.py` without running the unpatched version: `_find_running_
  server()` fails to reuse the real live backend (its `source_fingerprint`
  differs from whatever checkout runs the test); `_select_backend_port()` then
  prefers "the port recorded for OUR OWN display context" over the
  `--singleton-port` value handed in — i.e. the REAL backend's port, confirmed
  live on this machine as `52554`; `_start_backend_holding_lock(52554)` finds a
  version match with a fingerprint mismatch, logs "backend stale (source
  changed), evicting", and calls `_clear_stale_backend(52554)` — killing the
  real, currently-serving backend — before cold-starting a replacement from the
  test's own checkout on that same port. The test's teardown only reaps the
  entrypoint's own child processes, so nothing restores the evicted backend
  afterward, and every live session's browsers on it go down when it dies.
  This was reported to the team lead immediately on discovery, before any
  attempt to run it, and was only ever executed AFTER the fix (isolated HOME),
  which was then verified not to touch the real record: `server.json`
  identical before/after, real backend pid `5812` still alive, port `52554`
  still accepting connections.

## 5. Residuals

- `tests/test_cli.py::TestStatusProfiles::test_status_runs` and
  `test_status_labels_are_glossary_conformant` call `cli.main(["status"])` /
  `cli.main(["doctor"])` unmocked, against the real `~/.stealth-mcp` record —
  a genuine read of production state, though read-only by construction:
  `doctor` never writes without `STEALTH_MCP_NO_AUTO_RECOVERY` unset, and
  `tests/conftest.py` sets that env var to `"1"` globally for the whole suite
  (`os.environ.setdefault(...)`). Left as-is: fixing every incidental read
  would be a larger, separately-scoped change, and this shape cannot write or
  evict under the suite's own global guard. Flagging it here so a future
  change to `doctor`'s auto-recovery gate re-examines this test.
- The `isolated_state` idiom itself is duplicated verbatim across ten test
  files (this finding did not invent the duplication, and consolidating it
  into a shared `conftest.py` fixture was out of scope for a targeted
  hygiene fix — noted as a candidate follow-up, not acted on here).

## 6. F-885b — an independent reviewer found the class is worse than "log pollution" (2026-09-16)

An independent review of this finding's §4 sweep found the two rows corrected
above, and pointed out that the consequence is not just extra files landing in
the wrong directory. Every `configure_logging()` call — reached by ANY
unisolated backend or proxy process this shape spawns, not only the two fixed
here — ends by calling `logging_setup.prune_old_logs()` against the resolved
log dir (`logging_setup.py:262`). Unisolated, that resolves to the real
`~/.stealth-mcp/logs/`, and `prune_old_logs` **unlinks** every `*.log*` file
there beyond the newest 50 or older than 7 days (`keep_days=7, keep_files=50`).

So an unisolated test subprocess does not just add noise: it applies the
product's own retention policy to the developer's real backend/proxy logs —
the post-mortem evidence for whatever the developer's live sessions were
doing — and deletes the oldest of it, silently, as a side effect of a test
run. Measured directly: running the two UNPATCHED tests fixed in F-885b
against the real `~/.stealth-mcp/logs/` (294 files beforehand) left the
directory at 295 files, not 294+4 — `prune_old_logs` had already reaped older
real entries to make room for the four new ones the unpatched subprocesses
wrote. Running the FIXED tests left the directory's file set byte-identical
(same 294 names, `server.json` sha256 unchanged) — confirmed by name-only diff
before/after, since concurrent legitimate backend/proxy activity from other
sessions on the same machine during this measurement window changed some
`mtime`s without changing the file set.

This raises the bar on the sweep in §4: "does it touch the real record" is not
the only question a future audit of this shape needs to ask — "does it run
ANY unisolated `configure_logging()` call at all" is the one that also
matters, because that alone costs the developer real log retention even when
the record itself is never touched.
