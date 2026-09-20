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

## 4. Name check

- `Get-Command stealthy` on the development machine: **not found** — no
  collision on PATH.
- `https://pypi.org/pypi/stealthy/json`: **404** — no PyPI project of that name,
  so no third-party distribution can install a competing `stealthy` script today.

Neither is a guarantee about the future: a PyPI project named `stealthy` could be
registered tomorrow and ship a console script of the same name, and pip would let
whichever was installed last win. That risk is named rather than mitigated —
`stealth-chrome-devtools` remains installed and unambiguous.

## 3b. What the first review changed (and one thing it changed back)

The review at `a4b4741` found the promised closed exit-code set was not closed
and the session termination the client exists to guarantee was pinned only as a
keyword argument. Both are now behaviours rather than claims.

- **The exit codes are closed by construction.** `cli_call._verdict` maps every
  exception onto one code and `_run` catches `Exception`, `KeyboardInterrupt`
  and `BaseExceptionGroup` (the shape an interrupt takes through the transport's
  own task group). A transport failure is **3** and not 1: nothing on the
  backend saw the request, so there is no answer to report. Our own bug is
  **70** (`EX_SOFTWARE`) and deliberately not 1, which means "the tool said no".
  `Ctrl-C` is **130**. Measured under mutation: narrowing the `except` back to
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

## 5. Proof

`tests/test_stealthy_cli.py`, 74 hermetic nodes: argument parsing (JSON vs
string, the `--json` merge, the three usage refusals), result unwrapping (all
three measured shapes plus the two negative ones), prefix resolution (exact wins,
ambiguous names every match), TTY vs pipe, backend selection (one probe; the
`--no-start` exit 3; the start path; the never-ready exit 3), the closed exit-code
set end-to-end through `main` (each kind asserting the code, one line of stderr
and no `Traceback`), the four session-termination paths against the real SDK, the
six verbs' arguments and rendering, and `spawn --url`'s ordering on both the JSON
and the table path. Nothing starts a backend, opens a socket or launches Chrome —
and an autouse fixture points `singleton.SERVER_STATE_FILE` at a tmp file for
every node, so the operator's own record is unreachable from this file by
construction rather than by each node remembering.

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
5. **70 and 130 widen the advertised set from four codes to six.** The brief
   asked for 0/1/2/3 plus one documented code for `Ctrl-C`. A CLI bug mapped to
   1 would be the exact confusion M1 removes and mapped to 3 would be a lie, so
   it got `EX_SOFTWARE` rather than a lie or a collision. Named here because it
   is a deviation from what was asked for, not because it is in doubt.
3. **`--json` means two things.** On `call` it supplies the arguments object; on
   the other five it selects output. `call` has no output mode to choose (it
   always prints the tool's structured result), and the help text says so, but a
   caller typing `stealthy call list_instances --json` expecting output-JSON gets
   an argparse "expected one argument" error rather than what they meant. It is a
   loud failure, not a silent wrong answer, which is why it was accepted.
4. **`tools` pays an `import fastmcp`.** Section grouping and `--section` read
   `tool_registry.SECTION_TOOLS`, which is filled by `embedded/server.py`'s
   binding loop, so the verb goes through `cli._server()`. No other tool-driving
   verb does. A backend-side section field on the tool list would remove it.
5. **`ls` cannot show headed/headless or the profile.** `list_instances` reports
   neither (F-874's three record shapes). `get_instance_state` has them, per
   instance, one `stealthy call` away. Widening `list_instances` is a tool-surface
   change and was deliberately not made here.
6. **`cli.py` is at 956 raw lines against a 1000-LOC budget that ratchets down
   only.** The next verb extracts, it does not fit. The natural next cut is the
   ops verbs' bodies, on the same argument that moved these six out.
7. **`--profile` is superseded the day F-892+ lands.** The session vocabulary
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
8. **The PyPI name is unclaimed, not reserved.** See §4.
