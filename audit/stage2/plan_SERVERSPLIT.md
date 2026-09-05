# plan_SERVERSPLIT — shrink `embedded/server.py` by extracting the 94 tool bodies

**Status:** **Slice 0 EXECUTED** (branch `refactor/serversplit-slice0`, based on
`bb8b5ce` = release 2.1.1). Slices 1-12 not started.
**Measured at:** working tree on branch `fix/2.0.9-batch`, HEAD `6ce62fb`
(`git worktree list` also shows two sibling agents on `fix/F844-F845-tab-state` @ `de948a2`
and `fix/F843-fast-death-heal` @ `000e497` — see §7 R5).

> ### Execution log — drift between the plan and HEAD, and how it was resolved
>
> | Plan says | Reality at `bb8b5ce` | Resolution |
> |---|---|---|
> | `server.py` actual 3407 / cap 3411 (4 lines of headroom) | actual **3411** / cap **3411** — **zero** headroom (plan_F856 spent it) | Slice 0 is net-negative anyway: **3342**, cap ratcheted DOWN to the measured actual (§6.1) |
> | §5.4's contract test opens `assert SECTION_MODULES` | slice 0 leaves `SECTION_MODULES` empty by design, so that assertion would fail on its own slice | the AST checker was factored into `_check_section_module_source` and is driven against synthetic compliant/violating modules; the walk over `SECTION_MODULES` goes live in slice 1 |
> | §1.4 lists `tests/test_log_hygiene.py:184` as a vacuity risk | its subject (`mcp.run(..., uvicorn_config=…)`) never leaves `server.py`'s `__main__` block | widened for uniformity; the expected count is still exactly 1 across the set |
> | §5.5's `MIGRATION_ALIASES` is an unspecified list | — | derived from `tool_runtime.__all__` ∩ `dir(server)`, with a non-empty floor so the parametrize cannot silently vanish |
> | §2.4 lists the re-bound module singletons without saying who consumes them | `process_cleanup` / `in_memory_storage` / `clone_storage` are read by `app_lifespan`, which §3.4 re-points to `rt.*` | all of §2.4's names ship in slice 0; `__all__` re-exports them so `ruff` sees a deliberate re-export, not dead imports |
> | owner-tag grammar for a new per-file-ignore | `tools/check_suppression_owners.py`'s `OWNER_RE` only accepted `plan_M\w+` | widened to `plan_[A-Z]\w+` (still requires a plan id; `plan_F809`/`plan_F856` were already unrepresentable) |

**Baseline numbers, measured (not quoted from docs):**

| Fact | Value | Anchor |
|---|---|---|
| `embedded/server.py` actual LOC | **3407** | `wc -l` at HEAD |
| `embedded/server.py` grandfathered cap | **3411** (4 lines of headroom) | `tools/check_file_budgets.py:47-50` |
| Tool bodies (94 `@section_tool` defs, decorator line → line before next top-level stmt) | **2781 LOC**, `L334–3188` minus the two interruptions | AST measurement, §2 table |
| Module constant embedded mid-section (`_CAPTURE_OFF_NOTE`) | 7 LOC, `L1215–1221` | `server.py:1215` |
| `@mcp.resource` handlers (NOT `section_tool`-registered) | 67 LOC, `L1526–1592` | `server.py:1526,1543,1561,1576` |
| Shared helpers + knobs to extract | 86 LOC, `L67–152` | `server.py:67-152` |
| Singleton construction to extract | 5 LOC, `L325–329` | `server.py:325-328` |
| Tool count, derived | **94** across **11** sections | `sum(len(v) for v in SECTION_TOOLS.values())` |

---

## 0. The one-paragraph summary

The 94 tool bodies move out of `embedded/server.py` into **11 section modules** under a
new `embedded/tool_sections/` subpackage — one module per `SECTION_TOOLS` section. A
section module contains **plain, undecorated `async def`s** plus two exported names
(`SECTION`, `TOOLS`); it never touches `mcp`, never applies `@section_tool`, and resolves
every singleton as `rt.<name>` through a single new leaf, `embedded/tool_runtime.py`.
`server.py` keeps `mcp`, `registry`, `app_lifespan`, the four resources, `build_arg_parser`
and the `__main__` block, and gains a **four-line binding loop** that decorates and binds
all 94 functions into its own namespace **once per module-body execution**. That last
property is the whole point: it is what keeps the runpy double-load producing 94 tools per
`mcp` instance instead of 0, and what keeps `getattr(server, tool_name)` — the mechanism
both `tests/fakes.py:125` and `tests/e2e_helpers.py:86` depend on — working unchanged.

`server.py` ends at roughly **545 LOC** and leaves `GRANDFATHER` entirely.

---

## 1. Why the obvious design is wrong (read this before §2)

### 1.1 The runpy hazard, restated precisely

`stealth_chrome_devtools_mcp/server.py:82` does
`runpy.run_path(str(EMBEDDED_DIR / "server.py"), run_name="__main__")`. In a single
process, `embedded/server.py`'s **module body** can execute up to three times under three
different identities:

1. `stealth_chrome_devtools_mcp.embedded.server` — the canonical absolute import
   (24 test files bind it this way; `tools/release_evidence.py:978` too).
2. `server` — a bare-name identity created by `importlib.util.spec_from_file_location`
   (`tests/e2e_helpers.py:29-44`, `tests/test_browser_integration.py:39-52`, plus five
   root-level scratch scripts). It resolves because `embedded/__init__.py:23-25` puts
   `embedded/` on `sys.path`.
3. `__main__` — the runpy load above.

Each execution builds its **own** `mcp = FastMCP(...)` (`server.py:303`), its own
`registry = ToolRegistry(mcp)` (`server.py:321`) and — today — its own singletons
(`server.py:325-328`). `SECTION_TOOLS`, by contrast, lives in `tool_registry.py:39` and is
imported once, so it is **shared across all three executions**. That asymmetry is the
historical 3×94 = 282 bug, cured by the idempotent append at `tool_registry.py:107-109`
and pinned by `tests/test_tool_registry.py:79-94`.

### 1.2 The hazard the split *introduces* — and it is the mirror image

If a section module carries `@section_tool("cookies-storage")` at its own module scope,
that module is imported **once** per process (canonical absolute form → one `sys.modules`
identity). Its body runs on the first server.py execution and never again. Executions 2
and 3 would build a fresh `mcp` that **nothing ever registers into**:

* `SECTION_TOOLS` would still say 94 (populated by execution 1, idempotent), so the
  count tests at `tests/test_tool_registry.py:135-176` would stay **green**;
* `await server.mcp.get_tools()` on the second identity would return **0**, and
  `getattr(_server_mod, "spawn_browser")` in `tests/e2e_helpers.py:86` would raise —
  i.e. the entire E2E tier would `pytest.skip` itself into vacuous green.

This is strictly more dangerous than the 282 bug, because the count tripwire does not see
it. §5 specifies the detector that does.

**Therefore: rule 2 of the section-module contract — a section module never decorates.**
Registration is *driven from* the `server.py` execution, so each execution registers into
its own `mcp`.

### 1.3 The test seam is two independent couplings, not one

The census found two distinct mechanisms that both have to survive:

**(a) Tool lookup is a module-attribute read.** `tests/fakes.py:118-130`:

```python
tool_obj = getattr(server_mod, name)
fn = getattr(tool_obj, "fn", tool_obj)
```

and `tests/e2e_helpers.py:84-89` (`get_fn`) does the same against the path-loaded module.
`tests/test_server_network_tools.py` bypasses `call_tool` entirely with 15 direct
`server.<tool>.fn(...)` calls (`:38,49,53,58,63,70,73,88,98,109,113,129,135,137,140,145,164`).
So **all 94 names must remain attributes of `server`**. The binding loop provides this.

**(b) Singleton substitution is a module-attribute write.** `tests/conftest.py:208-225`:

```python
def _patch(**singletons):
    for name, obj in singletons.items():
        monkeypatch.setattr(server, name, obj)
    return server
```

There are **40 `patched_server(...)` call sites across 13 files** plus **12 direct
`monkeypatch.setattr(server, …)` sites across 5 files**. Once a tool body lives in
`tool_sections/cookies_storage.py`, its `__globals__` is that module's dict, and
`setattr(server, "browser_manager", fake)` becomes a **silent no-op** — the test passes a
fake and the tool uses the real `BrowserManager`. This is the single hardest part of the
split and §3 is dedicated to it.

### 1.4 Four guards read `server.py`'s *source text* and will go vacuous

These do not fail when code leaves the file — they pass over an emptier file. Each must be
widened in Slice 0 (§4.1, step 6):

