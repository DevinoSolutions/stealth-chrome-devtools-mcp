# F-946 — fastmcp 3 clears the last three advisories, and the move must not change what a client sees

**Severity:** Medium, for the same reason as F-943. The package pins `fastmcp`
EXACTLY, so every install got 2.14.7 and the two advisories fixed only in 3.x. Neither
was reachable here (this package never runs `fastmcp install` and configures no auth
provider), and `diskcache` was loaded only by fastmcp's OAuth token store.
**Files:** `pyproject.toml` (`fastmcp==3.4.8`, new `fastmcp-slim==3.4.8`), `uv.lock`,
`embedded/tool_registry.py`, `embedded/session_hygiene.py`,
`embedded/logging_setup.py` (`PAYLOAD_WARNING_LOGGERS`), `expected_events.py`
(`TOOL_CALL_LOGGER`, `ARG_VALIDATION_FRAMES`), `embedded/tool_failure.py`,
`tools/dump_tool_surface.py`, `tests/goldens/tool_surface.json`, `tests/fakes.py`
(`live_tools`), the tests that drove fastmcp 2's API, and the comments that named a
fastmcp 2 module or line (`observability.py`, `payload_log_sites.py`, `serve_startup.py`,
`server.py`, `tool_sections/cdp_functions.py`, `NAVMAP.md`).

---

## 1. Cause

