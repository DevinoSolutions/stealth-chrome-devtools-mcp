# Deep Dive DD-4: Dynamic Code-Execution Surface

Scope: `dynamic_hook_system.py` (521 lines), `cdp_function_executor.py` (841 lines),
`hook_learning_system.py` (565 lines), plus the MCP tool wiring in `server.py` and
`dynamic_hook_ai_interface.py` that connects them to `create_dynamic_hook`,
`create_simple_dynamic_hook`, `create_python_binding`, `execute_python_in_browser`,
and `call_javascript_function`.

## Headline answer

**No page-controlled data ever becomes code that reaches `exec()`/`eval()`.** Every
exec/eval site's *code* argument originates from an MCP tool-call parameter — i.e.
text the AI/operator wrote in this session — never from a network response, page
DOM, or JS global. Page/network data (request URLs, headers, response bodies) does
flow into these surfaces, but strictly as **data** (a dict passed as a function
argument to already-compiled code), not as the source text being compiled.

That keeps this whole surface on the "operator invokes on themselves" side of the
trust boundary the task brief asked to judge by — so the findings below are about
**broken/absent containment for self-inflicted code**, inconsistent hardening
between sibling modules, and behavioral bugs, not about remote code execution.

## Trust boundary, in words

```
[MCP client / AI operator]  --code text-->  exec()/eval() sites
         |                                        ^
         | (this is the ONLY source of code)      |
         v                                        |
   function_code, custom_condition,          namespace/globals dict
   python_code (tool args)                   (restricted or NOT — see table)

[Web page / network response]  --data only-->  arguments passed to
   (URL, headers, method,                        already-compiled functions
    post_data, response_body)                     (RequestInfo.to_dict(), eval()'s
                                                    local 'request' var)
         |
         | (separate channel — never touches Python exec/eval)
         v
[Browser JS realm via CDP Runtime.evaluate]  <-- execute_python_in_browser /
   runs with the PAGE's own origin privileges     inject_and_execute_script /
   (cookies, fetch, page JS state) BY DESIGN       call_javascript_function
```

`execute_python_in_browser` is *not* a Python exec surface at all: it transpiles
the given Python text to JavaScript (via `py2js`, a real declared dependency —
`pyproject.toml:52`) and runs the result through `Runtime.evaluate` inside the
target tab. That JS then legitimately runs with the page's own privileges, which
is the intended behavior of a browser-automation tool, not a boundary violation.

## Exec/eval site inventory

| Site | Fires on | Code source | Data exposed at runtime | Containment attempt | Verdict |
|---|---|---|---|---|---|
| `dynamic_hook_system.py:84` `exec(self.function_code, namespace)` | Hook creation (`create_dynamic_hook`, `create_simple_dynamic_hook`) — once per hook | `function_code` MCP tool arg (AI-authored) | none at exec time (only defines the function) | Restricted `__builtins__` dict (8 names) + `validate_hook_function` AST denylist gate | **Theater** — standard `().__class__.__bases__[0].__subclasses__()`-style gadget chains never touch a builtin name, so they bypass the restricted dict entirely; AST denylist only matches bare `ast.Name` calls |
| `dynamic_hook_system.py:123` `eval(condition_code, namespace)` | Every intercepted request/response, for any hook with `requirements['custom_condition']` — hot path, per network event | `custom_condition` MCP tool arg (AI-authored) | `request` local var = live `RequestInfo` (real URL/headers/method of the current network event) | Restricted `__builtins__` dict (only `len`, `str`) | Same gadget-chain bypass applies; **and this site is never run through `validate_hook_function` at all** — `create_dynamic_hook` only validates `function_code`, not `requirements.custom_condition` (`dynamic_hook_ai_interface.py:39`) |
| `server.py:3848` `exec(python_code, exec_globals)` | `create_python_binding` tool call | `python_code` MCP tool arg (AI-authored) | none | **None.** `exec_globals = {}` has no `__builtins__` key, so CPython auto-populates it with the real `builtins` module | No mitigation attempt whatsoever — full interpreter privileges in the server's own process (filesystem, env vars, subprocess) |
| `hook_learning_system.py:519,523,538` `ast.parse`/`ast.walk` (`validate_hook_function`) | Gates `create_dynamic_hook` only | n/a — static analysis of `function_code` | n/a | This *is* the gate | Denylist of 4 bare names (`eval`,`exec`,`open`,`input`) matched only when `node.func` is `ast.Name`; blind to `x.__subclasses__()`-style attribute calls, `getattr` indirection, or anything not a literal bare-name call |
| `cdp_function_executor.py:680-786` `execute_python_in_browser` → `_translate_python_to_js` → `Runtime.evaluate` | `execute_python_in_browser` tool call | `python_code` MCP tool arg, transpiled to JS | n/a (not a Python exec surface) | N/A by design — this is meant to run as JS in the page | Not a sandbox-escape surface, but the naming overpromises: on **any** exception from `py2js.convert()` (not just `ImportError`), it silently falls back to a naive regex transpiler that blindly substring-replaces `True`/`False`/`None`/`.append(` — corrupting string literals/identifiers that happen to contain those substrings, with no signal to the caller that the lossy path was used |

