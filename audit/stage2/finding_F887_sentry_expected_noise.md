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
  the caller already knows. 466 events. **This one is the trap**: its logger,
  message and chain are byte-identical to a `ValidationError` our OWN code
  raised, because `tool_manager` wraps the whole of `tool.run` — argument
  validation and tool body alike — in one `try` with one `logger.exception`. A
  rule that stopped at the logger dropped our own crashes with the noise; see §2
  and §3.
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

Three spelling rules behind that table were read out of the installed
`sentry_sdk/utils.py` rather than guessed, because **all three differ from the
obvious Python answer**, and each one was a live-vs-payload divergence before it
was read:

```python
def get_type_name(cls):            # :426
    return getattr(cls, "__qualname__", None) or getattr(cls, "__name__", None)

def get_type_module(cls):          # :430
    mod = getattr(cls, "__module__", None)
    if mod not in (None, "builtins", "__builtins__"):
        return mod
    return None
```

1. **`type` is `__qualname__`**, so a class declared inside a function
   serializes as `outer.<locals>.Name`. Reading `__name__` on the live path let a
   nested class match a `Kind` the serialized path could never match.
   `expected_events._type_name_of` mirrors the SDK.
2. **`module` omits `builtins` and `__builtins__` — and NOT `__main__`.** So a
   serialized `TimeoutError` carries `module: None` while the live class says
   `"builtins"` (normalized by `_UNWRITTEN_MODULES`), but a class from `__main__`
   keeps its module on BOTH paths. `embedded/server.py` runs as `__main__` under
   runpy, so this is not theoretical: a `__main__`-defined class named
   `TimeoutError` measured `dropped(live)=True dropped(payload)=False` while
   `_UNWRITTEN_MODULES` still carried `"__main__"`.
3. **`serialize_frame` writes `module` as `frame.f_globals["__name__"]`** and
   lists frames caller-first — the traceback's own order. That is what makes a
   live traceback walk and a serialized `stacktrace.frames` list directly
   comparable, which `caller-input` depends on (§3).

And the two paths do NOT agree on ORDER. `_exception_chain` walks outermost
first (`exc`, then its cause); Sentry's `values` list the root cause first and
the reported exception LAST (measured: the CDP budget chain serializes as
`[CancelledError, TimeoutError, ToolError]`). `_links` reverses the payload so
every rule is handed one order, outermost first.

`LoggingIntegration`'s event shape, also measured: `event["logger"]` is the
record name, `event["logentry"]` is
`{"message": …, "formatted": …, "params": []}`, `event["message"]` is absent, and
a record with no `exc_info` produces an event with **no** `exception` key and a
`hint` carrying `log_record` but not `exc_info`.

### The frames that separate a caller's typo from our own bug

`fastmcp/tools/tool_manager.py`:220-229 wraps `await tool.run(arguments)` in ONE
`try` and logs anything out of it the same way:

```python
try:
    return await tool.run(arguments)
except ToolError as e:
    logger.exception(f"Error calling tool {key!r}")
    raise e
except Exception as e:
    logger.exception(f"Error calling tool {key!r}")
```

`fastmcp/tools/tool.py`:295's `type_adapter.validate_python(arguments)` is
*inside* that `run`. So logger, message and chain are byte-identical for a
caller's bad kwarg and for a `ValidationError` our own body raised. Measured
frames of the outermost value, through the real `Tool.run` (fastmcp 2.11.2), on
both paths:

```
FASTMCP-ARG-VALIDATION   [0] <caller>                     call_tool
                         [1] fastmcp.tools.tool           run
                         [2] pydantic.type_adapter        validate_python

OURS (inside the body)   [0] <caller>                     call_tool
                         [1] fastmcp.tools.tool           run
                         [2] <our module>                 spawn_browser
```

`fastmcp.tools.tool` `run` is in the traceback of EVERY tool failure, ours
included — so presence is worthless and **adjacency** is the discriminator.

---

## 3. The rule chosen, and the two rejected

### Chosen — **five named classes, each ONE rule, read through one `Link` shape**

`src/stealth_chrome_devtools_mcp/expected_events.py`, one consumer
(`observability._expected_event_class`). `classify` returns the NAME of the class
that recognised the event, so a drop is attributable — reported, never branched
on; there is one drop.

