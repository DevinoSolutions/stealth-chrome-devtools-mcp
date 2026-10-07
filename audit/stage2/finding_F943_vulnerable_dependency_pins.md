# F-943 — the pinned dependency set carried 63 known advisories

**Severity:** Medium. Nothing here was shown to be exploited, but the package pins
`fastmcp`, `mcp`, `starlette`, `anyio`, `pillow`, `pydantic` and `python-dotenv`
EXACTLY, so every user install got the vulnerable versions and no resolver could
lift them.
**Files:** `pyproject.toml` (the exact pins), `uv.lock` (relocked),
`embedded/tool_failure.py` (new leaf, §5), `embedded/logging_setup.py`,
`expected_events.py` (`TOOL_MANAGER_LOGGER`, §5), and the comments and docs that
cite a measured version or line: `embedded/backend_env.py`,
`embedded/backend_client.py`, `embedded/payload_log_sites.py`, `observability.py`,
`NAVMAP.md`, `DESIGN.md`, and the test docstrings that name `mcp` 1.27.1 or
`fastmcp` 2.11.2.

---

## 1. Why GitHub showed nothing

The task was "clear the open security alerts (Dependabot, code scanning, secret
scanning)". All three were empty for a reason that is not "the repo is clean":

- the **dependency graph** parses 0 manifests for this repo, so Dependabot had
  nothing to match advisories against;
- **code scanning** is not configured;
- **secret scanning** was disabled. It was enabled during this task (push
  protection was not) and has raised 0 alerts.

So `pip-audit` over the locked set is the source of truth here, not the alert list.

## 2. Before

`uv export --frozen --no-hashes --all-extras --no-emit-project` at `cb678b9`, then
`pip-audit -r <file> --no-deps --disable-pip`: **63 unique advisories in 12
packages**: anyio 4.13.0, cryptography 48.0.0, fastmcp 2.11.2, joserfc 1.6.5,
mcp 1.27.1, pillow 11.3.0 (23 of them), pyjwt 2.12.1, python-dotenv 1.1.1,
python-multipart 0.0.28, starlette 1.0.0, urllib3 2.7.0, werkzeug 3.1.8.

## 3. Fix

Bump the exact pins to the lowest release that clears every fixable advisory, and
relock so the transitive ones follow:

| package | was | now |
|---|---|---|
| fastmcp | 2.11.2 | 2.14.7 |
| mcp | 1.27.1 | 1.28.1 |
| starlette | 1.0.0 | 1.3.1 |
| anyio | 4.13.0 | 4.14.2 |
| pillow | 11.3.0 | 12.3.0 |
| python-dotenv | 1.1.1 | 1.2.4 |
| pydantic | 2.11.7 | 2.12.5 (mcp 1.28.1 requires >=2.12 on Python 3.14) |
| cryptography, joserfc, pyjwt, python-multipart, urllib3 | — | relocked: 50.0.2, 1.7.5, 2.15.1, 0.0.32, 2.8.0 |

werkzeug left the lock entirely.

## 4. After

The same export and audit on the bumped lock: **3 unique advisories in 2 packages**,
none reachable from this package:

- **fastmcp PYSEC-2026-2475 (CVE-2025-64340)**, fixed only in 3.2.0: command
  injection through a server NAME passed to `fastmcp install claude-code|gemini-cli`
  on Windows. Nothing here runs `fastmcp install`; the README installs with
  `uv tool install` and a JSON `args` block.
- **fastmcp PYSEC-2026-2476 (CVE-2026-27124)**, fixed only in 3.2.0: the
  `OAuthProxy` does not check consent on the GitHub callback. The backend
  constructs `FastMCP(...)` with no `auth=` and no auth provider, so there is no
  `OAuthProxy` to reach (it also binds 127.0.0.1 unless `--host` says otherwise).
- **diskcache PYSEC-2026-2447 (CVE-2025-69872)**, no fixed release: pickle on read
  from a cache directory an attacker can write to. NEW with this bump: fastmcp 2.14
  pulls `py-key-value-aio[disk]`. The only `DiskStore` construction in the installed
  tree is `fastmcp/server/auth/oauth_proxy.py`, the same unused auth path.

Clearing the last two fastmcp advisories means fastmcp 3.x, a major migration
(the tool registration, settings and error surfaces this repo pins all moved). It
is named here as the follow-up and deliberately not folded into a security bump.

## 5. What the bump changed underneath

- **`fastmcp` error path.** `ToolManager.call_tool` (`tools/tool_manager.py`:153-170)
  now re-raises `FastMCPError` and pydantic `ValidationError` WITHOUT logging; only
  other exceptions are logged as `Error calling tool …` and wrapped. Our
  `tool_errors.ToolError` is a plain `Exception`, so our tools' failures still reach
  the client as `Error calling tool '<name>': <message>`, unchanged on the wire.
