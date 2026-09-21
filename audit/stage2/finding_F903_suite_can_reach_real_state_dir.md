# F-903 — the test suite can reach the operator's real directories

> Scope grew after the first pass. The finding opened on the backend **state
> dir** and the filename still says so; the **browser-session root** — the
> profile a human is logged into — was added on the same evidence and is §3e /
> §4.5 below. Both are fenced by one module because they share one tripwire.

**Status:** fixed on `fix/F903-suite-can-reach-real-state-dir`
**Severity:** high — a test run could cold-start a real backend into the
operator's live `~/.stealth-mcp`, and (before F-886) evict a backend holding
their logged-in browsers.
**Measured on:** Windows 11, Python 3.13.11, 2026-09-21, worktree
`.claude/worktrees/f903-state-dir-fence` at `13e65cc`.

---

## 1. The claim

`tests/conftest.py` redirected two roots — the clone output dir
(`STEALTH_MCP_CLONE_OUTPUT_DIR`) and the browser-session root
(`STEALTH_MCP_BROWSER_SESSION_ROOT`) — and **nothing else**. The third root, the
one that owns a live PROCESS, was never fenced: `backend_registry.STATE_DIR` is
`Path.home() / ".stealth-mcp"`, read at IMPORT time, and the suite inherited it.

Every fence was therefore per-file. About thirty test files each grew their own
copy of an `isolated_state` fixture; ten `integration`-marked files isolate a
CHILD process through `release_gate_harness._isolated_env`; eight need nothing
because `backend_registry`/`browser_pid_registry` never default a path
parameter. The files with none were safe by which collaborator a node happened
to mock, not by construction.

## 2. What actually happened

Two independent incidents, both measured, three days apart.

**(a) 2026-09-21, the incident that opened the finding.** A hermetic node drove
`singleton._proxy_streams` with a failing bridge. `_proxy_streams` hands the REAL
`ensure_server_running` to `proxy_selfheal.drive` as `ensure_running`, so the
heal path reached `_select_backend_port` → `_start_backend_holding_lock` →
`backend_launch.spawn`. A real backend (pid 55240, port 21770) was cold-started
into the operator's live record. F-886 spared the live siblings; nothing in the
suite would have.

**(b) 2026-09-21 06:27:21 UTC, during this finding's own census.** The census
probe written to enumerate the derived globals walked the package with
`pkgutil.walk_packages` + `importlib.import_module`. `__main__.py` is three lines
and the third is a bare `main()` at module level — correct for `python -m`, and
a live grenade for any sweep that imports by name. Importing it started a stdio
proxy, which cold-started a real backend.

```
proxy-188108.log:
  port 3881 holds another session's backend still serving 2 live browser(s);
  spawning ours beside it on a fresh port (F-886)
  backend spawned via the breakaway rung (pid 189088)

backend-189088.log:
  argv=['...\worktrees\f903-state-dir-fence\src\stealth_chrome_devtools_mcp\
         embedded\server.py', '--transport', 'http', '--port', '64986', ...]
```

Attribution is not inferred: the recorded `source_fingerprint`
`e021a4019176bcd07cbb19a753fbfff99761c7c3adbd139493c98f151f80b6ee` is byte-equal
to this worktree's own `build_identity.source_fingerprint(singleton.SOURCE_ROOT)`,
and the argv names this worktree's `server.py`. **The finding reproduced itself
while being investigated**, which is the strongest available evidence that the
route is reachable by ordinary means and not only by the one exotic node that
opened it. It was first contained with a deny-list
(`operator_fence._NEVER_IMPORT`) rather than a comment asking the next author to
be careful; round 2 fixed the product instead and deleted the deny-list (§4.6).

Cost: one stray backend and three added bytes-worth of record entry. It was not
worse only because F-886 (shipped 2.1.9) refuses to evict a backend owning live
browsers — port 3881's backend was holding two of the operator's logged-in
Chromes. Before 2.1.9 the same route terminated it.

Cleanup: pid 189088 terminated (it owned no browsers — `browser_pids.json`
attributed both to pid 47424), `heartbeat-64986.json` removed, `server.json`
restored to its baseline bytes.

## 3. Census — every route into the real state dir

Bindings are **measured, not grepped**: a probe imports every module in the
package and reports every global that is a `Path` at or under the real state
dir. That probe is now `operator_fence.derived_globals`, so the table and the
thing that checks it cannot drift.

### 3a. Module globals bound at import (the redirect's subject)

