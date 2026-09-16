# F-879 — the headed desktop hand-off composes a `/TR` with no length guard, and `schtasks` truncates one silently

**Status:** fixed on `fix/F879-desktop-launch-tr-guard` (RED pinned, GREEN, hermetic)
**Opened by:** `finding_F867_backend_inherits_the_clients_job_object.md` §"Still open", which named this exact function and deliberately left it: *"Worth its own finding rather than a drive-by fix here."*
**Source at:** `main` = `b0ae010`
**Severity:** MEDIUM. Latent on the shipped layout (`~/.stealth-mcp` leaves ~143 characters of slack), reachable on a deep or redirected home — a roaming profile, a UNC home share, a long account name. When it is reached it is silent in every direction: `schtasks` reports success, the task fails where nothing is watching, and the error the user finally sees names the wrong component.
**Not measured here:** the 253 figure is F-867's measurement (Windows 11 10.0.26200, 2026-09-14), carried over. No test in this change runs a real `schtasks`.

---

## 1. The mechanism

`schtasks /Create /TR <command>` **stores at most 253 characters of `<command>`,
drops the rest, and exits 0.** There is no warning on stdout, no non-zero return,
nothing in the `CompletedProcess` a caller could branch on. The documentation
says "~261"; that figure is wrong by eight characters, which is itself how F-867
found this — a command it believed was inside the documented limit was not.

A truncated command is not a broken command in any way Windows will tell you
about either. It names a launcher script whose path lost its tail, so the task is
created, is dispatched, and fails at run time with **Last Result 2**
(`ERROR_FILE_NOT_FOUND`). Task Scheduler records that in its own store; the
product never reads it and writes nothing.

## 2. What it cost in this module