| Guard | Anchor | What it scans for |
|---|---|---|
| F-164 CDP-timeout discipline | `tests/test_cdp_timeout.py:440-486` | `async def _with_cdp_timeout` defined **exactly once** in `server.py`; every other `asyncio.wait_for` in `server.py` carries a `# F-164 non-CDP` marker |
| F-202 call convention | `tests/test_server_call_conventions.py:23` | AST of `server.py`: `handle_response` is never awaited |
| log hygiene | `tests/test_log_hygiene.py:184` | AST of `server.py` |
| observability message pin | `tests/test_observability.py:1017` | `MSG_EXPORT_TIMEOUT in inspect.getsource(w15_server)` — the string lives in `export_debug_logs`, which moves in Slice 3 |

`tests/test_clean_shutdown_noise.py:242-248` also reads `Path(server.__file__)` but asserts
`mcp.run` sits under `if __name__ == "__main__":` — that stays in `server.py`, so it is
unaffected.

---

## 2. Target module layout

### 2.1 New files

```
src/stealth_chrome_devtools_mcp/embedded/
  tool_runtime.py                    # NEW leaf: the one patchable home
  tool_sections/
    __init__.py                      # NEW: the contract + SECTION_MODULES tuple
    browser_management.py            # NEW ×11
    element_interaction.py
    network_debugging.py
    cookies_storage.py
    debugging.py
    tabs.py
    element_extraction.py
    progressive_cloning.py
    file_extraction.py
    cdp_functions.py
    dynamic_hooks.py
```

**Subpackage name — `tool_sections/`, not `tools/`.** `embedded/__init__.py:23-25` puts
`embedded/` on `sys.path`, and the repo root already has a `tools/` directory
(`tools/check_file_budgets.py`, `tools/release_evidence.py`) which pytest's rootdir also
puts on the path. A bare `import tools` would then be ambiguous between two directories,
and namespace-package semantics can merge them silently. `tool_sections` collides with
nothing and reads as what it is: one module per `SECTION_TOOLS` section.

`tools/check_file_budgets.py:128` uses `pkg.rglob("*.py")`, so the new subpackage is picked
up by the budget gate with no change to that script.

### 2.2 Section → module map, with measured sizes

Two tools are physically misfiled today; the move **relocates them to their own section's
module**. This is a pure relocation (no rename, no behavior change) and it is what makes
module ↔ section a clean 1:1 — which in turn lets `SECTION` be a per-module constant
instead of a per-tool string.

| # | Module | Section | Tools | Current line ranges | Body LOC | Est. new-file LOC |
|---|---|---|---|---|---|---|
| 1 | `cookies_storage.py` | `cookies-storage` | 3 | `L1432–1525` | 94 | ~125 |
| 2 | `tabs.py` | `tabs` | 5 | `L1701–1803` | 103 | ~135 |
| 3 | `debugging.py` | `debugging` | 5 | `L1593–1700` + **stray `validate_browser_environment_tool` `L2127–2146`** | 128 | ~160 |
| 4 | `dynamic_hooks.py` | `dynamic-hooks` | 10 | `L3015–3188` | 174 | ~205 |
| 5 | `progressive_cloning.py` | `progressive-cloning` | 10 | `L2147–2323` | 177 | ~210 |
| 6 | `network_debugging.py` | `network-debugging` | 10 | `L1163–1214` + `L1222–1431` + `_CAPTURE_OFF_NOTE` `L1215–1221` | 262 + 7 | ~305 |
| 7 | `file_extraction.py` | `file-extraction` | 9 | `L2324–2390` + `L2428–2638` | 278 | ~315 |
| 8 | `element_extraction.py` | `element-extraction` | 9 | `L1804–2126` + **stray `extract_complete_element_cdp` `L2391–2427`** | 360 | ~395 |
| 9 | `cdp_functions.py` | `cdp-functions` | 13 | `L2639–3014` | 376 | ~410 |
| 10 | `browser_management.py` | `browser-management` | 8 | `L334–710` | 377 | ~410 |
| 11 | `element_interaction.py` | `element-interaction` | 12 | `L711–1162` | 452 | ~490 |
| | | | **94** | | **2788** | |

Every new module lands well under the 1000-LOC default budget
(`tools/check_file_budgets.py:14`). See §6.2 on why none of them gets a `GRANDFATHER` row.

### 2.3 What stays in `server.py`

| Block | Lines today | Why it stays |
|---|---|---|
| Imports | `L1–65` | shrinks to ~40 |
| `_install_asyncio_close_noise_filter` | `L155–190` | referenced as `server.…` by `tests/test_close_noise_filter.py:29`; process-level, not a tool concern |
| `_install_nodriver_cookie_compat` | `L193–213` | referenced by `tests/test_silent_excepts_log_7d.py:48`; same reason |
| `DEBUG_LOGGING_ENABLED`, `_LIFESPAN_STARTED`, `_SERVE_TRANSPORT` | `L216–227` | `_LIFESPAN_STARTED`/`_SERVE_TRANSPORT` are patched on `server` by `tests/test_lifespan_reentrancy.py:89,98,118,135` and are per-execution process state, not tool state |
| `app_lifespan` | `L230–301` | one lifespan per `mcp`; it is `mcp`'s constructor argument at `L318`. **Its body must be re-pointed to `rt.*` — see §3.4** |
| `mcp` + `registry` + `section_tool` + `apply_disabled_sections` | `L303–324` | the per-execution FastMCP app |
| **the binding loop** | NEW, ~8 lines | §3.2 |
| the 4 `@mcp.resource` handlers | `L1526–1592` | they need `mcp` (the decorator), which is per-execution. Keeping them here keeps `mcp` used in exactly one file |
| xpool-safe gate | `L3189–3193` | must run **after** the binding loop (§7 R6) |
| `build_arg_parser` | `L3194–3306` | reads `SECTION_TOOLS` at `:3205`; pinned by `tests/test_tool_registry.py:151` |
| `if __name__ == "__main__":` | `L3309–3407` | pinned by `tests/test_clean_shutdown_noise.py:242-248` |

### 2.4 `tool_runtime.py` — contents

```python
"""THE one home for what a tool body reaches for beyond its own arguments.

Every tool body resolves its dependencies as ``rt.<name>`` against THIS module at
call time, from whichever ``tool_sections`` module it lives in. That is what gives
the whole 94-tool surface exactly one patchable home (``tests/conftest.py``'s
``patched_server``), instead of one per section module.

Three kinds of thing live here and nothing else:
  * the stateful singletons a tool drives,
  * the tuned knobs read at call time,
  * the three guards that enforce those knobs.

It imports no ``mcp``, no ``registry``, no ``server`` — it is a leaf, so it is safe
to import from every section module and from ``server.py`` alike.
"""
```

Contents (all moved verbatim from `server.py`):

* **Constructed singletons** (from `L325–328`): `browser_manager`, `network_interceptor`,
  `dom_handler`, `cdp_function_executor`.
* **Re-bound module singletons** (imported today at `L17–51`): `cdp_element_cloner`,
  `file_based_element_cloner`, `progressive_element_cloner`, `dynamic_hook_ai`,
  `dynamic_hook_system`, `in_memory_storage`, `response_handler`, `debug_logger`,
  `process_cleanup`, `clone_storage` (a module object), `display_context` (a module object).
* **Knobs** (from `L67–78`): `CDP_OPERATION_TIMEOUT`, `MAX_TIMEOUT_MS`,
  `EXECUTE_SCRIPT_TIMEOUT`, `MAX_USER_SCRIPT_BYTES`.
* **Guards** (from `L83–152`): `_BLOCKING_SCRIPT_PATTERNS`, `_script_rejection_reason`,
  `_clamp_timeout`, `_with_cdp_timeout`.

`_with_cdp_timeout` reads `CDP_OPERATION_TIMEOUT` from *its own* module globals
(`server.py:143` today), so co-locating it with the knob keeps the read patchable at the
one home. This is the reason the knobs are here and not in a separate `cdp_timeout.py`.

**Rejected alternative — put `_with_cdp_timeout` in `tool_errors.py`.** It raises
`ToolError`, so the fit looks natural, but `tool_errors.py:32-42` and its `_require_landing_ok`
docstring (`tool_errors.py:162-165`) state three times over that the module imports neither
`server` nor `settings` — that is precisely why `_require_landing_ok` takes `timeout` as a
parameter instead of reading `CDP_OPERATION_TIMEOUT`. Moving a settings-reading helper in
there would break a stated contract to save one file.