| # | Binding | Value before the fence | How it got there |
|---|---|---|---|
| 1 | `backend_registry.STATE_DIR` | `~/.stealth-mcp` | `Path.home()` at import — THE definition |
| 2 | `backend_registry.PORT_FILE` | `…/server.port` | derived at import |
| 3 | `backend_registry.SERVER_STATE_FILE` | `…/server.json` | derived at import |
| 4 | `singleton.STATE_DIR` | `~/.stealth-mcp` | `from backend_registry import STATE_DIR` — a SECOND binding |
| 5 | `singleton.PORT_FILE` | `…/server.port` | same from-import |
| 6 | `singleton.SERVER_STATE_FILE` | `…/server.json` | same from-import |
| 7 | `singleton.LOCK_FILE` | `…/singleton.lock` | derived at `singleton`'s import |
| 8 | `process_cleanup.STATE_DIR` | `~/.stealth-mcp` | `from singleton import STATE_DIR` — a THIRD binding |
| 9 | `response_handler.STATE_DIR` | `~/.stealth-mcp` | `from singleton import STATE_DIR` — a FOURTH |
| 10 | `settings._STATE_DIR_ENV_FILE` | `…/.env` | recomputed from `Path.home()`; `settings` is a leaf and may not import the package |

The from-import chain is the whole reason a single `setattr` on
`backend_registry.STATE_DIR` fences nothing: `test_stealthy_cli.py`'s F-891
fixture patches three `backend_registry` names and one `singleton` name, and
still leaves `singleton.STATE_DIR`, `singleton.PORT_FILE` and
`singleton.LOCK_FILE` pointing at the operator's directory.

Binding 10 needs a second act. pydantic copies `_STATE_DIR_ENV_FILE`'s **value**
into `Settings.model_config` when the class body runs, so redirecting the global
alone leaves the suite reading the operator's own `~/.stealth-mcp/.env` — their
knobs silently becoming our test configuration, with `extra="forbid"` turning one
stale key of theirs into a suite-wide crash.

### 3b. Call-time derivations (fenced for free by 3a, verified)

| Site | Derives from | Fenced by |
|---|---|---|
| `logging_setup.resolve_log_dir()` | `singleton.STATE_DIR / "logs"` | #4, read at call time |
| `backend_launch._launch_dir()` | `backend_registry.STATE_DIR / LAUNCH_DIR_NAME` | #1 |
| `desktop_launch._launch_dir()` | `backend_registry.STATE_DIR / LAUNCH_DIR_NAME` | #1 |
| `response_handler._default_output_dir()` | `STATE_DIR / "element_clones"` | #9 |
| `ProcessCleanup.__init__` | `STATE_DIR / browser_pid_registry.RECORD_NAME` | #8 |
| `backend_registry.heartbeat_path(path, port)` | the record path ARGUMENT | nothing to fence |

### 3c. Act routes (no filesystem trace)

| Route | Reached from | Fenced by |
|---|---|---|
| `backend_launch.spawn` | `singleton._start_server_process` | 3a — port selection and the record it writes are fenced |
| `backend_eviction.terminate` | `_clear_stale_backend`, `stop_backend`, `restart_backend` | the KILL GUARD (§4.3) |

### 3d. Files that were unfenced

| File | Nodes | Could reach |
|---|---|---|
| `test_singleton_fast_handshake.py` | `TestFastHandshake` ×2 | the real startup path, via the real `_proxy_streams` → `proxy_selfheal.drive(ensure_running=ensure_server_running)` against a dead port. Whether a spawn lands before `tg.cancel_scope.cancel()` is a scheduler question, not a test property |
| `test_singleton_fast_handshake.py` | `TestEnsureServerRunningNonBlocking` ×2 | real `ensure_server_running`, but both mock exactly the collaborators that touch disk (`_find_running_server`, `_select_backend_port`, `_start_backend_holding_lock`). Unfenced; not reaching, today |
| `test_singleton_cold_start_logging.py` | `test_coldstart_failure_is_logged` | a real READ of `~/.stealth-mcp/server.json` via `_same_identity_backend_ready` → `backend_on_port(_read_server_state(), port)`. Writes blocked incidentally (lock faked, `_clear_stale_backend` raises first) |
| `test_process_cleanup_import_guard.py` | 6 real `ProcessCleanup()` constructions | `self.pid_file` bound to the real `browser_pids.json`; safe only because every node also mocks the two methods that open it |
| `test_process_cleanup.py` | `TestRecoveryFiltering._make_cleanup` | sets `pid_file` to `~/.stealth_browser_pids_test.json` — a real home path, avoiding collision by FILENAME only |
| any file | `pkgutil.walk_packages` + import | §2(b). No test does this today (`test_doc_claims.LIVE_TOPLEVEL` lists `"__main__"` for an `is_file()` check, never an import) |

### 3e. The browser-session root — the second real directory

