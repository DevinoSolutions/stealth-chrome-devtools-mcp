# F-890 — an inherited `FASTMCP_*` environment variable crashes every backend launch at import

**Severity**: HIGH — one environment variable in the MCP client's process makes the
backend unstartable for as long as that client runs, with no recovery path and no
message that names the cause.

**Status**: FIXED (this PR). `singleton._start_server_process` scrubs the child env
through the new `embedded/backend_env.py` before `backend_launch.spawn` sees it.

---

## 1. The mechanism

The backend is a child of the stdio proxy, and `singleton._start_server_process`
hands it the parent environment whole:

```python
child_env = dict(os.environ)
child_env.pop("STEALTH_MCP_NO_AUTO_RECOVERY", None)
...
launched = backend_launch.spawn(cmd, child_env, boot_log)
```

That environment belongs to the MCP client (Claude Code), not to us. Everything in
it reaches the backend, including names that belong to a THIRD party.

`embedded/server.py` imports `fastmcp` at module scope (`from fastmcp import FastMCP`,
line 9). The `fastmcp` distribution — 2.11.2, pinned in `pyproject.toml`, and a
different package from `mcp.server.fastmcp` — instantiates its settings **at module
import**, and those settings are a `pydantic_settings.BaseSettings` that reads the
process environment:

```python
# .venv/Lib/site-packages/fastmcp/settings.py
class Settings(BaseSettings):
    model_config = ExtendedSettingsConfigDict(
        env_prefixes=["FASTMCP_", "FASTMCP_SERVER_"],
        env_file=".env",
        extra="ignore",
        env_nested_delimiter="__",
        ...
    )
    port: int = 8000
```

So an inherited `FASTMCP_PORT` is parsed into `port: int` before one line of our code
runs — before `build_arg_parser`, before `--port` exists as a concept. An empty value
is not "unset" to pydantic: it is the string `""`, and `int("")` fails validation.
The import raises `pydantic.ValidationError` and the process dies with a non-zero
exit before `configure_logging` has installed anything, so the only trace is
`backend-boot.log` (F-303) — which is why the 04:26–05:33 window on 2026-09-18 shows
sixty-six minutes of launches that each died the same way while the proxy above them
retried, healed, and gave up.

**Measured, on the installed stack** (`mcp` 1.27.1, `fastmcp` 2.11.2, Python 3.13.11):

| env name set to `""` | outcome |
|---|---|
| `FASTMCP_PORT` | **`ValidationError: port — Input should be a valid integer, unable to parse string as an integer [type=int_parsing, input_value='', input_type=str]`** at `import fastmcp` |
| `port` / `PORT` / `Port` | no effect — `port=8000` |
| `HOST` / `DEBUG` / `LOG_LEVEL` / `HOME` / `TEST_MODE` | no effect |
| `FASTMCP_PORT` (against `mcp.server.fastmcp.FastMCP`) | no effect — that Settings takes every field as an explicit init kwarg, which outranks every env source |

**The incident brief's mechanism was wrong in one detail and it matters.** It named a
bare `port` variable. The bare names are NOT read: `fastmcp`'s `model_config` reports
`env_prefix=''`, which is what makes a bare name look plausible, but the actual source
is `ExtendedEnvSettingsSource`, which overrides `get_field_value` to walk
`env_prefixes` instead. The names that reach the backend's configuration are
`FASTMCP_<FIELD>`, `FASTMCP_SERVER_<FIELD>` (deprecated, still honoured, and it logs a
warning saying so) and `FASTMCP_EXPERIMENTAL_<FIELD>` for the nested experimental
block. A fix written against the bare names would have scrubbed `port` and `host`,
left `FASTMCP_PORT` in place, and shipped as a fix for a bug it did not touch.

On Windows the lookup is case-insensitive twice over — `case_sensitive` is False in
`model_config`, and the OS folds env-var case itself — so `fastmcp_port`,
`FastMCP_Port` and `FASTMCP_PORT` are one variable.

## 2. Why the backend must not inherit them at all

The narrow fix is "override `FASTMCP_PORT` with our own port". It is the wrong fix,
for the reason convention 4 exists: there would then be two places that decide what
port the backend listens on, and the second one is a string in someone else's
environment.