**Rejected alternative — three leaves (`tool_singletons.py`, `cdp_timeout.py`,
`script_guard.py`).** It would spread the patch seam across three modules and force
`patched_server` to grow a name → module routing table, i.e. a second way to answer "where
do I patch this" (CLAUDE.md convention 4).

### 2.5 `tool_sections/__init__.py` — the contract and the module set

```python
"""One module per ``SECTION_TOOLS`` section. The contract, stated once:

1. A section module exports exactly two names: ``SECTION`` (the section string) and
   ``TOOLS`` (a tuple of the section's tool functions, in surface order).
2. A section module NEVER applies ``@section_tool`` or any ``mcp.*`` decorator, and
   never imports ``mcp``, ``registry`` or ``server``. Registration is DRIVEN from
   ``server.py``'s module body, once per execution of it — see server.py's binding
   loop and DESIGN §8. A decorator here would register into the FIRST execution's
   ``mcp`` only, leaving the runpy ``__main__`` load with a zero-tool app.
3. Every singleton and knob is resolved as ``rt.<name>`` at call time. A
   ``from ...tool_runtime import browser_manager`` binding would create a second
   patchable home and silently defeat ``tests/conftest.py``'s ``patched_server``.
4. Tools raise ``tool_errors.ToolError`` on failure (DESIGN §9). Unchanged by the move.
"""

from stealth_chrome_devtools_mcp.embedded.tool_sections import (
    browser_management,
    cdp_functions,
    cookies_storage,
    debugging,
    dynamic_hooks,
    element_extraction,
    element_interaction,
    file_extraction,
    network_debugging,
    progressive_cloning,
    tabs,
)

#: THE one enumeration of the section modules. server.py's binding loop walks this;
#: the source-scanning guards derive their file set from it (see
#: tests/test_cdp_timeout.py). Adding a section module means adding it here — and
#: the 94-count tripwire fires if you forget.
SECTION_MODULES = (
    browser_management,
    element_interaction,
    network_debugging,
    cookies_storage,
    debugging,
    tabs,
    element_extraction,
    progressive_cloning,
    file_extraction,
    cdp_functions,
    dynamic_hooks,
)
```

### 2.6 Shape of a section module (this is `cookies_storage.py`, verbatim-derivable from `server.py:1432-1523`)

```python
"""The ``cookies-storage`` tools. See ``tool_sections/__init__.py`` for the contract."""

from __future__ import annotations

from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError, _require_tab

SECTION = "cookies-storage"


async def get_cookies(
    instance_id: str, urls: list[str] | None = None
) -> list[dict[str, Any]]:
    """<docstring copied verbatim from server.py:1436-1445>"""
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.network_interceptor.get_cookies(tab, urls), instance_id=instance_id
    )


async def set_cookie(...) -> bool:      # server.py:1453-1505, bodies unchanged except rt.*
    ...


async def clear_cookies(instance_id: str, url: str | None = None) -> bool:
    ...


TOOLS = (get_cookies, set_cookie, clear_cookies)
```

The **only** edits to a moved body are `browser_manager` → `rt.browser_manager`,
`network_interceptor` → `rt.network_interceptor`, `_with_cdp_timeout` → `rt._with_cdp_timeout`,
and the removal of the `@section_tool(...)` line. `ToolError` / `_require_tab` are imported
directly because `tool_errors` is a stateless leaf that no test patches.

---

## 3. The singleton-resolution design (the hard part)

### 3.1 The mechanism

A module attribute is looked up **at call time**, so `rt.browser_manager` inside a tool body
resolves against `tool_runtime`'s `__dict__` on every invocation. `monkeypatch.setattr(
tool_runtime, "browser_manager", fake)` therefore reaches every one of the 94 bodies no
matter which of the 11 modules it lives in — the same property `setattr(server, …)` relies
on today, relocated to a module that does not get re-executed three times.

The `from ...tool_runtime import browser_manager` form would **not** work: it binds the
object into the section module's namespace at import time, creating an eleventh, twelfth,
… patchable home. Contract rule 3 bans it, and §5.4 specifies the AST test that enforces it.

### 3.2 The binding loop in `server.py`

```python
from stealth_chrome_devtools_mcp.embedded.tool_sections import SECTION_MODULES

# Registration is driven from HERE, once per execution of this module body, so the
# canonical import, the bare-name spec load and the runpy __main__ load each get a
# fully-populated `mcp`. A @section_tool decorator inside a section module would run
# on the FIRST import only and leave every later execution with a zero-tool app —
# the mirror image of the 282 == 3 x 94 accumulation this repo has already paid for.
# Binding into globals() is what keeps `getattr(server, tool_name)` — the mechanism
# tests/fakes.py:125 and tests/e2e_helpers.py:86 both use — working after the move.
for _section_module in SECTION_MODULES:
    for _tool in _section_module.TOOLS:
        globals()[_tool.__name__] = section_tool(_section_module.SECTION)(_tool)
```

Why `globals()[…]` and not 94 explicit `spawn_browser = section_tool(...)(...)` lines:
94 near-identical lines is ~110 LOC of boilerplate in the file whose whole purpose is to
shrink, and a forgotten line fails the same way the loop's would (the 94-count tripwire).
The cost — the names being invisible to static analysis — is paid off by the explicit
`TOOLS` tuple in each module (which is what `vulture` sees, so no tool looks dead) and by
the surface-identity test in §5.3, which asserts every registered name is a `server`
attribute.

`section_tool` already appends idempotently (`tool_registry.py:107-109`), so re-running the
loop on executions 2 and 3 leaves `SECTION_TOOLS` at 94 while creating three independent
`FunctionTool` objects, one per `mcp`. `functools.wraps` in `_surrogate_safe_returns`
(`tool_registry.py:65,71`) and in `with_correlation_id` preserves the signature FastMCP
introspects, so the wire schema is unchanged.

### 3.3 Keeping `patched_server` alive — a one-line fixture change, zero call-site changes

`tests/conftest.py:208-225` becomes:

```python
@pytest.fixture()
def patched_server(monkeypatch):
    """Swap the tool singletons for fakes and hand back the ``server`` module.

    Tool bodies resolve their dependencies as ``rt.<name>`` against
    ``embedded/tool_runtime.py`` at call time (the ONE patchable home), so the
    substitution happens there. ``server`` is still what is returned, because tool
    LOOKUP is still a ``server`` attribute read (``fakes.call_tool``).

    While the plan_SERVERSPLIT migration is in flight, ``server`` also carries
    import aliases for the not-yet-moved bodies; patching both keeps the two eras
    consistent. The ``server`` half is deleted by the closing slice.
    """
    from stealth_chrome_devtools_mcp.embedded import server, tool_runtime

    def _patch(**singletons):
        for name, obj in singletons.items():
            monkeypatch.setattr(tool_runtime, name, obj, raising=False)
            if hasattr(server, name):
                monkeypatch.setattr(server, name, obj)
        return server

    return _patch