Raised by the F-902 reviewer, who had to fence their own run by hand. What lives
there is the `master` profile **a human is logged into** and every named session
copied from it — on this machine `C:\stealth-mcp-browser-sessions`, 87 session
directories.

`conftest.py` had redirected it since F-841 (e24b083, 2026-09-16) with
`os.environ.setdefault`, and **measured today that redirect works**: under
pytest all four roots resolve into `%TEMP%\stealth-mcp-test-browser-sessions`.
So this is not a live leak on an unmodified checkout. It is a structural hole
with physical residue, and both halves matter:

* **`setdefault` cannot tell two things apart.** It was deliberate — the comment
  says "so the gate's `runner.temp` value still wins" — but "the release gate
  redirecting the suite" and "the operator's own root arriving in an inherited
  environment" are the same string-shaped thing. A shell that inherited
  `STEALTH_MCP_BROWSER_SESSION_ROOT` from the MCP client's config runs the whole
  E2E tier against the real root, and nothing says so.
* **The Windows default IS the real root.** `clone_storage.default_session_root()`
  returns the hardcoded absolute `C:\stealth-mcp-browser-sessions` when nothing
  is set — it owes nothing to `Path.home()`, which is a second reason redirecting
  `HOME` would not have fenced this.
* **Three derived names escape a root-only redirect.**
  `BROWSER_MASTER_USER_DATA_DIR`, `BROWSER_PROFILE_CLONE_ROOT` and
  `BROWSER_MASTER_SNAPSHOT_DIR` are read directly when non-empty, so an
  inherited one names the operator's real master profile *underneath* a
  redirected root.

The residue, measured 2026-09-21 in the operator's real `sessions/`:

| Directory | mtime (UTC) |
|---|---|
| `e2e-warmup` | 2026-09-17 01:32 |
| `ci-warmup` | 2026-09-16 16:45 |
| `ci-chrome-identity`, `ci-cycle-0/1/2` | 2026-09-15 21:41 |
| `tree-kill-test`, `integration-test-profile`, `ci-basic-test` | 2026-09-15 21:40 |

`e2e-warmup` is `tests/e2e_helpers.py`'s autouse `_warmup`, which spawns
`user_data_dir="e2e-warmup"` — a NAME, anchored under whatever the clone root
resolves to. Those directories are the suite's own, in the operator's root.

## 4. The fix

One home: `tests/operator_fence.py`. One caller: `tests/conftest.py`, at IMPORT
time. Three parts.

### 4.1 The redirect

All ten bindings re-pointed at a per-process fence root under the system temp
dir. Each module is imported first — a binding cannot be redirected in a module
that has not loaded, and a module loaded later would recompute its own global
from `Path.home()` and escape.

**Installed at import time, not in a fixture.** `conftest.py` already carries
this argument for the session root: an autouse FUNCTION-scoped fixture is ordered
after a module-scoped one, and the E2E modules' `_warmup` is exactly that — it
spawns a browser during module setup, before any function fixture runs. Import
time is ahead of collection and of every fixture of every scope.

**Per-process, not the fixed path the session root uses.** There is nothing here
worth sharing (the session root shares a 108 MB master profile; this is two small
JSON files), and two concurrent pytest processes sharing one `server.json` would
fight over it exactly as two backends would.

### 4.2 The write guard

Enumerating bindings is only ever as complete as the last audit, so the fence
does not rest on it. **Every door the product goes through is wrapped** —
`io.open` and `builtins.open` (separate module attributes for the same function;
`pathlib` reaches the former), `os.open`, `os.mkdir`, `os.makedirs`,
`os.replace`, `os.rename`, `os.remove`, `os.unlink`, `os.rmdir`, `os.truncate`,
`os.utime`, plus `os.scandir`/`os.listdir` for the session root — and a target
resolving under a designated root raises. A path the redirect misses is caught
at the moment of harm rather than after it.

**It is not every filesystem primitive, and the first draft of this section
claimed it was** (review M2, measured on CPython 3.13.11). These reach a
designated root and do NOT raise, and each is a residual rather than a gap in
the argument:

| Not wrapped | Why it cannot be |
|---|---|
| `shutil.copy2` | the `_winapi.CopyFile2` fast path opens no Python file object |
| `Path.glob` / `rglob` | `glob._StringGlobber.scandir` is the BUILT-IN, bound inside `glob` at ITS import, so a later `setattr(os, "scandir", …)` cannot reach it |
| `os.chmod` / `link` / `symlink` | not wrapped; they are writes nothing in the product makes to these roots |
| `sqlite3`, any subprocess | the write happens outside this process's Python file layer |
| any fd opened BEFORE install | `os.write`, `mmap`, a live `logging` stream — there is no door left to guard |