F-943 bumped every pin it could and left three advisories that only a major version
clears: fastmcp PYSEC-2026-2475 and PYSEC-2026-2476 (fixed in 3.2.0), and diskcache
PYSEC-2026-2447 (no fixed release; pulled in by fastmcp 2's `py-key-value-aio[disk]`).

fastmcp 3 is a different library under the same name. Six of its changes reach this
package, and four of them would have changed behaviour without failing a single
pre-existing test:

1. **Descriptions.** fastmcp 3 parses the docstring with griffe and keeps only its
   first text section as the tool description. Every `Returns:` section and every note
   after `Args:` vanished from `tools/list`. It also copies each `Args:` line into that
   parameter's schema `description`: 264 of them, repeating text the tool description
   carries, which grew the `tools/list` answer by 28%. The HARD golden caught both.
2. **The session sweep (F-862) stopped being built.** fastmcp 3 constructs
   `FastMCPStreamableHTTPSessionManager`, not the SDK's `StreamableHTTPSessionManager`.
   `install()` still bound the old name, so the backend ran the stock manager, and
   abandoned probe sessions would again pile up forever (the 6.7 GB of F-862). The pin
   meant to catch this stayed green: it checked for the substring
   `"StreamableHTTPSessionManager("`, which is inside
   `"FastMCPStreamableHTTPSessionManager("`. Only the end-to-end test failed.
3. **A new payload line.** `FastMCP.call_tool` now logs
   `"Invalid arguments for tool %r: %s"` at WARNING with pydantic's `errors()`, whose
   `input` is the caller's value WHOLE. Measured on 3.4.8: an `execute_script` call
   missing `instance_id` logs the entire `script`. fastmcp 2.14 logged nothing there.
   At WARNING it reaches fastmcp's stderr handler (for the backend, `backend-boot.log`)
   and becomes a Sentry breadcrumb. This is the shape F-911 withheld for
   `mcp/shared/session.py`, which only quoted a truncated echo.
4. **The tool-call logger moved** from `fastmcp.tools.tool_manager` to
   `fastmcp.server.server`, and the frame pair that marks fastmcp's own argument
   validation moved from `fastmcp.tools.tool run` to
   `fastmcp.tools.function_tool _execute`.
5. **API removals:** `mcp.get_tools()`, `Tool.enabled`, `ToolManager`; `mcp.tool(fn)`
   now returns the bare function, and `mcp.remove_tool` is deprecated.
6. **Packaging:** since 3.3 `fastmcp` is a metapackage, and the `fastmcp` module ships
   in `fastmcp-slim`. `check_pinned_imports` requires the importing distribution to be
   pinned.

## 2. Fix

- `section_tool` registers through `mcp.add_tool(Tool.from_function(wrapped,
  description=...))`, passing `inspect.getdoc(wrapped)`. That is the one registration
  path, so the description is the WHOLE docstring again, as fastmcp 2 made it, and
  `server.<tool>` is still the `FunctionTool` the `.fn` seam reads.
  `apply_disabled_sections` calls `mcp.local_provider.remove_tool`.
- Before that call, the wrapper's `__doc__` is set to `None`. fastmcp 3.4.8 has no
  switch for the per-parameter copy: `ParsedFunction.from_function` always runs
  `parse_docstring(fn)`, which reads `inspect.getdoc(fn)` and injects every `Args:`
  entry. No `Tool.from_function` argument, setting or environment variable turns it
  off. The function it is handed is OUR wrapper, built one line earlier, so the
  docstring is withheld at the source rather than stripped from the schema afterwards.
  That would have been a second schema-shaping pass beside fastmcp's own. The tool's
  own function keeps its docstring one `__wrapped__` down. Two tests that read
  `.fn.__doc__` now read the served `description`. One of them,
  `test_animation_edit_recipes`, would otherwise have passed while reading an empty
  string, so it also asserts the docstring arrived.
- `HygienicSessionManager` now extends `FastMCPStreamableHTTPSessionManager`, so it
  keeps fastmcp's per-session event-store scoping. `install()` binds that name. The pin
  matches the whole name on a word boundary.
- `logging_setup.PAYLOAD_WARNING_LOGGERS = ("fastmcp.server.server",)` is held at
  ERROR by `apply_payload_log_floor`, the existing function. This is the F-906 mechanism
  (an explicit level on the library's own logger, upstream of every sink), applied to
  a payload line that sits AT WARNING. An AST census of that logger's calls below ERROR
  shows the floor costs nothing:
  - the two payload lines;
  - `FastMCPError.log_level` in `call_tool` and `read_resource`. No tool and none of
    the four `browser://` resource templates raises a `FastMCPError`;
  - the prompt line; this backend serves no prompt;
  - `mount` and `import_server`; it mounts and imports nothing.

  `Error calling tool` and `Error reading resource` (both ERROR) and `tool_failure`'s
  restored report still pass. The result is exactly fastmcp 2.14's output.
- `expected_events.TOOL_MANAGER_LOGGER` is renamed `TOOL_CALL_LOGGER`
  (`fastmcp.server.server`), and `ARG_VALIDATION_FRAMES` names `_execute`. The
  `caller-input` class is kept: in 3.x a caller's typo never becomes an event, but the
  class costs one row and is right again if a release logs one with its exception.
- `tools/dump_tool_surface.py` reads `mcp.local_provider.list_tools()` and
  `transforms.is_enabled`. Tests go through one new helper, `fakes.live_tools(mcp)`.
- Pins: `fastmcp==3.4.8` and `fastmcp-slim==3.4.8`. `py-key-value-aio` goes from 0.3.0
  to 0.4.6, without `[disk]`, so `diskcache` leaves the lock. 18 packages leave in
  all, among them `pydocket`, `redis`, `fakeredis`, `lupa` and `typer`. Four arrive:
  `fastmcp-slim`, `griffelib`, `aiofile` and `caio`. `mcp`, `starlette`, `anyio`,
  `pydantic` and `uvicorn` are unchanged.

### What a client sees in `tools/list`, deliberately

One additive key, fastmcp 3's, and nothing else in the input schema: the top level
says `"additionalProperties": false` (94 tools). pydantic already rejected an
unexpected argument on every call (measured: the answer is `Unexpected keyword
argument`), so the schema now states a rule that was always enforced. It is emitted
by fastmcp's own schema compression, and there is no argument to turn it off.

A second change is outside the input schema and outside the golden. fastmcp renamed
its own metadata key, so every tool and resource template in `tools/list` and
`resources/templates/list` carries `"_meta": {"fastmcp": {"tags": []}}` where 2.14
sent `{"_fastmcp": {"tags": []}}`. This was measured on the wire with both versions.
Nothing in this repo reads it. The four resource templates are otherwise identical,
full-docstring descriptions included: fastmcp 3 still uses `inspect.getdoc` for
those.

Removing `additionalProperties` gives the old golden exactly. The comparison covers
all 94 tools: descriptions byte-identical, names, types, defaults, `required`, output
schemas, tags and `enabled`. The `tools/list` answer, measured on the wire as compact
JSON through an in-memory client:

| | bytes | vs 2.14.7 |
|---|---|---|
| fastmcp 2.14.7 (main) | 89 263 | |
| 3.4.8, parameter descriptions left in (the first commit of this branch) | 114 526 | +28.3% |
| 3.4.8, as shipped | 91 895 | +2.9% |

The remaining 2.9% is the `additionalProperties` flag and the `_meta` rename. Both the
HARD golden and the SOFT per-section snapshot in `test_correlation_id.py` were
regenerated, with this justification beside them.

Changed on the wire in `tools/call`, measured on a live backend and not in the first
draft of this finding: a successful result of each of the 31 tools whose output fastmcp
wraps in `{"result": …}` carries `"_meta": {"fastmcp": {"wrap_result": true}}` (2.14.7
sent no `_meta`), and an unknown tool's error quotes the name, `Unknown tool:
'no_such_tool'` where it read `Unknown tool: no_such_tool`. Nothing in `src/` or the stdio
proxy reads either.

Unchanged on the wire, measured: `Error calling tool '<name>': …` for a failing tool;
a caller's bad argument answered `1 validation error for call[<tool>] …`; a body's own
pydantic error answered with its own text; 94 tools in 11 sections; MCP served at
`/mcp/` with a 200 to a probe that does not follow redirects.

### Behaviour that changed and is not visible to a client

- **The lifespan's timing is NOT a change.** An earlier draft of this finding said
  fastmcp 3 moved `app_lifespan` from the first MCP session to process start. That was
  wrong, and so were the comments it led to. On 2.14.7, `run_http_async` already entered
  `_lifespan_manager()` before `uvicorn.Server.serve()`. Measured with a bare FastMCP
  serve on both versions:
  - **Normal serve:** the lifespan is entered before the port is bound (2.14.7: 0.016 s
    vs bind at 1.047 s; 3.4.8: 0.013 s vs 0.538 s).
  - **Bind failure:** both enter it and then exit with `SystemExit(1)`.

  The real backend on a port another process holds, in a throwaway `HOME`, leaves the
  same residue on both versions: rc 1, a `heartbeat-<port>.json` sidecar naming the
  dead pid, and no `server.json` entry. The entry is the proxy's write, made before
  the spawn, on both. The "first MCP session" sentences in `serve_startup.py` and
  `server.py`'s B1 comment predate this and were already untrue on 2.14.7. They now
  say what was measured.

  Why the early lifespan cannot make a proxy think a backend is alive or adoptable:
  - **Adoption and reuse** (`adoption_candidates` → `probe_port`, and
    `_same_identity_backend_ready`) ask the port for a real MCP `initialize`.
  - **The cold-start lock** is held until that `initialize` answers (F-807).
  - **The heartbeat's one reader** is `backend_liveness.self_report`, used by the
    watchdog of a proxy that has ALREADY bridged after `await_ready`. It needs a
    `server.json` entry on that port whose pid equals the stamp's. It can only defer
    a condemnation, and only while the stamp is under `HEARTBEAT_STALE_SECONDS` (30 s).
  - **Eviction's `protected`** reads the entry's pid and the browser-pid registry, and
    the lifespan writes neither.

  So the failed-bind sidecar is pre-existing residue. At worst it is a 30 s
  "still alive" witness, and only if the proxy recorded exactly that pid; a stamp
  without a matching entry is ignored. It is not changed here.
- **The lifespan is re-entered per in-memory client** on 3.4.8 (2 entries over 2
  sequential `Client(mcp)` connections; 2.14.7 entered once). No production path uses
  an in-memory client: stdio and http each hold the lifespan for the whole process.
  `_LIFESPAN_STARTED` keeps startup to once per process either way.
- **The 5 synchronous tools** (the `dynamic-hooks` documentation tools) now run in a
  worker thread. They are pure and keep their correlation id.
- **Host/Origin guard.** fastmcp 3 refuses a non-loopback `Host` with 421 and a
  cross-origin `Origin` with 403 on a loopback bind. The proxy sends
  `http://127.0.0.1:<port>/mcp/` and no `Origin`, so it is unaffected.
- uvicorn's default `timeout_graceful_shutdown` is now 2. `backend_uvicorn_config()`
  still sets ours (F-809).
- **The backend's `initialize` capabilities differ.** 3.x adds `logging` and the
  `io.modelcontextprotocol/ui` extension and drops 2.14's `tasks` (measured). A
  client never sees them through the stdio proxy, which answers `initialize` itself
  with a fixed `{"tools": {"listChanged": false}}`. Only a client speaking HTTP to the
  backend directly would.
- `@mcp.resource` now returns the bare function, as `@mcp.tool` does.
  `server.get_browser_state_resource` and the other three are plain coroutines; one
  test read `.fn` off one.

## 3. Evidence

- **pip-audit**, run over `uv export --frozen --no-hashes --all-extras
  --no-emit-project` with `pip-audit -r <file> --no-deps --disable-pip`:
  - before (main `4fbad69`): 3 advisories in 2 packages: fastmcp 2.14.7
    PYSEC-2026-2475 and PYSEC-2026-2476, and diskcache 5.6.3 PYSEC-2026-2447;
  - after: **No known vulnerabilities found**.
- **Session sweep:** before the fix,
  `test_end_to_end_abandoned_probes_are_reaped_and_the_live_client_is_not` failed with
  "FastMCP did not build OUR manager". Mutation by runtime rebinding (no file
  modified): putting the fastmcp-2 binding back in `install` fails both the
  strengthened pin and the end-to-end test. The control passes both.
- **WARNING floor:** `TestTheFastmcpWarningLine` in `tests/test_payload_log_floor.py`.
  It drives the real `FastMCP.call_tool`, and a premise test shows the line fires with
  the marker. Mutation: rebinding `apply_payload_log_floor` to the families-only body
  fails `test_the_floor_withholds_it_and_keeps_the_error`, 1 failed / 2 passed; the
  control gives 3 passed. A third test pins the AST census.
- **Golden:** the comparison above. The script compared the two JSON files key by key:
  94 `additionalProperties` flags and zero other differences.
- **No parameter descriptions:** mutation by runtime rebinding (a pytest plugin that
  puts the docstring back on the wrapper before `Tool.from_function`, no file
  modified) fails `test_the_served_tool_surface_matches_the_golden` and the SOFT
  snapshot, 2 failed / 76 passed. The control gives 78 passed. The dedicated pin is
  `TestTheDocstringIsServedOnce` in `tests/test_tool_registry.py`. It checks one real
  `Tool` built from a documented function, and the live 94: no input-schema property
  carries a `description`, and each description equals `inspect.getdoc` of the
  unwrapped original. The same mutant fails both tests (2 failed / 9 passed). A second
  mutant keeps the docstring withheld and drops `description=`; that fails both on the
  description equality.
- **Who reads a tool's docstring** (census of `__doc__`, `getdoc`, `.description` and
  `__wrapped__` across `src/`, `tests/`, `tools/` and the docs):
  - nothing reads the wrapper's `__doc__` except the two tests moved to `.description`;
  - `dump_tool_surface` and `backend_client` read `tool.description`;
  - `--list-sections` prints hard-coded section labels;
  - `test_execute_script_async` reads the section module's own function, which is the
    unwrapped original, with its docstring intact;
  - no README or NAVMAP table is generated from docstrings.
- **Size in the golden's on-disk form** (CRLF): 120 635 bytes on 2.14.7, 150 270
  (+24.6%) with the parameter descriptions, 124 207 (+3.0%) as shipped.
- **Context7 was not reachable from this session.** The API facts above were read from
  the installed fastmcp 3.4.8 source and measured against it, not taken from its docs.