```

All **40** `patched_server(...)` call sites are unchanged, including
`tests/test_navigation_truthfulness.py:247`'s `patched_server(..., CDP_OPERATION_TIMEOUT=0.05)`
— that knob lives in `tool_runtime` too, and `_with_cdp_timeout` reads it from there.

The dual-patch is a deliberate, time-boxed second way, confined to **one fixture** with a
stated expiry (Slice 12). It is guarded by the identity pin in §5.5, which fails the moment
the two homes could diverge.

### 3.4 The direct-setattr sites and `app_lifespan`

Twelve sites patch `server` directly, outside the fixture. Slice 0 handles all of them:

| Site | Names | Slice-0 action |
|---|---|---|
| `tests/test_lifespan_reentrancy.py:83,84,85,86` | `browser_manager`, `process_cleanup`, `in_memory_storage`, `clone_storage` | route through `patched_server(...)` |
| `tests/test_lifespan_reentrancy.py:89,98,118,135` | `_LIFESPAN_STARTED`, `_SERVE_TRANSPORT` | **unchanged** — these stay on `server` |
| `tests/test_server_network_tools.py:29` | `network_interceptor` | route through `patched_server(...)` |
| `tests/test_cdp_command_normalization.py:195` | `browser_manager` | route through `patched_server(...)` |
| `tests/test_execute_script_deep_values.py:143` | `browser_manager` | route through `patched_server(...)` |
| `tests/test_execute_script_return_wrap.py:138` | `browser_manager` | route through `patched_server(...)` |
| `tests/test_tool_errors.py:123` | `browser_manager` | route through `patched_server(...)` |

`app_lifespan` (`server.py:230-301`) reads `browser_manager` (`:247,266,270`),
`process_cleanup` (`:246,278`), `clone_storage` (`:251`) and `in_memory_storage` (`:285`).
It stays in `server.py`, but **its four reads must become `rt.*`** in Slice 0 — otherwise
`patched_server(browser_manager=spy)` would substitute the tool bodies' manager while the
lifespan kept driving the real one. That is a six-line diff and it is what makes the
`test_lifespan_reentrancy` re-route above correct.

### 3.5 Name-level test imports that must be re-pointed

Three files import helpers *by name* from `server`:

| Site | Names | Action |
|---|---|---|
| `tests/test_cdp_timeout.py:20-25` | `CDP_OPERATION_TIMEOUT`, `MAX_TIMEOUT_MS`, `_clamp_timeout`, `_with_cdp_timeout` | re-point to `embedded.tool_runtime` |
| `tests/test_execute_script_guard.py:13-17` | `EXECUTE_SCRIPT_TIMEOUT`, `MAX_USER_SCRIPT_BYTES`, `_script_rejection_reason` | re-point to `embedded.tool_runtime` |
| `tests/test_error_typing.py:27` | `_with_cdp_timeout` | re-point to `embedded.tool_runtime` |

`tests/test_observability.py:328,345,368,490,541` reaches these through `w15_server.<name>`
(module-attribute), so `server.py`'s import aliases keep it green through the migration; it
is re-pointed in Slice 12 when the aliases go.

---

## 4. Migration order — 13 reviewable slices

One commit per slice. Every slice is independently green on the full battery in §5.
Sections are ordered smallest-and-most-isolated first, so the mechanism is proven on 3
tools before it is trusted with 452 lines of `element_interaction`.

### 4.1 Slice 0 — scaffolding, **zero tools moved**

This is the mechanism slice and deserves its own review.

1. Create `embedded/tool_runtime.py` with the contents in §2.4. `_BLOCKING_SCRIPT_PATTERNS`,
   `_script_rejection_reason`, `_clamp_timeout`, `_with_cdp_timeout` and the four knobs move
   **verbatim** from `server.py:67-152`; the four singleton constructions move verbatim from
   `server.py:325-328`.
2. Create `embedded/tool_sections/__init__.py` with the contract docstring and
   `SECTION_MODULES = ()` (empty tuple — no section modules exist yet).
3. `server.py`: add the binding loop from §3.2 (it iterates an empty tuple, a no-op), and
   replace the deleted definitions with a `from stealth_chrome_devtools_mcp.embedded.tool_runtime
   import (...)` alias block covering every name the 94 still-in-file bodies use. Place the
   loop **before** the xpool-safe gate at `L3189-3193`.
4. `server.py`: re-point `app_lifespan`'s four singleton reads to `rt.*` (§3.4).
5. `tests/conftest.py`: the dual-patch fixture (§3.3); re-route the 7 direct-setattr sites
   in §3.4; re-point the 3 name-level imports in §3.5.
6. **Widen the four source-scanning guards** (§1.4). Each derives its file set from one
   helper, added once:

   ```python
   # tests/<one home, e.g. tests/source_scan.py or an existing helper module>
   def tool_source_files() -> list[Path]:
       """Every file that can hold a tool body: server.py, tool_runtime.py and the
       section modules. DERIVED from SECTION_MODULES, so a new section module cannot
       escape the source-level guards by being forgotten here."""
       from stealth_chrome_devtools_mcp.embedded import server, tool_runtime
       from stealth_chrome_devtools_mcp.embedded.tool_sections import SECTION_MODULES

       files = [Path(server.__file__), Path(tool_runtime.__file__)]
       files += [Path(m.__file__) for m in SECTION_MODULES]
       assert len(files) >= 2, "the tool-source set collapsed — the guards would pass vacuously"
       return files
   ```

   `tests/test_cdp_timeout.py:461-468` becomes "`_with_cdp_timeout` is defined exactly once
   **across the set**" (it will be in `tool_runtime.py`), and `:471-486`'s
   `asyncio.wait_for`-marker scan iterates the set. Same for
   `tests/test_server_call_conventions.py:23` and `tests/test_log_hygiene.py:184`.
   The `>= 2` assertion (raised to `>= 12` in Slice 11) is what converts "the guard went
   vacuous" from a silent pass into a red test.
7. Add `tests/test_tool_module_reload.py` — the double-load detector (§5.1).
8. Add the surface-identity test (§5.3), the section-module contract test (§5.4) and the
   alias-identity pin (§5.5).
9. Take the **wire-surface snapshot baseline** (§5.2) and commit it as
   `tests/goldens/tool_surface.json`. This is a HARD golden: it must not change in any
   slice of this plan.

Net effect on `server.py`: −91 LOC of definitions, +~30 LOC of alias imports, binding loop
and comment ⇒ roughly **−60**. Because the file has only 4 lines of headroom
(3407 / 3411, `check_file_budgets.py:47-50`), Slice 0 **must** be net-negative — if the
measured actual comes out above 3407, stop and shrink rather than seeking a cap raise
(`CONTRIBUTING.md`: "Never pad a cap").

### 4.2 Slices 1–11 — one section each

| Slice | Module | Tools | Body LOC | Why this position |
|---|---|---|---|---|
| 1 | `cookies_storage.py` | 3 | 94 | smallest section that still exercises the full shared-dep set (`browser_manager`, `network_interceptor`, `_require_tab`, `_with_cdp_timeout`, `ToolError`) |
| 2 | `tabs.py` | 5 | 103 | first use of `_require_browser`, `_require_landing_ok` and a **direct** `CDP_OPERATION_TIMEOUT` read (`server.py:1762,1767`) — proves the knob is patchable from a section module |
| 3 | `debugging.py` | 5 | 128 | first **stray relocation** (`validate_browser_environment_tool`, `L2127-2146`) and first `inspect.getsource` retarget (`tests/test_observability.py:1017`) |
| 4 | `dynamic_hooks.py` | 10 | 174 | the only section with **sync** tools (`server.py:3132,3143,3154,3165,3176`) — proves `_surrogate_safe_returns`' sync branch (`tool_registry.py:71-75`) survives the move. Zero shared deps beyond `dynamic_hook_ai` |
| 5 | `progressive_cloning.py` | 10 | 177 | first golden-backed section (`tests/goldens/progressive_expand_styles.json`, `progressive_list_stored_elements.json`) — goldens must not move |
| 6 | `network_debugging.py` | 10 | 262 + 7 | `_CAPTURE_OFF_NOTE` (`L1215-1221`) moves with it; heaviest raw-`.fn` test file (15 sites in `tests/test_server_network_tools.py`) |
| 7 | `file_extraction.py` | 9 | 278 | goldens `file_based_structure_to_file.json`, `extract_element_structure_list_convert.json` |
| 8 | `element_extraction.py` | 9 | 360 | second stray (`extract_complete_element_cdp`, `L2391-2427`); goldens `extract_element_styles.json`, `cdp_complete_element.json`, `canonical_engine.json` |
| 9 | `cdp_functions.py` | 13 | 376 | the section the xpool-safe gate disables (`server.py:3189-3191`) — verify `--xpool-safe` and `--disable-cdp-functions` still remove exactly 13 tools |
| 10 | `browser_management.py` | 8 | 377 | `spawn_browser` (`L334-482`, the largest single tool), the F-808 display guard, `clone_storage` reads |
| 11 | `element_interaction.py` | 12 | 452 | largest; `execute_script` + the script guard (`server.py:1029,1035`); raise `tool_source_files()`'s floor assertion to `>= 13` |

Each slice's mechanical recipe is identical:

1. Create `tool_sections/<module>.py`; move each tool body verbatim, dropping the
   `@section_tool(...)` line and rewriting bare singleton/knob/helper reads to `rt.<name>`.
   Keep docstrings byte-identical — FastMCP surfaces them as the tool description, and the
   §5.2 snapshot will catch any drift.
2. Append `SECTION = "<section>"` and `TOOLS = (...)` in the section's current surface order
   (order determines `SECTION_TOOLS[section]`'s list order, which `release_evidence.registry_sections()`
   sorts at `tools/release_evidence.py:982` — but keep it faithful anyway).
3. Add the module to `tool_sections/__init__.py`'s import block and to `SECTION_MODULES`.
4. Delete the moved lines from `server.py`.
5. Prune any `server.py` import that no longer has a consumer (`ruff` will flag it).
6. Re-point any test that name-imported something that just moved (grep the moved symbol
   names across `tests/` before committing).
7. Ratchet `server.py`'s cap to the newly measured actual (§6.1).
8. Run the full battery (§5).

### 4.3 Slice 12 — closing

1. Delete `server.py`'s `tool_runtime` alias block (nothing in `server.py` reads the
   singletons any more except `app_lifespan`, which uses `rt.*`). Keep only the aliases that
   have a named consumer — notably `clone_storage`, pinned by
   `tests/test_clone_storage.py:89` (`assert server.clone_storage is clone_storage`), and
   `SECTION_TOOLS`, pinned by `tests/test_doc_claims.py:235`.
2. Delete the `if hasattr(server, name)` half of `patched_server` (§3.3) and the
   alias-identity pin (§5.5) — both existed only for the migration.
3. Re-point `tests/test_observability.py`'s `w15_server.<helper>` reads to `tool_runtime`.
4. Add `assert not hasattr(server, "browser_manager")` (and the other three constructed
   singletons) to the surface test — converting "there is one home" from a convention into
   a guard.
5. **Remove `"embedded/server.py"` from `GRANDFATHER`** in `tools/check_file_budgets.py:47-50`;
   the file is now governed by the 1000-LOC default.
6. Docs, same commit:
   * `CLAUDE.md` navigation map — add `tool_runtime.py` and a `tool_sections/` row; update
     the "Tool count = 94" paragraph to say the count derives from `SECTION_TOOLS`, filled
     by `server.py`'s binding loop over `SECTION_MODULES`.
   * `DESIGN.md` §8 — amend the "no embedded module imports server" paragraph with the
     section-module corollary: *a module that holds tool bodies must not register them either*.
   * `CONTRIBUTING.md` — "adding a tool" now means adding a function to a section module and
     to its `TOOLS` tuple.
   * `RELEASE_CONTRACT.md` — regenerate (`tools/gen_release_contract.py --write`), same commit,
     per `CONTRIBUTING.md`.

---

## 5. Verification battery

### 5.0 Per slice, in order (stop at the first red)

```powershell
# from the repo root, with the project venv
.venv\Scripts\python.exe tools\check_file_budgets.py
.venv\Scripts\python.exe -m ruff format --check
.venv\Scripts\python.exe -m ruff check
.venv\Scripts\python.exe tools\check_suppression_owners.py
.venv\Scripts\python.exe -m vulture src\stealth_chrome_devtools_mcp\ tools\vulture_allowlist.py
.venv\Scripts\python.exe -m ty check --exit-zero-on-warning src\stealth_chrome_devtools_mcp\

