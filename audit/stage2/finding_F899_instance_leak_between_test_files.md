# F-899 — adopted instances leak between test files through a process-global store

**Severity**: MEDIUM — harness only. No shipped behaviour is wrong. What is wrong is
that two test files cannot be run together, and the lane is green only because a
third, unrelated file happens to sort between them and clear the store on its way
past. Nothing holds that arrangement in place.

**Status**: FIXED (this PR) — restored at the one fixture home
(`tests/conftest.py`'s autouse `_in_memory_storage_hygiene`), pinned
order-independently by `tests/test_in_memory_storage_isolation.py`.

**Verdict**: **test-only**. The product's use of this store is symmetric and
correct; the finding is that a test PROCESS holds one of an object production
holds one of PER BACKEND. §3 states the evidence for that verdict and the product
defect it was checked against.

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
* `browser_reattach.py:993` — the adoption pass, at the end of `adopt`.

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

## 3. Test-only, and the product defect it was checked against

The product's lifecycle for this store is symmetric:

| event | store |
|---|---|
| `spawn_browser` / `adopt` | entry written |
| `close_instance` | entry removed (`browser_manager.py:208`, `:969`) |
| lifespan shutdown | `clear_all()` |

The one shape that WOULD be a product defect is an adoption that is rolled back
without removing the entry it wrote — `list_instances` would then advertise a
browser that never existed for the life of the backend. It does not happen:
`browser_reattach.adopt` writes at line 993, the LAST statement of its `try`, after
the instance is already in `manager._instances`; every abort path raises before it.
`test_browser_reattach.py`'s own aborted-adoption node
(`"i-kept" not in manager._instances`) wrote nothing to the store in the probe run
above, which is that argument measured rather than read.

So nothing is fixed in `src/`. The finding is the harness's.

## 4. Blast radius

Harness only. Today: two files that cannot be run together, and a green lane that
depends on an incidental side effect of a third. The class is larger than the
reported pair — any future file that calls `list_instances`, `get_instance_state`,
or asserts on an empty store, placed anywhere after `test_browser_reattach.py` and
before the mask, fails for a reason that has nothing to do with its subject. The
previous victim spent its failure budget on `test_tool_failure_visibility.py`, a
file about the debug ring.

## 5. The rule chosen, and the one rejected

**Chosen: RESTORE, at `tests/conftest.py`.** An autouse
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
2. **The direct module-scope import of the singleton stands.**
   `browser_manager.py:32` and `browser_reattach.py:100` still bind it by value, so
   `patched_server(in_memory_storage=...)` still does not reach those two writers.
   Re-pointing them at `rt.in_memory_storage` would make them patchable, and it was
   not done here: neither is a tool body, `tool_runtime`'s stated contract is what a
   TOOL BODY reaches for, and changing a production import to serve a test is a
   bigger claim than this finding earns. It is named so the next person does not
   rediscover it from the failure.
3. **A restore fixture cannot report WHICH test leaked.** By construction. The pin
   in §6 is what fails if the fixture is removed; it will not say who wrote the
   entry. The probe used in §2 and §7 is reproducible in ten lines and the finding
   records its shape.
