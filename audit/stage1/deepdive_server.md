# Deep Dive DD-1: server.py god object + handler coupling

Scope: `src/stealth_chrome_devtools_mcp/embedded/server.py` (4207 lines) and its coupling to
`dom_handler.py`, `network_interceptor.py`, `response_handler.py` (coupling only, not a full
internals re-audit of those three). Findings: F-101..F-109, written to `findings_dd1.json`.

Baseline facts, verified programmatically (not estimated):

- 4207 lines, 162 `def`/`async def` definitions, 96 registered via `@section_tool(...)` across
  11 sections (browser-management 11, element-interaction 8, element-extraction 10,
  file-extraction 9, network-debugging 10, cdp-functions 15, progressive-cloning 10,
  cookies-storage 3, tabs 5, debugging 6, dynamic-hooks 12 — per the `--list-sections` help text).
- 17 first-party local modules imported at top level (`browser_manager`, `cdp_element_cloner`,
  `cdp_function_executor`, `comprehensive_element_cloner`, `debug_logger`, `dom_handler`,
  `element_cloner`, `file_based_element_cloner`, `models`, `network_interceptor`,
  `dynamic_hook_system`, `dynamic_hook_ai_interface`, `persistent_storage`,
  `progressive_element_cloner`, `response_handler`, `platform_utils`, `process_cleanup`), plus
  `nodriver` and `fastmcp`.
- 22 `except Exception`/bare `except:` blocks.
- Only 1 `exec()` call site (server.py:3848); no `eval()`; `getattr(...)` dynamic-attribute
  reads are common but none resolve an attacker/caller-controlled attribute name.

## Axis 1 — Coupling

There is exactly one indirection layer for tool registration: `section_tool(section)`
(server.py:1212-1217), which appends the function name to a `SECTION_TOOLS` dict and then calls
`mcp.tool(func)`. That's it — no registry of tool metadata beyond the section label, no
per-domain module boundary. All 96 tool implementations (validation, delegation, response
shaping, error handling) are written inline in this one file. See **F-101**.

Positive finding, worth stating precisely because it's easy to over-claim the opposite: server.py
does *not* generally reach past the public APIs of `dom_handler` / `network_interceptor` /
`response_handler`. Every `dom_handler.*` call (11 call sites) and `network_interceptor.*` call
(16 call sites) goes through a named public method (`query_elements`, `click_element`,
`type_text`, `list_requests`, `get_cookies`, `set_cookie`, etc.), consistently wrapped in
`_with_cdp_timeout(...)`. `response_handler.handle_response(...)` is called synchronously (never
awaited — see Axis 4 for the bug this recently was) at 8 sites. The only bypass of the
handler-abstraction pattern is `_install_nodriver_cookie_compat()` monkey-patching the
**third-party** `nodriver` library directly (Axis 2), which is a different kind of coupling
concern (to an external dependency, not to the first-party handler modules in scope).

