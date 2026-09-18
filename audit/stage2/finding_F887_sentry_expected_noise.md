# F-887 — Sentry is drowned by the expected events the `before_send` hook was designed to drop

**Severity**: MEDIUM — nothing breaks for a user; what breaks is the maintainer's
only view of what breaks. ~13 700 events in seven days, of which the issues a
maintainer would act on are a rounding error.

**Status**: FIXED on `fix/F887-sentry-expected-noise`.

---

## 1. The mechanism

`observability._scrub_event` is THE one `before_send`. It does two things in
order: step 0 drops an event that is only the product working as designed, step 1
scrubs what survives. Step 0's rule, as shipped:

> Drop only when EVERY exception in the chain is our `tool_errors.ToolError`.

The rule is right in spirit — that is CLAUDE.md convention 2 doing its job, and
it must never drop a chain with a real bug in it. It is wrong about the chains
this product actually produces, in two separate ways.

**It requires an exception chain at all.** The largest single issue in the
project's Sentry has no exception values. `mcp/server/lowlevel/server.py`:707
(mcp 1.27.1) is:

```python
case Exception():  # pragma: no cover
    logger.error(f"Received exception from stream: {message}")
```

That is an f-string with no `exc_info`, so the logging integration produces a
message-only event. And the thing it formats is normally a `ClientDisconnect`,
whose `str()` is the empty string (measured), so the message is the literal
`"Received exception from stream: "` with nothing after the colon. 6 500 events,
all identical, and step 0's `_payload_is_expected_tool_failure` returned `False`
for every one of them by design: "nothing to classify" is not "expected".

**It requires the chain to be ONLY ours.** A bounded operation's chain is three
links. `tool_runtime._with_cdp_timeout` is:

```python
try:
    return await asyncio.wait_for(coro, timeout=t)
except TimeoutError:
    raise ToolError(f"CDP operation timed out after {t:.0f}s{tag}. …")
```

`asyncio.wait_for` cancels the coroutine it gave up on and raises `TimeoutError`
*from* that `CancelledError`, so the chain Sentry serialises is
`CancelledError` → `TimeoutError` → `ToolError` — measured, three values, one of
them ours. `browser_manager.navigate`:1224 has the same shape with an explicit
`from error`. So the single most likely runtime failure in the product, the one
whose `ToolError` the client has already received, shipped as ~20 separate issues
(the instance uuid is in the message, so each instance gets its own).

Four more classes never had a rule at all:

* `starlette.requests.ClientDisconnect` under `streamable_http`'s
  `_handle_post_request → await request.body()`. A client that went away
  mid-POST. 6 200 events.
* pydantic `ValidationError` from `fastmcp/tools/tool.py`'s
  `type_adapter.validate_python`, for `call[spawn_browser]`. Callers sending
  `window_width=` at a tool whose parameters are `viewport_width` /
  `viewport_height`. FastMCP answers the caller with the validation message, so
  the caller already knows. 466 events.
* `ConnectionResetError` `[WinError 10054]` out of CPython's own
  `asyncio/proactor_events.py`:154 (3.13.11):

  ```python
  def _call_connection_lost(self, exc):
      …
      finally:
          # XXX If there is a pending overlapped read on the other
          # end then it may fail with ERROR_NETNAME_DELETED if we
          # just close our end.  First calling shutdown() seems to
          # cure it, but maybe using DisconnectEx() would be better.
          if hasattr(self._sock, 'shutdown') and self._sock.fileno() != -1:
              self._sock.shutdown(socket.SHUT_RDWR)
  ```

  If the peer has already reset, that `shutdown` raises out of a `call_soon`
  callback and the loop's default handler logs it on the `asyncio` logger as
  `Exception in callback _ProactorBasePipeTransport._call_connection_lost(…)`.
  The `XXX` comment is CPython acknowledging the workaround; the raise out of it
  is ours to absorb, not to fix. 231 events. (Deliberately cited as the source
  line rather than as a tracker number: an issue id I could not verify is worse
  than none.)
