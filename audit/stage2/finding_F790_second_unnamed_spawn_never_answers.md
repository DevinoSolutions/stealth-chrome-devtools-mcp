# F-790 — the auto-clone spawn path blocks forever on an unanswered `roots/list`

**Status:** OPEN — characterized by plan_RELEASE W13, not fixed (W13 is zero-`src/`).
**Severity:** HIGH. An unbounded **server→client** round trip sits on the
default `spawn_browser` path. A client that does not implement MCP `roots` gets
no result, no error, and no timeout — ever.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/clone_storage.py`
— `_client_session_seed()` (`await context.list_roots()`), reached from
`_clone_profile_dir_for_session` ← `resolve_profile_selection`'s
`_profile_has_running_browser(master)` branch ← `embedded/server.py::spawn_browser`.
**Found by:** plan_RELEASE §2.13 (W13) while building MQ-139's multi-instance
isolation node.

## The behavior

`spawn_browser` documents `user_data_dir` as *"Leave UNSET for normal use"*, so
the unnamed call is the advertised path. It resolves a profile like this:

* **master free** → use the master directory. No client round trip. Fast.
* **master already held by a live instance** → auto-clone, and the clone's name
  is derived from `_client_session_seed()`, which does:

```python
from fastmcp.server.dependencies import get_context
context = get_context()
for root in await context.list_roots():      # <-- server asks the CLIENT
```

`context.list_roots()` sends a `roots/list` **request to the client** and awaits
the reply. There is no timeout on it. The surrounding `except Exception` cannot
help: an unanswered request never raises, it simply never completes.

Measured over the raw wire, with one instance already holding master:

| second `spawn_browser` | client answers `roots/list`? | outcome |
|---|---|---|
| unnamed | **no** | **no answer at all** — observed to 420 s: no result, no error, no timeout |
| unnamed | yes (4 requests answered: 1 + 3 spawn retries) | typed failure: `Error calling tool 'spawn_browser': Failed to spawn browser: …` |
| named (`user_data_dir="…"`) | n/a — this branch never asks | **ok, 0.9 s** |

The first spawn, on the same machine, in the same backend, over the same wire,
answers in **0.7 s** — so slowness is not the explanation. The backend log shows
the stuck call entering the tool and never leaving it:

```
… tool spawn_browser start          # and no matching "tool spawn_browser end"
```

with no `browser_manager.spawn_browser: Platform: …` line, i.e. parked before
Chrome is ever launched. Sampling the session root during the stall confirms it:
no clone directory is ever created.

The frame itself is the proof, and it is the pin's oracle:

```json
{"method": "roots/list", "jsonrpc": "2.0", "id": 0}
```

## Why it is worth a finding

1. **An unbounded wait on a client capability the protocol makes OPTIONAL.**
   MCP `roots` is optional; a client may legitimately declare no `roots`
   capability. This product then hangs a core tool on it. Claude Code answers
   `roots/list`, which is why nothing has noticed.
2. **No bound and no typed failure.** Every other failure here is bounded —
   `navigate` clamps to 60 s, `_with_cdp_timeout` wraps the CDP surface. This one
   has no deadline, and `CLAUDE.md` convention 2 (tools raise on failure) cannot
   apply to a call that never returns.
3. **It is on the concurrency path specifically.** The product exposes
   `list_instances`, per-instance ids and `close_instance` — an explicitly
   multi-instance API whose default path cannot reach instance number two.

## What is NOT claimed

With `roots/list` answered, the second unnamed spawn still failed — typed, after
three retries (`Failed to spawn browser`). That was measured on a machine
carrying ~266 stray Chrome processes from concurrent work, so it is recorded as
an open observation, **not** as part of this finding. The finding is the
unbounded wait, which reproduces regardless of Chrome's state because it never
reaches Chrome.

## Evidence

`tests/test_wire_semantics.py::test_a_second_unnamed_spawn_blocks_on_an_unanswered_roots_list`
(`@pytest.mark.characterization`, route:F-790). Three things make it a
measurement rather than a flake:

* a **sensitivity control in the same node** — the first spawn's latency is
  asserted to be a fraction of the bound, so a busy machine fails the control
  instead of manufacturing an F-790;
* the **named mechanism** — the `roots/list` request frame must actually be on
  the wire while the spawn is parked (and must be absent from the working
  master-profile spawn), so an unrelated stall cannot satisfy the pin;
* the **pipe is proved healthy** — while the second spawn is parked, the first
  instance still answers `list_instances` and closes cleanly.

It runs in its own `gate_workspace` so the abandoned call dies with a backend
nothing else shares.

`tests/test_wire_semantics.py::test_two_named_instances_stay_isolated_under_interleaved_calls`
is the positive half: two instances DO coexist and stay isolated when both name
their profiles.

## What closing it requires

Bound the `list_roots()` await (or drop the round trip and seed the clone name
from something local — the function already falls back to
`codex_workspace` / `claude_project_dir` / `pwd` / `os.getcwd()`), and make the
failure typed. The fallback chain is already there; only the unbounded await
stands between it and a working default path.

## Contract limitation wording (for W5 §Limitations)

> When one instance already holds the master profile, a `spawn_browser` call
> that does not set `user_data_dir` asks the client for its roots and waits
> without a deadline for the reply. A client that does not implement MCP `roots`
> receives no result, no error, and no timeout. Concurrent instances require an
> explicit `user_data_dir` per instance.

## Routing

- MQ-139 in `tests/MANUAL_QA_PROTOCOL.md` is **satisfied for the named-profile
  form only**; the step says so, and names this finding as why the unnamed form
  is excluded.
- No `--mq` id in `release-gate.yml` is bound to this pin.
- Related: F-788 (a navigation timeout wedges the CDP connection) and F-789
  (`close_instance` returns `False` after a crash) are the other two places
  where the instance lifecycle's advertised behavior and its real behavior part
  company.
