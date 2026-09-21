# F-929 — an `ExceptionGroup` bypasses every chain-keyed expected-event class

**Status:** FILED, not fixed. No code change ships with this document.
**Module:** `src/stealth_chrome_devtools_mcp/observability.py` (`_exception_chain`),
consumed by `src/stealth_chrome_devtools_mcp/expected_events.py` (`classify`).
**Found:** 2026-09-21, from the other side, while closing F-913's review defect S3
in `embedded/payload_log_sites._chain`.

---

## 1. The census line

`observability._exception_chain` follows `__cause__`, else `__context__` unless
`raise ... from None` suppressed it, and stops:

```python
        following = current.__cause__
        if following is None and not current.__suppress_context__:
            following = current.__context__
        current = following
```

It never traverses `BaseExceptionGroup.exceptions`. So for an exception that
arrives inside a task group, the chain it produces is **one link long** and that
link is the group.

That chain is the input to `expected_events.classify`, which is step 0 of the one
`before_send` — the only place this product DROPS a Sentry event.

## 2. Why it matters

F-887 built `expected_events` because seven days of this project's Sentry were
**13 700+ events** of noise the product produces on purpose (the volumes are in
that module's own docstring). Two of its five classes are keyed on the exception
CHAIN — `error-convention` and `client-disconnect`'s first arm — and both are
bypassed entirely when the exception is grouped.

The direction of that harm is **under-dropping**: noise that should have been
dropped ships. Nothing is wrongly dropped, and nothing leaks. That bounds the
severity, and it is why this is filed rather than fixed in F-913's lane.

**There are TWO defects here and the second is the one that earns a number.**
The first is the noise above — real, bounded, and arguably worth living with.
The second is that `expected_events` **documents an invariant that is false and
that nothing tests**: "Both paths must judge the same set" (`:121`). It is false
for a group (§3.1, measured), and no pin asserts it. That is a worse class than
the noise, because it is load-bearing for code that does not exist yet: the next
author to write a rule in this module will read that sentence, believe it, and
key their rule on chain MEMBERSHIP rather than on position — at which point one
event classifies two ways depending on whether the SDK happened to hand
`before_send` a live exception. A comment stating a guarantee nothing enforces
is a trap with a long fuse.

## 3. The measurement

Driven through the real `sentry_sdk.utils.event_from_exception` and the real
`expected_events.classify`, with `observability._exception_chain` as the live
chain, exactly as `before_send` receives them.

| exception | bare, live | bare, payload | grouped, live | grouped, payload |
|---|---|---|---|---|
| `ToolError("timed out")` | `error-convention` | `error-convention` | **`None`** | **`None`** |
| `ToolError` raised `from TimeoutError` | `error-convention` | `error-convention` | **`None`** | **`None`** |
| `starlette.requests.ClientDisconnect` | `client-disconnect` | `client-disconnect` | **`None`** | **`None`** |
| `ConnectionResetError(10054)` | `None` | `None` | `None` | `None` |

And through the whole of `observability._scrub_event`, which is what actually
decides:

```
bare ToolError           -> DROPPED
ToolError in a group     -> SHIPPED
```

The last row of the table is a CONTROL, not a finding: a bare
`ConnectionResetError` is already `None` because `proactor-teardown` is keyed on
CPython's own message, so there is nothing for the group to take away. Its
presence is what shows the other three rows are about grouping and not about the
harness.

### 3.1 The two paths see different SETS, and agree only by accident of POSITION

`expected_events`' module docstring states the invariant in one sentence,
under the heading "One rule, two paths" (`expected_events.py`:121):

> **Both paths must judge the same set**, so every link is reduced to a `Link`
> first — a type NAME, a MODULE and the FRAMES, each spelled the way the SDK
> spells them.

Measured, for a `ToolError` inside a one-leaf group:

```
live     : ['ExceptionGroup']
payload  : ['ToolError', 'ExceptionGroup']
_links(live)    : ['ExceptionGroup']
_links(payload) : ['ExceptionGroup', 'ToolError']
```

**The sets differ — one link against two.** The payload path does see the leaf,
because Sentry serialises it (F-913 §6.1 residual 4 has that measurement); the
live path cannot, because `_exception_chain` has no group arm.

The VERDICTS agree today, and the reason is worth writing down because it is
fragile: `error-convention` is keyed on the **outermost** link, the group is
outermost on both paths, and `ExceptionGroup` is neither ours nor a tolerated
budget link — so both paths fail at the same first test. Any rule that reads
membership rather than position ("some link is ours", "every link is ours")
would answer differently on the two paths for the same event. That is the latent
defect, and `_links`' own contract is what it violates:

```python
    if chain:
        return tuple(_live_link(exc) for exc in chain)
    return _payload_links(event)
```

The live chain, when present, **replaces** the payload entirely.

So the invariant is not merely unenforced, it is already false, and it is false
in the one direction a reader cannot see from the code: `_links` looks like it
normalises two shapes to one, and it does — but only over whatever the caller
managed to put in `chain`, and for a group that is strictly less than the event
carries. Whichever fix §5 takes, **it has to leave a pin behind that fails when
the two paths disagree**, keyed on the sets rather than on a verdict: today's
verdicts match, so a verdict-keyed pin would be green the moment it was written
and would stay green through exactly the change it exists to catch
(`green-pin-needs-a-mutation`'s shape, which this lane has already hit twice).

## 4. Reachability

Not measured against live Sentry volume — this document does not claim a number
of events. It is reachable by construction, and this codebase has already paid
for the shape once:

* `cli_call._unwrapped` (`:272-284`) exists precisely for it, and says so:
  *"The transport runs under `anyio.create_task_group`, which raises a GROUP"*.
  It unwraps only **singly-nested** groups — *"a group of several is a
  genuinely composite failure"*, its own words — and it lives on the CLI's
  verdict path, not on any Sentry path. `_run` catches `Exception`, which
  covers `ExceptionGroup`, so a multi-leaf group reaches the report path as a
  group.
* Four `anyio.create_task_group()` sites in `embedded/`: `proxy_selfheal.py`:411
  (the backend-generation loop), `session_hygiene.py`:87, and `singleton.py`:906
  and `:939` (`_proxy_streams`). **Three of those run in the stdio proxy**, which
  bootstraps its own Sentry (`server.py`'s stdio branch —
  `configure_logging("proxy")` + `_start_proxy_error_reporting`, F-827).
* A `ToolError` is what a bounded operation converts its timeout into, and
  `error-convention` exists to drop exactly that. A tool body that fails inside
  a task group produces the first row of the table above.

## 5. Options, and why none is taken here

1. **Fold `exceptions` into `_exception_chain`'s walk**, the way F-913 did for
   `payload_log_sites._chain` (commit `c539247`): breadth-first over
   `__cause__`, `__context__` and `exceptions`, `isinstance(x,
   BaseExceptionGroup)` rather than a duck-typed `getattr`. One expression, and
   it makes the live path see what the payload path already sees.

   **It changes a DROP rule, which is the whole reason it is not done here.**
   Widening the set `error-convention` judges makes events start being dropped
   that ship today. That is the intent, but the blast radius is "which faults
   stop reaching a maintainer", and it deserves its own measurement of what
   would newly be dropped — not a line folded into a redaction lane.

2. **Unwrap at the report site**, `cli_call._unwrapped`'s approach, applied on
   the Sentry path. Rejected on convention 4 on sight: it would be a second home
   for "what is the chain of this exception", one import from the first.

3. **Do nothing and accept the noise.** Defensible — the cost is Sentry volume,
   which is what F-887 was about, so "defensible" is not "free".

Option 1 is the likely fix. **Two things gate it**, and neither is optional:

* a measurement of what newly DROPS. This defect under-drops; a careless fix
  over-drops, and that is the direction that loses real errors. "Which faults
  stop reaching a maintainer" is the blast radius and it has to be enumerated,
  not asserted;
* a pin over the invariant itself, per §3.1 — comparing the two paths' link
  SETS for a grouped exception, not their verdicts. Closing the walk without it
  fixes today's instance and leaves the documented-but-untested guarantee
  standing for the next one.

## 6. Scope, and what this is NOT

* **Not a PII leak.** F-913's own group hole WAS one — a quoting
  `ValidationError` inside a group reached Sentry with its whole `input_value=`
  echo — and that is closed (`payload_log_sites._chain`, commit `c539247`,
  pinned by `test_a_group_that_carries_a_quoting_leaf_is_restated_too`). This
  finding is the same structural blind spot in a different module with a
  different consequence, and the redaction rule no longer depends on it.
* **Not a wrong drop.** Measured one-directional: grouped events that should be
  dropped ship. No input produced a drop that the bare form did not also
  produce.
* **`expected_events` itself is not at fault.** It judges the chain it is
  handed. The missing arm is in `observability._exception_chain`.
* **The message-keyed and frame-keyed classes are unaffected** by the chain arm:
  `proactor-teardown` and `nodriver-dead-browser` read the message, and
  `caller-input` reads the traceback frames. A group changes which frames are
  adjacent, which is a separate question this document does not measure.

## 7. Related

* `audit/stage2/finding_F913_validation_error_echoes_tool_result.md` — §6.1
  residual 4, where this limitation is named from the other side, with the
  measurement that Sentry serialises every leaf of a group as its own
  `exception.values` entry. `payload_log_sites._chain`'s docstring states the
  divergence deliberately: the two walks no longer mirror each other.
* `audit/stage2/finding_F887_sentry_expected_noise.md` — why `expected_events` exists, and the 13 700+
  events a week that make under-dropping a cost rather than a curiosity.
* F-902 / `embedded/cdp_transport.py` — the precedent for a rule whose blind
  spot is measured, named, and either closed or filed rather than left implied.
