# F-795 — `execute_script` reports `success: true` for a script that threw

**Status:** RESOLVED (2.0.1 stabilization, branch `fix/truthful-success-flags`).
Characterized by plan_RELEASE W13 (which was zero-`src/`); fixed here.
**Severity:** MEDIUM. A silent wrong success on the most-used escape hatch.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/server.py::execute_script`
→ `embedded/dom_handler.py::DOMHandler.execute_script` → `nodriver`'s
`Tab.evaluate`, which **returns** the CDP `Runtime.evaluate` `exceptionDetails`
record in the value's place instead of raising.
**Found by:** plan_RELEASE §2.13 (W13), incidentally: MQ-139's first draft used
`return 'x';` as its probe script, which is a `SyntaxError` for an expression
evaluator, and the node passed the success check before failing on the value.

## The behavior (2.0.0)

```jsonc
// execute_script(instance_id=…, script="return 'illegal-here';")
{
  "success": true,
  "error": null,
  "result": {
    "exception_id": 1,
    "text": "Uncaught",
    "exception": {
      "class_name": "SyntaxError",
      "description": "SyntaxError: Illegal return statement",
      …
    }
  }
}
```

`success` is `true`, `error` is `null`, and the MCP envelope's `isError` is
false. Nothing at the level a caller is documented to check says the script did
not run. The exception is *present*, but only as a nested field inside the value
the caller is told is the script's result.

## Why it was worth a finding

1. **It defeated the documented check.** The repo's own helper,
   `tests/e2e_helpers.py::eval_js`, asserts `r["success"] is True` and returns
   `r["result"]` — its docstring says this makes "a page/JS error surface
   immediately rather than as a confusing downstream `None`". It did not: a
   throwing script sailed through and returned the exception record as the
   value. (With the fix, the docstring is finally true.)
2. **`execute_script` is the escape hatch.** Its own docstring positions it as
   the default exec-family tool. A wrong success here is a wrong success on the
   surface callers reach for when nothing else fits.
3. **It contradicted `CLAUDE.md` convention 2.** A tool failure is supposed to
   be raised, not encoded in a success payload.

`return` at top level was a *convenient* reproduction, not the scope: any
`throw`, any reference error, any exception inside the evaluated expression took
the same path.

## The fix

The cause is one nodriver behavior — `Tab.evaluate` returns
`cdp.runtime.ExceptionDetails` **instead of** the value when the script throws —
so the fix is one guard at the eval boundary, not a per-tool check:

* `embedded/tool_errors.py::_require_js_value` — the single converter from that
  record to the error convention. Duck-typed on `exception_id` + `text` so
  `tool_errors` stays the dependency-free leaf the import convention requires.
* `embedded/dom_handler.py::DOMHandler.execute_script` returns
  `_require_js_value(result)`. The call sits **outside** the existing
  `except Exception` block on purpose: a script that threw is a failure of the
  script, not of the CDP call, so it must not be re-wrapped in the M6-pinned
  operational message `Failed to execute script: …`.

A caller now sees a raised `ToolError`:

> `Script raised an exception: SyntaxError: Illegal return statement`

and over the wire, `isError: true` with that text.

**Sibling exec tools were checked, not changed.** `inject_and_execute_script`,
`call_javascript_function`, `execute_function_sequence` and
`execute_python_in_browser` do **not** share the `Tab.evaluate` path: they call
`tab.send(cdp.runtime.evaluate(...))` themselves, wrap the caller's source in a
JS `try/catch`, and already inspect the `exceptionDetails` half of the response
— so none of them reported a silent success. They report failure as a
`{"success": False, "error": …}` dict, which is a *different* (and pre-existing)
divergence from convention 2, out of scope for this fix and deliberately not
converted here.

The success envelope `{"success": True, "result": …, "error": None}` and the
input-validation rejection dict are unchanged — both are named KEEP contracts
(`DESIGN.md` §9).

## Evidence

* `tests/test_wire_semantics.py::test_execute_script_reports_failure_for_a_script_that_threw`
  — the W13 node that found the defect, flipped to assert the fix over the real
  stdio wire (`isError: true`, the exception text, and the SAME tab still
  running a valid script afterwards). Its `characterization` mark is gone: it
  now pins a contract, not a quirk.
* `tests/test_truthful_success_flags.py::test_execute_script_raises_when_the_script_throws`
  — both throw classes (evaluator `SyntaxError` and a runtime `throw`) against
  real headless Chrome, plus the no-wedge follow-up.

Verified RED before the fix: with `src/` stashed, the new node fails.

## Contract wording (supersedes the W5 §Limitations draft)

The 2.0.0 limitation draft — *"`execute_script` returns `success: true` even when
the evaluated script raises… a caller must inspect `result` for an `exception`
key rather than trusting the success flag"* — never landed in
`RELEASE_CONTRACT.md` and is now obsolete. As of 2.0.1: **a script that raises is
reported as a tool error; `success: true` means the script ran.**

## Routing

- No MQ step depended on this; it was found while building MQ-139.
- No `--mq` id in `release-gate.yml` is bound to the node.
- Sibling: **F-802** (`navigate` reported success for a failed navigation),
  found and fixed in the same branch, same defect class, second guard in the
  same home.
- Related, still open: F-781 (raised errors carry no structured diagnostic) and
  F-783 (the timeout path escapes the error convention).