* `ConnectionRefusedError` `[WinError 1225]` / `[Errno 111]` from nodriver's
  `Browser.update_targets()`, fired without being awaited; when its Chrome is
  gone the websocket connect is refused and the loop complains
  `Task exception was never retrieved … coro=<Browser.update_targets() done,
  defined at …nodriver\core\browser.py:561>`. 151 events.

---

## 2. Evidence

Measured 2026-09-18 against release 2.1.8, "last 7 days" in the project's
Sentry. Counts are the issue counters as reported, not sampled.

| events | logger | shape |
|---|---|---|
| 6 500 | `mcp.server.lowlevel.server` | message only, `Received exception from stream: ` |
| 6 200 | `mcp.server.streamable_http` | `ClientDisconnect`, "Error handling POST request" |
| 466 | `FastMCP.fastmcp.tools.tool_manager` | pydantic `ValidationError`, `call[spawn_browser]` |
| 231 | `asyncio` | `ConnectionResetError`, proactor `connection_lost` callback |
| 151 | `asyncio` | `ConnectionRefusedError`, nodriver `update_targets` task |
| 140+ | `FastMCP.fastmcp.tools.tool_manager` | `ToolError` timed out (CDP ~20 issues; navigation 36 + 7) |

### How the SDK spells each one (sdk 2.64.0, measured, not assumed)

The two paths step 0 has — a live exception via `hint`, and the serialized
payload — must judge the same set, so the exact serialization matters:

| live class | `type` | `module` |
|---|---|---|
| `starlette.requests.ClientDisconnect` | `ClientDisconnect` | `starlette.requests` |
| `ConnectionResetError` | `ConnectionResetError` | `None` |
| `ConnectionRefusedError` | `ConnectionRefusedError` | `None` |
| `TimeoutError` (and `asyncio.TimeoutError`, which IS it on 3.11+) | `TimeoutError` | `None` |
| `asyncio.CancelledError` | `CancelledError` | `asyncio.exceptions` |
| pydantic `ValidationError` | `ValidationError` | `pydantic_core._pydantic_core` |
| `tool_errors.ToolError` | `ToolError` | `stealth_chrome_devtools_mcp.embedded.tool_errors` |

`module: None` for the builtins is `sentry_sdk.utils.get_type_module`, which
drops `builtins` / `__builtins__` / `__main__` / `None`. The live class says
`"builtins"`. That difference is the whole reason the two paths could drift, and
normalizing it (`expected_events._UNWRITTEN_MODULES`) is what lets one rule read
both.

`LoggingIntegration`'s event shape, also measured: `event["logger"]` is the
record name, `event["logentry"]` is
`{"message": …, "formatted": …, "params": []}`, `event["message"]` is absent, and
a record with no `exc_info` produces an event with **no** `exception` key and a
`hint` carrying `log_record` but not `exc_info`.

---

## 3. The rule chosen, and the two rejected

### Chosen — **five named classes, each ONE rule, read through one `Link` shape**

`src/stealth_chrome_devtools_mcp/expected_events.py`, one consumer
(`observability._expected_event_class`). `classify` returns the NAME of the class
that recognised the event, so a drop is attributable — reported, never branched
on; there is one drop.

| class | rule |
|---|---|
| `error-convention` | at least one link is our `ToolError` (subclasses included) and every OTHER link is `TimeoutError` or `asyncio.CancelledError` |
| `client-disconnect` | chain entirely `starlette.requests.ClientDisconnect`; OR no links, logger `mcp.server.lowlevel.server`, message EXACTLY `Received exception from stream: ` |
| `proactor-teardown` | logger `asyncio` + message prefix `Exception in callback _ProactorBasePipeTransport._call_connection_lost` + chain entirely `ConnectionResetError` |
| `nodriver-dead-browser` | logger `asyncio` + message prefix `Task exception was never retrieved` + `nodriver` in the message + chain entirely `ConnectionRefusedError` |
| `caller-input` | logger `FastMCP.fastmcp.tools.tool_manager` + chain entirely pydantic `ValidationError` (module under `pydantic`) |

Five properties of that table are load-bearing:

1. **A budget link never stands alone.** `error-convention` requires one of ours
   in the chain. A `TimeoutError` nobody converted is a place the error
   convention is MISSING — a finding, not noise — and a bare `CancelledError` is
   how a task nobody is watching dies.
2. **The stream message is matched by equality.** That line reports real protocol
   faults too; the sibling issue
   `Received exception from stream: Received response with an unknown request ID:
   … Method not found` (2 events) must keep shipping, and a prefix match would
   have taken it with the 6 500.
3. **The proactor callback is the whole claim.** A `ConnectionResetError` our own
   code saw is a fact about this product's sockets. Only CPython's one teardown
   callback is absorbed.
4. **`nodriver` in the message is what tells the libraries' orphaned task from
   ours.** Same logger, same complaint, same exception type; the task repr names
   the coroutine's defining file and that is the only discriminator available.
5. **A logger name is never the test on its own.** The `AttributeError` in
   `navigate` that this project actually shipped arrived on
   `FastMCP.fastmcp.tools.tool_manager`, the very logger the noise arrives on.
   Where a rule names a logger it names an exception kind beside it.

One rule, both paths: every link is reduced to a `Link` (type NAME + MODULE) and
every class is a `Kind` test over those two fields. There is exactly **one**
`isinstance` in the module, `_is_ours`, because convention 2 is about a CLASS and
a subclass declared anywhere must be covered (pinned next door). Every other kind
matches by name and module on both paths *deliberately*: an `isinstance` on the
live path would accept subclasses that the serialized path, seeing only the
subclass's own name, could never accept, and the two paths would quietly
disagree — which is the defect this module's docstring exists to prevent.

### Rejected — **`ignore_logger` on the noisy loggers**

The obvious fix, and the one the neighbouring test file already pins as wrong.
`tool_manager` calls `logger.exception` for *every* raising tool before
re-raising, so silencing it silences the real bugs with the noise; F-887 adds two
more instances of the same trap, because `asyncio` carries both CPython's
teardown race and any unawaited task of ours, and `mcp.server.lowlevel.server`
carries both the 6 500 and real protocol faults.

### Rejected — **Sentry-side inbound filters / rate limits**

A server-side rule is invisible from the checkout, cannot be tested, cannot state
its evidence, and would have to be re-derived by whoever next wonders why a
crash never arrived. The project already has ONE home for "should this event
leave the machine" and a convention that a second way to do something already
done is a defect.

---

## 4. Blast radius

`observability.py` keeps everything about the two event SHAPES — `_hint_exception`
(the `hint["exc_info"]` / `hint["log_record"].exc_info` pair), `_exception_chain`
(the SDK's own `__cause__`/`__context__` walk), `_expected_error_base` (the lazy
`tool_errors` import) — and loses the taxonomy. 614 → 515 LOC; the new leaf is
361, stdlib only. Neither is grandfathered and both sit under the 1000-LOC
default.

Three behaviour notes:

* `_is_expected_tool_failure` keeps its name and widens to "is this event one of
  the five classes". Its four existing call sites across
  `tests/test_observability_toolerror_filter.py` and `tests/test_error_typing.py`
  hold unchanged: an all-ours chain is still class (a), and `ToolError` over
  `AttributeError` is still recognised by nothing.
* `_expected_error_base()` is now resolved **only when there is a live chain**.
  That is a small improvement in the direction its own docstring argues for: an
  ops-CLI process shipping a message-only event no longer fires
  `embedded/__init__.py`'s `sys.path` shim for an `isinstance` there is nothing to
  perform.
* Step 0 still runs before step 1, and that ordering gained a second reason: two
  of the five classes are decided from the log message, and after step 1 a path
  inside that message would already have been rewritten by the scrubber.

Nothing touches `capture_lifecycle`, its level, or the scrubbing rules.

---

## 5. Tests

`tests/test_observability_expected_noise.py`, 46 tests, both directions for every
class. RED before the change: 13 failed / 33 passed — the ten "should be dropped"
assertions failed as `assert False` and the three naming tests as
`ImportError: cannot import name 'expected_events'`. That 33 of the tests passed
at RED is the point: the negatives were already true, so they are not vacuous.