- **A `ValidationError` OUR code raised would have vanished. Fixed here.** The
  unlogged re-raise covers ours as well as a caller's bad argument, and `mcp`'s
  low-level handler turns it into an error RESULT with no logging either. So the
  unknown-`STEALTH_MCP_*`-key `Settings()` crash, which fails every spawn, would
  have reached neither the log file nor Sentry. `with_correlation_id`, the one
  wrapper every tool runs under, now records a failure through
  `tool_failure.record`, whose `_report_own_validation_error` logs it where and
  as 2.11.2 did: on `fastmcp.tools.tool_manager`, `Error calling tool '<name>'`, with the
  exception attached. Only a `ValidationError` is reported, because everything
  else fastmcp still logs itself and a second record would double-count. A
  caller's bad argument is still not reported: fastmcp validates arguments
  before it calls the wrapper, so the wrapper never sees it.
  `tool_failure.py` is a new leaf because this helper would have taken
  `logging_setup.py` to 1036 LOC against its 1000 budget, and caps only ratchet
  down. F-835's debug-ring record (`_record_tool_failure`) moved with it, so the
  leaf is "what a failed call leaves behind" and `logging_setup.py` is 963.
- **`fastmcp` logger names.** fastmcp 2.14's `get_logger` drops the `FastMCP.`
  prefix, so the tool logger is `fastmcp.tools.tool_manager`, not
  `FastMCP.fastmcp.tools.tool_manager`. `expected_events.TOOL_MANAGER_LOGGER`
  follows, and a test now reads the name off the installed library rather than
  restating it. The `caller-input` expected-noise class is now dormant: the
  caller's half never arrives, and the frame test keeps ours from matching it.
  It is kept, with a note, for the fastmcp 3 migration to decide. The `fastmcp`
  root that shields the tool-argument DEBUG line (`server/server.py`:1537) is now
  `fastmcp` itself, with its own level and `propagate=False`, and the
  payload-floor pin asserts that.
- **Tool schemas lose their `title` keys.** fastmcp 2.14 prunes the
  per-parameter `title` that pydantic derives from a parameter's name
  ("Block Resources" for `block_resources`). This is the one change a client can
  see in `tools/list`. Both schema goldens were regenerated in this PR, with
  the justification written beside them:
  - the HARD `tests/goldens/tool_surface.json` lost exactly its 333 titles;
  - the per-section snapshot in `test_correlation_id.py` lost its 45.

  Compared with string-valued `title` keys removed, the 94 tools showed zero
  differences on either side. Names, types, defaults, `required`, descriptions
  and output schemas are otherwise unchanged.
- **`fastmcp` settings.** `env_prefix` is `FASTMCP_` only (2.11.2 also read
  `FASTMCP_SERVER_`), plus the nested `FASTMCP_DOCKET_` and `FASTMCP_EXPERIMENTAL_`
  blocks. `backend_env`'s single prefix still covers all of them, and fastmcp still
  crashes at import on `FASTMCP_PORT=""`, which is why that scrub exists.
- **Line numbers re-measured.** In `mcp/server/lowlevel/server.py`, "Received
  exception from stream" moved 707 → 713 and "Received message" moved 676 → 682.
  `mcp/server/sse.py` moved 193 → 203, the streamable-HTTP GET handler moved
  659-728 → 660-752, fastmcp's tool-argument DEBUG line moved 672 → 1537 and
  `tool.py`'s `validate_python` moved 295 → 381. An AST census of `mcp`'s logging
  calls is 208 (was 206), still 11 on root. The two new calls are named-logger
  WARNINGs that carry a session ID and no payload. Every cited line in `src/`,
  `tests/`, `NAVMAP.md` and `DESIGN.md` was checked against the installed tree.
  Accounts of past measurements, and the dated audit records, were left as
  they were.
- **Unchanged and re-checked:** `mcp/client/streamable_http.py` is byte-identical;
  `streamable_http`'s three payload sites keep their lines; pydantic 2.12.5 keeps
  the 50-character `input_value=` echo cap; fastmcp still hard-codes
  `timeout_graceful_shutdown: 0`.

## 6. Evidence

- pip-audit before: 63 unique / 12 packages. After: 3 unique / 2 packages, listed
  in §4.
- **The new pins in `tests/test_tool_failure_visibility.py`** run a real
  `fastmcp.FastMCP` app through `fastmcp.Client` in memory, with every tool
  wrapped by `with_correlation_id`. They record what lands on the tool logger:
  - a `ValidationError` our body raised gives exactly ONE record, with the
    exception attached;
  - a `RuntimeError` gives exactly one, not two;
  - a caller's `{"count": "abc"}` gives none;
  - the logger name is read off `fastmcp.tools.tool_manager.logger`.
- **RED first.** On the bumped lock, before the fix, the `ValidationError` pin
  failed with zero records. With the fix it passes.
- **Two mutants, both killed.** The mutations were made by runtime rebinding
  from a `-p` plugin, not by editing the tree:
  - turning `_report_own_validation_error` into a no-op fails the
    `ValidationError` pin;
  - dropping its `isinstance` filter, so that it reports every failure, fails
    the once-not-twice pin.
- **Shape tests now read the library.**
  - `test_backend_env_scrub` collects the prefixes from every `BaseSettings` in
    `fastmcp.settings`.
  - `test_payload_log_floor` accepts the `if settings.log_enabled:` guard
    around `configure_logging`, asserts that it defaults to True, and checks
    the `fastmcp` root's level and `propagate`.
- The bump-sensitive subset and the full non-integration lane on the bumped lock
  (results in the PR).
