# F-962 — page tools act on whichever tab the browser last switched to, so sessions sharing a browser read and write each other's tabs

**Severity:** High. Data crosses between chats: reads already landed on another chat's pages, and a
click or keystroke would land there too, possibly on a signed-in console. Reported by the owner
2026-10-10: BioFlow's and uprank's `execute_script` reads landed on another chat's GCP console and
MinIO login tabs in the shared `fleet` browser.
**Files:** `embedded/tab_binding.py` (new: the caller, its bound tab, `tab_for_caller`, `claim`,
`adopt`/`bind`/`forget_tab`, `navigate_callers_tab`, `scoped`), `embedded/tool_errors.py`
(`_require_tab` resolves through it), `embedded/tool_registry.py` (`section_tool` applies `scoped`),
`embedded/backend_client.py` (`CALLER_HEADER`/`CALLER_ID` on the one transport),
`embedded/tab_open.py` (`find`, moved from `BrowserManager._find_tab`, returns a `Tab`),
`embedded/browser_manager.py` (`navigate(pinned=)`, `get_page_state(tab=)`; cap 1454 → 1449),
`embedded/tool_sections/{tabs,browser_management,cdp_functions}.py`, `embedded/tool_runtime.py`,
`tests/goldens/tool_surface.json` (regenerated, see §5), `NAVMAP.md`, `CHANGELOG.md`,
`tools/check_file_budgets.py`.
Tests: `tests/test_tab_binding.py` (new, 20), `tests/test_e2e_two_sessions_own_tabs.py` (new, 1,
real Chrome, two stdio proxies).
**Branch:** `fix/f962-page-tools-bind-to-callers-tab`, from `origin/main` (a3bd392).

---

## 1. What happened

Several chats share the `fleet` browser, each in its own tab. One chat's page tools read another
chat's GCP console and MinIO login pages. Nothing errored: the reads simply came from the wrong page.

## 2. Root cause

