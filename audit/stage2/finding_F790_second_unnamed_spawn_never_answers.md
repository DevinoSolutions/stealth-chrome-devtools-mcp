# F-790 — the auto-clone spawn path blocks forever on an unanswered `roots/list`

**Status:** RESOLVED for 2.0.1 — the `list_roots()` await is bounded and falls
back to the local seed chain; the W13 characterization node was promoted to a
regression oracle in the same change. Fix: §The fix, below. (Originally OPEN,
characterized by plan_RELEASE W13, which is zero-`src/`.)
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

Post-fix, the first row is what changed: the unnamed spawn with an unanswered
`roots/list` now answers within one cold-Chrome budget (18 s for the whole node
including its control spawn), because the await expires and the local seed chain
names the clone. The other two rows are unchanged.

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

## Evidence (as measured while OPEN)

`tests/test_wire_semantics.py::test_a_second_unnamed_spawn_blocks_on_an_unanswered_roots_list`
(`@pytest.mark.characterization`, route:F-790) — since replaced by the
regression node named under §The regression test. Three things made it a
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

## What closing it required

Bound the `list_roots()` await (or drop the round trip and seed the clone name
from something local — the function already falls back to
`codex_workspace` / `claude_project_dir` / `pwd` / `os.getcwd()`). The fallback
chain was already there; only the unbounded await stood between it and a working
default path.

## The fix (2.0.1)

The await is bounded and the existing fallback absorbs the expiry. Nothing else
about the path changed, so a client that DOES answer is byte-for-byte unaffected.

`src/stealth_chrome_devtools_mcp/settings.py` — the new typed knob (settings.py
is THE env home, so the deadline is not a literal in the caller):

```python
client_roots_timeout_seconds: float = Field(5.0, ge=0)
#   -> STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS; 0 disables the round trip.
```

`src/stealth_chrome_devtools_mcp/embedded/clone_storage.py` —
`_client_session_seed()`:

```python
        # F-790: bound this OPTIONAL server->client round trip (see settings.py).
        bound = get_settings().client_roots_timeout_seconds
        listed = await asyncio.wait_for(get_context().list_roots(), bound)
        roots = [path for path in (_root_to_path(r) for r in listed) if path]
    except Exception as e:
        message = str(e) or f"{type(e).__name__} awaiting roots/list"
        debug_logger.log_warning("server", "_client_session_seed", message)
        roots = []
```

Three properties of the shape are deliberate:

* **No new error surface.** `asyncio.TimeoutError` *is* `TimeoutError` on 3.11+,
  so the pre-existing `except Exception` catches it and the function returns the
  same local seed an unsupported client already produced. CLAUDE.md convention 2
  is untouched: there is no failure to raise, because falling back is the
  documented behavior for a client without `roots` — it just now also covers the
  client that has `roots` and stays silent.
* **The expiry is audible.** `TimeoutError` stringifies to `""`, which would have
  logged a blank warning; the message names the mechanism instead
  (`TimeoutError awaiting roots/list`). The wire regression asserts that line is
  in the backend log, which is what proves the deadline released the call rather
  than luck.
* **`asyncio.wait_for` is the codebase's one timeout idiom** (~15 sites in
  `browser_manager` / `cdp_function_executor` / `proxy_forwarder`), and the MCP
  SDK's `BaseSession.send_request` pops its response stream in a `finally`, so
  cancelling the await leaves no dangling request state.

The edit is **net-zero LOC** in `clone_storage.py` (1057/1057, its grandfathered
cap): the per-root `for`/`if`/`append` block collapses into one comprehension,
which pays for the bound and the comment.

## The regression test

`tests/test_wire_semantics.py::test_a_second_unnamed_spawn_is_bounded_when_roots_list_is_never_answered`
— the same node, flipped. It is real stdio against the installed console
launcher in its own `gate_workspace`, and it strengthens the original client:
this one **advertises** `{"roots": {"listChanged": false}}` at `initialize` and
then answers no `roots/list` at all, which is the worst *conforming* client, not
merely a lazy one. (`RawStdioWire.initialize()` grew an optional `capabilities`
argument for it — extended in place, not forked.) The workspace pins
`STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS=2.0` so the node measures the bound
rather than the default's patience.

Four assertions, each load-bearing:

* the second (auto-clone) `spawn_browser` **answers** inside `SPAWN_BOUND` — a
  full cold-Chrome budget, versus the 420 s of nothing measured by hand;
* **exactly one** response frame carries that id;
* `roots/list` is on the wire, is a request (not a notification), and is still
  unanswered when the reply lands — so the spawn did not simply take another
  route;
* the backend's own log carries the `_client_session_seed` fallback warning.

The original sensitivity control is kept: the first (master-profile) spawn is
measured on the same machine seconds earlier and must not itself have asked for
roots.

Verified RED/GREEN on the same tree: with only `clone_storage.py` reverted the
node fails (136 s, no reply); with the fix it passes in 18 s.

Unit half: `tests/test_clone_storage.py::TestClientRootsRoundTripIsBounded` —
a silent client falls back within the bound; a client that answers still seeds
from its roots (preserved behavior); `0` never waits; and the bound is a typed
`Settings` field with a `5.0` default, readable from
`STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS`.

## Contract limitation wording (for W5 §Limitations) — WITHDRAWN

The clause below was drafted while the finding was open. It no longer describes
the product and must NOT be added to `RELEASE_CONTRACT.md`:

> ~~When one instance already holds the master profile, a `spawn_browser` call
> that does not set `user_data_dir` asks the client for its roots and waits
> without a deadline for the reply. A client that does not implement MCP `roots`
> receives no result, no error, and no timeout. Concurrent instances require an
> explicit `user_data_dir` per instance.~~

## Routing

- MQ-139 in `tests/MANUAL_QA_PROTOCOL.md` named this finding as why the unnamed
  form was excluded from the step; that exclusion is now withdrawn there, and the
  step records the regression node as its support. Naming a profile remains the
  cheaper form — it skips the round trip entirely — but it is no longer required.
- No `--mq` id in `release-gate.yml` is bound to this pin.
- **Still open, and NOT claimed fixed here**: the "What is NOT claimed" section
  above. A second unnamed spawn can still fail on Chrome's side; this change owns
  the hang only.
- Related: F-788 (a navigation timeout wedges the CDP connection) and F-789
  (`close_instance` returns `False` after a crash) are the other two places
  where the instance lifecycle's advertised behavior and its real behavior part
  company. Both remain OPEN.