So: coupling to the three named handler modules is clean at the call-site level; the coupling
problem is at the file level — 96 unrelated tool domains sharing one compilation/import unit with
no fault isolation (F-101's second half).

## Axis 2 — Hidden state

Three distinct mechanisms combine into one root cause (module import has unscoped side effects,
no explicit init boundary) — folded into **F-103**:

1. Four singletons constructed at bare module scope: `browser_manager = BrowserManager()`,
   `network_interceptor = NetworkInterceptor()`, `dom_handler = DOMHandler()`,
   `cdp_function_executor = CDPFunctionExecutor()` (server.py:1297-1300). Every one of the 96
   tools reads these as bare module globals, not as injected dependencies.
2. `if parse_bool_env("XPOOL_SAFE_MODE", default=False): DISABLED_SECTIONS.add("cdp-functions");
   apply_disabled_sections()` at module scope (server.py:4072-4074) — outside
   `if __name__ == "__main__"`, so merely importing server.py can unregister a tool section based
   on an environment variable.
3. `_install_nodriver_cookie_compat()` (server.py:195-213) replaces
   `nodriver.cdp.network.Cookie.from_json` with a wrapped version at runtime — patching a
   third-party class's behavior process-wide, guarded by a marker attribute set on the library's
   own class object so it's idempotent, but still a global mutation with no lifecycle boundary.

Consequence visible in the test suite: `tests/test_browser_integration.py` cannot do a plain
`import server` — it manually builds a module spec via
`importlib.util.spec_from_file_location(...)` and wraps `exec_module` in `try/except` just to
import the file defensively.

`hot_reload()` (Axis 3 detail, but also a hidden-state mechanism) additionally *mutates* the
`browser_manager` / `network_interceptor` / `dom_handler` globals at arbitrary runtime points via
explicit `global` statements deep inside a tool handler (server.py:2996, 2999, 3002, 3005) — see
**F-102**.

## Axis 3 — Error handling

22 broad `except` blocks; sampled every one that returns a value (not just logs-and-continues) and
found **at least four incompatible error contracts** in simultaneous use — see **F-104** for the
full instance list with line numbers. Summary:

| Pattern | Example | Shape |
|---|---|---|
| success/result/error triple | `execute_script` (1939-1949) | `{"success": bool, "result": ..., "error": str \| None}` |
| bare error dict | `get_debug_lock_status` (2558) | `{"error": str(e)}` — no `success` key |
| re-raised generic Exception | `new_tab` (2662) | `raise Exception(f"Failed to create new tab: {e}")` |
| string-typed for both outcomes | `hot_reload` (3009), `reload_status` (3037) | `-> str`, success and failure both return plain strings distinguishable only by substring |

The one shared helper that *could* have been the place to unify this, `_with_cdp_timeout`
(server.py:139-156), itself just does `raise Exception(f"CDP operation timed out...")` — a
generic, untyped exception, not a structured/typed error.

Nothing in the test suite asserts error-shape consistency, so this is free to keep diverging as
tools are added.

## Axis 4 — Testability

Two different pictures depending on what kind of code:

- **Pure/path logic extracted to module scope** (profile resolution and friends) tests cleanly:
  `tests/test_profile_resolution.py` does `from server import (_is_relative_to,
  _profile_ignore_names, _copy_profile_delta, _copy_profile_tree, _snapshot_needs_refresh,
  _next_available_explicit_dir, _resolve_profile_selection, _default_session_root,
  _master_profile_dir, _clone_root_dir, _master_snapshot_dir)` and drives them with `tmp_path` +
  env-var patches, no browser needed. Several other test files (`test_clone_*`,
  `test_execute_script_guard.py`, `test_sweep_deferred_cleanup.py`, `test_server_entrypoint.py`,
  `test_server_call_conventions.py`) follow the same `import server` + reach-into-module-internals
  pattern for other pure helpers. This is a real, working pattern — 10 test files use it.
- **The 96 `@section_tool` coroutine bodies themselves** — the actual MCP tool entry points —
  have no equivalent. The only coverage is `tests/test_browser_integration.py`
  (`pytestmark = pytest.mark.integration`, requires a real installed Chrome, skippable/skipped in
  CI without it) or narrow AST-based static conventions. **F-105** documents the concrete proof:
  a synchronous/awaited call-convention bug shipped silently in 3 tools and was only caught and
  fixed on the current commit (`2267b83`), with the fix accompanied by a new
  `tests/test_server_call_conventions.py` whose own docstring states plainly: *"A live-browser
  test can't cover all 96 tools cheaply, but the convention is statically checkable."* That's an
  admission, in the codebase itself, of the gap this finding describes.

## Axis 5 — Blast radius

- **Import-time blast radius**: because all 96 tools live in one module with no per-section
  module boundary, a single bad top-level statement, a broken decorator argument, or an import
  failure in *any* of the 17 first-party dependencies takes down registration for all 11 sections
  simultaneously — not just the section where the break occurred. `DISABLED_SECTIONS` /
  `apply_disabled_sections()` only prune tools *after* a fully successful import, so they provide
  no protection against this. Folded into **F-101**.
- **Runtime code-execution blast radius**: `create_python_binding` (server.py:3808) runs bare
  `exec(python_code, exec_globals)` with a fresh-but-unrestricted globals dict (full
  `__builtins__` available) and no sandboxing — full arbitrary code execution in the server
  process, reachable as a normal MCP tool call. See **F-107**. No other `eval()`/dynamic-dispatch
  mechanism (`globals()[...]`, attacker-controlled `getattr` target) was found; all `getattr(...)`
  usages read fixed, hardcoded attribute names.
- **Dev-loop blast radius**: `hot_reload()` doesn't fail loudly when it doesn't work — it reports
  success while silently leaving stale class definitions in place (F-102), which risks a developer
  drawing false conclusions from a "fixed" server that never actually picked up the fix.

## `_resolve_profile_selection` assessment (requested)

Confirmed D-grade complexity, but **not** an untested black box — `tests/test_profile_resolution.py`
covers it directly and it's genuinely tricky, not falsely flagged. Concretely (server.py:1080,
body to ~1160):

