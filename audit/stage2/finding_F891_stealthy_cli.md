# F-891 — nothing in a shell could call a tool on the running backend

**Status**: fixed (feature). **Kind**: capability gap, not a defect.
**Severity**: MEDIUM — no incorrect behaviour, but the one recovery path the
product has for a stranded login was unreachable without writing a program.

---

## 1. The measurement

On 2026-09-19 the orchestrator had to recover a logged-in Seller Central Chrome
whose backend had gone away. The product supports that in one call —
`spawn_browser(user_data_dir=<profile>)`, F-888's re-attach — and there was no
way to make it. `stealth-chrome-devtools` had seven verbs (`status`, `profiles`,
`cleanup`, `doctor`, `stop`, `restart`, `kill-orphans`, `serve`) and every one of
them operates the backend's *lifecycle*; none of them can ask it to *do*
anything.

What that cost, measured: a 40-line hand-written MCP stdio client,
`C:\Users\amind\AppData\Local\Temp\reattach_seller_central.py`, which spawned a
proxy with `StdioServerParameters`, drove `ClientSession.initialize()`, called
two tools and unwrapped the answers by hand. Three facts it had to know that no
caller should have to:

1. `spawn_browser`'s answer arrives in `structuredContent`, and `content` may be
   **empty** — the script's `show()` helper reads `res.content[0].text if
   res.content else None` precisely because reading it unconditionally raised.
2. a tool whose return is not a dict arrives FastMCP-wrapped as
   `{"result": …}` — the script unwraps that shape explicitly.
3. `spawn_diagnostics.reattached` is where F-888 records the one fact the whole
   exercise was about, and it is nested two levels down.

Every one of those is now in one place with a pin on it.

## 2. What shipped

**One CLI, a new name, six new verbs.** `[project.scripts]` gains `stealthy` →
`stealth_chrome_devtools_mcp.cli:main`; `stealth-chrome-devtools` stays, bound to
the *same* `main`. That is an alias and not a second CLI (convention 4): one
parser, one `_DISPATCH`, one set of verbs, and `cli._prog_name()` resolves
argv[0] against the closed `cli.SCRIPT_NAMES` tuple so help text names the
command the operator typed. `tests/test_doc_examples.py` asserts the whole
`[project.scripts]` table as an identity, so the two CLI names drifting onto two
different mains fails.

| verb | what it is |
|---|---|
| `tools [--section X]` | the LIVE backend's surface, with the installed registry's count beside it |
| `call <tool> [--arg k=v] [--json '<obj>']` | any tool; no per-tool mirror |
| `ls` | `list_instances`, as a table or as its record |
| `spawn [--profile X] [--headed\|--headless] [--url]` | `spawn_browser` + optional navigation |
| `nav <instance> <url> [--wait]` | `navigate`, ids by unique prefix |
| `close <instance>` | `close_instance`, ids by unique prefix |

**Two new leaves.** `embedded/backend_client.py` owns the session, the call and
the one reading of an answer; `cli_call.py` owns the six verb bodies. `cli.py`
keeps every parser and both names.

## 3. The decisions that are not taste

**Selection is `status`'s, made once.** `cli_call.backend_url` binds
`singleton._probe_backend_status` — the one selection `status`, `doctor`, `stop`
and `kill-orphans` already make (F-868) — and `_run` makes it ONCE per command
and hands the url down, so a command that makes two calls cannot make them to two
different backends. A second record read here would have been the same defect
F-868 closed in the status block, at a new surface.

It answers a **pair**, `(url, started)`, and the bool is load-bearing. The first
draft always followed selection with `_await_backend_http`, which for a backend
that had just answered `responsive` re-proves the same fact on the *cold-start*
deadline. Measured: the hermetic suite hung — `pytest tests/test_stealthy_cli.py`
did not finish inside 600 s against a patched probe reporting a port nothing was
listening on. Only a backend this command asked for is waited out.

**Starting one is `ensure_server_running`'s and nothing else's.** That is the
stdio proxy's own startup path, so the cold-start lock, F-886's step-aside and
F-889's adopt-forward rule all apply to a `stealthy` invocation unchanged. A CLI
that spawned its own backend would have none of them. `--no-start` turns the
absence into exit 3 rather than a spawn.

**No stdio proxy per command.** A proxy is a Claude-Code-session concept with a
session's cold start; the CLI speaks streamable-HTTP to the backend directly.

**No per-tool argparse mirror.** Mirroring 94 signatures would mean a release to
serve a 95th tool, and the tool count in this repo is derived and never typed.
The tool's own schema on the backend is the validation.

**`--arg` is JSON-when-it-parses.** `headless=false` has to be a bool and
`browser_args=["--x"]` a list, while `user_data_dir=seller-central` and
`C:\Users\me\profile` have to stay strings — a JSON-only rule fails the paths
this feature exists for, and a string-only rule cannot express a bool. The split
is on the FIRST `=` so a url's query string survives.

**`--master` was built and then REMOVED before shipping.** The brief asked for a
flag that lands on the master profile itself, and the measurement behind it
stands: an argument-less `spawn_browser()` reaches master only while master is
FREE — `clone_storage.resolve_profile_selection` (`clone_storage.py:971`) tests
`_profile_has_running_browser(master)` and falls through to a clone the moment a
browser holds it — so "the master profile itself" genuinely cannot be spelled as
an unnamed spawn, and naming the directory is also what buys F-888's re-attach
(the branch in `tool_sections/browser_management.py` is gated on
`if user_data_dir:` and runs BEFORE profile selection). What removed the flag is
the parallel design study (`design_session_ux.md`, 2026-09-20), on two grounds
this finding did not have:

1. **The word is a trap.** `master` as a bare NAME does not mean the master
   profile — a relative `user_data_dir` is anchored under the clone root, so
   `user_data_dir="master"` resolves to `sessions/master`, a different profile
   that exists on this machine at 0.46 GB (F-894, measured). A flag named
   `--master` teaches that word one resolution step away from a directory that
   is not what it says.
2. **It would be renamed next release.** The master/snapshot vocabulary is being
   replaced by `--session NAME` + `--from <session>` with `default` reserved as
   the presented name for today's master (F-892+). Shipping `--master` into a
   surface whose owning study has already decided to retire the word is shipping
   a rename.

So phase 1's `spawn` takes `--profile <name-or-path>` (straight through as
`user_data_dir`, which reaches master's directory by absolute path for anyone who
wants it — with role `explicit`, because nothing special-cases the master path:
`master_profile_dir()` has three call sites, all inside `clone_storage`),
`--headed`/`--headless` and `--url`. The verb PRINTS
`spawn_diagnostics.profile_selection` (role + directory) and `reattached`, so
what the resolver chose is visible rather than asserted by a flag name.

**F-874 survives the table.** A `partial` or `stored` record deliberately carries
no `current_url`. `instance_rows` marks the last-known value with
`LAST_KNOWN_MARK` rather than filling the column from `last_navigated_url`, which
would re-commit the exact defect F-874 closed. The table also has no
headed/headless and no profile column, because `list_instances` reports neither
and a column filled from elsewhere would be the CLI asserting something the
backend never said.

**The session is terminated.** `backend_client.opened` passes
`terminate_on_close=True` explicitly although it is also the SDK's default: the
DELETE is what the context manager exists to guarantee, and a default is a value
a dependency bump can change with nothing here failing.

**The budget lands on `sse_read_timeout`.** `streamablehttp_client` builds
`httpx.Timeout(timeout, read=sse_read_timeout)`
(`mcp/shared/_httpx_utils.py:73-77`), so `timeout` bounds connecting and the
second bounds what a tool call waits under. `--timeout` therefore sets the
second; connecting keeps a separate 30 s so a vanished socket is reported in
seconds rather than at the end of a 180 s tool budget.

## 3b. What the two reviews changed (and one thing they changed back)

The review at `a4b4741` found the promised closed exit-code set was not closed
and the session termination the client exists to guarantee was pinned only as a
keyword argument. Both are now behaviours rather than claims.

- **The exit codes are closed by construction.** `cli_call._verdict` maps every
  exception onto one code and `_run` catches `Exception`, `KeyboardInterrupt`
  and `BaseExceptionGroup` (the shape an interrupt takes through the transport's
  own task group). A transport failure is **3** and not 1: nothing on the
  backend saw the request, so there is no answer to report. Our own bug is
  **70** (`EX_SOFTWARE`) and deliberately not 1, which means "the tool said no".
  `Ctrl-C` is **130**, and a reader that went away is **141** (the second
  review's find, below). The whole set is **0 / 1 / 2 / 3 / 70 / 130 / 141**,
  and all three surfaces that state a SET state that one: `cli_call`'s
  constant-block docstring, README's *Exit codes are a closed set* table and
  the CHANGELOG entry. RUNBOOK deliberately names one code only — the 3 its own
  `--no-start` paragraph produces — because a second full table there is a
  second place for the set to drift. Measured under mutation: narrowing the
  `except` back to
  the named refusals makes the transport node raise through `main` and the
  interrupt node abort the pytest run outright — which is what a shell saw.
- **The DELETE is pinned, not the flag.** `TestSessionHygiene` drives the REAL
  `mcp` SDK over an `httpx.MockTransport` bound at `backend_client.http_client`,
  the one transport seam, and asserts the session id reaches a `DELETE` on four
  ways out: success, a tool error, a socket that dies mid-`tools/call`, and
  cancellation. Setting `terminate_on_close=False` turns all four RED; the
  previous single node stayed green, because its double's `__aenter__` raised
  and the exit path never ran.
- **`spawn --url` prints before it navigates.** Swapping the two statements back
  turns both new nodes RED with `out=''`.
- **S1 was implemented, then reverted to its measurement.** The first attempt
  added `backend_liveness.any_responsive` — adopt any recorded backend that
  answers, ignoring display context. It prevents no eviction: `probe_recorded`
  is already identity-blind, so a live foreign-BUILD backend on this desktop is
  adopted today, and the only entry the widening newly reaches is on another
  desktop, where the cold start targets a different port and terminates
  nothing. What it did add was a `spawn --headed` opening a window on a desktop
  the operator is not watching, a CLI that silently disagrees with its own
  `status`, and — measured, as the failing node that caught it — a real
  `initialize` sent at the operator's live backend from a hermetic test, because
  the widened walk read `SERVER_STATE_FILE` directly. Deleted; the pins for what
  IS true (a foreign identity is adopted, the reuse gate is never asked, only an
  empty answer reaches a cold start) stayed.

The review at `1eee656` then found the set still open at two doors, both outside
the six verbs and therefore outside everything the first round had pinned.

- **`stealthy` with no subcommand returned 1.** A pre-F-891 line, harmless until
  this feature gave 1 a meaning: it told a script the tool had answered and
  refused, about a shell that had not named a verb. It returns
  `EXIT_USAGE` now, the same code argparse's own refusals carry.
- **A broken pipe was reported as an unreachable backend.** `BrokenPipeError` is
  an `OSError`, so `stealthy ls | head -1` fell through to the transport row and
  printed `error: could not reach the backend (BrokenPipeError: [Errno 32]
  Broken pipe)` — a false statement about the backend, made by the one function
  whose job is to keep transport and tool apart, on the commonest idiom in the
  shell. It has its own row ABOVE the transport row now, `EXIT_BROKEN_PIPE`
  (141 = 128 + SIGPIPE, what a coreutils program dying of SIGPIPE reports, so
  `set -o pipefail` sees the same thing from `stealthy` as from `ls | head`) and
  no message at all. `_abandon_stdout` closes the same hole at the other end:
  without it the interpreter's exit flush fails again OUTSIDE every handler and
  CPython exits **120**, a code outside the advertised set produced after the
  set had been honoured. Measured under mutation — moving the row below the
  transport row reproduces the shipped sentence verbatim.

  **The delta review then measured that half of that was still open, on the
  path a user meets first.** `_abandon_stdout` was reachable only from the
  verdict, i.e. only when a pipe error surfaced INSIDE a verb. A verb that
  SUCCEEDS printed through `print`, returned 0, and left the tail of any output
  above the 8 KB `TextIOWrapper` buffer to be written at interpreter
  finalisation — outside every handler. Reproduced product-free on this machine:
  exit **120** with `Exception ignored on flushing sys.stdout`, and on Windows
  the error is `OSError EINVAL` (errno 22), not `BrokenPipeError` at all. The
  reachable shape is any answer over 8 KB through a closed pipe — `stealthy call
  get_page_content | head -1`, `get_debug_view`, `list_network_requests` — and
  four documents were by then claiming 141 for it. Every run now ends with ONE
  explicit `sys.stdout.flush()` — `cli._delivered`, in `main`, after whichever
  dispatch table answered — keyed on `OSError` so the measured shape is covered
  and not only the pipe type. It sat in `_run` first, which covered the six tool
  verbs and left the eight ops verbs (`stealthy profiles | head -1`) on 120
  under the same binary (round-4 S2); one home in `main` is what makes the claim
  true of the CLI rather than of one table. It is a second guarded site and not
  a `_verdict` row on purpose: `_verdict` would route EINVAL to the transport
  row and answer 3. Pinned on both errnos for a tool verb and for an ops verb,
  mutation-checked (dropping the flush reddens all three), and witnessed through
  a REAL pipe by `tests/test_stealthy_cli_e2e.py` — the installed launcher,
  `tools --json` (far over 8 KB), read end closed before the first byte, exit
  141 and no `Exception ignored` on stderr. **That witness was RED on its first
  run and it was not the harness**: on Windows the write into the closed pipe
  raised inside the verb as a bare `OSError(EINVAL)`, not `BrokenPipeError`, so
  the type-keyed pipe row missed it and the transport row answered
  `error: could not reach the backend (OSError: [Errno 22] Invalid argument)`,
  exit 3 — about a round trip that had succeeded, the exact false statement
  review M2 fixed for the POSIX spelling. Every hermetic double had passed. The
  judgement is `_reader_gone` now: `BrokenPipeError` by type, or an `OSError`
  whose errno is EPIPE — or EINVAL on Windows only, §6.14 — asked in front of
  the table because it is the one judgement that reads an attribute; the
  hermetic pin runs under both spellings on every cell through the patchable
  `_WINDOWS` name. **Round 5 (delta review of `b09f3e7`, M1) measured that the
  flush in `main` was still only half of it for the ops verbs**: `_delivered`
  guarded the FLUSH and not the HANDLER, and an ops verb has no handler of its
  own, so output that crosses the 8 KB buffer raised from inside the verb and
  left `main` as a traceback and exit **1** — inside the set, "the tool said
  no" — measured with CPython's own `TextIOWrapper`/`BufferedWriter` stack (100
  B → 141, 60 000 B → escaped, both spellings). `main` now guards the dispatch
  as well, keyed on the same `_reader_gone` so a verb's genuine `OSError` still
  propagates; pinned over-buffer under both spellings and negatively for a
  `PermissionError`. Round-4 M1, from the same review:
  the fd-leak nit had left `_abandon_stdout`'s `os.open(os.devnull)` between two
  guarded calls, unguarded, so EMFILE escaped `main` from inside a handler —
  every call in that body is now tolerated, and the stack print under
  `--traceback` is under the same `suppress(OSError)` as the one-line report
  (round-4 S1: `... --traceback 2>&1 | head -1`).

  Two smaller doors out of the same set went with it: the one-line report is
  emitted under `contextlib.suppress(OSError)`, because `... 2>&1 | head -1`
  closes stderr and that `print` sits INSIDE the handler, so its own
  `BrokenPipeError` reached `sys.excepthook` — a traceback, exit 1 and a Sentry
  ship produced by the line reporting the failure quietly; and
  `asyncio.CancelledError` joined `_run`'s `except`, the last `BaseException`
  shape that was neither an `Exception`, a `KeyboardInterrupt` nor a group.
  `_abandon_stdout`'s body is now executed by a pin of its own (it asserts the
  DESCRIPTOR moved), where before only its CALL was pinned and replacing the
  body with `pass` passed the whole file.
- **`--traceback` shipped the tool's payload to Sentry.** It re-raised, so the
  exception left `main` past `sentry_init()` and `sys.excepthook` sent it —
  carrying a `BackendCallError` built from the tool's own words, which this
  module's docstring promises it never sends anywhere, and which
  `expected_events`' convention rule would not have dropped (it is not a
  `ToolError`). It prints the stack with `traceback.print_exc()` now and exits on
  the verdict's code. Removing the re-raise is also what made `BLE001` fire on
  `_run`'s catch, which now carries an owner-tagged suppression rather than a
  lint-shaped excuse for narrowing the one place that must catch everything.
- **`http_client` — "THE one transport seam" — was executed by no test.** The
  `fake_backend` fixture RE-IMPLEMENTED its body (the same `httpx.Timeout`, the
  same `follow_redirects`) and then replaced the production function, so
  swapping which clock got the caller's budget, or dropping the redirect
  setting, failed nothing. Two nodes now drive the real one: the budget lands on
  `read` and `connect` keeps its own short clock, and `--timeout 9` is asserted
  end to end at the seam. Measured under mutation — swapping the two clocks, and
  making `_timeout` ignore the flag, each turn exactly one of them RED.
- **`TestToolsVerb` was mutating the real `os.environ` permanently.** Every node
  there reaches `cli._server()`, which `setdefault`s `STEALTH_MCP_NO_AUTO_RECOVERY`
  and then runs `backend_env.scrub_process_env()`, whose `_remove` is a real
  `del` — and `conftest` has no autouse env isolation, so the residue reached
  every later node in the session. An autouse fixture swaps the whole mapping
  for a copy; swapping rather than `delenv` is what covers the deletions, whose
  names belong to the operator and are not knowable here.
- **The autouse record fixture redirected one NAME and claimed the path.** It
  set `singleton.SERVER_STATE_FILE` only — sufficient for today's nodes, and one
  un-redirected writer away from the leak the fixture exists to prevent, since
  every writer resolves its own name in `backend_registry`. That module's
  `STATE_DIR`, `SERVER_STATE_FILE` and `PORT_FILE` are redirected in the same
  fixture now, so the docstring is true of the code rather than of the intent.
- **README documented `--no-start` without the eviction disclosure** the other
  three surfaces carry (`backend_url`'s docstring, the flag's own help, RUNBOOK's
  recovery recipe). It is the most user-facing of the four and the only one an
  installing user reads; it carries the same clause now.

## 4. Name check

- `Get-Command stealthy` on the development machine: **not found** — no
  collision on PATH.
- `https://pypi.org/pypi/stealthy/json`: **404** — no PyPI project of that name,
  so no third-party distribution can install a competing `stealthy` script today.