`desktop_launch._run_task` built

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<state dir>\desktop-launch\<32 hex>.ps1"
```

and handed it straight to `_schtasks`. 58 fixed characters plus the script path;
the path is the account's home plus `\.stealth-mcp\desktop-launch\`, a 32-hex
token and `.ps1`. On the shipped layout that is comfortable — which is why this
was latent rather than live — but the slack is entirely a property of how long
this account's home happens to be, and nothing checked it.

When it is exceeded, the sequence is:

1. `/Create` returns 0, so the code proceeds.
2. `/Run` returns 0 — the task exists and was dispatched.
3. The launcher never runs, so no pid file is ever written.
4. `launch_and_attach` polls for the whole `PORT_READY_TIMEOUT` (20 s).
5. It raises `F-810: the delegated browser never opened its DevTools port
   9333 within 20s` — naming the one component that was never involved.

A user reading that error has no path to the real cause. There is no log line for
the truncation (it did not raise), none for the task failure (Task Scheduler's
store is not read), and the message points at Chrome and a port.

## 3. What was NOT the problem

The obvious suspicion — that Chrome's own command line rides in `/TR` and a long
`--user-data-dir` or a proxy's credentials blow the budget — is false, and F-810
had already solved it. The launcher **script** carries Chrome's argv; `/TR`
carries `powershell.exe`, four fixed switches and one path. A 400-character
profile path and 400 characters of switches cost `/TR` exactly nothing, and the
fix pins that both ways (the `/TR` stays under the cap **and** those values do
reach Chrome's argv — a `/TR` that is short because the args were dropped would
otherwise pass a length assertion).

So the routing the brief offered as the preferred remedy — move the browser
launch onto `backend_launch`'s `_LAUNCHER_SCRIPT` + JSON spec — was not taken,
for two reasons:

* **It buys nothing here.** The spec-file property it exists to deliver ("`/TR`
  carries short paths only") is already what this module does; its `.ps1` *is*
  the spec. The two `/TR`s are the same shape and the same length class.
* **It would cost the rung.** `_LAUNCHER_SCRIPT` runs under a **non-venv**
  `pythonw.exe` (`_intermediary_interpreter` returns `None` for a venv
  redirector, which would rebuild F-866's kill-on-close job). `backend_launch`
  can afford that gate because it has a `plain` rung to fall to. The headed
  hand-off has **no fallback** — delegation is the only way a non-visible
  context can show a window — and the backend is very often installed in a venv
  or as a uv tool, so the routing would turn a latent length bug into a regularly
  unavailable feature. PowerShell is present on every Windows host; a base
  `pythonw.exe` is not.

## 4. The fix

The cap is a fact about `schtasks`, and `desktop_launch._schtasks` is THE
`schtasks` seam in the tree, so the fact moved to sit beside it:

* `TR_MAX_CHARS = 253` and `TOKEN_CHARS = 12` now live in `desktop_launch`.
  `backend_launch` no longer defines either; it reads them at call time through
  the lazy import it already uses for `_schtasks`, `_cleanup` and `_read_pid`.
  The direction matters: `backend_launch` reaching into `desktop_launch` extends
  a documented existing coupling, whereas `desktop_launch` importing
  `backend_launch` would be the browser path depending on the backend spawner.
* `tr_overflow(command)` is the one home for the **comparison**, not just the
  number — it returns the length when it would truncate and `None` otherwise, so
  neither composer can drift on the cap, on the inclusive boundary (253 is what
  schtasks *stores*, so exactly 253 arrives whole) or on forgetting to ask. It
  returns the length rather than a bool because both callers report it: a cap
  alone tells an operator nothing about how far over their machine is.
* `_tr_command(script)` is this module's ONE composition site and **cannot return
  a command that would truncate**. It raises `ToolError` naming the measured cap,
  the actual length and the state dir that is long.
* It is called **before** `launch_dir.mkdir`, so an impossible layout costs no
  directory, no scratch file, no scheduled task and no 20 s deadline. The pin
  asserts `schtasks.calls == []` — not one call, including the teardown's
  `/Delete`.
* The two paths diverge deliberately on what over-length **means**:
  `backend_launch` logs a WARNING and drops to the `plain` rung, because it has
  one and a killable backend beats none; `desktop_launch` raises, because it has
  none and a silent 20 s timeout blaming the wrong component is the alternative.
* The per-attempt token here is 12 hex characters rather than 32 — 20 more
  characters of headroom, and one spelling of "how long is a per-attempt token"
  instead of two.
* `_launcher_script`'s docstring no longer repeats the documented "~261". The
  measured number is a named constant now, not prose that can be read and
  believed.

## 5. The pins

`tests/test_desktop_launch.py::TestTheStoredCommandLengthCap`, all hermetic —
`_schtasks` is faked and `backend_registry.STATE_DIR` is redirected to `tmp_path`,
so nothing touches the real `~/.stealth-mcp` and no scheduled task is created:

| pin | what it would catch |
|---|---|
| `test_chromes_own_command_line_never_reaches_tr` | someone "simplifying" the launcher script away and putting Chrome's argv back in `/TR` |
| `test_a_state_dir_that_would_truncate_refuses_before_schtasks` | the defect itself: refusal, the two numbers in the message, and that nothing at all was created |
| `test_the_shipped_layout_leaves_room` | a guard that is correct but so tight the feature refuses on a normal machine |
| `test_a_command_of_exactly_the_cap_is_accepted` / `test_one_character_more_is_refused` | an off-by-one on a boundary that is inclusive |
| `test_backend_launch_reads_this_modules_tr_cap` | a second `253` or a second token length reappearing in `backend_launch` |

`tests/test_backend_launch.py`'s three cap tests now patch and read
`desktop_launch.TR_MAX_CHARS` / `TOKEN_CHARS`; that they still pass is the
positive proof that the scheduler rung consults the one home.

RED was verified twice: once by the missing names, and once — the one that
matters — by reinstating the constants and neutralising only the comparison, at
which point the two refusal pins fail on `DID NOT RAISE`, i.e. on the defect
rather than on a harness error.

## 6. Still open

* **The 253 itself is inherited, not re-measured.** It is F-867's figure from one
  machine and one Windows build. If a future build stores more or fewer, every
  pin here still passes (they are all relative to the constant) and the product
  would be wrong in the same silent way. Re-measuring on a second Windows build
  would be worth doing the next time a real `schtasks` is in reach.
* **Task Scheduler's own result is still never read.** `Last Result 2` is
  recorded by the OS and the product does not look at it, here or in
  `backend_launch`. That is a different remedy (a `/Query /V` after a deadline
  miss, costing ~0.85 s per call — the exact cost F-867's orphan sweep was
  restructured to avoid) and a different finding.
* **`powershell.exe` is left to PATH** in this `/TR`, unlike `schtasks.exe`,
  which `_system_binary` resolves absolutely. The task runs as the logged-on
  user, whose PATH the product does not set, and an absolute system path would
  spend about 30 of the 253 characters. Noted rather than changed; it is a
  different question (trust) from this one (length).