- ~10 branch points including a 4-way `if source_override is not None / elif snapshot.exists() /
  elif master.exists() / else: raise`.
- 2 ternary expressions (clone-dir naming; snapshot-vs-master source fallback).
- 3 early `return`s + 1 `raise`.
- ~14 distinct collaborator calls (`_master_profile_dir`, `_clone_root_dir`,
  `_master_snapshot_dir`, `_is_relative_to`, `_profile_has_running_browser`,
  `_next_available_explicit_dir`, `_snapshot_needs_refresh`, `_refresh_master_snapshot_if_safe`,
  `_clone_profile_dir_for_session`, `_unique_clone_dir`, `_available_clone_dir`,
  `_spawn_background_sweep`, `_protect_clone_dir`, `_copy_clone_from_source`), mixing decision
  logic with real filesystem side effects (`mkdir`, triggering a snapshot refresh, kicking a
  background sweep) inline.
- The retry/fallback protocol built on top of it spans **three** functions:
  `spawn_browser`'s 3-attempt loop (server.py:1360) → `_resolve_profile_selection` →
  `_fallback_profile_selection` (server.py:~1141, itself re-invoking
  `_resolve_profile_selection` with override args) — coordinated only via a stringly-typed
  `Dict[str, Any]` (`profile_role` ∈ `{"explicit", "master", "clone"}`), not an enum/dataclass.

See **F-106** for the full writeup and fix direction (split decision logic from I/O side effects).

## Findings index

| ID | Severity | Category | One-line summary |
|---|---|---|---|
| F-101 | High | architecture | Monolithic file: 96 tools, 1 decorator, no module split; shared import-time failure domain across all 11 sections |
| F-102 | High | operability | `hot_reload()` reassigns singletons using the pre-reload class (stale `from...import` binding); reports success while doing nothing |
| F-103 | Medium | architecture | Import-time side effects: singleton construction, env-gated section disabling, third-party monkey-patch, no init boundary |
| F-104 | High | code_health | 4+ incompatible error-response shapes across 22 except-blocks; no shared error envelope |
| F-105 | High | testing | None of the 96 tool-handler bodies unit tested; proven gap (real bug shipped, fixed this commit, patched reactively with an AST check) |
| F-106 | Medium | code_health | `_resolve_profile_selection` D-grade complexity: ~10 branches, 14 collaborators, 3-function retry protocol on stringly-typed state (tested, but costly to change) |
| F-107 | High | security | Unsandboxed `exec()` in `create_python_binding` — full RCE-equivalent blast radius by design |
| F-108 | Low | docs | Tool count hand-typed in 3 places (90 / 96 / 99), all disagree |
| F-109 | Low | code_health | 3 reach-ins to other modules' `_private` attributes, bypassing their public API |