Our backend's configuration comes from ONE place and always has: the argv
`_server_process_cmd` builds, consumed by `build_arg_parser` and handed to
`mcp.run(transport="http", host=args.host, port=args.port)`. Every `FASTMCP_*` name is
an input to a decision we have already made. `port` and `host` can make the backend
unstartable or make it bind somewhere the record does not name; `log_level` fights
`configure_logging`; `json_response` and `stateless_http` change the transport the
proxy is written against; `streamable_http_path` moves the URL `_backend_http_url`
hardcodes. None of them has a legitimate reading for this process. So the rule is
**drop the whole `FASTMCP_` family**, not "override the two that have bitten us".

## 3. The rule chosen, and the two rejected

### Chosen — scrub by PREFIX, at the one child-env composition site

`backend_env.scrub(env)` removes every key whose name starts with `FASTMCP_`
(case-folded), returns the names removed, and logs them at INFO — **names only, never
values**, because an env var is a place secrets live and this line reaches the durable
log. One prefix covers all three families (`FASTMCP_SERVER_` and
`FASTMCP_EXPERIMENTAL_` both start with it), so there is one constant and not three.

The call sits in `singleton._start_server_process`: that is THE one place a child
environment is composed, and `backend_launch.spawn` deliberately passes whatever it is
handed (its scheduler rung serialises the env into a JSON spec — F-867 — so a scrub
after that point would have to happen twice).

It **absorbs** the `STEALTH_MCP_NO_AUTO_RECOVERY` pop that was already on the line
above it rather than sitting beside it. That pop (M8-2) exists because a spawned
backend must always reap its own orphaned browsers even when the CLI-invoking parent
set the flag to skip its own recovery-on-import — which is the same sentence as the
one this finding is about: *a name in the parent's environment must not reach a
decision the backend makes for itself*. Two removal sites two lines apart, answering
one question, is the shape convention 4 calls a defect, so there is now one. The
behaviour is byte-identical (`tests/test_singleton_stop_restart.py::TestSpawnEnvScrub`
still passes unchanged) and `singleton.py` went 999 -> 997 LOC rather than growing,
which is what left room for F-889's wiring in the same file.

### Added by the review (M5) — the same table, applied to our OWN environment

The composer is not every path by which this package imports `fastmcp`. Two more reach
that import with the operator's environment untouched:

- `server.main()`'s `runpy` fallthrough — `--transport http` runs `embedded/server.py`
  **in this process**, so an operator with a stray `FASTMCP_PORT=""` in their shell gets
  the identical import-time `ValidationError`, with the identical absence of a log line;
- `stealth-chrome-devtools serve --http`, which reaches the same line through
  `cli._cmd_serve`.

`backend_env.scrub_process_env()` is `scrub` applied to `os.environ`, called ONCE as the
first statement of `server.main()` — the one entrypoint every one of those paths goes
through, including `_cmd_serve`, which delegates to it. One function, one table, one
call site. The composer keeps its own call because `restart_backend` reaches it from the
ops CLI without passing through that entrypoint at all.

**The `os.environ` exception, argued rather than assumed.** This repo confines
`os.environ` to `settings.py`. That rule is about CONFIGURATION: every `STEALTH_MCP_*`
knob is a typed field in one home, and a second reader of one is a second answer. This
reads no configuration and produces no value — it deletes a THIRD PARTY's names from our
own process before a library parses them, by the table stated one function above.
Putting the call in `settings.py` would move a `FASTMCP_` prefix into the `STEALTH_MCP_*`
home and make `settings` an importer of `embedded/`; splitting the table from its
application would put one rule in two files. Neither is better than one named exception,
so the deviation is named here and in the module docstring.

Deleting from `os.environ` is a real `unsetenv`, so the removal also covers anything the
process later spawns — which is why the composer's call is belt-and-braces rather than
the only defence. `scrub` takes a `MutableMapping` for exactly these two callers.

The prefix constant lives in `backend_env`, not in a test, and a test proves it still
covers the installed library: `tests/test_backend_env_scrub.py` imports
`fastmcp.settings.Settings`, reads `env_prefixes` off its own `model_config`, and
fails if any of them stops starting with ours. That is the shape this tree uses for a
third-party fact — the constant is ours, the pin is the library's.

### Rejected — enumerate the field names

`FASTMCP_PORT`, `FASTMCP_HOST`, … derived from `Settings.model_fields`. It reads more
precise and it is worse: it requires importing `fastmcp` to compute, and **the stdio
proxy must never import `fastmcp`**. The proxy today reaches `backend_launch` without
touching the MCP server stack at all; `desktop_launch` already carries a paragraph
about why its nodriver import is lazy (≈175 ms warm versus ≈470 ms to reach a seam).
Paying a full `fastmcp` import in every proxy cold start to compute a list of names we
can write down is the same defect with a more impressive spelling. A hardcoded list
would drift the first time the library adds a field.