| class | rule |
|---|---|
| `error-convention` | the OUTERMOST link is our `ToolError` (subclasses included) and every other link is ours or a budget link (`TimeoutError` / `asyncio.CancelledError`) |
| `client-disconnect` | chain entirely `starlette.requests.ClientDisconnect`; OR no links, logger `mcp.server.lowlevel.server`, message EXACTLY `Received exception from stream: ` |
| `proactor-teardown` | logger `asyncio` + message prefix `Exception in callback _ProactorBasePipeTransport._call_connection_lost` + chain entirely `ConnectionResetError` |
| `nodriver-dead-browser` | logger `asyncio` + message prefix `Task exception was never retrieved` + `nodriver` in the message + chain entirely `ConnectionRefusedError` |
| `caller-input` | logger `FastMCP.fastmcp.tools.tool_manager` + chain entirely pydantic `ValidationError` + the outermost link's frames carry `fastmcp.tools.tool run` **immediately above** `pydantic.type_adapter validate_python` |

Six properties of that table are load-bearing:

1. **A budget link may sit BEHIND ours; it may never BE the reported
   exception.** In every real budget chain the conversion is the last raise
   (`tool_runtime._with_cdp_timeout`, `browser_manager.navigate`:1224), so the
   `ToolError` is outermost. Mere set membership licensed the opposite: a body
   whose cleanup timed out UNCONVERTED while handling a `ToolError` has that
   `ToolError` in its chain, and Sentry titles the issue with the
   `TimeoutError`. That is the missing-convention case and it ships.
2. **`caller-input` is decided by the FRAMES, not the logger.** See §2. A
   logger-only rule measured `dropped=True` on both paths for the unknown-env-key
   `Settings()` crash and for `browser_manager.py`:429's `BrowserInstance(...)`
   — the first of which fails EVERY spawn. The frame pair must be ADJACENT,
   because `fastmcp.tools.tool` `run` appears in every tool failure's traceback.
3. **The stream message is matched by equality.** That line reports real protocol
   faults too; the sibling issue
   `Received exception from stream: Received response with an unknown request ID:
   … Method not found` (2 events) must keep shipping, and a prefix match would
   have taken it with the 6 500.
4. **The proactor callback is the whole claim.** A `ConnectionResetError` our own
   code saw is a fact about this product's sockets. Only CPython's one teardown
   callback is absorbed.
5. **`nodriver` in the message is what tells the library's orphaned task from
   ours.** Same logger, same complaint, same exception type; the task repr names
   the coroutine's defining file and that is the only discriminator available.
6. **A logger name is never the test on its own.** The `AttributeError` in
   `navigate` that this project actually shipped arrived on
   `FastMCP.fastmcp.tools.tool_manager`, the very logger the noise arrives on.
   Where a rule names a logger it names an exception kind beside it, and where
   even that is not enough (`caller-input`) it names the frames.

### The one rule that is WIDER than its name

`client-disconnect`'s message-only arm matches **"an exception with no text at
`mcp.server.lowlevel.server`"**, not "a `ClientDisconnect`". Line 707 is a
`case Exception():` catch-all formatting `str(exc)` with no `exc_info`, so the
empty tail means `str(exc) == ""`. Measured, all three dropped:

```
TimeoutError()   -> 'Received exception from stream: '  dropped=True
RuntimeError()   -> 'Received exception from stream: '  dropped=True
ValueError()     -> 'Received exception from stream: '  dropped=True
```

A bare `RuntimeError()` from our own session handling is a real fault and goes
with the 6 500. The rule cannot be narrower: that event carries no exception
values, no frames and no extra, so there is genuinely nothing else in it to read.
The trade is taken deliberately, is pinned as a parametrized test, and is said in
those words in the leaf's docstring, the CLAUDE.md row, the CHANGELOG and §6.4.

One rule, both paths: every link is reduced to a `Link` (type NAME + MODULE +
FRAMES) and every class is a `Kind` test over the first two, plus an adjacency
test over the third for `caller-input`. There is exactly **one** `isinstance` in
the module, `_is_ours`, because convention 2 is about a CLASS and a subclass
declared anywhere must be covered (pinned next door). Every other kind matches by
name and module on both paths *deliberately*: an `isinstance` on the live path
would accept subclasses that the serialized path, seeing only the subclass's own
qualified name, could never accept, and the two paths would quietly disagree —
which is the defect this module's docstring exists to prevent.

`Kind.modules` is a set of module ROOTS matched exactly or on a dotted boundary,
not a raw prefix: pydantic arrives as `pydantic_core._pydantic_core` and ours as
`stealth_chrome_devtools_mcp.embedded.tool_errors`, so a root set is needed — and
the boundary keeps a package merely SPELLED like one of ours (`pydanticfoo`,
`stealth_chrome_devtools_mcp_extra`) out.

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
`tool_errors` import) — and loses the taxonomy. It did not get smaller: the
docstrings arguing the five rules grew by more than the taxonomy weighed.

