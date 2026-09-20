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
| `spawn [--profile\|--master] [--headed\|--headless] [--url]` | `spawn_browser` + optional navigation |
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

**`--master` names the directory.** This is the part most likely to be
"simplified" later, so: an argument-less `spawn_browser()` reaches master only
while master is FREE — `clone_storage.resolve_profile_selection`
(`clone_storage.py:971`) tests `_profile_has_running_browser(master)` and falls
through to a clone the moment a browser holds it. A flag whose promise is "the
master profile itself" therefore cannot be spelled as an unnamed spawn. Naming
the directory also buys F-888: the re-attach branch in
`tool_sections/browser_management.py` is gated on `if user_data_dir:` and runs
BEFORE profile selection, so a master that is already running is re-attached to
rather than cloned past. The measured consequence is that the role is
`explicit`, not `master` — nothing special-cases the master path
(`master_profile_dir()` has three call sites, all inside `clone_storage`) — and
the verb PRINTS the role it got instead of relabelling it.

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

## 5. Proof

`tests/test_stealthy_cli.py`, 55 hermetic nodes: argument parsing (JSON vs
string, the `--json` merge, the three usage refusals), result unwrapping (all
three measured shapes plus the two negative ones), prefix resolution (exact wins,
ambiguous names every match), TTY vs pipe, backend selection (one probe; the
`--no-start` exit 3; the start path; the never-ready exit 3), the six verbs'
arguments and rendering, and the `terminate_on_close` pin. Nothing starts a
backend, opens a socket or launches Chrome.

`tests/test_stealthy_cli_e2e.py`, one node marked `integration`: an isolated
backend started in a throwaway `HOME`, a `server.json` written by hand so the
real record is never opened, and the installed `stealthy` console script driven
as a subprocess through `tools`, `call list_instances` and `ls`. No Chrome.

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
6. **`cli.py` is at 957 raw lines against a 1000-LOC budget that ratchets down
   only.** The next verb extracts, it does not fit. The natural next cut is the
   ops verbs' bodies, on the same argument that moved these six out.
7. **The `--master` role reads `explicit`.** Truthful, and mildly surprising to
   anyone expecting `master`. Whether `resolve_profile_selection` should
   recognise its own master directory when it is named explicitly is a
   profile-selection question, owned by the parallel master/snapshot UX study,
   and was deliberately not decided here: changing it silently would move what
   every existing caller of `user_data_dir=` gets.
8. **The PyPI name is unclaimed, not reserved.** See §4.