# the FULL hermetic lane — never a narrow selector. Two reasons: the count,
# idempotency and reload tests live in four different files, and a narrow pytest
# selector in this repo loses the Chrome-warmup fixtures.
.venv\Scripts\python.exe -m pytest -m "not integration" -q

# the __main__ path end to end, as a real subprocess
.venv\Scripts\python.exe -m stealth_chrome_devtools_mcp --transport http --list-sections
#   -> must print "Total: 94 tools"

# the generated contract must not have drifted
$env:PYTHONUTF8=1; .venv\Scripts\python.exe tools\gen_release_contract.py --check
```

Then, per slice: the **wire-surface snapshot** (§5.2). Then the transport lane
(`pytest -m transport -q`) on every slice, and the integration lane
(`pytest -m integration -q --timeout=120`) on slices 2, 6, 8, 10 and 11 at minimum, plus
Slice 12. Before any integration run, count live Chrome processes
(`(Get-Process chrome -ErrorAction SilentlyContinue).Count`) — a fleet-exhausted machine
produces spawn failures that read exactly like product bugs.

### 5.1 The double-load detector — `tests/test_tool_module_reload.py` (new, Slice 0)

This is the test the brief asks for. It detects **both** failure modes explicitly.

```python
"""The runpy/spec double-load must leave every loaded identity with a full 94-tool
app, and must not accumulate in the shared SECTION_TOOLS map.

Two failure modes, opposite in shape, both fatal, and only one of them is visible to
the count tripwire:

  * 3x ACCUMULATION (the historical bug): SECTION_TOOLS grows to 282 == 3 x 94
    because the map is shared across executions. Cured by the idempotent append at
    tool_registry.py:107-109; this test keeps it cured.

  * 0x REGISTRATION (the hazard the plan_SERVERSPLIT split introduces): a section
    module decorates at ITS OWN module scope, so it registers into the FIRST
    execution's `mcp` and never runs again. SECTION_TOOLS still says 94, so the
    count tests stay green — but the second and third identities carry an EMPTY
    app, and tests/e2e_helpers.py's getattr lookups silently skip the whole E2E
    tier. Only the per-identity assertions below see it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import server as canonical
from stealth_chrome_devtools_mcp.embedded import tool_registry

EXPECTED_TOOL_COUNT = 94


def _exec_copy(alias: str):
    """Execute embedded/server.py's body again under a fresh module identity —
    exactly what tests/e2e_helpers.py:29-44 does to create the bare-name `server`."""
    spec = importlib.util.spec_from_file_location(alias, Path(canonical.__file__))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def three_identities():
    """The canonical import plus two more executions == the 3x load that produced 282."""
    saved = {a: sys.modules.get(a) for a in ("server", "_split_reload_probe")}
    try:
        yield (canonical, _exec_copy("server"), _exec_copy("_split_reload_probe"))
    finally:
        for alias, previous in saved.items():
            if previous is None:
                sys.modules.pop(alias, None)
            else:
                sys.modules[alias] = previous


def test_section_tools_does_not_accumulate_across_loads(three_identities):
    total = sum(len(v) for v in tool_registry.SECTION_TOOLS.values())
    assert total == EXPECTED_TOOL_COUNT, (
        f"{total} tools in SECTION_TOOLS after three executions "
        f"({total / EXPECTED_TOOL_COUNT:.0f}x registration)"
    )
    for section, names in tool_registry.SECTION_TOOLS.items():
        assert len(names) == len(set(names)), f"duplicate names in {section}: {names}"


async def test_every_loaded_identity_owns_a_full_app(three_identities):
    for module in three_identities:
        tools = await module.mcp.get_tools()
        assert len(tools) == EXPECTED_TOOL_COUNT, (
            f"{module.__name__}'s FastMCP app has {len(tools)} tools, not "
            f"{EXPECTED_TOOL_COUNT}. A section module that decorates at its own "
            f"module scope registers into the first execution only."
        )


def test_every_registered_name_is_an_attribute_of_every_identity(three_identities):
    expected = {n for names in tool_registry.SECTION_TOOLS.values() for n in names}
    for module in three_identities:
        missing = sorted(n for n in expected if not hasattr(module, n))
        assert not missing, (
            f"{module.__name__} is missing {missing} — tests/fakes.py:125 and "
            f"tests/e2e_helpers.py:86 both reach tools by getattr on this module."
        )


def test_the_apps_are_independent(three_identities):
    apps = [m.mcp for m in three_identities]
    assert len({id(a) for a in apps}) == len(apps), (
        "each execution must build its own FastMCP app; a shared one means the "
        "runpy __main__ load is serving another identity's registrations"
    )
```

Manual one-liner for the same check, useful mid-slice:

```powershell
.venv\Scripts\python.exe -c "import asyncio,importlib.util,sys;from pathlib import Path;from stealth_chrome_devtools_mcp.embedded import server as c, tool_registry as r;
def x(a):
 s=importlib.util.spec_from_file_location(a,Path(c.__file__));m=importlib.util.module_from_spec(s);sys.modules[a]=m;s.loader.exec_module(m);return m
ms=[c,x('server'),x('probe')];print('SECTION_TOOLS:',sum(len(v) for v in r.SECTION_TOOLS.values()));print('per-app:',[len(asyncio.run(m.mcp.get_tools())) for m in ms])"
# healthy:  SECTION_TOOLS: 94   per-app: [94, 94, 94]
# 3x bug:   SECTION_TOOLS: 282  per-app: [94, 94, 94]
# 0x bug:   SECTION_TOOLS: 94   per-app: [94, 0, 0]
```

### 5.2 The wire-surface snapshot (the strongest single check)

A pure move must not change one byte of what a client sees. Slice 0 writes the baseline;
every later slice diffs against it.

```python
# tools/dump_tool_surface.py  (new, Slice 0)
"""Dump the served tool surface — names, descriptions and input schemas — so a
refactor that claims to change nothing can be held to it."""
import asyncio, json, sys
from stealth_chrome_devtools_mcp.embedded import server

async def main() -> int:
    tools = await server.mcp.get_tools()
    # Verified against the pinned fastmcp 2.11.2: Tool.model_fields ==
    # name/title/description/tags/meta/enabled/parameters/output_schema/
    # annotations/serializer. `parameters` is the input JSON schema FastMCP
    # derives from the signature, which is exactly what a docstring or
    # annotation lost in a copy-paste would change.
    surface = {
        name: {
            "description": tool.description,
            "input_schema": tool.parameters,
            "output_schema": tool.output_schema,
            "tags": sorted(tool.tags or ()),
            "enabled": tool.enabled,
        }
        for name, tool in sorted(tools.items())
    }
    json.dump(surface, sys.stdout, indent=2, sort_keys=True)
    return 0

sys.exit(asyncio.run(main()))
```

Per slice:

```powershell
.venv\Scripts\python.exe tools\dump_tool_surface.py > surface_after.json
.venv\Scripts\python.exe -c "import json,sys;a=json.load(open('tests/goldens/tool_surface.json'));b=json.load(open('surface_after.json'));print('IDENTICAL' if a==b else 'DRIFT: '+str(sorted(set(a)^set(b)) or [k for k in a if a[k]!=b.get(k)]))"
```

`tests/goldens/tool_surface.json` is a **HARD** golden for the duration of this plan
(`CONTRIBUTING.md` golden discipline): a diff during any slice is a real regression, never
something to regenerate. It is the check that catches a docstring lost in a copy-paste, a
type annotation that changed meaning when `from __future__ import annotations` was added to
a section module, or a tool that silently failed to register.

### 5.3 Surface-identity test (new, Slice 0)

```python
def test_every_registered_tool_is_a_server_attribute():
    """The binding loop's contract. tests/fakes.py:125 and tests/e2e_helpers.py:86
    both reach a tool by getattr on the server module, so a registered name that is
    not bound there is invisible to the entire hermetic and E2E tiers."""
    from stealth_chrome_devtools_mcp.embedded import server
    from stealth_chrome_devtools_mcp.embedded.tool_registry import SECTION_TOOLS

    for section, names in SECTION_TOOLS.items():
        for name in names:
            bound = getattr(server, name, None)
            assert bound is not None, f"{section}/{name} is registered but not bound"
            assert hasattr(bound, "fn"), f"{name} is not a FastMCP tool object"
```

### 5.4 Section-module contract test (new, Slice 0)

```python
def test_section_modules_never_register_and_never_rebind_singletons():
    """Contract rules 2 and 3 (tool_sections/__init__.py), enforced by AST.

    Rule 2 keeps the runpy __main__ load from getting a zero-tool app (§1.2).
    Rule 3 keeps tool_runtime the ONE patchable home (§3.1): a
    `from ...tool_runtime import browser_manager` binds at import time and would
    make tests/conftest.py's patched_server a silent no-op for that module.
    """
    import ast
    from pathlib import Path
    from stealth_chrome_devtools_mcp.embedded.tool_sections import SECTION_MODULES

    assert SECTION_MODULES, "no section modules — this guard would pass vacuously"
    for module in SECTION_MODULES:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        src = ast.dump(tree)
        assert "section_tool" not in src, f"{module.__name__} decorates its own tools"
        assert "mcp" not in {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }, f"{module.__name__} reaches for the FastMCP app"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith(
                "tool_runtime"
            ):
                raise AssertionError(
                    f"{module.__name__} binds {[a.name for a in node.names]} from "
                    "tool_runtime at import time; use `import tool_runtime as rt` "
                    "and resolve `rt.<name>` at call time"
                )
        assert isinstance(module.SECTION, str) and module.TOOLS
```

Plus: `{m.SECTION for m in SECTION_MODULES} == set(SECTION_TOOLS)` and
`sum(len(m.TOOLS) for m in SECTION_MODULES) == 94` — both derived, neither typed.

### 5.5 Alias-identity pin (Slice 0, deleted in Slice 12)

```python
@pytest.mark.parametrize("name", MIGRATION_ALIASES)  # the tool_runtime names server.py re-exports
def test_server_alias_is_the_tool_runtime_object(name):
    """While bodies live in both places, the two homes must be the same object —
    otherwise patched_server's dual-patch is hiding a divergence."""
    from stealth_chrome_devtools_mcp.embedded import server, tool_runtime
    assert getattr(server, name) is getattr(tool_runtime, name)