| file | physical | non-blank |
|---|---|---|
| `observability.py` on `origin/main` | 613 | 521 |
| `observability.py` on this branch | 616 | 527 |
| `expected_events.py` (new) | 610 | 507 |

Physical lines are the gate's metric (`tools/check_file_budgets.py`:171,
`len(text.splitlines())`); both counts are given because the two are easy to mix
and the earlier draft of this section did mix them, pairing main's physical count
with the branch's non-blank one and reading as a ~100-line shrink. Neither file is
grandfathered and both sit under the 1000-line default.

Four behaviour notes:

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
* Every drop is recorded. `_is_expected_tool_failure` logs the class it got at
  DEBUG, so "a drop is attributable" is true of a running process and not only of
  the suite. DEBUG for the reason the module's other log calls are: the logging
  integration turns INFO into breadcrumbs and ERROR into events, and an event
  raised while deciding about an event is how a reporting loop starts.

Nothing touches `capture_lifecycle`, its level, or the scrubbing rules.

---

## 5. Tests

`tests/test_observability_expected_noise.py`, 58 tests, both directions for every
class. Two RED runs, because the file was written twice: once for the original
change and once for this review's findings.

**RED 1** (before any production change): 13 failed / 33 passed — the ten "should
be dropped" assertions failed as `assert False` and the three naming tests as
`ImportError: cannot import name 'expected_events'`. That 33 of the tests passed
at RED is the point: the negatives were already true, so they are not vacuous.