Neither is a guarantee about the future: a PyPI project named `stealthy` could be
registered tomorrow and ship a console script of the same name, and pip would let
whichever was installed last win. That risk is named rather than mitigated —
`stealth-chrome-devtools` remains installed and unambiguous.

## 5. Proof

`tests/test_stealthy_cli.py`, 81 hermetic nodes: argument parsing (JSON vs
string, the `--json` merge, the three usage refusals), result unwrapping (all
three measured shapes plus the two negative ones), prefix resolution (exact wins,
ambiguous names every match), TTY vs pipe, backend selection (one probe; the
`--no-start` exit 3; the start path; the never-ready exit 3), the closed exit-code
set end-to-end through `main` (each kind asserting the code, one line of stderr
and no `Traceback`), the four session-termination paths against the real SDK, the
six verbs' arguments and rendering, and `spawn --url`'s ordering on both the JSON
and the table path, the one transport seam's two clocks and `--timeout` reaching
it. Nothing starts a backend, opens a socket or launches Chrome — and an autouse
fixture points `singleton.SERVER_STATE_FILE` **and `backend_registry`'s
`STATE_DIR`/`SERVER_STATE_FILE`/`PORT_FILE`** at a tmp dir for every node, so the
operator's own record is unreachable from this file by construction rather than
by each node remembering.