`os.truncate` and `os.utime` were in that list until this round and are not any
more: both are one-argument-path writes, `os.utime` is the fast path
`Path.touch()` takes on a file that already EXISTS (so a touch bumped a fenced
mtime while the `os.open` wrapper looked like it covered touching), and adding
them to the wrap loop cost nothing but two strings.

The reviewer verified that what IS wrapped catches the product's own profile
copy: `_copy_profile_delta` walks with `os.walk` (`clone_storage.py`:603) and
dies at the walk before a byte is read — and with it `Path.walk`,
`Path.iterdir`, `glob.glob`, `shutil.copytree`/`copy`/`copyfile`/`rmtree`/
`move`, `tempfile.*(dir=)` and `logging.FileHandler`. For everything in the
table above the REDIRECT is the cover and the tripwire is the backstop, which
is the right way round.

**Keyword forms are inspected too** (review M4). `_guard_paths` walked `args`
only, so `os.remove(path=…)`, `os.rmdir(path=…)`, `os.mkdir(path=…)` and
`os.replace(src=…, dst=…)` went straight through — measured, the fenced file
was deleted. Nothing in the product spells them that way today, but "this door
is closed" may not be true of one call syntax out of two.

`BaseException`, and that is load-bearing. The product is fail-open by design —
`backend_registry` is a never-raise cache, `proxy_selfheal` never raises,
`observability` never raises — so an `Exception` is swallowed at the first
handler it meets and the node goes green over a real write. Measured on the F-900
branch, whose `_RealStartupReached(Exception)` was eaten at
`proxy_selfheal.py:~325`.

**Writes only, deliberately.** `release_gate_harness._reserved_ports()` reads the
operator's real `server.json` through `Path.home()` so an isolated backend never
binds a port a LIVE backend holds. Guarding reads would break the one mechanism
protecting the live backends from a port collision, to prevent a harm reading
cannot do.

Membership is a normalised-prefix test with a separator, not a bare
`startswith`: `.stealth-mcp-browser-sessions` is a sibling the suite writes to
constantly and must not be swallowed. `normpath` on the hot path, never
`resolve()` — `resolve()` stats the filesystem and would recurse into the
primitives being guarded.