Fixtures are the SDK's own. `event_from_exception` builds the exception shape, and
the real `LoggingIntegration` `EventHandler` is driven against a scoped
`sentry_sdk.Client` whose `before_send` hands back the event it BUILT — which is
the post-serialization shape production's hook receives (`client.py` serialises at
:880 and calls `before_send` at :896). `TestTheFixturesAreTheSdks` asserts the
fixtures really carry what the rules read, including that the budget chain has all
three links with the modules tabulated in §2, so no test below it can pass
vacuously. The budget chains are produced by running the real
`tool_runtime._with_cdp_timeout` and the real `asyncio.wait_for`, not by hand: the
`CancelledError` under the `TimeoutError` is `wait_for`'s mechanism and nothing
else puts it there.

The negatives, each its own test: `ToolError` over `AttributeError`;
`ToolError: Failed to spawn browser` over nodriver's plain `Exception`; a bare
`TimeoutError`; a bare `CancelledError`; F-883's `InvalidStateError` over
`StopIteration`; nodriver's `ProtocolException`; a `ConnectionResetError` from a
different callback, and from a different logger, and a different exception type in
the proactor callback; an unawaited task of ours with nodriver's exception type;
the stream message with a real tail, and with the right tail on the wrong logger;
a `ValidationError` from `stealth.backend`; and F-827's four `capture_lifecycle`
messages. `TestNeverRaises` covers the malformed shapes the new readers added, and
`TestTheClassesAreNamed` pins that each real shape is named by its own rule and
that the whole thing is still reached through the one registered `before_send`.

Run, with `STEALTH_MCP_NO_ERROR_REPORTING=1`:

```
tests/test_observability.py
tests/test_observability_scrubbing.py
tests/test_observability_toolerror_filter.py
tests/test_observability_expected_noise.py
tests/test_error_typing.py
tests/test_doc_claims.py
tests/test_no_silent_excepts.py
tests/test_silent_excepts_log.py
tests/test_proxy_sentry_reporting.py
```

380 passed. `ruff check src tests` clean, `ruff format` clean,
`tools/check_file_budgets.py` clean, `vulture --min-confidence 80` clean, `ty`
adds no diagnostic for either changed file.

---

## 6. Residuals

1. **The predicate is still spelled `_is_expected_tool_failure`.** It now decides
   five classes, only one of which is a tool failure. The rename is deliberately
   deferred: `tests/test_observability_toolerror_filter.py` and
   `tests/test_error_typing.py` pin the name, and F-887 was scoped not to touch
   the first of those. A follow-up may rename it and both call sites in one
   commit.
2. **The counts are a snapshot, not a monitor.** Nothing verifies after the fact
   that the volume actually fell, or that a class stopped matching because the
   library changed its wording. Two of the five rules read a message another
   project authors (`mcp` 1.27.1's line 707, CPython's callback name); a rename
   upstream turns a silent drop back into noise, which is the safe direction but
   is not announced. A periodic re-triage is the only check there is.
3. **`caller-input` is a symptom being filtered.** 466 events in seven days is
   one caller repeatedly sending `window_width=`. Dropping them is right — FastMCP
   already answered — but the underlying ask (tool parameter names an agent
   guesses wrong) is untouched, and the events were the only place it was visible.
4. **`nodriver-dead-browser` keys on a path in a task repr.** If nodriver is ever
   vendored, re-exported, or imported under a different distribution name, the
   word `nodriver` may leave the message and the class stops matching. Again the
   safe direction, and again unannounced.
5. **A page-authored exception could, in principle, impersonate a class.** All
   five rules require a type NAME and a MODULE that only a real class carries in
   a real chain, so nothing a page returns reaches them; but `classify` reads a
   payload it does not own, and the guarantee is "no rule matches an unreadable
   value" (`_UNREADABLE`) rather than a proof about arbitrary input.
6. **Neither the `ClientDisconnect` volume nor the proactor reset was traced to a
   root cause.** 6 200 disconnects in a week may be normal for a fleet of agents
   whose sessions end, or may be a liveness probe cancelling POSTs it should not.
   F-887 stopped reporting them; it did not ask why there are so many.
