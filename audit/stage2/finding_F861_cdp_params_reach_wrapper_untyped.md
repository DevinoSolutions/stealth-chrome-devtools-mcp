# F-861 — `execute_cdp_command` hands a caller's JSON to a nodriver wrapper that expects its typed classes

**Status:** FIXED in this PR (product defect; live on 2.1.1 and on `main` at `bc93d02`)
**Opened by:** Sentry triage of 2026-09-11, requested by the maintainer; reproduced live the same day on the freshly installed `main` backend
**Source at:** `origin/main` = `bc93d02`
**Severity:** MEDIUM. Every CDP command whose parameters are typed (enums, newtypes, dataclasses, lists of them) is unusable from `execute_cdp_command` with the values the CDP docs show; the failure is an AttributeError raised inside nodriver that names neither the parameter nor the type, and it ships to Sentry as an unhandled-crash shape although it is a caller mistake.

---

## 1. What was observed

| fact | evidence |
|---|---|
| `AttributeError: 'str' object has no attribute 'to_json'` — 74 events, 2026-09-01 → 2026-09-10, release 2.1.1 | Sentry `STEALTH-CHROME-DEVTOOLS-MCP-4T`; innermost frame `nodriver/cdp/input_.py:406 dispatch_mouse_event: params['button'] = button.to_json()` |
| same message, 2 events, 2026-09-08 → 2026-09-10 | Sentry `STEALTH-CHROME-DEVTOOLS-MCP-79`; innermost frame `nodriver/cdp/browser.py:355 grant_permissions: params['permissions'] = [i.to_json() for i in permissions]` |
| `AttributeError: 'int' object has no attribute 'to_json'` on the new `main` backend, 2026-09-11 16:36 UTC | `get_debug_view` on backend pid 114636 (this session's live test); innermost frame `nodriver/cdp/browser.py:627 set_window_bounds: params['windowId'] = window_id.to_json()`, correlation id `3b446e71b666` |
| in every case the first-party frame is the same line | `cdp_function_executor.py:245 result = await tab.send(build_cdp_call(cdp_method, params))` |

## 2. Mechanism

1. `tool_sections/cdp_functions.py::execute_cdp_command` passes the caller's `params` dict through `rt.cdp_function_executor.execute_cdp_command`.
2. `resolve_cdp_command` (F-813) finds nodriver's generated wrapper; `build_cdp_call` (F-816) folds the wire's param NAMES onto the wrapper's (`windowId` → `window_id`) and calls it with the caller's VALUES as they arrived.
3. nodriver's generated wrappers (`nodriver/cdp/*.py`) declare every non-primitive parameter as a generated class — `WindowID(int)`, `TargetID(str)`, `MouseButton(enum.Enum)`, `Bounds` (dataclass), `typing.List[PermissionType]` — each with `from_json`/`to_json`, and the body serialises with `.to_json()` unconditionally.
4. A JSON scalar/dict/list has no `to_json`, so the generator raises AttributeError at `next()` inside `nodriver.core.connection.Transaction.__init__`. The executor's `except Exception` turns it into `{"success": False, "error": "'str' object has no attribute 'to_json'"}` and `debug_logger.log_error` ships it — as an AttributeError, which `observability._scrub_event`/`before_send` does not drop (it drops `ToolError` by type).

Net: the caller sees a message that names neither the parameter nor the expected type; Sentry receives a caller mistake as a crash.

## 3. Fix (this PR)

- New leaf `embedded/cdp_params.py` — **THE one home for "a caller's JSON, as the type a CDP wrapper declares"**. `typed(method, kwargs)` reads the wrapper's resolved type hints (`typing.get_type_hints`; the generated modules use string annotations) and builds each value into the declared type: `from_json` for any class carrying it, recursing through `Optional[..]` (None passes) and `List[..]` (each element); primitives, other generics and values already of the type pass through untouched. A value the type cannot take raises `ToolError("param 'bounds' expects Bounds, got 'big': …")`.
- `cdp_function_executor.build_cdp_call` calls `cdp_params.typed` after F-816's name folding — the one composition site, so both forgivenesses (name, then value) sit in one line.
- Nothing is typed by hand: the mapping is derived from nodriver's own signatures, so a nodriver upgrade that adds or retypes a parameter is covered without a change here.

## 4. Verification

- `tests/test_cdp_params.py` (new, hermetic): the three Sentry shapes (`Input.dispatchMouseEvent button:"left"`, `Browser.grantPermissions permissions:[..]`, `Browser.setWindowBounds windowId:7`), plus dataclass-from-dict, str-newtype inside `Optional`, `None` inside `Optional`, `List[CookieParam]`, enum inside `Optional`, already-typed pass-through, primitives untouched, un-coercible value → `ToolError` naming param and type, and the executor end to end over `FakeTab`. RED: 8 of 10 failed on the wrapper's own `to_json` AttributeError before the fix (the two pass-through pins were green, as they must be). GREEN after.
- `tests/test_cdp_command_normalization.py` (F-813/F-816, 24 tests): unchanged and green — every pinned frame is byte-identical.
- `cdp_function_executor.py` 1012 → 1004 LOC; cap ratcheted to 1004 (cap == actual).

## 5. Not claimed

- No change to which commands `list_cdp_commands` advertises, nor to the executor's `{"success": False}` KEEP contract (the caller still receives the dict; only its `error` text improves and the exception type changes to the convention's).
- The `'int'` variant was never in Sentry because it occurred on the new backend under `STEALTH_MCP_NO_ERROR_REPORTING`-free conditions only today; it is the same mechanism, not a separate defect.