**The ROOTS, though, are resolved once** (review S1). A string comparison fences
one spelling, and Windows answers to several for the same directory: the
reviewer reached both real roots through an NTFS junction, a directory symlink,
the 8.3 short name (`.stealth-mcp` → `STEALT~1`,
`stealth-mcp-browser-sessions` → `STEALT~2`, neither of which contains its
mark), the `\\?\` prefix, UNC `\\localhost\C$\…`, and a trailing dot on the root
component — every one confirmed with `samefile()`. `_root_spellings` therefore
puts the `realpath`, the `GetShortPathName` form, the two local admin-share UNC
forms and the `\\?\` form of each root in the table as extra rows, once per table
rebuild, for zero per-open cost.

Two things stay open and are named rather than papered over. A junction used
MID-PATH in the TARGET is a spelling of a fenced file that no root string is a
prefix of, and closing it needs a `realpath` per call — the cost this guard
exists to avoid. And the UNC family cannot be enumerated: any name resolving to
this machine works, as would a remote host's share of the same volume, so the
two spellings the reviewer actually reached are covered and the shape is
recorded. The redirect covers both; §6 lists them.

### 4.3 The kill guard

The real record is read ONCE, before the redirect points the readers elsewhere,
and a pid it names may not be ended. Separate from the write guard because a
kill leaves no filesystem trace.

**The guard is on the ACT, not on a caller** (review M1, and the first version
was wrong). It wrapped `backend_eviction.terminate` and searched its ARGUMENTS
for a protected int, on the reasoning that `terminate` is the one act that ends
a backend. The reasoning about `terminate` is right and the guard built on it
does not work, measured failing both ways:

* **False negative.** `terminate`'s first parameter is a PORT; its kill target
  is a local, computed inside it (`pid = pid_on_port(port)`) by a machine-wide
  `psutil.net_connections` scan. The guard never saw a pid, and ALLOWED a call
  that would have ended the operator's pid 47424.
* **False positive.** The ints it DID see were `port` and `recorded_pid`, so a
  test-owned backend that happened to bind TCP port 47424 was refused about a
  process the operator never owned.

What every kill in the tree has in common is not a caller, it is two doors:
`psutil.Process.terminate`/`kill`/`send_signal` and `os.kill`. The guard wraps
those and reads `self.pid` / the `pid` argument — the number the OS is about to
act on. That is strictly less code (no `backend_eviction` import, no argument
walk) and it also covers the NINE kill sites the argument walk could not see:
`process_cleanup._kill_process_by_pid`, `spawn_leak.reap_launched_browsers`,
`browser_manager._blocking_teardown` (the tree's only `os.kill`) and
`desktop_launch._kill_delegated`, all of which reach live Chrome pids from a
cmdline scan — and the operator's two backends own human-login Chromes.
`send_signal` is wrapped alongside the other two because on POSIX they are thin
wrappers over it and on Windows they are not.

It refuses only RECORDED pids; several files drive the real eviction act and the
real orphan reaper against pids of their own, and a blanket ban would fence the
suite by breaking it. The pins are all against a DECOY pid set, never the
operator's, and each takes its wrapper back off in a `finally` — the first set
installed a layer per node and left every one of them on `psutil.Process` for
the rest of the session (review S2).

### 4.4 Why not redirect `HOME`

It would fence all ten bindings and every future one for free. It was **built and
rejected** for the reason in §4.2: `_reserved_ports()` reads `Path.home()`. Under
a redirected HOME it reads the EMPTY fence record, excludes nothing, and an
isolated backend can bind the port a live backend is serving on — the fence would
have created the collision it exists to prevent. Child-process HOME redirection
(`release_gate_harness._isolated_env`) is a different mechanism for a different
process and is untouched.

### 4.5 The session root: forced, and read-guarded

`STEALTH_MCP_BROWSER_SESSION_ROOT` is **set, not `setdefault`-ed**, and the three
derived names are **cleared** so they go back to deriving from it. Forcing costs
the release gate nothing — its value is a throwaway temp dir and so is ours —
and it is the only rule that does not depend on telling two identical strings
apart. `tmp_session_root` / `tmp_empty_root` set all four per test through
`patch.dict`, which still wins over this baseline.

What is DESIGNATED forbidden is the union of what the operator's own run would
have used: the inherited value (if any) **and** the product's own default, since
on Windows that default is the real root with no env var at all.

**Reads are forbidden here and allowed for the state dir**, and the asymmetry is
the point of each. Nothing in the harness reads a profile directory, while
copying one is exactly how a test would take the operator's logged-in cookies
into a clone — so for this root a read IS the harm. For the state dir the
opposite holds: `_reserved_ports()` must read the real `server.json`. The flag
lives per-row in one table, so there is still exactly one `open` wrapper and
neither policy can be applied to the wrong root (pinned both ways).
`os.scandir`/`os.listdir` join the guarded set for this root's sake:
`_copy_profile_tree` walks a directory before it opens a byte in it.

A root with no final path component (`C:\`, `/`) is dropped rather than obeyed —
designating one would fence the suite off the whole disk.

### 4.6 The product half: `__main__.py` (round 2) — now **F-904**

**This defect has its own finding**:
[`finding_F904_importing_main_starts_a_backend.md`](./finding_F904_importing_main_starts_a_backend.md),
written in parallel by another agent and merged onto this branch (`46c67ef`).
That is the home for it; this section is kept because F-903's census is where it
was found and because the whole-package rule that replaced this finding's
deny-list lives on the same pin file. Where the two disagree, F-904 wins.
The two test files were folded into one, `tests/test_package_entrypoints.py`.

Rounds 1 and 2 of this finding differ in kind, and the difference is worth
naming. Everything above fences the SUITE. This fences nothing — it removes the
hazard.

```python
from stealth_chrome_devtools_mcp.server import main