**RED 2** (the review's findings, against the shipped rules): 8 failed / 46
passed, one failure per finding —

```
TestTheBudgetIsTheProductAnswering::test_an_unconverted_outermost_timeout_still_ships
TestTheBudgetIsTheProductAnswering::test_the_unconverted_timeout_still_ships_from_the_payload_alone
TestTheBudgetIsTheProductAnswering::test_a_main_module_class_named_timeouterror_ships_on_both_paths
TestCallerInput::test_our_own_settings_crash_under_the_same_logger_still_ships
TestCallerInput::test_our_own_model_crash_under_the_same_logger_still_ships
TestCallerInput::test_a_pydantic_error_with_no_fastmcp_frame_at_all_still_ships
TestTheClassesAreNamed::test_the_two_paths_are_normalized_to_one_order
TestTheClassesAreNamed::test_a_drop_is_recorded_with_the_name_of_the_rule
```

The `__main__` fixture is also what surfaced the `__qualname__` rule, which the
review had not reached: it failed on
`assert ['main_module_timeout.<locals>.TimeoutError', 'ToolError'] ==
['TimeoutError', 'ToolError']`, i.e. the SDK had named the class by its
`__qualname__` where the live path was reading `__name__`. That is the same
defect class as the `__main__` module one, by the other field, and it got its own
pin (`test_a_nested_class_is_named_the_way_the_sdk_names_it`).

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

The negatives, each its own test: `ToolError` over `AttributeError`; an
unconverted outermost `TimeoutError` (both paths);
`ToolError: Failed to spawn browser` over nodriver's plain `Exception`; a bare
`TimeoutError`; a bare `CancelledError`; a `__main__`-defined class merely NAMED
`TimeoutError` (both paths); F-883's `InvalidStateError` over `StopIteration`;
nodriver's `ProtocolException`; a `ConnectionResetError` from a different
callback, and from a different logger, and a different exception type in the
proactor callback; an unawaited task of ours with nodriver's exception type; the
stream message with a real tail, and with the right tail on the wrong logger;
our OWN pydantic `ValidationError` under FastMCP's own logger in three shapes
(the `Settings()` env crash, the `BrowserInstance(...)` shape, and one with no
FastMCP frame at all); a `ValidationError` from `stealth.backend`; and F-827's
four `capture_lifecycle` messages.

`TestTheFixturesAreTheSdks` proves the fixtures carry what the rules read;
`TestNeverRaises` covers the malformed shapes the readers added, including an
unreadable exception VALUE (`"not a dict"`), which must never be recognised;
`TestTheClassesAreNamed` pins that each real shape is named by its own rule, that
both paths are normalized to one ORDER, that a nested class is named the way the
SDK names it, that a drop is logged with the rule's name, and that the whole
thing is still reached through the one registered `before_send`. The over-drop
this rule set accepts — any text-free exception at the mcp session logger — is
pinned as a parametrized test rather than left implicit.

Run, with `STEALTH_MCP_NO_ERROR_REPORTING=1`:

```
tests/test_observability.py                    57 passed
tests/test_observability_scrubbing.py          77 passed
tests/test_observability_toolerror_filter.py  142 passed
tests/test_observability_expected_noise.py     58 passed
tests/test_error_typing.py                     19 passed
tests/test_doc_claims.py                       12 passed
tests/test_proxy_sentry_reporting.py           19 passed
tests/test_no_silent_excepts.py                     }
tests/test_silent_excepts_log.py                    }  the contract / source-sweep
tests/test_tool_sections_contract.py                }  pins, run together
tests/test_security_boundary.py                     }
```

446 passed together. All seven pre-commit gates green: `ruff format --check`,
`ruff check`, `ty --exit-zero-on-warning` (115 diagnostics = main's baseline;
**zero** for `expected_events.py`, and the two on `observability.py` are the
pre-existing `**exception` and `capture_message` lines this change did not
touch), `vulture`, `check_suppression_owners.py`, `check_file_budgets.py`,
`check_pinned_imports.py`.

---

## 6. Residuals

1. **The predicate is still spelled `_is_expected_tool_failure`.** It now decides
   five classes, only one of which is a tool failure. The rename is deliberately
   deferred: `tests/test_observability_toolerror_filter.py` and
   `tests/test_error_typing.py` pin the name, and F-887 was scoped not to touch
   the first of those. A follow-up may rename it and both call sites in one
   commit.
2. **The counts are a snapshot, not a monitor.** Nothing verifies after the fact
   that the volume actually fell, or that a class stopped matching because a
   library changed its wording. A periodic re-triage is the only check there is,
   and residuals 3-5 are all instances of the same exposure.
3. **`caller-input` keys on two frames of FastMCP's own internals.**
   `fastmcp.tools.tool` `run` directly above `pydantic.type_adapter`
   `validate_python` (fastmcp 2.11.2, pydantic 2.11.7). If FastMCP renames
   `Tool.run`, moves it, or wraps it in a decorator that inserts a frame, the
   adjacency breaks and the 466 events come back. That resolves toward SHIPPING,
   which is the right direction and is the only reason this is a residual rather
   than a defect — but nothing announces it, and the alternative discriminator
   ("no frame from our package") was rejected because it drops any third-party
   `ValidationError` under that logger instead. There is no version pin on the
   frame names.
4. **`client-disconnect`'s message-only arm is wider than its name**, and the
   width is real: ANY text-free exception at `mcp.server.lowlevel.server` is
   dropped, a bare `RuntimeError()` from our own session handling included (§3,
   measured). It cannot be narrowed — the event has no values, no frames and no
   extra — so the only honest mitigation is that the class is *named* and pinned
   rather than silent. If that logger ever starts carrying our own faults, this
   is the rule to revisit first.
5. **`nodriver-dead-browser` keys on a path in a task repr.** If nodriver is ever
   vendored, re-exported, or imported under a different distribution name, the
   word `nodriver` may leave the message and the class stops matching. Safe
   direction, unannounced.
6. **`caller-input` is also a symptom being filtered.** 466 events in seven days
   is one caller repeatedly sending `window_width=`. Dropping them is right —
   FastMCP already answered — but the underlying ask (tool parameter names an
   agent guesses wrong) is untouched, and the events were the only place it was
   visible.
7. **A hand-built or hostile event could, in principle, impersonate a class.**
   All five rules require a type NAME, a MODULE and (for `caller-input`) FRAMES
   that only a real chain carries, so nothing a page returns reaches them; but
   `classify` reads a payload it does not own, and the guarantee is "no rule
   matches an unreadable value" (`_UNREADABLE`) rather than a proof about
   arbitrary input.
8. **Neither the `ClientDisconnect` volume nor the proactor reset was traced to a
   root cause.** 6 200 disconnects in a week may be normal for a fleet of agents
   whose sessions end, or may be a liveness probe cancelling POSTs it should not.
   F-887 stopped reporting them; it did not ask why there are so many.
9. **The two paths agree by construction, not by proof.** `_links` normalizes
   name, module, frames and order, and three tests pin those four. A future
   `Link` field would have to be added to both readers, and nothing mechanical
   fails if only one of them learns it — the `EventFacts` dataclass makes the
   surface visible but does not enforce symmetry.