Every page tool resolves its tab through `tool_errors._require_tab`, which answered
`BrowserManager.get_tab(instance_id)`: ONE stored tab per instance (`_instances[id]["tab"]`). Four
paths rewrite that one slot for everyone: `switch_tab` (`switch_to_tab` stores the target tab),
`navigate` (stores the tab it navigated), `close_tab` (F-845 re-points to a survivor, which can be
another chat's tab) and the navigation recycle/recovery (`_replace_main_tab`). The instance had no
notion of WHO was calling, so the slot was "whoever moved it last". `new_tab` did not even touch
it, so a chat that opened its own tab with `new_tab` and then called `execute_script` acted on
whatever the slot held, not on the tab it had just opened.

Measured on origin/main (product exported with `git archive`, `tool_errors.__file__` checked): A
reads, B switches, A reads again → `A reads before B switches: A's tab | after: B's tab`.

## 3. Fix

A caller is bound to a tab, in one module (`tab_binding`):

* **Who the caller is.** The stdio proxy's one transport (`backend_client.http_client`) stamps
  `x-stealth-caller: <random per process>` on every request: one value per Claude Code session,
  unchanged across a heal (F-959 re-initializes and gets a NEW `mcp-session-id`, so the session
  id would have cost a healed chat its tab). Without the header (an older proxy, a raw HTTP client)
  the caller is the `mcp-session-id`. An in-process call (the hermetic lanes, `call_tool`) has no
  caller and keeps the exact pre-F-962 behaviour.
* **Resolution** (`tab_for_caller`, behind `_require_tab`): the call's `tab_id`, else the caller's
  bound tab, else the instance's tab, which is claimed if no one holds it and REFUSED if another
  caller does ("this session has no tab of its own … call new_tab"). A bound tab that was closed is
  reported ("no longer open … new_tab / switch_tab") and the binding dropped. It is never swapped for
  the instance's tab, because that swap is exactly the misdirected read.
* **Binding:** `spawn_browser` answers with `tab_id` (`claim`: the caller's live tab, else the
  free instance tab, else a fresh `about:blank` tab, armed like the spawn tab (F-935) so network
  capture covers it; never fails a spawn, `tab_error` instead). `new_tab` and `switch_tab` bind the
  caller to their tab; `close_tab` forgets every binding to the closed tab.
* **`tab_id` on every page tool, derived.** `tool_registry.section_tool` passes every tool through
  `tab_binding.scoped`: a tool whose code references a name in `RESOLVERS` (`_require_tab`,
  `tab_for_caller`, `navigate_callers_tab`) gains a keyword-only `tab_id` (default `None`) and an
  `Args:` line; the value rides a context variable for that call only. 53 tools gained it. The 11
  tools with `instance_id` and no `tab_id` act on the whole instance (`close_instance`, `list_tabs`,
  `new_tab`, the network-capture tools, `get_function_executor_info`, `list_dynamic_hooks`). A
  test pins that no tool reads the instance tab directly except `spawn_browser`.
* **`navigate`** (`navigate_callers_tab`): a session's own tab is navigated where it is
  (`BrowserManager.navigate(pinned=…)`: no recycle, no replacement, not stored as the instance tab,
  a failure reported rather than retried on a replacement). The instance's own tab keeps F-824/F-940's
  recycle and stale-tab recovery, and a caller bound to it follows it to the replacement.
* **`get_instance_state`** reads the caller's tab (`get_page_state(tab=)`), and `get_active_tab`
  reports it. `cdp_functions`' three direct `get_tab` reads go through `tab_for_caller` too.
* `BrowserManager._find_tab` moved to `tab_open.find`, the tab home. It now returns a `Tab` built
  and registered in place of the bare `Connection` that `update_targets` registers for a
  rediscovered target (F-771/F-775), so a tab a caller picks can take every page tool. The move paid
  for the new lines, so the cap ratchets 1454 → 1449.

Not changed: `switch_tab` still brings the tab to the front and still sets the instance tab, which
is now only the fallback for callers that have none (and is refused to them when someone else
holds it).

## 4. Tests

* `tests/test_tab_binding.py` (20, hermetic): B's switch does not move A; two callers resolving
  concurrently (20 rounds each) never cross; unbound claim / refusal; in-process legacy;
  `InstanceNotFoundError` kept; `tab_id` wins without rebinding; unknown `tab_id` named; closed
  bound tab reported, not swapped; `forget_tab`; `claim` (own tab, reuse, armed, never fails,
  in-process); `navigate` pinned vs follow-the-replacement; `scoped` (signature, doc line, per-call
  value under concurrency); every registered page tool takes `tab_id` and only `spawn_browser` reads
  the instance tab directly; over REAL streamable HTTP (in-process uvicorn, no Chrome): two proxies
  are two callers, a header-less client is keyed by its session, `tab_id` arrives; the proxy
  transport stamps one caller id per process.
* `tests/test_e2e_two_sessions_own_tabs.py` (integration + transport, real Chrome): one isolated
  backend, two stdio proxies, `spawn_browser(session="fleet")` twice (second `already_running` with a
  DIFFERENT `tab_id`), simultaneous `navigate`, six rounds of simultaneous reads while each session
  in turn `switch_tab`s, simultaneous `type_text` into the same field, `tab_id` reaching the other
  tab without rebinding, `get_active_tab`, and a closed tab reported instead of swapped.

## 5. Evidence

* RED (origin/main, out of tree): the script above, exit 1, `A's tab` → `B's tab`.
  `tests/test_tab_binding.py` does not collect there (`tab_binding` does not exist).
* GREEN: `tests/test_tab_binding.py` 20 passed. Affected hermetic suites: see the PR.
* The real-Chrome node was attempted once locally. The backend never booted (empty
  `backend-boot.log`: the scheduler-rung launch on a machine just restarted with ~3 GB free), so it
  never reached F-962's code, and the owner then ruled out local e2e runs until RAM recovers. Its
  verdict is the CI gate's integration cells.
* `tests/goldens/tool_surface.json` regenerated deliberately (`dump_tool_surface.py --write`): +`tab_id`
  on the 53 page tools, and the new `switch_tab` / `new_tab` / `get_active_tab` descriptions. The
  tool count stays 97.

## 6. Rollout

Ships in 2.1.28. The backend change is complete on its own. Telling chats apart needs the 2.1.28
PROXY: a chat on an older proxy is keyed by its MCP session id, which survives everything except
a backend replacement; after one, that chat must call `new_tab`/`spawn_browser` again (the refusal
says so). Bindings live in memory: a backend restart forgets them, and the first caller to touch a
re-attached instance claims its tab.
