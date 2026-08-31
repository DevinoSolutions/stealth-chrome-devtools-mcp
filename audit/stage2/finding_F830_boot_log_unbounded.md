# F-830 — `backend-boot.log` grows without bound (794 MB observed)

**Severity:** HIGH (operability / disk). **Status:** FIXED on
`fix/F830-F840-log-hygiene`.
**Files:** `src/stealth_chrome_devtools_mcp/embedded/logging_setup.py`,
`src/stealth_chrome_devtools_mcp/embedded/singleton.py`,
`src/stealth_chrome_devtools_mcp/embedded/server.py`.
**Tests:** `tests/test_log_hygiene.py`.

---

## 1. Observation

`~/.stealth-mcp/logs/backend-boot.log` on the reporting machine measured
**794 MB**: roughly 13 million lines, overwhelmingly uvicorn HTTP access-log
records. F-820's write-up had already caught the same file at **733 MB** two
days earlier (`audit/stage2/finding_F820_watchdog_condemns_busy_backend.md`
§2), so the growth rate is on the order of tens of megabytes a day on a single
developer machine.

## 2. Why it grows, and why nothing was catching it

Three properties compound:

1. **The content is a per-request firehose.** The backend serves FastMCP over
   HTTP, and uvicorn logs one INFO access line per request by default. The
   client-side watchdog probes the backend roughly every 2 s *per live stdio
   proxy*; with several Claude Code sessions attached that is a steady stream
   of `POST /mcp/ 200` lines carrying no information a reader wants. Our own
   tool-call logging (`logging_setup.with_correlation_id` →
   `stealth.backend`) is a different logger writing to a different, already
   size-rotated file — it was never the problem.

2. **The destination is one file for every boot, forever.**
   `singleton._start_server_process` opens `<logdir>/backend-boot.log` in
   append mode and hands the descriptor to `Popen` as the child's
   stdout/stderr. That design is correct and deliberate: an import-time crash
   in `embedded/server.py` dies before `configure_logging` can install
   anything, so a raw stream redirect is the *only* thing that can capture it,
   and the file must be named before the child exists (hence a single shared
   name rather than `backend-<pid>.log`).

3. **Nothing could rotate it.** Because it is a raw fd inherited by the
   running child, the child pins it for its whole life. An in-process
   `RotatingFileHandler` never sees these bytes at all. An *external* rotation
   fails on Windows (sharing violation) or silently succeeds-but-does-nothing
   on POSIX (the child keeps writing to the renamed inode). `prune_old_logs`
   globs `*.log*` and so does visit the file, but only ever considers deleting
   it by *age* — and its mtime is refreshed continuously by the running
   backend, so it is permanently "new" and permanently exempt.

The result is a monotonically growing file that no existing mechanism in the
codebase was structurally capable of bounding.

## 3. Fix — two independent halves

### 3a. Do not emit the spam (`logging_setup.backend_uvicorn_config`)

`embedded/server.py`'s HTTP branch already passed a `uvicorn_config` dict for
F-809's graceful-shutdown timeout. That composition moved into
`logging_setup.backend_uvicorn_config()` — the observability spine is the
right home for "how the backend's HTTP server logs" — and now also carries
`access_log=False`. `server.py` swaps one argument for a call
(`uvicorn_config=backend_uvicorn_config()`) and *sheds* the constant and its
comment block, so it lands **net −4 LOC** against its 3411 cap; the human
ruling forbidding a cap raise is honoured with room to spare.

Nothing diagnostic is lost. Every call that matters is logged by
`with_correlation_id` against `stealth.backend`, with a correlation id, an
argument-free start/end pair and a duration — strictly more useful than
`POST /mcp/ 200` and written to the size-rotated per-pid file.

### 3b. Roll what is already there (`logging_setup.roll_boot_log`)

`roll_boot_log(log_dir)` rotates `backend-boot.log` past
`_BOOT_LOG_MAX_BYTES` and returns the (now free) path.
`singleton._start_server_process` calls it on the line where it used to build
that path, so the call is free in LOC terms and the function nets **0** at
999/1000.

The *placement* is the whole point, and the code says so: the launcher sits
between two backends and holds no descriptor on the file, which makes it the
one and only safe hand-off point at which the rename can happen (see §2.3).

Constants and rationale:

| Constant | Value | Why |
|---|---|---|
| `_BOOT_LOG_MAX_BYTES` | 16 MB | Comfortably holds many boots' worth of tracebacks — the file's actual purpose — while staying small enough to open in an editor and to `tail` over a slow link. Well under the 794 MB pathology and above any plausible single-crash traceback. |
| `_BOOT_LOG_BACKUPS` | 2 | Keeps `.1`/`.2`; caps the whole boot-log family at ~48 MB. A rotation that accumulated `.N` forever would only rename F-830, so the bound is asserted by test. |

Rotation is best-effort and never raises: a boot log that cannot be rolled
must not block a spawn, matching plan_M3 §7's fail-open discipline and the
caller's own `OSError` → `DEVNULL` fallback right below it.

### What this fix does *not* do

It does not touch the existing 794 MB file. Rotation happens at the next
backend spawn on that machine, at which point the current file exceeds the
threshold and is moved to `backend-boot.log.1`; the operator can delete that
sibling at leisure (nothing holds it open). Deleting the live file out from
under a *running* backend is exactly the unsafe operation §2.3 describes.

## 4. Tests (`tests/test_log_hygiene.py`)

* `TestRollBootLog` — oversize file is rolled aside and the live name is free;
  a small file is untouched; a missing file and a missing directory are not
  errors; six consecutive oversize boots leave a *bounded* `.N` tail with `.1`
  newest; the shipped threshold and backup count are within sane bands.
* `TestLauncherRollsBeforeOpeningTheFd` — `_start_server_process` (real
  function, mocked `Popen`, `tmp_path` log dir) rolls the oversize boot log
  and hands the child a zero-byte file.
* `TestBackendUvicornConfig` — `access_log` is `False`, the F-809 timeout still
  rides in the same dict, and an AST check pins that `server.py`'s
  `mcp.run(transport="http", …)` really passes the composed config.

All filesystem work is on `tmp_path`; the size threshold is monkeypatched down
to 64 bytes so no test writes megabytes.

## 5. Residual risk

* `access_log=False` is a FastMCP/uvicorn keyword. If a future FastMCP stops
  forwarding `uvicorn_config` keys verbatim the setting silently reverts to
  noisy — the AST test catches removal of the call but cannot catch upstream
  ignoring the key. Half 3b bounds the damage regardless.
* Boot-log *rolls* (`backend-boot.log.1`) are still swept by
  `prune_old_logs`'s age rule, and deliberately are **not** covered by F-840's
  post-mortem exemption; a test pins that, so the exemption cannot silently
  re-open this finding.