```

### 5.6 Existing tests that must stay green untouched (the regression net)

* `tests/test_tool_registry.py:79-94` — idempotency.
* `tests/test_tool_registry.py:135-176` — `TestCountTripwire`: live app count == section sum
  == 94; parser description derived; `--list-sections` subprocess total.
* `tests/test_tool_dispatch.py:30,100,106,134,160,171` — 94 and 94 − 5 sync.
* `tests/test_doc_claims.py:232-240` — docs say 94; `tool_registry.SECTION_TOOLS is server.SECTION_TOOLS`.
* `tests/test_mcp_protocol_surface.py:49-56` — cross-checks the `.fn` seam against the real
  protocol result. This is the one test that would catch the `.fn` seam and the wire
  diverging, which memory records as a real phenomenon in this repo.
* `tests/test_e2e_functions_hooks.py:476-479` — the E2E coverage partition, `len(all_names) == 94`.
* `tests/test_release_evidence.py:895-899` + `tools/release_evidence.py:970-983`.
* `tests/test_proxy_sentry_reporting.py:171-194` — `main()` twice, `loaded == ["__main__", "__main__"]`.
* `tests/test_backend_logging.py:122-129` — double-load logging idempotency.
* `tests/test_clone_storage.py:74-89` and `tests/test_hot_reload_removed.py:24,29` — the
  negative-surface assertions. Note these get *easier* to satisfy after the split, which is
  exactly why §4.3 step 4 adds new ones rather than relying on them.

---

## 6. LOC-cap bookkeeping

### 6.1 `server.py`'s ratchet, per slice

The rule from `tools/check_file_budgets.py:16-21` and `CONTRIBUTING.md` is absolute: **cap ==
measured post-`ruff format` actual, never padded, ratcheting DOWN only.** Measure with
`(Get-Content <file>).Count` after `ruff format`, then write that exact number into
`GRANDFATHER` in the same commit.

| After slice | Removed | Projected `server.py` actual → cap |
|---|---|---|
| — (today) | — | 3407 / **3411** |
| 0 | helpers, knobs, singleton construction; +alias block, +binding loop | ~3350 |
| 1 cookies-storage | 94 | ~3256 |
| 2 tabs | 103 | ~3153 |
| 3 debugging | 128 | ~3025 |
| 4 dynamic-hooks | 174 | ~2851 |
| 5 progressive-cloning | 177 | ~2674 |
| 6 network-debugging | 269 | ~2405 |
| 7 file-extraction | 278 | ~2127 |
| 8 element-extraction | 360 | ~1767 |
| 9 cdp-functions | 376 | ~1391 |
| 10 browser-management | 377 | ~1014 |
| 11 element-interaction | 452 | ~562 |
| 12 closing | alias block | **~545 — row DELETED from `GRANDFATHER`** |

Every number after "today" is a projection; the committed value is whatever the file
actually measures. Note that `server.py` is still above 1000 after Slice 10, so the
`GRANDFATHER` row survives until Slice 11 and is removed in Slice 12.

The `GRANDFATHER` comment block for `"embedded/server.py"` (`check_file_budgets.py:16-50`)
gets one appended paragraph per slice in the established style — plan id, what moved, and
the statement that the cap is the measured actual — until the row is deleted.

### 6.2 The new modules — a deliberate deviation from the brief

The brief asks for `cap == actual` `GRANDFATHER` rows for the new modules. **Recommend
against**, and here is why: `GRANDFATHER` exists for files that exceed the 1000-LOC default
and are permitted to stay over it (`check_file_budgets.py:14,132-139`). Every new module
lands between ~125 and ~490 LOC, comfortably inside the default. Freezing a 125-line
`cookies_storage.py` at 125 would forbid a one-line bug fix without a human gate ruling —
friction with no benefit, and it introduces a second policy for the same question ("how big
may this file be?"), which is the defect CLAUDE.md convention 4 names.

The §2.2 table records each module's measured actual so that, if the team lead still wants
the rows, they can be written from it in Slice 12 with no further measurement. Flagging this
rather than silently doing either.

---

## 7. Risks

**R1 — 0× registration on the second and third loads.** The hazard this split creates
(§1.2), invisible to the 94-count tripwire. *Mitigation:* contract rule 2, the AST guard
(§5.4), and the per-identity assertions in `test_tool_module_reload.py` (§5.1). Severity:
would silently disable the entire E2E tier.

**R2 — Patch-seam divergence during the migration.** Two patchable homes exist between
Slices 0 and 12. *Mitigation:* the dual-patch confined to one fixture (§3.3), the
alias-identity pin (§5.5), and the `app_lifespan` re-point (§3.4) that removes the one place
where a stale alias would actually be used. Both the dual-patch and the pin are deleted in
Slice 12 — the migration's second way has a written expiry.

**R3 — Source-scanning guards going vacuous.** The nastiest failure mode in this plan,
because a guard that scans a file the code has left does not fail; it passes over an emptier
file. Four such guards (§1.4). *Mitigation:* Slice 0 converts all four to iterate a derived
file set carrying a non-empty floor assertion, raised to `>= 13` in Slice 11. Do this in
Slice 0, before any body moves — after the fact you cannot tell a vacuous pass from a real one.

**R4 — One `BrowserManager` per process instead of one per execution.** Moving
`BrowserManager()` from `server.py:325` into `tool_runtime` means the three identities share
one instance rather than holding three.
`tests/test_browser_integration.py:25-36` documents the current per-exec construction as
*safe*, not as *required*, so this is a permitted change — but it is a real behavior change
and must be verified, not assumed. `BrowserManager.start_idle_reaper` is already idempotent
(`browser_manager.py:406-407` returns early on a live task), which is the main axis of
concern; `stop_idle_reaper` (`:417-432`) is likewise. *Mitigation:* Slice 0 runs the
integration and E2E lanes, and `tests/test_lifespan_reentrancy.py` is re-routed (§3.4) so it
exercises the new topology.

**R5 — Concurrent work on the same tree.** Two sibling agents hold worktrees on
`fix/F844-F845-tab-state` (`browser_manager.py`) and `fix/F843-fast-death-heal`
(`proxy_selfheal.py`). This plan touches **neither file**, so there is no textual conflict —
but `server.py` has only 4 lines of headroom, so *any* concurrent line added to `server.py`
collides with Slice 0's budget. Start Slice 0 only from a tree rebased onto their merges,
and confirm `server.py`'s actual LOC at that base before writing the first cap.

**R6 — Registration order versus the xpool-safe gate.** `server.py:3189-3191` mutates
`DISABLED_SECTIONS` and calls `apply_disabled_sections()` at module scope. If the binding
loop ran *after* it, the 13 `cdp-functions` tools would register into a gate that had
already closed. *Mitigation:* the loop is placed immediately after the singleton/registry
block and well before `L3189`; Slice 9's verification explicitly runs
`--xpool-safe` and `--disable-cdp-functions` and asserts the served count drops by exactly 13.

**R7 — `get_cookies`' known `.fn`-seam quirk.** This repo has a recorded case of `get_cookies`
hanging on the in-process `.fn` seam while working over real stdio. Slice 1 is
`cookies-storage`. *Mitigation:* record the pre-slice status of every cookies test on the
base commit before moving anything, so a red in Slice 1 can be attributed rather than guessed
at; and confirm the slice over the transport lane, not only the `.fn` seam.

**R8 — Chrome exhaustion masquerading as a product bug.** Integration lanes run repeatedly
across 13 slices on a machine that also hosts a multi-agent fleet. *Mitigation:* count Chrome
processes before each integration run and run a known-good control if a spawn fails.

**R9 — `globals()[…]` tripping a lint rule or `ty`.** Not expected in the curated ruleset, but
unverified. *Mitigation:* Slice 0 runs `ruff check` and `ty` with the loop present and an
empty `SECTION_MODULES`, so any lint objection surfaces before a single tool has moved.

---

## 8. Non-goals

1. **No tool renames.** `tool_registry.py:12-22` anticipates that renames happen "at the Ph2
   per-section move" — i.e. here. This plan explicitly declines that. Bundling renames with
   the move destroys the one verification that makes the move safe (§5.2's byte-identical
   wire surface), and the package has real third-party installs on PyPI, so a rename is a
   breaking change that needs its own deprecation story. Renames should be a separate plan
   executed *after* this one, when each section is a small file that can be renamed and
   reviewed on its own.
2. **No behavior changes.** No error-convention conversions, no new `ToolError` raises, no
   payload-shape edits, no docstring rewording. If a defect is noticed mid-slice, record it
   as a finding and fix it in a separate commit — a behavior change inside a "pure move"
   slice makes the surface snapshot uninterpretable.
3. **No new tools, no removed tools.** The count stays 94 at every commit.
4. **`app_lifespan`, the four `@mcp.resource` handlers, `build_arg_parser` and the `__main__`
   block stay in `server.py`.** They are per-execution `mcp` concerns; moving them would
   re-open exactly the hazard §1.2 describes.
5. **No changes to `singleton.py`, `proxy_selfheal.py`, `backend_registry.py` or
   `browser_manager.py`.** The proxy/backend lifecycle is out of scope and is under
   concurrent edit (R5).
6. **No change to `tool_registry.py`'s idempotent append.** It stays as the belt to the
   binding loop's braces.
7. **No `GRANDFATHER` rows for the new modules** — see §6.2 for the reasoning and the escape
   hatch if the team lead disagrees.

---

## Execution log — slices 1-3 (branch `refactor/serversplit-slices-1-3`, based on `da087f9` = slice 0 on `main`)

**Status:** slices 1, 2 and 3 EXECUTED, one commit each, each independently green on
the full §5 battery before the next began. 13 of 94 bodies moved; `server.py`
**3342 → 3014** LOC (cap ratcheted DOWN to the measured actual in every commit).

| Slice | Module | Tools | `server.py` before → after | Plan projected | Also removed |
|---|---|---|---|---|---|
| 1 | `cookies_storage.py` | 3 | 3342 → **3248** | ~3256 | — |
| 2 | `tabs.py` | 5 | 3248 → **3144** | ~3153 | the now-unused `_require_browser` import |
| 3 | `debugging.py` | 5 | 3144 → **3014** | ~3025 | the now-unused `get_platform_info` / `validate_browser_environment` imports |

### Drift between the plan and reality, and how it was resolved

| Plan says | Reality | Resolution |
|---|---|---|
| §5.4's checker asserts `"section_tool" not in ast.dump(tree)` | `ast.dump` renders string CONSTANTS, so a section module whose **docstring** narrates its own dropped decorator by name fails contract rule 2 — a false positive on prose, hit on the first module written | the section-module docstrings paraphrase ("the dropped registration decorator"); the trap is now stated in `tests/test_tool_sections_contract.py`'s module docstring so slices 4-11 do not rediscover it. The checker was NOT loosened: a name-only check would stop catching a decorator applied through an alias |
| §4.1 step 6 / §4.2 slice 11 raise `MIN_TOOL_SOURCE_FILES` once, at slice 11 (and disagree on 12 vs 13) | a floor of 2 held across ten slices would let a section module dropped from `SECTION_MODULES` read as a legitimately smaller set — precisely the R3 failure mode the floor exists to convert into a red test | the floor RATCHETS UP one per slice, the mirror of the LOC cap's ratchet down: 2 → 3 → 4 → **5** after slice 3, ending at 13. `test_the_tool_source_set_is_server_plus_tool_runtime_plus_the_sections` (`== 2 + len(SECTION_MODULES)`) and the collapse RED test both keep working unchanged |
| §4.2 slice 3 calls for an `inspect.getsource` retarget in `tests/test_observability.py` | slice 0 had already re-pointed that pin to `source_scan.tool_source_text()`; there is no `inspect.getsource` left anywhere in `tests/` or `tools/` | no test change was needed. Verified the guard is not vacuous: `"Export timeout - file too large"` now appears in `tool_sections/debugging.py` and **nowhere in `server.py`**, so slice 0's widening was load-bearing exactly here |
| §4.2 step 6 expects test re-points for the moved symbols | **zero** were needed across all three slices: no test name-imports a tool from `server`, no `patch("...server.<tool>")` string target exists, and every lookup is a module-attribute read the binding loop still satisfies | the census (`server.<tool>` reads, `from ...server import`, `patch(...)` string targets, `inspect.getsource`) is recorded here so slices 4-11 re-run it rather than assuming the same answer — `test_server_network_tools.py`'s 15 raw `.fn` sites land in slice 6 |
| §6.2 declines `GRANDFATHER` rows for the new modules, and says nothing about their **lint** profile | each module fires a subset of `server.py`'s file-wide per-file-ignore list the moment it exists (`ruff check` is repo-wide, so even an un-wired module is already checked) | one `per-file-ignores` entry per section module, owner-tagged `plan_SERVERSPLIT slice N`, listing the EXACT subset the moved lines fire and nothing more — 3 codes for `cookies_storage`, 3 for `tabs`, 8 for `debugging`, against `server.py`'s 41. Same carry-verbatim discipline as the `clone_storage.py` / `tool_runtime.py` extractions. Note `debugging.py`'s `A002`: `export_debug_logs`' `format` parameter is part of the WIRE surface and so cannot be renamed by a pure move |
| §2.5 orders `SECTION_MODULES` in canonical section order without noting the consequence | the binding loop runs BEFORE the bodies still decorated in `server.py`, so mid-migration `SECTION_TOOLS` lists the MOVED sections first (`['cookies-storage', 'debugging', 'tabs', 'browser-management', …]`) | harmless and verified: `release_evidence.registry_sections()` and `gen_release_contract` both sort, `--list-sections` only totals, and no golden pins the key order. `gen_release_contract --check` is clean at every slice. Stated in `tool_sections/__init__.py` so it is not rediscovered as a bug |
| §5.0 lists the transport lane as `pytest -m transport` | the tabs churn (`new_tab → switch_tab → close_tab → list_tabs`) has its ONLY real-transport coverage in `test_soak_stability.py`'s soak cycle, not in the canonical journey | slice 2 ran `test_e2e_transport.py` **and** `test_soak_stability.py`, one file at a time, Chrome counted before each (84-88 live processes, R8). Slice 1 ran `test_e2e_transport_cookies.py`; slice 3 re-ran the canonical journey. All green — R7's `get_cookies` `.fn`-seam quirk appeared neither on the seam nor on the wire |

### Baseline note

The unit lane on the base commit reported **2333 passed, 1 failed** —
`tests/test_close_instance_offload.py::test_loop_stays_responsive_during_stuck_kill`,
a loop-responsiveness assertion sensitive to machine load. It passes on a re-run of
its own file and passed in all three slice lanes (**2334 passed, 1 skipped** each).
Measuring the base before the first edit is what made that attributable rather than
a suspected regression.

---

## Execution log — slices 4-6 (branch `refactor/serversplit-slices-4-6`, based on `6c3b616` = slices 1-3 merged to `main`)

**Status:** slices 4, 5 and 6 EXECUTED, one commit each, each independently green on
the full §5 battery before the next began. 43 of 94 bodies moved; `server.py`
**3014 → 2391** LOC (cap ratcheted DOWN to the measured actual in every commit,
`tests/source_scan.py`'s floor ratcheted UP 5 → 8).

| Slice | Module | Tools | `server.py` before → after | Plan projected | Also moved / removed |
|---|---|---|---|---|---|
| 4 | `dynamic_hooks.py` | 10 | 3014 → **2839** | ~2851 | the now-unused `dynamic_hook_ai` alias import |
| 5 | `progressive_cloning.py` | 10 | 2839 → **2660** | ~2674 | the now-unused `progressive_element_cloner` alias import |
| 6 | `network_debugging.py` | 10 | 2660 → **2391** | ~2405 | `_CAPTURE_OFF_NOTE` moved WITH the section; no import pruned |

### Drift between the plan and reality, and how it was resolved

| Plan says | Reality | Resolution |
|---|---|---|
| §5.0 treats the unit-lane count as a stable number a slice must not lower | the lane count **DROPS BY ONE** on any slice that prunes a `tool_runtime` alias from `server.py`: §5.5's pin is parametrized over `tool_runtime.__all__` ∩ `dir(server)`, so a pruned alias deletes a parametrize case. Base 2334 → slice 4 **2333** (`dynamic_hook_ai`) → slice 5 **2332** (`progressive_element_cloner`) → slice 6 **2332** (nothing pruned) | not a regression and not to be "fixed": the pin's whole job is to shrink as the migration retires aliases, and its `test_the_alias_set_is_not_empty` floor is what stops it vanishing. Recorded here so slices 7-11 do not read a −1 as a lost test. Slices 1-3 never saw it because the imports they pruned (`_require_browser`, `platform_utils`) are not `tool_runtime` aliases |
| §5.0's battery does not mention `ty` moving | `ty` counts **rise** as bodies move: 77 (base) → 77 (slice 4) → 78 (slice 5) → 80 (slice 6). `server.py` is listed in `[tool.ty.src] exclude`; a section module is not, so annotations the exclusion hid become visible the moment a body moves | all are WARNINGS under the existing warnings-only baseline (`missing-type-argument`/`deprecated` are `warn` in `[tool.ty.rules]`) and `ty` still exits 0. Every one is WIRE surface a pure move may not touch: slice 5's `expand_children(depth_range: list)` and slice 6's two `response.dict()` / `request.dict()` pydantic-v1 calls. Narrowing them is a behaviour change (§8 non-goal 2) and belongs to a later plan; the honest record is that the split makes pre-existing debt visible rather than creating it |
| §4.2 step 6 expects test re-points for the moved symbols | **zero** again across all three slices. Census re-run per slice (`server.<tool>` reads, `from …embedded.server import …`, `patch("…server.<name>")` string targets, `inspect.getsource`, raw `.fn` calls): slice 6's `tests/test_server_network_tools.py` holds the plan's heaviest coupling — 15 `server.<tool>.fn(...)` sites plus two `server.search_network_requests.description` reads — and every one is a module-attribute read the binding loop still satisfies. There is no `patch("…server.<tool>")` string target anywhere in `tests/` | the census stays a per-slice step, not an assumption. Note slices 1-3 had already converted `test_server_network_tools.py`'s one direct `setattr(server, "network_interceptor", …)` to `patched_server`, which is why the heaviest file needed nothing here |
| §2.2 records `_CAPTURE_OFF_NOTE` as "a module constant embedded mid-section" without saying where it lands | it sat between `get_request_details` and `get_response_details` | it moves to the TOP of `network_debugging.py`, beside the three `capture_note` tools that are its only readers. The string is byte-identical; only its position changed — stated in the module docstring and in the `GRANDFATHER` note so the relocation is not mistaken for a rewrite |
| §2.5 orders `SECTION_MODULES` canonically; slices 1-3 recorded that mid-migration `SECTION_TOOLS` therefore lists the moved sections first | slice 6 moves a section that is canonically FIRST (`network-debugging`), so the `SECTION_TOOLS` key order changed again mid-migration | verified harmless a second time: `gen_release_contract --check` clean, `--list-sections` totals only, `release_evidence.registry_sections()` sorts, and the HARD wire golden is keyed by tool name. `SECTION_MODULES` is kept in canonical order (`network_debugging, cookies_storage, debugging, tabs, progressive_cloning, dynamic_hooks`) rather than landing order, so the tuple never needs re-sorting at slice 11 |
| §5.0 lists the transport lane as `pytest -m transport` | as in slices 1-3, the real-Chrome coverage for these three sections is not in the `transport`-marked files. dynamic-hooks lives in `test_e2e_functions_hooks.py`; progressive-cloning and network-debugging in `test_e2e_data_tools.py`, with `test_e2e_network_capture_shape.py` and `test_e2e_extra_headers.py` covering capture shape and header modification | each slice ran its own coverage file(s) **plus** the canonical real-stdio journey (`test_e2e_transport.py`), one file at a time, Chrome counted before each (107-110 live processes, R8). All green: slice 4 `test_e2e_functions_hooks.py` 7 + transport 1; slice 5 `test_e2e_data_tools.py` 5 + transport 1; slice 6 `test_e2e_network_capture_shape.py` 1 + `test_e2e_data_tools.py` 5 + `test_e2e_extra_headers.py` 2 + transport 1 |
| §4.2 slice 5 says "goldens must not move" without saying how that is shown | the two progressive goldens are driven by `tests/test_cloner_schemas.py` against `progressive_element_cloner` DIRECTLY, one layer below the tool bodies, so a tool-body move cannot reach them | shown rather than asserted: the file is re-run in the slice-5 lane and `git status -- tests/goldens/` is empty in the slice-5 commit. Slices 7 and 8 carry goldens of the same shape and can use the same evidence |
| §5.0 says to measure the base lane first, without saying to do it on a CLEAN tree | measuring the base in the BACKGROUND while editing corrupted it: `test_tool_module_reload.py` re-executes `server.py` FROM DISK, so the two per-identity assertions failed against a half-edited tree and reported a fake 2-failure base | the base was re-measured on a stashed tree — **2334 passed, 1 skipped, 180 deselected, fully green** (no `test_close_instance_offload` flake this time). A base lane must be a foreground run on an unmodified tree; a source-re-reading test suite has no snapshot of the tree it started against |

### Baseline note

Unlike the slices 1-3 base, this one was clean: **2334 passed, 1 skipped** with no
`test_close_instance_offload.py::test_loop_stays_responsive_during_stuck_kill`
flake. Every slice lane below it is accounted for line by line in the first drift
row above.
