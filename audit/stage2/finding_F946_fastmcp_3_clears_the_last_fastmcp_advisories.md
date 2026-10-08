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
   after `Args:` vanished from `tools/list`. The HARD golden caught it.
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
  description=inspect.getdoc(wrapped)))`. That is the one registration path, so the
  description is the WHOLE docstring again, as fastmcp 2 made it, and `server.<tool>`
  is still the `FunctionTool` the `.fn` seam reads. `apply_disabled_sections` calls
  `mcp.local_provider.remove_tool`.
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

Two additive keys, both fastmcp 3's, and nothing else:

- each parameter's `description` is its line from the docstring's `Args:` section
  (264 across 94 tools). The same text is still in the tool description.
- the top-level input schema says `"additionalProperties": false` (94 tools). pydantic
  already rejected an unexpected argument on every call (measured: the answer is
  `Unexpected keyword argument`), so the schema now states a rule that was always
  enforced.

A third change is outside the input schema and outside the golden. fastmcp renamed
its own metadata key, so every tool and resource template in `tools/list` and
`resources/templates/list` carries `"_meta": {"fastmcp": {"tags": []}}` where 2.14
sent `{"_fastmcp": {"tags": []}}`. This was measured on the wire with both versions.
Nothing in this repo reads it. The four resource templates are otherwise identical,
full-docstring descriptions included: fastmcp 3 still uses `inspect.getdoc` for
those.

Stripping the two input-schema keys gives the old golden exactly. The comparison covers all 94
tools: descriptions byte-identical, names, types, defaults, `required`, output schemas,
tags and `enabled`. `tools/list` grows from 120 635 to 150 270 bytes in the golden's
form. Stripping the keys instead of accepting them would have been a schema-rewriting
pass beside fastmcp's own, a second way. Both the HARD golden and the SOFT per-section
snapshot in `test_correlation_id.py` were regenerated, with this justification beside
them.

Unchanged on the wire, measured: `Error calling tool '<name>': …` for a failing tool;
a caller's bad argument answered `1 validation error for call[<tool>] …`; a body's own
pydantic error answered with its own text; 94 tools in 11 sections; MCP served at
`/mcp/` with a 200 to a probe that does not follow redirects.

### Behaviour that changed and is not visible to a client

- **The lifespan runs at process start.** fastmcp 2 entered `app_lifespan` on the
  first MCP session; fastmcp 3 enters it once around the whole HTTP serve, before
  uvicorn binds. `_LIFESPAN_STARTED` still guards it, its HTTP teardown is still a
  no-op, and its slow work is still in the background (F-856). The consequence: a
  backend that no client ever reaches now still arms `process_cleanup` and its
  heartbeat.
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
- **Golden:** the stripped comparison above. The script compared the two JSON files
  key by key: 264 descriptions, 94 flags, zero other differences.
- **Context7 was not reachable from this session.** The API facts above were read from
  the installed fastmcp 3.4.8 source and measured against it, not taken from its docs.