`tests/test_stealthy_cli_e2e.py`, nodes marked `integration`: an isolated
backend started in a throwaway `HOME`, a `server.json` written by hand so the
real record is never opened, and the installed `stealthy` console script driven
as a subprocess through `tools`, `call list_instances` and `ls`. No Chrome. A
backend that never becomes ready is a `pytest.fail` with the console tail and no
longer a `skip` — both nodes hang off that fixture, so a skip made a broken
backend read as a green tier.

Updated: `tests/test_doc_examples.py` (the scripts table, the README CLI
section's allowed names, and a second launcher-resolution node),
`tests/test_doc_claims.py` (the six verbs and the new leaf).

## 6. Residuals

1. **`shot` is not here.** Phase 2, with the rest of the sugar worth having
   (`tabs`, `state`, shell completion, `--watch`).
2. **A verb that makes two calls makes two MCP sessions.** `spawn --url`, `nav`
   and `close` each open one per call. Deliberate — one home for open-and-
   terminate beats every verb owning a session's lifetime — but it is two
   handshakes where one would do, and a shared per-command session is the obvious
   later refinement.
3. **A cold start from this CLI can still evict a wedged backend of another
   build.** When nothing answers, `backend_url` takes `ensure_server_running` —
   the proxy's own path — and a wedged foreign-identity backend on this display
   context owning no live browser is terminated and replaced. It is not narrowed,
   because a second startup path would be a second way to start a backend and
   this one carries the cold-start lock; `backend_eviction.protected` still
   spares anything holding a browser, and `--no-start` is the opt-out. Disclosed
   in `backend_url`'s docstring, `--no-start`'s help and RUNBOOK's recovery
   recipe. What is NOT a residual: a backend that answers is never evicted,
   whatever build it is, and that is pinned.
4. **`_scalar` is bare `json.loads`, so `--arg x=null` is `None` and
   `--arg x=NaN` is a float** (review S5). Unpinned, and unchanged this round.
   `null` is the likeliest real surprise after `headless=false`. Nothing
   non-ASCII is pinned either, and a unicode argument round-trips through
   `json.dumps` and `print` onto a Windows console, which is a live
   `UnicodeEncodeError` surface adjacent to F-823.
5. **70, 130 and 141 widen the advertised set from four codes to seven.** The
   brief asked for 0/1/2/3 plus one documented code for `Ctrl-C`. A CLI bug
   mapped to 1 would be the exact confusion M1 removes and mapped to 3 would be
   a lie; a broken pipe mapped to 3 was the lie review M2 found shipped. Each
   got its own conventional code rather than a collision. Named here because it
   is a deviation from what was asked for, not because it is in doubt.
6. **The three calls after `_abandon_stdout`'s `os.open` are not executed by
   any test.** The broken-pipe nodes assert it is CALLED (it is monkeypatched),
   because letting the real one run would point the pytest process's own fd 1
   at the null device; the two EMFILE nodes run the real body but return at the
   failed `os.open`, before `dup2`, its `except` and the `os.close` suppress.
   What is pinned is the decision; what is not is the stdlib call under it.
7. **The cancellation node drives `opened()` and not `call_tool`.** So "a tool
   call cancelled mid-flight still DELETEs" is pinned one layer below the shape
   an operator's `Ctrl-C` actually takes. Making it faithful needs a transport
   that hangs, and `httpx.MockTransport`'s handler is synchronous — it would
   block the loop it is supposed to let cancel.
8. **`--json` means two things.** On `call` it supplies the arguments object; on
   the other five it selects output. `call` has no output mode to choose (it
   always prints the tool's structured result), and the help text says so, but a
   caller typing `stealthy call list_instances --json` expecting output-JSON gets
   an argparse "expected one argument" error rather than what they meant. It is a
   loud failure, not a silent wrong answer, which is why it was accepted.
9. **`tools` pays an `import fastmcp`.** Section grouping and `--section` read
   `tool_registry.SECTION_TOOLS`, which is filled by `embedded/server.py`'s
   binding loop, so the verb goes through `cli._server()`. No other tool-driving
   verb does. A backend-side section field on the tool list would remove it.
10. **`ls` cannot show headed/headless or the profile.** `list_instances` reports
   neither (F-874's three record shapes). `get_instance_state` has them, per
   instance, one `stealthy call` away. Widening `list_instances` is a tool-surface
   change and was deliberately not made here.
11. **`cli.py` is at 937 raw lines against a 1000-LOC budget that ratchets down
   only** — **63 lines of headroom** (890 after the extraction; rounds 4–5's
   `_delivered` and the dispatch guard cost 47), and the cut that bought them
   has been made rather than merely named. `cli_call.py` sits at 999.

   It came due at the merge of main's F-892..895, whose `profiles` seed lines
   put the file at **1016** and made pre-commit refuse the merge commit. The
   two candidates were F-895's `profiles` rendering (`_collect_profiles`,
   `_role`, `_seed_line`, ≈75 lines) and F-891's own tool-verb SURFACE
   (`_backend_flags`, `_add_tool_verbs`, the six `_cmd_*` shims, ≈131). The
   second was taken, for a reason that is not its size: the shipped split put
   `spawn`'s `--profile` declaration in `cli.py` while the only statement of
   what `--profile` MEANS was in `cli_call`, so adding a flag touched two homes
   and either could drift — convention 4 reached from the side where the second
   way is a second FILE. `cli_call.add_parsers(sub)` and `cli_call.DISPATCH`
   are the whole interface; `cli.py` keeps the one parser tree, the one `main`,
   the dispatch order and both script names, so "not a second CLI" is still
   true and is now a statement about the TREE rather than about where six
   `add_argument` calls are written. The profiles alternative would have left
   that split in place and bought a third of the headroom.

   It costs one thing, stated rather than hidden: building the parser now
   imports `cli_call` on every invocation, so `stealthy status` pays for it.
   Measured, that is `argparse` and `sys` — the module's entire import list —
   because `backend_client`, and through it `httpx` and the `mcp` SDK, are
   still reached inside the functions that need them. The property the lazy
   `_cli_call()` was written for ("an ops verb never pays for the MCP client")
   is intact; the docstring no longer claims more than that.

   `tests/test_doc_claims.py` grew the pin the split needs: every documented
   verb must resolve through the SAME two-table lookup `main` uses, and the
   reverse direction — every dispatchable verb is offered by the parser — is
   its own node, because two tables is exactly how a body could come to have no
   parser.

   The number has been wrong here more than once, so the method is recorded
   rather than the answer alone. `tools/check_file_budgets.py:207` counts
   `len(path.read_text(encoding="utf-8").splitlines())` — raw lines, blanks and
   comments included — and that is the only count the gate enforces. Measured
   that way through git blobs: `c13b12a` 956, `1eee656` 977, `dfcbc44` 981,
   `db26a23` 981, the F-892..895 merge **1016** (over, and refused), this commit
   **890**. An earlier draft of this section said 956, which was true of
   the commit it was written against and stale afterwards; a later one said 985
   with "15 lines of headroom", which was true of no commit at all — it was
   asserted from memory rather than read, which is the same failure as the 819
   below by a shorter route. The round-2 review said 819 and concluded "181
   lines of headroom"; 819 is what `rtk proxy git show <rev>:<path> |
   Measure-Object -Line` returns for a file whose real count is 977, and the
   same pipeline returns 564 for a 693-line `cli_call.py` — so the shell
   pipeline, not the file, is what shrank. **Count this file by reading its
   bytes, never through that pipeline** (`rtk-grep-false-negatives`, the same
   hazard at a different verb).
12. **`--profile` is superseded the day F-892+ lands.** The session vocabulary
   (`--session NAME` to name a session, `--from <session>` to say what it is
   seeded from, `default` reserved for today's master) is the spelling a user of
   the session system should ever need; `--profile` then stays as the RAW
   directory escape hatch — the one way to hand `spawn_browser` a
   `user_data_dir` verbatim, including an absolute path to master's own
   directory. Two consequences are live until then and are not this CLI's to
   fix: a bare relative name resolves under the clone root, so `--profile master`
   is `sessions/master` and not the master profile (F-894), and whether
   `resolve_profile_selection` should recognise its own master directory when it
   is named explicitly (the role reads `explicit`, truthfully) is a
   profile-selection question owned by that study. Changing either silently
   would move what every existing `user_data_dir=` caller gets.
13. **The PyPI name is unclaimed, not reserved.** See §4.
14. **The reader-gone judgement is keyed on the ERROR, not on the site** (delta
   review S3, round 4). It is `_reader_gone`: `BrokenPipeError` by type, or an
   `OSError` whose errno is EPIPE — or EINVAL on Windows only — asked in front
   of `_verdict`'s table (and, since round 5, around `cli.main`'s dispatch for
   the ops verbs). The docstring's "it comes from OUR OWN `print`" is the
   overwhelmingly common case rather than a guarantee, and it now costs two
   things. (a) `httpx` writing to a backend socket the peer closed raises
   `BrokenPipeError` out of the transport, so the operator gets an empty stderr
   and 141 where the truthful answer is 3; low-frequency (loopback, and it is
   the READ that usually fails, giving `TimeoutError`). (b) On Windows an
   `OSError(22)` from the cold start inside the same `try` (`ensure_server_running`
   spawns, locks and writes the record; `ERROR_INVALID_PARAMETER` maps to
   errno 22) also reads as 141 with an empty stderr where `81086bd` answered 3
   with a named message — and since round 5 the same judgement wraps all eight
   ops-verb BODIES in `cli.main`, so a `cleanup --apply` whose delete fails with
   `OSError(22)` on Windows is a silent 141 too (measured), where every other
   errno still propagates as the verb's own failure (§6.17). The arm is scoped to `win32` so six of nine CI cells
   carry no widening; the transport itself is NOT an exposure — anyio converts
   every socket `OSError` to `Broken/ClosedResourceError`, httpcore has none,
   httpx maps to `httpx.*`. Kept rather than fixed, deliberately: narrowing to
   the site means a flag set around every emit and consulted here, a second way
   to know where an exception came from, in the one function whose whole job is
   to not need one.
15. **`stealthy` bare is pinned in two suites.** The same two assertions live in
   `tests/test_cli.py` and in `TestExitCodesAreClosed`. Left as a pair on
   purpose — they are two suites' contracts, and the class that owns the closed
   set should be able to see the one path through `main` that reaches no verb —
   but it is the one duplicate the dedupe pass kept, so it is named here rather
   than rediscovered.
16. **Two doors out of the set that stay open, named.** (a) `cli._delivered`'s
   flush catches a blanket `OSError`, so a full disk (`stealthy ls --json >
   out` on a full volume, ENOSPC) answers 141 "the reader went away" with an
   empty stderr; same family as §6.14, one line of a script's `set -o pipefail`
   away from a misdiagnosis, and the cost of not keying that flush on errno
   before the finalisation shape is measured on more than one platform. (b)
   `stealthy --help | head -1` is still 120: argparse prints help and raises
   `SystemExit(0)` from inside `parse_args`, so `main` never returns and no
   flush runs; 1722 B of help dies at finalisation. No document claims a code
   for it. Closing it means catching `SystemExit` around `parse_args` in `main`
   and returning `_delivered(exc.code)` — a second exit path for argparse's own
   2 alongside the raise, which is why it is named here as a follow-up rather
   than added to a round already at the `cli_call.py` cap.
17. **An ops verb's own `OSError` still leaves `main` as a traceback and exit
   1.** `main`'s guard converts only the reader-gone shape (`_reader_gone`);
   any other errno from an ops-verb body — a directory `cleanup --apply` cannot
   delete, a record `stop` cannot rewrite — propagates exactly as before F-891,
   pinned negatively (`test_an_ops_verbs_own_oserror_is_not_mistaken_for_a_pipe`).
   Deliberate: those verbs pre-date the closed set, no document claims a code
   for their failures, and mapping them to 141 would hide a real failure behind
   "the reader went away". Giving the ops verbs their own verdict is the
   follow-up, and it is a `cli.py` change, not a `cli_call.py` one.
