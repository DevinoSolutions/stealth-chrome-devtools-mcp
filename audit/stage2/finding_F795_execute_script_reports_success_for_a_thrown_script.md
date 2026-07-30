# F-795 — `execute_script` reports `success: true` for a script that threw

**Status:** OPEN — characterized by plan_RELEASE W13, not fixed (W13 is zero-`src/`).
**Severity:** MEDIUM. A silent wrong success on the most-used escape hatch.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/server.py::execute_script`
→ `embedded/cdp_function_executor.py` — the CDP `Runtime.evaluate`
`exceptionDetails` field is returned as the *result* instead of being turned
into a failure.
**Found by:** plan_RELEASE §2.13 (W13), incidentally: MQ-139's first draft used
`return 'x';` as its probe script, which is a `SyntaxError` for an expression
evaluator, and the node passed the success check before failing on the value.

## The behavior

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

## Why it is worth a finding

1. **It defeats the documented check.** The repo's own helper,
   `tests/e2e_helpers.py::eval_js`, asserts `r["success"] is True` and returns
   `r["result"]` — its docstring says this makes "a page/JS error surface
   immediately rather than as a confusing downstream `None`". It does not: a
   throwing script sails through and returns the exception record as the value.
2. **`execute_script` is the escape hatch.** Its own docstring positions it as
   the default exec-family tool. A wrong success here is a wrong success on the
   surface callers reach for when nothing else fits.
3. **It contradicts `CLAUDE.md` convention 2.** A tool failure is supposed to be
   raised, not encoded in a success payload.

`return` at top level is a *convenient* reproduction, not the scope: any
`throw`, any reference error, any exception inside the evaluated expression
takes the same path.

## Evidence

`tests/test_wire_semantics.py::test_execute_script_reports_success_for_a_thrown_script`
(`@pytest.mark.characterization`, route:F-795). It pins `success is True`,
`error is None`, and the exact `SyntaxError: Illegal return statement`
description, so the moment a throwing script reports failure the node goes red
and must be updated deliberately.

The module also routes every script it sends through one `_echo()` helper whose
docstring names this finding, so no later W13 node can be silently wrong about
whether its probe actually ran.

## Contract limitation wording (for W5 §Limitations)

> `execute_script` returns `success: true` even when the evaluated script
> raises. The exception appears as `result.exception`; the `success` and `error`
> fields do not reflect it. A caller must inspect `result` for an `exception`
> key rather than trusting the success flag.

## Routing

- No MQ step depends on this; it was found while building MQ-139 and is routed
  rather than absorbed.
- No `--mq` id in `release-gate.yml` is bound to the pin.
- Related: F-781 (raised errors carry no structured diagnostic) and F-783 (the
  timeout path escapes the error convention) are the other two places where the
  error convention does not reach the whole surface.
