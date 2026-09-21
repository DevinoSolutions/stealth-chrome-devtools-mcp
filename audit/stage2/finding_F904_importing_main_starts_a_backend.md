# F-904 — importing `__main__` by name starts a real backend

**Severity**: HIGH — a bare import ran production side effects. No user input, no
CLI invocation, no `python -m` involved: any tooling that imports the package's
`__main__` submodule by its dotted name got a cold-started backend as a free side
effect of the import statement.

**Status**: FIXED (this PR) — one `if __name__ == "__main__":` guard, nothing else.

---

## 1. The defect

`src/stealth_chrome_devtools_mcp/__main__.py` was three lines:

```python
"""Allow running as `python -m stealth_chrome_devtools_mcp`."""

from stealth_chrome_devtools_mcp.server import main

main()
```

The third line is a top-level, unconditional call. Python executes it the moment
the module is imported — by any mechanism, not only `python -m`. There is no
`if __name__ == "__main__":` guard, so the module cannot tell the difference
between "I am being run as the program" and "something merely imported me by
name".

`server.main()` is not inert: it scrubs the process environment, parses `sys.argv`
for `--transport`/`--standalone`/`--singleton-port`, and — for the default
`stdio` transport — calls `ensure_server_running(...)`, which cold-starts a real
backend process into `~/.stealth-mcp` if none is already recorded there (see
`singleton.py`'s row in `CLAUDE.md`). None of that is gated on how `__main__.py`
itself was reached.

## 2. Measurement

On 2026-09-21, importing `stealth_chrome_devtools_mcp.__main__` by name (the shape
`pkgutil.walk_packages` + `import_module` produces while enumerating a package, and
the shape any ad hoc `importlib.import_module("stealth_chrome_devtools_mcp.__main__")`
or a stray `import stealth_chrome_devtools_mcp.__main__` produces) started a real
backend: pid 189088, port 64986, recorded in the operator's own `~/.stealth-mcp`.
This happened twice independently on the same day. Nothing about the import
statement suggested "run a server" to whoever wrote it.

## 3. Why `python -m` still worked despite the same body

`python -m stealth_chrome_devtools_mcp` does not import the module by its dotted
name — it resolves the package to its `__main__` submodule and executes that
submodule's code with `__name__` forced to the string `"__main__"`
(`runpy._run_module_as_main`, the same primitive `runpy.run_module(pkg,
run_name="__main__")` uses). The bare `main()` call ran either way: as a genuine
`python -m` invocation (intended) or as a side effect of any other import of the
module (not intended, and not previously prevented). The guard makes the two
paths diverge the way every other `__main__`-shaped module in this tree already
does (`stealth_chrome_devtools_mcp/server.py`, `embedded/server.py`, `cli.py` all
carry the same guard already — `__main__.py` was the one file in the package that
did not).

## 4. Fix

```python
"""Allow running as `python -m stealth_chrome_devtools_mcp`."""

from stealth_chrome_devtools_mcp.server import main

if __name__ == "__main__":
    main()
```

Nothing else changed. `from ... import main` still binds the name at import time
(so a patch to `stealth_chrome_devtools_mcp.server.main` made *before* the import
is what a test observes), but the call itself now runs only when the module is
executed as the program.

## 5. Pins

`tests/test_package_entrypoints.py`, three nodes:

* `TestImportingDoesNotRun::test_importing_by_name_never_calls_main` — patches
  `stealth_chrome_devtools_mcp.server.main` to a tripwire, removes
  `stealth_chrome_devtools_mcp.__main__` from `sys.modules`, imports it by name via
  `importlib.import_module`, and asserts the tripwire was never called. RED before
  the fix (measured: the tripwire fired), GREEN after.
* `TestRunningAsMainStillRuns::test_python_dash_m_still_reaches_main` — the same
  tripwire, but drives `runpy.run_module("stealth_chrome_devtools_mcp",
  run_name="__main__")` — the mechanism `python -m` itself uses — and asserts the
  tripwire fired exactly once. Guards against a fix that satisfies the first node
  by breaking the module's one real job.
* `TestGuardShape::test_the_only_top_level_call_is_guarded` — an AST read of
  `__main__.py`'s source: no top-level `Expr(Call(...))` outside an
  `if __name__ == "__main__":` block, and exactly one such guard exists. Prevents
  a future edit from reintroducing a second, unguarded call beside the guarded
  one.

## 6. Consumer sweep

Grepped `src/`, `tests/`, and `pyproject.toml` for `__main__`, `-m
stealth_chrome_devtools_mcp`, and `runpy`:

* `singleton._server_process_cmd` builds `[<interpreter>, "-m",
  "stealth_chrome_devtools_mcp", "--transport", "http", ...]` — this runs the
  module AS `__main__` (`python -m` semantics), so it is unaffected by the guard;
  this is precisely the path the guard is meant to keep working, and
  `TestRunningAsMainStillRuns` pins that it does.
* `[project.scripts]` points `stealth-chrome-devtools-mcp` at
  `stealth_chrome_devtools_mcp.server:main` and both `stealthy` /
  `stealth-chrome-devtools` at `stealth_chrome_devtools_mcp.cli:main` — neither
  console script imports `__main__.py` at all.
* Every other `__main__` occurrence in `src/` is either a docstring/comment
  reference to this file or another module's own, already-correct
  `if __name__ == "__main__":` guard (`server.py`, `embedded/server.py`,
  `cli.py`) — none of them import `stealth_chrome_devtools_mcp.__main__` as a
  module.
* `pyproject.toml` has no reference to `__main__` or `runpy`.

## 7. Residual

None expected in the PRODUCT. The guard is the same shape every other entrypoint
module in this tree already carries; `__main__.py` was the one omission.

One was found in the PINS, by the pre-push lane, and is fixed here.
`TestRunningAsMainStillRuns` popped `stealth_chrome_devtools_mcp` from
`sys.modules` to force a fresh `runpy` execution, and left it popped. Popping a
PACKAGE while its submodules stay cached is unsound: the next
`import stealth_chrome_devtools_mcp` builds a NEW module object, and
`import stealth_chrome_devtools_mcp.embedded` is a `sys.modules` HIT that never
re-binds `embedded` as an attribute of that new parent — not even an explicit
`importlib.import_module` repairs it (measured). The package is then permanently
un-walkable by attribute, which is exactly how pytest resolves a dotted
monkeypatch target (`__import__`, then a `getattr` walk), so
`tests/test_python_exec_timeout.py` failed two nodes with
`module 'stealth_chrome_devtools_mcp' has no attribute 'embedded'` — 2 failed /
3413 passed, and only in a full lane, because this file sorts before that one
and either file alone re-imports the package cleanly.

The pops are kept (they are what forces the fresh execution) and wrapped in
`_pristine_package_modules`, which restores the `sys.modules` mapping for the
package exactly. Three pins at the end of the file assert the invariant this
file is uniquely able to break: the two named subpackages resolve by attribute
after an import, no cached submodule is unnamed by its own parent, and a real
dotted `monkeypatch.setattr` resolves. They go red on all three without the
restore, so the failure is now reported in the file that causes it rather than
in whichever file happens to sort next.

Their reach is this file and everything sorted before it, which is where the
mechanism lives — no other test file pops or reloads a real package module
(`test_element_cloner_output_dir` and `test_tool_module_reload` both restore
what they take, verified). Covering the whole session would mean a per-test
teardown hook, which is not worth its cost for one file.