### Rejected — set `FASTMCP_PORT` to our port instead of dropping it

Two homes for one decision, and it only fixes the field that happened to crash. An
inherited `FASTMCP_LOG_LEVEL=` would still be a `ValidationError` against a `Literal`,
and an inherited `FASTMCP_STATELESS_HTTP=true` would not crash at all — it would
silently give every proxy a transport the bridge is not written against, which is a
worse outcome than a crash that names itself.

## 4. Blast radius

`scrub` has two callers. `_start_server_process` passes a `dict` copy it owns, so that
call affects nothing but the child it is about. `scrub_process_env` passes `os.environ`
and therefore DOES change the running process — deliberately, and only by removing
`FASTMCP_*` and `STEALTH_MCP_NO_AUTO_RECOVERY`, none of which any code of ours reads.
For the stdio proxy that is a no-op in effect; for the `runpy` fallthrough it is the
whole fix.

A backend that WANTED a `FASTMCP_*` setting loses it. There is no such caller: none of
our code reads one, `mcp.run` is given host and port explicitly, and
`RUNBOOK.md`/`settings.py` document `STEALTH_MCP_*` as the knob surface. `STEALTH_MCP_*`
names are untouched — `settings.py` remains the one env home.

The `.env` file is a residual and not addressed here; see §6.

## 5. Tests

`tests/test_backend_env_scrub.py`:

- an inherited `FASTMCP_PORT=""` in the parent env is absent from the child env the
  composer hands to `backend_launch.spawn` — the F-890 pin, driven through
  `singleton._start_server_process` with `spawn` captured, so it pins the real
  composition site and not the leaf in isolation;
- `FASTMCP_SERVER_PORT`, `FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER` and the
  case-folded `fastmcp_port` go too;
- `STEALTH_MCP_*`, `PATH` and every unrelated name survive;
- `STEALTH_MCP_NO_AUTO_RECOVERY` still goes, so absorbing the pop did not lose it;
- `scrub` never touches `os.environ` — the child-env path leaves the composer's own
  environment unchanged;
- **the M5 half**: `server.main()` with `--transport http` reaches `runpy` with
  `FASTMCP_PORT` already absent from `os.environ`, the stdio branch is scrubbed too
  (one call at the top of the one entrypoint, not one per branch), and
  `scrub_process_env` is pinned to be `scrub(os.environ)` rather than a second name
  table;
- the INFO line names the variables and carries no value;
- the library pin: every prefix in the installed `fastmcp.settings.Settings`'
  `env_prefixes` starts with `backend_env.FASTMCP_PREFIX`, and `port` is still
  annotated `int` (which is why an EMPTY value is a crash and not an override);
- **the mechanism itself**, in a subprocess (the crash is at import and this process
  has already imported `fastmcp`): `import fastmcp` under `FASTMCP_PORT=""` exits
  non-zero with `ValidationError` naming `port`, and the same environment after
  `scrub` imports cleanly. Everything else in the file would pass just as happily if
  the variable were harmless; this node is what says the scrub is load-bearing. 3.5 s.

## 6. Residuals

1. **`.env` in the backend's working directory is still read.** `fastmcp`'s
   `model_config` sets `env_file=".env"`, and the backend inherits the proxy's CWD,
   which is the MCP client's — frequently a project directory that has one. The
   prefix still applies, so only a `FASTMCP_*` key in that file matters, which is
   rare enough that changing the backend's CWD (a much larger behavioural change,
   with its own effect on every relative path a tool is handed) is not worth it here.
   The scrub does not and cannot cover it: pydantic-settings reads the file itself.
2. **Only `FASTMCP_` is scrubbed.** Any other import-time `BaseSettings` in any
   dependency we acquire later has the same exposure, and this fix does not
   generalise to it. Generalising is not possible without knowing the prefixes, which
   is the same import cost §3 rejects. What makes this acceptable is that the failure
   is loud and named: an import-time `ValidationError` lands in `backend-boot.log`
   with the offending field in it.
3. **A backend already crash-looping keeps crash-looping until the client restarts.**
   The scrub fixes the launch, so a proxy that heals will now bring one up — but a
   backend spawned by a 2.1.9 proxy in the same fleet still inherits the variable.
   Mixed-version fleets must upgrade together, which is the standing rule.
4. **We never learn that the variable was there** unless the backend spawns. The INFO
   line is written by the spawning proxy, so a session that only ADOPTS an existing
   backend reports nothing — correctly, since it composed no environment.
