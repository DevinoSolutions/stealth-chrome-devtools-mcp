# F-899 — adopted instances leak between test files through a process-global store

**Severity**: MEDIUM — harness only. No shipped behaviour is wrong. What is wrong is
that two test files cannot be run together, and the lane is green only because a
third, unrelated file happens to sort between them and clear the store on its way
past. Nothing holds that arrangement in place.

**Status**: FIXED (this PR), in two parts — (a) the reported leak, restored at the
one fixture home (`tests/conftest.py`'s autouse `_in_memory_storage_hygiene`) and
pinned order-independently by `tests/test_in_memory_storage_isolation.py`; (b) one
PRODUCT path in the same subject, found by the review of (a) and fixed beside it —
`BrowserManager.close_instance` could strand an entry it had already popped.

**Verdict on the REPORTED leak**: **test-only**. The `i-kept`/`i-held` entries come
from `browser_reattach.adopt`, whose write is symmetric; the finding there is that a
test PROCESS holds one of an object production holds one of PER BACKEND.

**And one path that was not**: the review traced every write and removal
independently and found `close_instance` asymmetric under cancellation. It is
pre-existing, it is not what made the two test files fail, and it is the same
sentence — *nothing may leave an entry in this store* — so it is fixed here rather
than deferred. §3 is the trace; §5 has both rules.

---

## 1. The mechanism

`embedded/in_memory_storage.py` ends in a module-level singleton:

```python
in_memory_storage = InMemoryStorage()
```

Production creates one per backend PROCESS. A pytest run creates one for the whole
SESSION, shared by every test in every file.

Two production writers put instance records in it:

* `browser_manager.py:750` — `spawn_browser`, and
* `browser_reattach.py:994` — the adoption pass, at the end of `adopt`.

Both import the singleton **directly, by value, at module scope**
(`browser_manager.py:32`, `browser_reattach.py:100`). That is the same shape
`browser_manager` has always had and it is not itself the defect — but it is why
the obvious isolation does not reach them: `conftest.patched_server(
in_memory_storage=FakeStorage())` sets the attribute on `tool_runtime`, and a name
bound at import time in another module does not follow it. A hermetic test that
drives `browser_reattach.adopt` against a fake manager therefore writes the **real**
singleton.

The reader is `tool_sections/browser_management.py:505-524`. `list_instances` merges
what the manager holds with what the store holds, and reports any store entry the
manager does not know about as a `source: "stored"` record. So one file's leftover
adoption becomes another file's phantom browser.

**The single sentence.** A test process shares one copy of an object production
gets a fresh copy of, and nothing in the harness put it back.

## 2. Evidence

Measured on main `f18ecc5`, in this worktree, Windows 11:

```
uv run python -m pytest tests/test_browser_reattach.py \
                        tests/test_tool_failure_visibility.py -q
2 failed, 102 passed in 11.60s

FAILED tests/test_tool_failure_visibility.py::test_a_successful_call_records_no_error
FAILED tests/test_tool_failure_visibility.py::test_a_throwing_debug_ring_does_not_break_a_succeeding_tool

E  AssertionError: assert [{'instance_i...stored', ...}] == []
E    Left contains 2 more items, first extra item:
E    {'instance_id': 'i-kept', 'last_navigated_title': 'Fake Page',
E     'last_navigated_url': 'https://fake.test/page', 'source': 'stored', ...}
```

Each file alone is green. `pytest-randomly` is not installed, so the ordering is
deterministic — this is a latent failure, not a flake.

**The writers**, from an ad-hoc `pytest_runtest_call` probe over
`tests/test_browser_reattach.py` (state read after each test BODY):

```
TestManagerAdoption::test_the_client_keeps_its_instance_id_and_gets_the_live_page   +i-kept
TestManagerAdoption::test_adoption_restamps_the_owner_through_the_one_write         +i-kept
TestAnAdoptedInstanceTellsTheTruth::test_headless_comes_off_the_holders_command_line +i-held
  ... and five more in that class and TestHeldAdoptionNeverReaps                     +i-held
```

**The mask**, from the same probe seeded with two sentinels and asked who removes
them:

```
[F899-MASK] tests/test_mcp_protocol_surface.py::test_sync_hook_doc_tool_via_protocol
            cleared=['f899-sentinel-a', 'f899-sentinel-b']
```

`test_mcp_protocol_surface.py` sorts between the two symptom files. Its first node
patches a `FakeStorage`; every node after it opens `fastmcp.Client(server.mcp)` with
**no** `patched_server`, so the real `app_lifespan` runs against the real singleton
and its shutdown branch (`server.py:202-215`) calls `rt.in_memory_storage.clear_all()`.
An unrelated file wiping the leak in passing is the entire reason the lane is green.
Delete that file, rename it, mark it integration, or let collection order change, and
the pair goes red somewhere else.

## 3. The product's lifecycle — every write and removal traced

The first pass of this finding called the lifecycle "symmetric" and stopped there.
The review traced it independently and found one row that is not. Both are below,
because "symmetric" without the qualifier is exactly what the next reader would
check and find false.

| site | file:line | verdict |
|---|---|---|
| spawn write | `browser_manager.py:750` | last statement of the `try`; nothing after it can strand the entry |
| adoption write | `browser_reattach.py:994` | last statement of the `try`; `except BaseException` re-raises with nothing in between |
| stale-instance discard | `browser_manager.py:207-208` | removal present, unconditional |
| lifespan shutdown | `server.py:203-210` | `clear_all()` |
| **close** | `browser_manager.py:869` vs `:968-969` | **ASYMMETRIC under cancellation — the defect below** |

**The reported leak is still test-only.** `i-kept` and `i-held` come from `adopt`,
which is symmetric: it writes at `:994`, the LAST statement of its `try`, after the
instance is already in `manager._instances`, and every abort path raises before it.
`test_browser_reattach.py`'s own aborted-adoption node (`"i-kept" not in
manager._instances`) wrote nothing to the store in the probe run above — that
argument measured rather than read.

**The close path was not.** `close_instance` popped `_instances` in Phase 1 (`:869`)
and removed the store entry in Phase 4 (`:968-969`) — but Phase 4 sat inside the
`try` whose handler is `except Exception` (`:971`). Every inner step of Phases 2-3
has its own handler, so that outer `except` is effectively unreachable for ordinary
errors; what it cannot catch is `asyncio.CancelledError`, a `BaseException`, which
walks straight past it. **Six `await`s separate the pop from the removal**
(`close_target`, `cdp_browser.close()`, `connection.disconnect()`,
`_close_proxy_forwarder_ref`, `to_thread(_blocking_teardown)`, `stop_coro`), and the
tool body at `tool_sections/browser_management.py:544` carries no
`_with_cdp_timeout`, so a cancellation there comes from the REQUEST TASK — a client
that disconnects mid-close.

The result is the manager without the instance and the store with its entry: a
`source: "stored"` row in `list_instances` **for the life of the backend**, cleared
only by lifespan shutdown, naming a browser that is already being torn down. That is
the "ghost row forever" shape, reached by a different door than the one reported.

## 4. Blast radius

**The harness half**: two files that cannot be run together, and a green lane that
depends on an incidental side effect of a third. The class is larger than the
reported pair — any future file that calls `list_instances`, `get_instance_state`,
or asserts on an empty store, placed anywhere after `test_browser_reattach.py` and
before the mask, fails for a reason that has nothing to do with its subject. The
previous victim spent its failure budget on `test_tool_failure_visibility.py`, a
file about the debug ring.

**The product half** is narrow but permanent while it lasts: one ghost row per
cancelled close, for the life of that backend, in the one listing an operator reads
to find out what is running. It needs a client to disconnect during a close, which
is why it has never been reported — and why the window is six awaits wide, one of
them bounded at `CLOSE_KILL_TIMEOUT`.

## 5. The rules chosen, and the ones rejected

### 5a. The product: drop the entry where the instance is claimed

**Chosen: move the removal into Phase 1, under `self._lock`, beside the pop.** The
store is a cross-check of `_instances`; the two are one fact and now move together.
There is **no `await` between them**, so there is no window at all — not a narrower
one. `close_instance` keeps exactly ONE removal site (`:877` after the move; the
other `in_memory_storage.remove_instance` in the file, `:208`, is the unrelated
stale-instance discard path and is untouched), so nothing can double-remove, and
`InMemoryStorage.remove_instance` is idempotent anyway — it guards on membership.

**Rejected: a `finally` around Phases 2-4.** It works, and it is strictly weaker:
`finally` still runs *after* the cancellation unwinds, so the entry exists during
the unwind, and a second cancellation or a crash inside the handler can still skip
it. It also leaves the removal far from the pop, which is the arrangement that
produced the defect. The lock costs microseconds and closes the window to nothing.

One behaviour changes and is stated rather than hidden: an exception from
`remove_instance` that is NOT `KeyError` used to be swallowed by
`except Exception: return False` and now propagates. The real implementation raises
neither, the `contextlib.suppress(KeyError)` moved with the call so that shape is
unchanged, and propagating is convention 2's direction.

`browser_manager.py` is **grandfathered at 1485/1485 LOC with zero headroom**, so
this was done LOC-neutral and measured at each step: the Phase-4 block (comment,
`with`, call, blank) paid for the Phase-1 block, and the docstring's `Phase 4`
line paid for the continuation line on `Phase 1`. Final `(Get-Content).Count` =
**1485**. The cap is not raised and not lowered.

### 5b. The harness: RESTORE, at `tests/conftest.py`

An autouse
`_in_memory_storage_hygiene` snapshots the store before each test and puts it back
after, through the public API only (`clear_all()` then `set()` per key). It is a
direct sibling of the `_stealth_logger_hygiene` fixture immediately above it, which
answers the same question for `stealth.*` logger state, and it lives at the one
fixture home rather than in the two symptom files — neither of which is at fault.

The snapshot is two levels deep because that is every mutation the class offers:
`store_instance`/`remove_instance` write inside `_data["instances"]`, `set` writes a
top-level key, `clear_all` replaces the whole dict.

**Rejected: an autouse assert-and-fail guard** that reds the test which leaked.
It names the culprit, which is genuinely better diagnostics, and it was rejected
anyway for two reasons. First, the tests that write here are driving production code
that is RIGHT to write — failing them asks each test to clean up a global it never
chose to touch, and the cleanup would then be spelled once per file, which is the
second way this repo's fourth convention exists to refuse. Second, it hides nothing
to restore instead: a test that asserts its own write was removed still asserts that
inside its own body, before teardown. The diagnostics are recovered by the pin
below, which says in one place what the guard would have said in many.

## 6. Tests

`tests/test_in_memory_storage_isolation.py` — the order-independent pin, two nodes
in one file:

1. `test_a_test_may_write_the_process_global_store` writes through the same public
   call both production writers end in, and deliberately does not clean up;
2. `test_the_next_test_does_not_inherit_it` asserts the store is empty **and** that
   `list_instances` answers `[]`, because the reported defect was the second one.

Pytest runs a file's nodes in declared order, so step 2 always follows step 1 no
matter which other files are collected — the pin cannot be masked by the accident
that masked the original pair, and needs no sibling file to stay adjacent to it.

A module-global `_WROTE_THE_STORE` witness closes the one way step 2 could pass
about nothing: `-k`, a single-node selection and — the one that matters — `--lf`
after a step-2 failure all rerun step 2 WITHOUT step 1, where the store is empty
because nobody filled it. It now refuses instead (measured: `1 failed` for both
`::test_the_next_test_does_not_inherit_it` alone and `-k not_inherit`), naming the
reason: *run the whole file — step 1 is this pin's other half*.

A third node in the same file pins §3's product half —
`test_a_cancelled_close_does_not_strand_its_store_entry`. It is hermetic (no Chrome,
no backend): a `BrowserManager` seeded with one instance whose browser has empty
`tabs` and a `connection.send` that never returns, so `close_instance` suspends at
exactly ONE place, past the Phase-1 pop. The test cancels the task there and asserts
both halves — the `CancelledError` still PROPAGATES (this is a client that went
away, not a close that succeeded) and the store no longer lists the id. Deliberately
against the REAL singleton, which is why it lives here rather than in
`tests/test_close_instance_offload.py`, whose autouse fixture replaces
`in_memory_storage.remove_instance` with a `MagicMock` and so could not see this
defect at all.

RED before the product fix, for the right reason — the pop had happened, the
cancellation had propagated, and the entry was still there:

```
FAILED tests/test_in_memory_storage_isolation.py::test_a_cancelled_close_does_not_strand_its_store_entry
E  AssertionError: assert 'f899-cancelled-close' not in
   {'f899-cancelled-close': {'instance_id': 'f899-cancelled-close', 'state': 'active'}}
1 failed, 2 passed
```

RED before the fixture existed, for the right reason (step 1 green, step 2 red on
the inherited entry):

```
FAILED tests/test_in_memory_storage_isolation.py::test_the_next_test_does_not_inherit_it
E  AssertionError: assert 'f899-leaked-instance' not in
   {'f899-leaked-instance': {'instance_id': 'f899-leaked-instance', ...}}
1 failed, 1 passed
```

GREEN with it, together with the original repro pair:

```
uv run python -m pytest tests/test_in_memory_storage_isolation.py \
    tests/test_browser_reattach.py tests/test_tool_failure_visibility.py -q
106 passed in 10.15s
```

That the pin cannot be masked by the accident in §2 is measured, not asserted.
With the fixture temporarily switched to `autouse=False` and the masking file
collected alongside the pin, it fails **in both orders**:

```
pytest tests/test_in_memory_storage_isolation.py tests/test_mcp_protocol_surface.py
  -> 1 failed, 7 passed
pytest tests/test_mcp_protocol_surface.py tests/test_in_memory_storage_isolation.py
  -> 1 failed, 7 passed
```

Neither placement helps, because the mask clears the store between FILES while
the pin's two nodes are adjacent inside one.

## 7. The sibling sweep

131 hermetic test files were swept in eleven batches with a `pytest_runtest_call`
probe reading the state right after each test BODY — inside the new fixture, so its
restore could not hide a would-be leak. It watched the store's instances, the
store's top-level keys, and the real `browser_manager`, `dynamic_hook_system` and
`network_interceptor` singletons.

One sibling of the same class, outside the reported pair:

* `test_animation_schema_v2.py::TestAdaptersReadTheV2Shape::test_progressive_expand_passes_the_whole_v2_object_through`
  leaves the top-level `progressive_elements` key behind (`progressive_element_cloner`
  writes it through `in_memory_storage.set`). Covered by the same fixture, because the
  snapshot is of the whole `_data` and not only of `instances`.

No file leaked instances into the real `BrowserManager`, hooks into the real
`DynamicHookSystem`, or capture data into the real `NetworkInterceptor`. A
confirming run of `test_animation_schema_v2.py`, `test_browser_reattach.py`, the new
pin and `test_tool_failure_visibility.py` together (210 nodes) reported **no**
post-teardown leak at all.

## 8. Residuals

1. **The sweep's scope is stated, not total.** 131 of ~186 files. Excluded by name:
   the E2E/integration tier and the files that spawn real backends or real Chrome
   (`test_e2e_*`, `test_soak_*`, `test_startup_herd`, `test_singleton_*`,
   `test_proxy_backend_death`, `test_proxy_selfheal`, `test_resilience`,
   `test_concurrent_spawn_collision`, and eighteen more), because running them
   concurrently with other agents' lanes on this machine is the larger harm. Those
   tiers spawn real instances and close them through `close_instance`, which removes
   the entry — and they are now covered by the fixture regardless of whether they
   were swept.
2. **The direct module-scope import of the singleton stands, and it is a CLOSED
   question, not an open invitation.** `browser_manager.py:32` and
   `browser_reattach.py:100` bind the singleton by value, so
   `patched_server(in_memory_storage=...)` cannot reach those two writers. Do not
   "fix" that by re-pointing them at `rt.in_memory_storage`: **`tool_runtime.py`
   imports both writers at its own module scope** — `browser_reattach` at `:41`,
   `BrowserManager` at `:47` — so a module-scope `rt` import in either closes a
   cycle. It would survive only if `tool_runtime` were always imported first, and it
   is not; measured:

   ```
   python -c "from ...embedded.browser_manager import BrowserManager"
     -> browser_manager imported standalone OK; tool_runtime loaded? False
   ```

   Import `browser_manager` first — which the suite does constantly — and
   `tool_runtime:47` reaches back into a module executed only as far as `:32`, where
   `BrowserManager` does not exist yet: `ImportError`. A function-LOCAL `rt` import
   at the four call sites would work and buys patchability at the price of a third
   way to reach one store (`rt.` here, module-global in
   `progressive_element_cloner.py:15`, direct import elsewhere) — convention 4 with
   the sign flipped. `tool_runtime`'s contract is explicitly "what a TOOL BODY
   reaches for beyond its own arguments", and these two modules are its own
   DEPENDENCIES, not its consumers. The by-value binding is correct; isolating a
   process-global is the harness's job.
3. **A restore fixture cannot report WHICH test leaked.** By construction. The pin
   in §6 is what fails if the fixture is removed; it will not say who wrote the
   entry. The probe used in §2 and §7 is reproducible in ten lines and the finding
   records its shape.
4. **`pytest-xdist` would blind the pin, though not red it.** The two-node pair
   relies on in-file adjacency, which holds because neither `pytest-randomly` nor
   `pytest-xdist` is in the test extra and `[tool.pytest.ini_options]` sets no
   `addopts`. If xdist ever lands, `-n` may split a file across workers and step 2
   would pass in a process where step 1 never ran — the `_WROTE_THE_STORE` witness
   catches exactly that and turns it into a red with a message, which is the right
   failure but still a reason to revisit the shape rather than silence it.
5. **The `except Exception` at `browser_manager.py:971` is now demonstrably
   unreachable** for its own body: every inner step of Phases 2-3 has its own
   handler and Phase 4 no longer exists. It is left in place — deleting a blanket
   handler from a four-phase teardown is a separate decision with its own blast
   radius, and it is not what this finding measured.