## Other things this deep dive turned up (maintainability/operability, priorities 1–2)

- **`_process_request_hooks` sorts all matching hooks by priority, then only ever
  executes `matching_hooks[0]`** (`dynamic_hook_system.py:301`), despite its own
  docstring claiming "priority chain processing" (`:270`) and the AI-facing docs
  telling operators to "use priority... to control hook execution order"
  (`hook_learning_system.py:465`). Any hook ranked below the top match for a given
  request is silently never invoked — not a bug in matching, a gap between the
  advertised feature and the implementation.
- **`_execute_hook_action` triplicates the same `dict[str,str] → List[HeaderEntry]`
  conversion loop** across the fulfill (`:448-451`), modify/response (`:474-477`),
  and modify/request (`:486-489`) branches — the concrete driver behind the
  reported high cyclomatic complexity of this method and `_process_request_hooks`.
- **CDP timeout wrappers never call `Runtime.terminateExecution`.** Both
  `server.py`'s `_with_cdp_timeout` (`:139-154`) and `cdp_function_executor.py`'s
  `execute_python_in_browser` (`asyncio.wait_for(..., timeout=10.0)`) give up
  waiting on timeout but issue no CDP command to stop the browser-side script.
  `list_cdp_commands()` even enumerates `terminateExecution` as a known command
  (`cdp_function_executor.py:117`) — it's just never invoked automatically. A
  genuinely blocking/infinite-loop script wedges that tab's renderer thread
  indefinitely; every subsequent CDP call targeting that same tab (screenshots,
  clicks, further evaluates) will also hang and eventually time out the same way,
  until the browser instance is torn down and respawned. This is scoped to the one
  wedged tab, not the whole server — other instances/tabs are unaffected since each
  `tab.send()` is an independent awaited call.
- **`create_simple_dynamic_hook(action="log")` ships broken.** Its generated
  template does `import sys` inside `process_request` (`dynamic_hook_ai_interface.py:316-322`),
  but the restricted `__builtins__` dict used at hook-compile time
  (`dynamic_hook_system.py:73-82`) omits `__import__`, so every firing of this hook
  raises, which `DynamicHook.process()`'s broad `except Exception` silently
  swallows into `HookAction(action="continue")` (`dynamic_hook_system.py:158-160`).
  The built-in "log" convenience action never logs anything; there's no error
  surfaced to the caller, only a debug-log line.

## What I deliberately did *not* find

No corruption/race window in the `hooks`/`instance_hooks` shared dicts. `asyncio.Lock`
guards `create_hook`/`remove_hook`, and readers in `_process_request_hooks`/
`setup_interception` don't acquire it — but every mutation and every read-then-use
sequence I traced has no `await` in the middle, so CPython's cooperative single-thread
scheduling already makes those blocks atomic; the lock is effectively redundant
rather than a real gap. Flagging a race here would have been a false positive.