main()            # was: at module level
```

There is no `if __name__ == "__main__":`, so importing the module and running
the product are the same act. Every tool that walks a package module by module —
a doc generator, an import linter, a coverage sweep, an IDE indexer — is the
census probe of §2, and gets the same backend. The fix is the guard.

**Nothing depends on the import-time execution, verified rather than assumed:**

| Route | Reaches `main()` how | Affected |
|---|---|---|
| `python -m stealth_chrome_devtools_mcp` | runs the file under the name `__main__` | no — the guard passes |
| `[project.scripts]` (all three names) | `server:main` / `cli:main` directly | no — never touches this file |
| `server.py`'s `runpy.run_path` | loads `embedded/server.py`, not the package `__main__` | no |
| `singleton._server_process_cmd` / `backend_launch` | `-m stealth_chrome_devtools_mcp …` argv | no — that IS the `-m` route |
| `import …__main__` (any sweep) | module body | **yes, and that is the fix** |

`tests/test_package_entrypoints.py` is the one home for both halves. The `-m`
contract is measured in-process through `runpy.run_module(..., run_name=
"__main__")` — which is what `-m` IS — with `server.main` tripwired, so the node
proves the guard lets the real door through without starting anything. The
end-to-end node is a real child process with `HOME`/`USERPROFILE` redirected, and
it takes `--transport http --help`, because **the bare spelling is not safe to
run** (see §6.1).

The deny-list that round 1 needed is **deleted, not emptied**. A standing
exclusion list is a second defence for a hazard that no longer exists, and it
rots — so what replaces it is a positive rule over the whole package:
`TestNoModuleBodyDoesWork` AST-walks `src/stealth_chrome_devtools_mcp/` and
refuses a bare module-level call. Measured, there is exactly one in the tree and
it is allow-listed by name with its reason (`tool_runtime`'s
`cdp_transport.install()`: idempotent, no I/O, CLAUDE.md's documented one call
site, and a module body is the only place with its shape). A deny-list catches
the module someone remembered; this catches the one nobody thought of.

## 5. What was NOT deleted, and why

**Every per-file `isolated_state` fixture stays.** They answer a different
question: they give each NODE a clean `tmp_path` record, while the suite-wide
fence gives the SESSION one directory. Deleting them would introduce cross-node
state leakage inside a file — two nodes writing `server.json` would now share one
— which is a regression, not a simplification. The two are not a second way to do
one thing; they are per-test isolation and operator safety, and the suite needs
both.

`test_singleton_fast_handshake.py` gained an autouse ASSERTION that the fence is
installed, rather than a thirty-first copy of `isolated_state`. A local fixture
would fence that file and leave the next one written without one exposed, which
is the shape this finding is about.

## 6. Residuals

1. ~~**`__main__.py` still runs `main()` unguarded.**~~ **FIXED** in round 2 —
   see §4.6. The deny-list it justified is deleted rather than emptied.

   A NEW residual took its place and is now **F-905**, fixed on this branch —
   see [`finding_F905_help_cold_starts_a_backend.md`](./finding_F905_help_cold_starts_a_backend.md).
   A bare `python -m stealth_chrome_devtools_mcp --help` cold-started a backend:
   `server.main` builds its parser with `add_help=False` and reads it with
   `parse_known_args`, so `--help` was an unknown argument to it and the default
   `--transport stdio` carried it past the parser into `ensure_server_running`.
2. **The write guard is per-process.** A test that spawns a CHILD gets no fence
   from it; children are covered by `release_gate_harness._isolated_env`'s HOME
   redirection, which is unchanged and separately correct.
3. **Reads of the real record remain possible** by design (§4.2). A test that
   read the operator's record and asserted on its contents would be flaky rather
   than dangerous; none does.
4. **`test_process_cleanup.py::TestRecoveryFiltering`** still points `pid_file`
   at `~/.stealth_browser_pids_test.json`. Outside the state dir, so the fence
   does not cover it; no node in that class writes it. Left as found — changing
   it is unrelated to this finding.
5. ~~**`_NEVER_IMPORT` is a deny-list**, so a SECOND module that executes on
   import would not be caught until it caused harm.~~ **RESOLVED** in round 2:
   the deny-list is DELETED rather than emptied, and what replaces it is a
   positive whole-package rule —
   `test_package_entrypoints.py::TestNoModuleBodyDoesWork` refuses a bare
   module-level call anywhere under `src/stealth_chrome_devtools_mcp/`, with one
   named allowance (`tool_runtime`'s `cdp_transport.install()`, which is
   idempotent, does no I/O and is CLAUDE.md's documented one call site). A
   deny-list protects against the module someone remembered; this one fails on
   the module nobody thought of.
6. **The fence has no suite-wide guard on the real STARTUP path** — only on
   writes and on terminate. `ensure_server_running` leaves no filesystem trace
   until a backend is already coming up, so the write guard cannot see it; it
   needs a tripwire on the FUNCTION. F-900 landed exactly such a tripwire in
   `tests/test_proxy_bridge_transport.py`, per-file, which is the pattern this
   finding exists to remove.

   Round 3 took the half that is safe: the EXCEPTION is now one symbol in one
   home (`operator_fence.RealStartupReached`, carrying F-900 review M2's
   `BaseException` argument, which is the same argument the fence's other three
   tripwires are built on), and the bridge test binds it instead of declaring a
   twin. The INSTALL deliberately stays per-file, because refusing a spawn
   suite-wide decides for the integration tier and the herd test, which cold
   start legitimately — and that cannot be discharged without running the
   integration tier wholesale. Making it default-on, with the same opt-in shape
   the kill guard has, is the named follow-up.
7. **CI itself was not exercised.** Pushing is out of scope for this task and this
   repo runs zero checks on a branch push without a PR, so "verify on CI" is not a
   thing this worktree can do. The warmup was verified locally against a tmp root
   instead (§7). The residual CI-specific risk is a runner whose `TEMP` differs in
   shape, which the fence handles by construction: it assumes no path, only that
   `tempfile.gettempdir()` is writable.
8. **The session-root leak is structural, with historical residue — not a live
   leak.** Measured today: under pytest all four roots already resolved into a tmp
   dir before this change, because the inherited env happened to be unset.
   `setdefault` remains the wrong instrument, since it cannot tell a deliberate
   redirect from an inherited real root, and the residue in the operator's real
   `sessions/` (`e2e-warmup`, `ci-warmup`, `ci-cycle-0/1/2`, `tree-kill-test`,
   `integration-test-profile`, `ci-basic-test`, beside 87 real ones) shows it has
   failed before. Recorded as what it is rather than dramatised.
9. **The tripwire is not every filesystem primitive**, and the table in §4.2
   names the ones it is not: `shutil.copy2`'s Win32 fast path, `Path.glob`'s
   import-bound `scandir`, `os.chmod`/`link`/`symlink`, `sqlite3`, subprocesses,
   and any fd opened before install. The redirect covers them; the claim has
   been narrowed everywhere it was made (review M2).
10. **A junction used MID-PATH in a target still reaches a fenced file**, and so
    does a UNC spelling through a host name the table does not carry. The roots
    are designated by every spelling the OS answers to for THEM (§4.2, review
    S1/N3), which closes the 8.3, `\\?\`, root-level-link and local-admin-share
    cases for zero per-open cost. A link in the middle of the target would need
    a `realpath` per call, and the set of names that resolve to this machine is
    not enumerable.
11. **`tests/test_backend_escapes_client_job.py`'s helper child** ran real
    backend-spawn code with the operator's real `HOME` (review S7). Nothing
    landed, because the child hand-stubs its one writer — but it was one
    un-stubbed writer from the real record and no in-process fence can reach a
    child. Fixed here by adding `HOME`/`USERPROFILE` to its `_child_env`, which
    is what every other spawning test in the suite already does.

## 7. Verification

Batched, never a full lane, `-m "not integration"`, ≤15 files per pytest process.
Two sweeps, one per half of the fix:

| Sweep | Scope | Result |
|---|---|---|
| 1 (state dir) | 196 files, 14 batches | 3 307 passed, 0 failed |
| 2 (session root added, after the `origin/main` merge) | 197 files, 15 batches | **3 318 passed, 0 failed** |

The fence was installed before the first batch of either sweep ran, so no batch in
this exercise was ever unfenced.

### The warmup, on the shape CI runs it

The brief asked for CI's shape, and this repo runs **zero** checks on a branch push
without a PR, so CI could not be the witness. The call itself could: the real
`e2e_helpers.warmup_once()` — the same coroutine CI's autouse `_warmup` drives, a
real headless Chrome spawn and close — was run under the fence, with its profile
deleted from the tmp root first so that finding it afterwards could only mean this
run made it.

| | |
|---|---|
| root the product resolved | `%TEMP%\stealth-mcp-test-browser-sessions` |
| root the fence designates as real | `C:\stealth-mcp-browser-sessions` |
| `sessions/e2e-warmup` before | absent (cleared) |
| `sessions/e2e-warmup` after | **present, non-empty**, created in 1.98 s |
| the operator's real `sessions/` | 87 entries, mtime unchanged; its own `e2e-warmup` still dated 2026-09-16 |

So the warmup does not merely avoid the real root — it still WORKS against a tmp
one, which is the half a redirect can silently break.

### The integration tier, run for real (round 2)

`warmup_once()` on its own is the mechanism. Two whole E2E files were then run
under the fence to exercise it the way CI does — chosen because the first is the
one that spawns real Chrome on real named profiles and drives adoption:

| | |
|---|---|
| files | `test_e2e_persistent_profile_reattach.py`, `test_e2e_load_milestone.py` |
| result | **5 passed** in 16.1 s |
| `sessions/e2e-warmup` written | in the TMP root, 03:31:44 |
| the operator's real `sessions/` | 87 entries, mtime still 2026-09-20 20:46 |
| Chrome processes before / after | 89 / 89 |
| left behind by the run | **none** |

The Chrome accounting is by PID SET and not by count, because the counts matching
would have hidden exactly the thing worth checking: one pid did appear (119068)
and one did vanish (101780). Both were identified rather than waved through — the
new one is an `--type=renderer --extension-process` child of the operator's own
browser (parent pid 4568, running since the previous morning), and the vanished
one is ordinary renderer churn in that same browser. The two Chromes on the
machine that DO match a test-profile pattern (`--headless=new`, a tmp
`hsw-chrome-*` user-data-dir) were both started 2026-09-20 11:35, appear in the
BEFORE snapshot, and belong to neither this repo's suite nor this run.

The write guard wraps a primitive called on every import, so its cost was
measured rather than assumed — 20 000 `open`+`read` cycles, this machine:

| | per open |
|---|---|
| unguarded | 37.2 µs |
| guarded, first cut (`abspath` + re-normalised root every call) | 45.1 µs (+5.6 µs, +14.3 %) |
| guarded, as shipped (memoised root, substring gate, `abspath` only when relative) | 34.8 µs |

**That last row was a bad number and the review was right to say so** (N1).
34.8 µs is BELOW the unguarded figure, which is not a thing a wrapper can do:
it means the measurement was inside the noise, and the reviewer's interleaved
run (N=20 000 × 8 rounds) put the band at 11–13 µs — an order of magnitude
larger than the effect being reported. The honest number is the isolated one:
`_designated` costs **0.46–0.53 µs per call** (1.45 µs for a relative path,
which pays `getcwd`) and `_designated_roots()`'s cache-hit check 0.083 µs, so
the true overhead is **≈ 0.6 µs per open, about 1.7 %**. The caching works as
designed; the table above is kept only to show which shape was rejected and
why. Resolving the root spellings (§4.2) does not move this — it happens once
per table rebuild, not per open.

The substring gate is exact only for an ABSOLUTE path (the root's whole string
is a prefix, so its last component must appear). A relative path has no such
guarantee — `server.json` opened from a cwd inside the state dir is under it and
contains none of its name — so relative paths keep the exact slow path, and
both halves are pinned.

`~/.stealth-mcp/server.json` SHA-256 before and after the whole exercise:
`1B24991EBA294507195B2BAC386A56BA78B0300B6B26837FF7360314F6D2ADAB` — byte
identical. Both live backends (pid 47424 on port 3881, pid 167540 on port 35273)
alive at the end, and exactly two `--transport http` processes on the machine.

Three pre-existing pins needed re-aiming, each because it asserted a derived path
against `Path.home()` when its actual subject was a convention:

| Pin | Was | Now |
|---|---|---|
| `test_clone_output_dir::test_defaults_to_user_state_dir` | `== Path.home()/".stealth-mcp"/"element_clones"` | `== rh_mod.STATE_DIR / "element_clones"` — the binding the function reads |
| `test_clone_output_dir::test_blank_env_falls_back_to_default` | same | same |
| `test_settings::test_the_env_file_is_our_state_dir_never_the_cwd` | `== Path.home()/".stealth-mcp"/".env"` | `== backend_registry.STATE_DIR / ".env"` — absolute and ours, i.e. never the host project's cwd (#55/#56), which is what the node is about |

The "the state dir is `~/.stealth-mcp`" claim those three carried implicitly now
has ONE home, `test_operator_fence::test_the_real_state_dir_is_the_home_convention`
— which also keeps the fence honest, since `REAL_STATE_DIR` is what the write
guard designates.

### Round 4 — the review's must-fixes

Each of M1, M2 and M4 was demonstrated RED against the code it replaces, by
restoring the shipped implementation under the NEW pins rather than by
reasoning about it:

| | RED (shipped code, new pins) | GREEN |
|---|---|---|
| M1 kill guard | 5 failed / 4 passed — the three `psutil` doors, `os.kill`, and the port-equals-a-pid false positive | 54 passed |
| M2 `os.truncate`/`os.utime` + M4 keyword forms | 5 failed / 7 passed, in one selection | 54 passed |

The M1 pins run against a DECOY pid set (`424242`), asserted at the top of the
class not to be one the operator's record names, and every one of them takes its
wrapper back off in a `finally` — S2's leak was in the pins, not the guard.

Re-run after the changes, hermetic only, batched:

| Batch | Files | Result |
|---|---|---|
| pin set | `test_operator_fence`, `test_package_entrypoints`, `test_proxy_bridge_transport`, `test_doc_claims`, `test_doc_examples`, `test_check_suppression_owners` | 141 passed |
| kill paths + singleton | 13 files (`backend_eviction`, `backend_launch`, `process_cleanup`, `spawn_leak`, `desktop_launch`, `sweep_deferred_cleanup`, `singleton_*`, …) | 273 passed |
| record / storage / CLI | 12 files (`cli`, `clone_storage`, `backend_registry`, `logging_setup`, `profile_lock`, `settings`, …) | 262 passed |
| `test_backend_escapes_client_job` (S7's file, Windows-only, spawns a real child) | 2 | 2 passed, rung `scheduler` both |

`~/.stealth-mcp/server.json` SHA-256 at the start and at the end of round 4:
`1B24991EBA294507195B2BAC386A56BA78B0300B6B26837FF7360314F6D2ADAB` — byte
identical, and the same digest as rounds 1-3. Both live backends (pid 47424 on
port 3881, started 2026-09-20 10:45:31; pid 167540 on port 35273, started
2026-09-20 23:08:04) alive at the end.
