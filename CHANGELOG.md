# Changelog

## Unreleased

### Added — F-897: a new session can start from an existing one

`spawn_browser` gains **`seed_from`**, and the CLI **`stealthy spawn --session
NAME --from SOURCE`**: when `session` names a session that does not exist yet,
it is created as a copy of `seed_from` instead of a copy of `default`. Leave it
unset and nothing changes — unset means `default`, which is what every session
has always been seeded from, and the two are ONE code path rather than two that
agree today.

```console
stealthy spawn --session work --headed        # log in by hand
stealthy close <instance>
stealthy spawn --session work2 --from work    # already logged in
```

`seed_from` takes a session NAME through the same reader and the same gate as
`session` (`profile_seed.require_name` → `require_allowed`), so a path, a
reserved word (`master`, `master-snapshot`) and a spelling the filesystem folds
onto one of them (`default.`) are all refused there and not by a second
resolver. The refusal says `seed_from`, because a caller told about `session`
goes and edits the wrong argument.

It follows F-896's rules for a name-shaped argument exactly, because it now
shares the code that makes them: **`seed_from=""` means "not given"** and seeds
from `default` like an unset `--from`, while a NON-empty value that is empty
once stripped (`"   "`, `"\t"`) **raises** — with the same sentence `session`
and `user_data_dir` raise, differing only in the field name it opens with. A
drive hidden behind a space (`" C:profile"`) is refused too.

**It applies at CREATION and nowhere else.** For a `session` that already
exists it RAISES, naming where that session was actually seeded from. The two
alternatives were rejected deliberately: a silent no-op tells a caller their
session came from `work` when it did not, and a re-seed overwrites a login
somebody typed by hand.

**It also raises when the target is not a session at all** — a `user_data_dir`
naming a directory outside the session root is opened exactly as it is, so
there is nothing for `seed_from` to apply to and it would have been silently
ignored. Reachable only through the deprecated path spellings
(`--arg user_data_dir=<path>`, `stealthy spawn --profile`), and refused there
rather than dropped.

**Every one of these refusals reaches you as a refusal**, not as
`Failed to spawn browser: …`. A source that is open, a source that does not
exist and a source naming a reserved word used to be raised from inside the
spawn's own error handler and came back labelled as a spawn that had failed —
about a spawn that never started.

**A source that is open in a browser is refused BY NAME, and nothing is created
on disk.** This is the safety argument the feature rests on: a profile copy
answers a file Chrome holds by skipping it with a warning, so copying a live
profile yields a session that looks complete and is missing exactly the logins
that were asked for, with no way to enumerate the gap. The message names the
session and the remedy. `default` is the one exception, and for a mechanical
reason rather than a privilege of the word: the product maintains a separate,
closed, copyable form of it (the seed), so seeding from `default` works whether
or not it is open — at the cost that the copy can be as old as the last time
`default` was closed, which `seed_changed_since` reports. Carrying a login out
of a RUNNING source needs a CDP hand-off rather than a file copy; that is
F-898.

**Per-session seeds are deliberately not built.** Each would cost ~0.47 GB plus
its own refresh trigger, staleness witness and in-use rule — a second copy of
the lifecycle F-892/F-893 spent two findings getting right for one seed, bought
to avoid a refusal whose remedy is closing a window.

**Provenance.** The new session's marker records the source's NAME, so
`spawn_diagnostics.profile_selection.seeded_from` is now any session's name
rather than only `default` — and still a word you can pass straight back as
`session=`. `stealthy spawn` prints the same `seeded from <name> at <when>`
sentence `stealthy profiles` does; both phrase it through
`profile_seed.seed_sentence`, so they cannot drift.

That sentence also stops reporting an **unreadable** source as an unchanged
one. `seed_changed_since` is `None` whenever no login witness can be read in
the recorded source — which a deleted or renamed source guarantees — and until
now that rendered byte-identically to "read it, nothing has moved". It reads
`(source unreadable)` instead: lowercase and parenthetical, because it is a
caveat about what could be read and not the alert `SEED CHANGED SINCE` is.

**Three files were cut to pay for it, because caps ratchet down only.**
`embedded/profile_copy.py` is the new home for copying a Chrome profile
directory — what such a copy leaves behind and what it does about a locked file
— which took `clone_storage.py` from its grandfathered 1054 to under the
1000-LOC default, so its `GRANDFATHER` row is deleted rather than ratcheted.
`cli_render.py` is the new home for the `ls`, `spawn` and `tools` renderings,
which took `cli_call.py` from exactly 1000 to 911. `embedded/profile_source.py`
is the new home for `seed_from` itself — which session a new one is copied
from, and whether there is a new session for that copy to apply to — which took
`profile_seed.py` from 1112 to 917: that module answers what a seed IS and what
a caller may SAY on every spawn, while nothing in the new one runs unless a
caller wrote `seed_from`. All three are internal moves with no behaviour
change.

### Fixed — F-909: two pins raced a wall clock and a loaded Windows runner won

Two tests asserted truthfully about things that had never happened, and both
only on Windows cells under load. No product code changed.

`test_the_adoption_path_goes_through_the_reclaiming_attach` patched the
adoption budget to 250 ms and then spent it on a REAL locked
`browser_pids.json` claim before the attach door was ever reached — measured at
~3 ms locally, but the budget was racing that write as well as the door's own
sleep. When the claim won, nothing was ever opened and the node reported an
open connection that had never existed. The claim is now stubbed (and, for the
first time, ASSERTED — taken, and handed back), the door parks on an
`asyncio.Event` instead of sleeping, the thread pool is warmed before the call,
and reaching the door is its own assertion with its own message. The budget
stays 250 ms: widening it would hide the race rather than remove it.

`test_service_worker_installs_activates_controls_and_unregisters` gated on
`navigator.serviceWorker.ready`, which resolves on an ACTIVE REGISTRATION and
says nothing about whether THIS document is controlled — and the fixture's
worker awaits a network round trip before `clients.claim()`, so the node read
`controller` in that gap. `tests/fixture_routes.py`'s `w16Register` now reports
a distinct `controlled` state, reached by awaiting `controllerchange` with a
re-check of `controller` after arming the listener; the node polls for it.
Nothing sleeps, and a worker that activates but never claims now stops at
`ready`, so the poll's own failure names that state instead of answering with
`uncontrolled`.

Both were reproduced causally first — a stalled claim for one, 1500 ms in front
of `clients.claim()` for the other — and both fixes pass under the same stall
that breaks the old pins.
`audit/stage2/finding_F909_windows_latent_pin_races.md` carries the
measurements.

### Fixed — F-906: a caller's `basicConfig(level=DEBUG)` could route cookies into our logs

`nodriver` writes the **whole raw CDP reply** into its own log text
(`connection.py`:445, DEBUG) and the **whole event message** when a field will
not parse (`connection.py`:451, INFO); `browser.py`:824/:869 log a cookie's name
and value outright, and `websockets` logs the frame underneath (a short one is
printed whole). Cookie names and values ride in all of them.

None of that could reach a handler in any configuration this product ships —
F-902 measured that, and it is re-measured here across backend, proxy,
`--debug` and `STEALTH_MCP_LOG_LEVEL=DEBUG`. But the only thing stopping it was
the log LEVEL, and those loggers carry none of their own, so the level was
**root's to give away**. One `logging.basicConfig(level=DEBUG)` — a test, a
notebook, a caller embedding this backend — gave it away for the whole process:
**measured, both orders**, all four payload lines then reached a root handler
(and so stderr, which for the backend is redirected into `backend-boot.log`, a
durable file), and the INFO one additionally reached Sentry as a breadcrumb,
because `LoggingIntegration`'s breadcrumb handler sits at INFO.

`logging_setup.apply_payload_log_floor()` now holds `nodriver` and `websockets`
at WARNING with an **explicit** level on the family root, set as the first
statement of `configure_logging`. `basicConfig` only ever sets ROOT's level and
`getEffectiveLevel` stops at the first ancestor that has one, so ours wins
whichever way round the two calls happen.

Nothing an operator sees changes: **WARNING is the effective level every
shipped configuration already had**, which is the point — it closes the one door
that was open and no other. nodriver's real diagnostics are untouched
(`connection.py`:483 names the callback and the event *class*, never the
payload), and our own `stealth.*` levels, including `STEALTH_MCP_LOG_LEVEL`, are
not touched at all.

A level rather than a filter or a Sentry `before_breadcrumb`: Sentry patches
`logging.Logger.callHandlers`, which is only reached for a record the level
already admitted, so one mechanism closes all four sinks at once — and
`connection.py`:451 pre-interpolates its payload with `%`, leaving
`record.args` empty, so a filter could only pattern-match text the library is
free to reword.

**It is a floor, not a census**, and what sits above it is named rather than
implied: `element.py`:537/:624/:633 interpolate an element at WARNING, and
nodriver's `Element.__repr__` renders that element's descendant TEXT. That one
needs no `basicConfig` at all and is tracked as **F-907**; raising this floor
over it would silence nodriver's real diagnostics, which is the trade this
change deliberately refuses.

### Fixed — F-907: a page's own form fields and text no longer reach our logs through nodriver

F-906 held `nodriver` at WARNING because everything below that line quotes raw
CDP. **This is the half above it**, and unlike F-906 nothing has to be
misconfigured for it to reach a sink: WARNING is the effective level every
shipped configuration already has.

`nodriver/core/element.py` logs `"could not calculate box model for %s"` with a
live `Element` at **WARNING**, in three places, and `Element.__repr__` renders
the tag, **every attribute as `name="value"`**, and the element's **whole
recursive text content**. So an `<input type=password>`'s `value=`, a `data-*`
carrying a session token and a balance in a `<div>` all went into the line.
`Tab.__repr__` does the same one object up with the tab's URL, query string
included. Measured, an emitted record at that level reaches stderr — which for
the backend is redirected into `backend-boot.log`, a durable file — **and**
Sentry as a breadcrumb on the next event, in the plain shipped backend and
proxy. It needs no handler of anyone's either: in a fresh process the root
logger has none, so the stdlib's own `logging.lastResort` carries a WARNING to
stderr regardless.

**What it is not.** A first reading of this had the three lines firing on every
`click_element` against a `display: none` target. Measured, they do not fire at
all in nodriver 0.47: `Position.center` is a 2-tuple and therefore always
truthy — even for a zero-size box at the origin, `(0.0, 0.0)` — so
`if not center:` cannot open, and the other two paths out of `get_position()`
(a raised `Exception`, or `None`) both leave `mouse_click` before the warning
line. So this ships as insurance and as correctness for any future nodriver
WARNING that renders an object, not as a patch for a live leak; the
reachability premises are pinned, so the day a nodriver bump makes those sites
live, CI says so.

The line still arrives and still names the element; what it loses is every
VALUE and all of its text:

```
could not calculate box model for
  <input attrs=[type, value, data-session-token, class_] children=1>
```

Anything else from nodriver — a `Tab`, a `Connection`, a CDP record — renders as
its type alone, because no half of it has been measured safe. **An exception is
never shaped**, whatever package defined it: its `str()` is a diagnostic, and at
`connection.py`:483 — nodriver's one real WARNING — `exc_info=True` rides beside
it, so every sink that formats a traceback renders the text anyway and shaping
the `%s` would cost only the Sentry breadcrumb its meaning. A
`nodriver.core.connection.ProtocolException` is a nodriver type and the
commonest thing a CDP-touching handler raises, so that line kept reading
`=> Inspected target navigated [code: -32000]` rather than
`=> <nodriver.core.connection.ProtocolException>`.

The rule reads the **argument** and deliberately not the logger's name, so one
of our OWN records carrying a nodriver object is redacted exactly as nodriver's
is — whether a rendering carries page content is a property of the object, not
of the logger it was handed to. Our `stealth.*` sites keep their own redaction
rules (F-869 and its successors); this is the floor under them.

A `logging` **record factory** rather than a filter, because both alternatives
were measured and neither reaches: a `logging.Filter` on the `nodriver` family
root never fires for a `nodriver.core.element` record (filters belong to the
logger a call is made on; only handlers are inherited), and a filter on a
handler is no use in the configuration that leaks, since there we do not own
the handler. A factory sits upstream of handlers, stderr and Sentry alike,
covers loggers created later — including a module a future nodriver adds — and
survives `dictConfig(disable_existing_loggers=True)`. It is keyed on the
argument's **type**, never on the message text, so nodriver may reword these
lines freely; and it chains to any factory already installed. Measured cost:
~350 ns per record for the chained factory call — the price of installing any
factory at all — plus ~80 ns per argument for the scan, against ~1.7 µs to
build a record. Nothing here is on a per-request path; uvicorn access logging
is off.

One thing this does **not** close, and it is now the larger half: `element.py`
:499 raises `Exception("could not find position for %s " % self)` with the same
repr, on the branch that *is* live, and an exception is not a log record — no
logging mechanism reaches it. It is filed as **F-912** rather than left as a
note under a closed finding.

### Fixed — F-908: the same door, from the other end — the SSE transport logged every tool RESULT

F-906 closed what **Chrome said to us**. This closes what **we said back**.
`sse_starlette/sse.py`:362 is `logger.debug("chunk: %s", chunk)`, and for this
backend that chunk is the whole serialised answer to a `tools/call` —
`get_cookies`' jar, `get_page_content`'s HTML, `get_instance_state`'s
localStorage. Measured by driving the real `EventSourceResponse`, not read off
the call:

```
sse_starlette.sse DEBUG chunk: b'event: message\r\ndata: {"jsonrpc":"2.0","id":3,
  "result":{"structuredContent":{"cookies":[{"name":"SID","value":"…"}]}}}'
```

The SSE frame is what carries every answer, because the SDK and FastMCP both
default `json_response` to `False`, nothing here passes it, and an inherited
`FASTMCP_JSON_RESPONSE` cannot reach fastmcp either (F-890 drops the prefix).

**A tool answer has two ends, and both are logged.** The backend logs the SSE
chunk it sends; the **stdio proxy** re-parses the identical bytes and logs the
message — `mcp/client/streamable_http.py`:218 is `logger.debug(f"SSE message:
{message}")`, the whole `JSONRPCResponse`, with :547 as its argument-side twin.
Both processes call `configure_logging`, so capping one end was half a fix:
with `sse_starlette` already held down, the same cookie jar still reached a
root handler in the proxy (measured). So `mcp.client` joins the list too.

It is `mcp.client` and not the `mcp` family: the **server** tree renders
nothing for a request (its whole-message line hands `%s` a `RequestResponder`,
which defines neither `__repr__` nor `__str__`), and capping it would silence
the server SDK's own diagnostics for no gain. It is `mcp.client` and not the
single logger, because a full census of the client package found a **second**
renderer of the same shape in `client/sse.py`.

Identical premise, identical mechanism: these loggers carry no level of their
own, so one caller-side `logging.basicConfig(level=DEBUG)` opened them —
**measured, both orders**. `sse_starlette` and `mcp.client` join
`PAYLOAD_LOG_FAMILIES`, and that is the whole change. WARNING is again the
effective level every shipped configuration
already had, and here the floor costs even less than it did for nodriver:
`sse_starlette` has no call at WARNING or above anywhere in the package, so
there is not one diagnostic for it to stand in front of.

**The families deliberately left OUT are measured too, and pinned**, because
"why is `mcp` not in that list" is the next person's question: `starlette` and
`anyio` log nothing below WARNING at all; `uvicorn`'s whole-ASGI-message logger
replaces bodies with a `<N bytes>` placeholder by construction and logs below
what `basicConfig(DEBUG)` admits; `httpcore`'s body traces carry no return
value, so their message is the trace name; `fastmcp`'s tool-ARGUMENT line is
already shielded by the library's own `FastMCP` root level plus
`propagate=False`; and the `mcp` **server** tree's whole-incoming-message line
renders a `RequestResponder` for a request, while its notification arm — which
does render in full — can carry none of the five client notifications' payload
(enumerated from the SDK's own union, pinned, so a sixth goes RED). Capping the
server family or `fastmcp` wholesale would have silenced those SDKs' own
diagnostics and closed no door.

Two sites a level cannot reach are **recorded rather than fixed**:
`mcp/shared/session.py`:383-384 and :430-432 use module-level
`logging.warning` / `logging.debug`, i.e. the **root** logger, so no family cap
reaches them. :383 is at WARNING, therefore reachable as shipped, carrying
pydantic's middle-truncated `input_value=` echo of a caller's arguments; and
:430-432 renders the **whole message** at WARNING in one line. Both are on
validation-failure paths, both need a different mechanism, and they are named in
`audit/stage2/finding_F908_sse_starlette_logs_tool_result.md` §6 — beside
F-907, the other line this floor sits below.
### Added — F-898: `--from` a session that is still OPEN

F-897 refuses `--from work` while `work`'s browser is running, and it is right
about the mechanism it had: measured on Chrome 153, a file copy of a running
profile carries **zero** cookies — the SQLite jar is held open and skipped — and
nothing can say afterwards what was lost. Since F-888 a named session's browser
survives its backend, so "close it first" stopped being a remedy anyone takes.

A source whose browser **this backend drives** is now seeded anyway: the file
copy runs for everything it can still carry, and the COOKIES are handed over the
two browsers' CDP connections (`Storage.getCookies` → `Storage.setCookies`)
after the new browser launches.

```console
stealthy spawn --session work --headed          # log in by hand — and leave it open
stealthy spawn --session work2 --from work      # already logged in
```

```
instance   : 4f0c…
role       : explicit
profile    : C:\stealth-mcp-browser-sessions\sessions\work2
seeded     : seeded from work at 2026-09-21 14:02
cookies    : 14 handed over from the running source
```

**What it carries is cookies, and the whole jar.** Every kind measured — session,
persistent, `HttpOnly`, `Secure`, `SameSite=None`, `Partitioned`/CHIPS — with
every shared field round-tripping exactly, including a session cookie, which a
file copy can never carry because it is never written to disk. It carries **no**
`localStorage`, `sessionStorage`, IndexedDB, Cache Storage, service-worker
registration or saved passwords, so a site that keeps its token in
`localStorage` will NOT be logged in. And it carries every site the source
session is logged into, not just the one you had in mind — which is what a copy
of a closed session already does.

**`--from default` gets the same hand-off, and that is the case most spawns
take** — an unset `--from` means `default`, so this is `stealthy spawn --session
NAME`. `default` is the session you log in to by hand, its browser normally
stays open, and while it is open its seed is never refreshed — so the copy alone
could be days old. The copy still comes from the seed (a closed, safe copy) and
the live jar is now written on top of it. Two things follow that are worth
stating: a `default` held by a Chrome this backend does NOT drive is copied and
never refused, because the seed exists and is exactly what 2.1.12 promised; and
a machine with no seed yet AND `default` open is now refused by name instead of
silently copying the live directory, which carries no cookies at all. Closing
that window once writes the seed and the refusal is gone for good.

A running NAMED source this backend does NOT drive (another backend's, or a
Chrome nobody here launched) is still refused by name, and the refusal now says
which half is missing: there is no CDP connection of ours to ask for its
cookies.

A hand-off that fails does not fail the spawn — the session exists and works
without the source's cookies, and the answer says
`seeded_via: "copy"` with a shape-only `cookie_handoff_error`.

**No cookie name or value reaches a log line, a message, the returned record or
Sentry** — counts, the CDP method and an exception type only. A cookie name
identifies on its own and a value is the session itself.

Internals: the new `embedded/cookie_handoff.py` is the one home for the jar
transfer and for which profile directories this backend drives; the regenerable
profile trim moved from `clone_storage` to `profile_copy`, beside the list it
reads, which took `clone_storage` from 1000 to 993 lines.

### Fixed — F-912: a hidden password field's value no longer leaves the machine in an error message

`get_element_state` on an element that lays out no box answered with the
element rendered in full — tag, **every attribute as `name="value"`**, and all
of its descendant text:

```
Failed to get element state: could not find position for <input id="pwhidden"
  type="password" value="SECRET-VALUE" style="display:none"></input>
```

That text is nodriver's, not ours: `element.py`:499 raises
`Exception("could not find position for %s " % self)`, and every `except` block
that relays `str(exc)` carries the whole element with it.

**What was new is that the content LEFT THE PROCESS.** `get_element_state`
already returns `attributes` (including `value`), `text` and `text_all` to the
caller on the success path by design — a caller asking for an element's state is
entitled to the field's value, and this changes the SHAPE of a failed call's
error text rather than making the tool more secretive. The exposure is the two
sinks nobody asked for: `click_element` wrote the same rendering to the backend
log at DEBUG (then clicked the element synthetically and succeeded), and the
`ToolError` reached **Sentry** — not dropped as a tool failure, because
`get_element_state` raises it from inside an `except` and `expected_events`'
error-convention rule tolerates only a timeout or a cancellation behind ours.

**It is reachable by three ordinary shapes**, which is what separates this from
F-907's insurance: measured on Chrome 153, `DOM.getContentQuads` answers an
EMPTY LIST — not an error — for a `display:none` element and for an `<option>`
inside a `<select>`, and by construction for a detached node, while
`visibility:hidden`, a zero-size box, an empty inline and
`content-visibility:hidden` all answer one quad and never reach it.

The fix is at the **raise**, which is the one moment the element is still an
OBJECT: `embedded/element_box.py` wraps `Element.get_position` and, for the one
exception type that line produces — the bare builtin `Exception`, keyed on the
TYPE and never on the message, because a library may reword its own sentences —
replaces the text with F-907's shape:

```
The element has no layout box, so its position cannot be read:
<input attrs=[type, value, data-session-token] children=1>.
display:none, an <option> and a detached node all render nothing.
```

Redacting is not silencing: which control had no box is the whole diagnostic
value of the line, and the new `ElementBoxError` names a condition a bare
`Exception` named not at all. Everything that is **not** that exact type passes
through untouched — a `ProtocolException` keeps Chrome's own words, an
`AttributeError` still reaches `mouse_click`'s handler, a cancellation still
cancels. The replacement is raised OUTSIDE the `except` block rather than with
`raise … from None`: both keep nodriver's message out of a traceback and out of
Sentry's chain (every reader measured honours `__suppress_context__`), but
leaving the handler first makes the `__context__` ABSENT rather than suppressed,
which nothing downstream can opt out of. One shaper — F-907's
`logging_setup._shape` — and no second one; no change to any of the three call
sites, because all three are correct once what they relay is shape-only.
Measured cost: +0.113 µs per `get_position`, against 430.9 µs median for the
same call over real CDP.

### Fixed — F-911: the MCP SDK logged a caller's arguments on the ROOT logger, where no floor could reach them

The two sites F-908 recorded and could not fix. `mcp/shared/session.py` logs
with the module-level `logging.warning` / `logging.debug` **functions**, so its
records are the **root logger's** — and both mechanisms this file owns miss
them structurally, not narrowly:

* a record on `root` has no family to name, so `PAYLOAD_LOG_FAMILIES` is inert
  however that tuple grows, and "cap root" is not the missing entry — root's
  level is the caller's, and lowering it silences the process;
* every one of the lines is an **f-string**, so the payload is already inside
  `record.msg` with `record.args` empty, which is what F-907's argument rule
  reads.

```
root WARNING session.py:383  Failed to validate request: 31 validation errors …
  input_value={'tok': '…'}, input_type=dict          ← the caller's own arguments
root WARNING session.py:430  Failed to validate notification: … Message was:
  method='notifications/…' params={'tok': '…'}       ← the whole message, untruncated
```

**Two of the three are at WARNING, so this needed no `basicConfig` from
anybody** — it was live in the plain shipped backend and the plain shipped
proxy. And the SDK arranges its own sink: `logging.warning` at module level
calls `logging.basicConfig()` when root has no handlers, so the first such line
installs a stderr `StreamHandler` on our root — and the backend's stderr is
`backend-boot.log`, a durable file. At WARNING they are Sentry breadcrumbs on
the next event too.

The fix is a **second rule in the one record factory** F-907 already installs,
with its table and matching in a new leaf, `embedded/payload_log_sites.py`
(extracted rather than inlined because the addition crossed `logging_setup`'s
1000-LOC budget, which ratchets down only): a record MADE BY a
payload-rendering third-party module has its rendered text withheld and keeps
everything else —
`<mcp/shared/session.py:430 message withheld: 1847 chars>`. The record still
arrives, on the same logger, at the same level, naming the line that fired,
which is the whole of what makes it actionable.

**The unit is the MODULE and the key is `record.pathname`.** Not the message
text, which the library may reword (F-906's rule). Not the line number, the
least stable thing in a dependency — a rule keyed on 383/384/430 goes silently
inert on the next release. A file path is what F-906's family root is, one
level finer, and when the module stops existing the entry is inert and
**visible**, because the pins read the installed SDK's source.

A root `logging.Filter` *would* have worked here — unlike F-907's case, the
call is on root, so `Logger.handle` consults root's own filters. It is declined
on convention 4: a second mechanism answering one question, one import from the
first, covering strictly less. No `before_breadcrumb` either, and that is
measured rather than argued — `makeRecord` runs upstream of
`Logger.callHandlers`, the one method `LoggingIntegration` patches, so the
breadcrumb is built from an already-withheld record. Both halves of that
premise are pinned.

Scope is `mcp/shared/session.py` alone, from an AST census of all 206 logging
calls in the package (11 on root): `client/session_group.py`'s three root
WARNINGs are real but this tree never constructs a `ClientSessionGroup`, and
`server/sse.py`:193 carries a session id.

One site is **recorded rather than fixed, and it is filed as F-913**, because it
is sharper than either of F-908's: `mcp/client/streamable_http.py`:240
`logger.exception("Error parsing SSE message")` has a static message and no
args, so neither rule sees anything — but its `exc_info` carries a pydantic
`ValidationError` whose `input_value=` echoes the **SSE data**, i.e. a tool
result on the proxy leg (`:394` and `:574` are the same shape on the other
legs). It is at ERROR, so F-908's floor does not reach it and Sentry ships it as
a full **event**. It needs a third mechanism — what a validation error may say
about its input — so it gets a number rather than a residual nobody picks up:
`audit/stage2/finding_F913_validation_error_echoes_tool_result.md`, cross-linked
from `audit/stage2/finding_F911_root_logger_tool_payload.md` §6.

### Fixed — F-910: closing a session could lose the login you just made

`close_instance` sent Chrome the graceful `Browser.close` and then terminated
it **immediately** — and `Browser.close` is the START of Chrome's shutdown, not
the end of it. The shutdown is what writes the cookie store, so the terminate
landed mid-flush and a `max-age` cookie set shortly before the close could be
gone when the session was re-opened. Measured: **Chrome's process was still
`running` at the kill site on 20 closes out of 20**, exiting on its own
0.129–0.165 s later; the cookie was lost about once in thirty closes on an idle
machine and on both Windows CI runs that saw it on 2026-09-21, across two
unrelated branches and on the same Chrome build as the green run before them.

Now the browser gets a bounded grace to leave on its own before anything kills
it — `embedded/process_exit.py`, the new one home for ending a browser's
process, which also took in the terminate → kill → SIGTERM ladder so the wait
and the kill it gates sit together. The grace is `EXIT_GRACE_SECONDS` = 5.0,
~30× the slowest exit measured, and it is a ceiling rather than a wait: a
browser that has already gone costs one process read. A **wedged** Chrome makes
one close up to 5 s slower and is then killed exactly as it was before. No
knob, no platform branch; `browser_manager.py`'s LOC cap ratchets 1485 → 1452.

`close_instance` also writes one close-diagnostics line saying whether Chrome
left unaided and how long it took, so a post-mortem can read it.

**The seed a new session is copied from is the same fix's blast radius.**
Closing the `default` session refreshes that seed from the profile the close
has just finished with, so a truncated shutdown is a truncated seed, inherited
by every session created afterwards. Both halves are pinned against a
redirected session root with a synthetic profile: the refreshed seed carries
the cookie set before the close, and the copy skips no locked file.

**And the refresh now REPORTS.** `close_instance` answers a record rather than
a bare boolean — `closed`, plus `seed_refreshed` (`True` refreshed, `False`
refused with `seed_error` in the words the refresh itself produces, `None` for
a close that owed no refresh). The refusal used to go into a dict the tool
discarded one line after building it, which is precisely why a seed that had
stopped moving would have been invisible from outside the process. Its schema
is declared in `tests/goldens/tool_surface.json`, so that SOFT golden moves
here deliberately; `stealthy close` prints the refusal, and a record being
always truthy is why that verb no longer says "closed" for a close that failed.

**The wait does not reap.** On POSIX a browser we launched is a child asyncio
is already waiting on, and a second `waitpid` would make asyncio report
`returncode 255` for a browser that exited cleanly — so the grace polls and
treats a zombie as exited, leaving asyncio its reap. Linux and macOS execute
this code for the first time on the gate; the finding says which claims they
are the first to check.

**Attribution, measured rather than assumed.** The CI node
(`test_storage_and_cookies_survive_one_profile_and_no_other`) spawns into the
suite's own fenced session root, whose profiles carry real cookie databases —
so the 18-byte placeholder stores found in the `tmp_session_root` fixture are
absent from its chain and cannot be the cause. The node passes 10× on this fix
and fails 5× out of 5 — with the fix in place — the moment the graceful close
is suppressed, in the CI failure's own words. That fixture is fixed anyway
(`tests/conftest.py` now writes real empty SQLite databases, pinned in
`tests/test_profile_seed_truth.py`): a real browser opened on an unreadable
store keeps its cookie jar in memory, which manufactures this finding's symptom
out of nothing.

Known gaps and the one defect these pins caught in the fix itself are in
`audit/stage2/finding_F910_cookie_lost_on_close.md` §5.1 and §7.

### Fixed — F-916 / F-917 / F-918: startup recovery no longer kills what it could not establish

Three defects on one path, and one sentence holds them: **an answer we could not
establish must resolve toward NOT killing.** Startup orphan recovery runs on
every backend cold start, and what it decides about may be a human's logged-in
Chrome — the one piece of state reconnecting cannot rebuild, because a killed
browser does not flush its session.

**F-916 — an entry we could not CLASSIFY was reaped.**
`browser_reattach._adoptable_entry` answered a bare `None` both for "this is not
one to adopt" and for "we could not establish whether it is", so an entry that
is alive, ours and on a PERSISTENT profile fell outside `Classified.spare` and
was killed. Measured on the pre-fix source, five different entries produced one
verdict: a 2.1.8/2.1.9 record carrying no `cdp_port`, one with no `create_time`
and one with an unreadable `pid` were all `REAPED`, indistinguishable from a
disposable auto-clone and from a Chrome that is provably gone. The first of
those is not hypothetical — 2.1.8/2.1.9 recorded no port at all, and those are
the records holding today's stranded logins. Those three now answer
`reap_guard.UNDECIDED` and land in `.spare`: not adopted, and not ended.
"The Chrome this entry names is gone" stays a DECISION, so a dead entry can
still leave the record and `browser_pids.json` cannot grow without bound.

**F-917 — the reap was DIRECTORY-matched while the spare was INSTANCE-ID-matched.**
Recovery skips by `instance_id` and the reap it then performs kills every
browser on the entry's `user_data_dir`, so on a SHARED profile — which is what
the master is — one stale entry's reap reached a browser another entry had just
been spared for. Measured: with `i-live` (pid 7777, adoptable, spared) and
`i-stale` (pid 7778, Chrome gone) on one directory, the reap of `i-stale` called
the kill path **with pid 7777**, and the record still listed `i-live`
afterwards — so the record claimed a live adoptable browser whose process had
just been ended. The spared entries' pids now travel with the spare and are
subtracted at the one place the kill set is spent. The same filter is applied to
`browser_reattach.run`'s failed-adoption reap, which had the shape from the
other door.

**F-918 — an UNVERIFIABLE process was terminated.** `_kill_process_by_pid`
logged "Could not verify process {pid}" from a blanket `except` and then fell
through to `terminate()`. Measured: a `psutil.AccessDenied` on `.name()` — what
Windows answers for a process this account may not open — and a plain `OSError`
both terminated the pid and returned `True`, so the caller counted an
unidentified process as successfully reaped and dropped its record entry. Both
now refuse and answer `False`, with a line that states the decision. A zombie
was already correct (`ZombieProcess` subclasses `NoSuchProcess`) and is
unchanged; it is pinned because it looks like it should have changed.

This makes orphan recovery uniform with the places in the tree that already
resolved an unreadable witness toward safety: `profile_lock._browser_pids`
(`None` for "could not be asked", distinct from `()` for "asked, nothing
running"), `backend_eviction`'s refusal to evict what it cannot prove is idle,
and F-919's `spawn_leak.launched_pid`, which leaves a failed spawn's leftovers
running rather than guess at them. It also interlocks with F-919 rather than
overlapping it: that fence decides WHICH process a failed spawn may end, this
guard decides whether a pid may be ended at all, and F-919's reap reaches its
kill through this very function.

**What it costs, stated rather than hidden:** a browser we can neither adopt nor
reap is left running and left recorded, and an orphan whose pid we cannot
identify is left alone. Both are bounded — the entry is re-classified on every
later cold start and is reaped the moment its Chrome actually exits — and
`kill-orphans --force` still skips the whole classification by design. A leaked
Chrome is recoverable; a login is not.

New leaf `embedded/reap_guard.py` carries the rule and its three pieces
(`UNDECIDED`, `spared_pids`, `killable`). Two files were at their LOC caps and
caps ratchet down only, so each fix paid for itself: the CDP **endpoint ladder**
moved out of `browser_reattach` into a new leaf named for the question it
answers, `embedded/cdp_endpoint.py` — "where is the CDP endpoint of the browser
this RECORD ENTRY describes" — taking that file 999 → 991, and
`_kill_process_by_pid`'s two near-identical escalation rungs became one table,
taking `process_cleanup` 1009 → 1007 with its grandfather row ratcheted to
match. No behaviour changed in either move. `cdp_attach` is untouched and
remains a leaf: it owns the DOOR, and it never calls the ladder — both callers
are `browser_reattach`'s.

Full measurements, the before/after tables and the residuals are in
`audit/stage2/finding_F916_unclassifiable_entry_is_reaped.md`,
`…finding_F917_reap_matches_directory_while_spare_matches_instance.md` and
`…finding_F918_unverifiable_process_is_terminated.md`.

### Fixed — F-922: a named profile is reaped by the RECORD, never by its directory

The fourth reading of the same sentence, and an owner ruling rather than a
judgement call. Orphan recovery built its kill set by scanning the entry's
`user_data_dir` whatever KIND of profile it was — so a Chrome the owner had
started **by hand** on one of their own named sessions was killed by a stale
entry's reap. No record entry names such a browser, so no spare can reach it:
F-916's classification and F-917's `protected_pids` both protect RECORDED pids,
and this one is not recorded at all.

Measured before the fix: a stale entry on a named profile killed the owner's
unrecorded Chrome, through recovery **and** through the close path; and even a
fully justified reap — the entry's own browser, correctly identified — took the
bystander with it, because both were on the directory.

**The ruling:** directory-wide reaping stays only for DISPOSABLE auto-clone
directories; on a named or shared profile a reap may end only pids the record
actually names. The asymmetry is the point. An auto-clone directory is ours by
construction — a human never opens one by hand — while a named profile is
exactly what a human does open, and since F-888 a browser on one is meant to
outlive its backend. Explicitly not chosen: "record-only everywhere", which
would let orphaned clone browsers accumulate forever.

It is one conditional on the one line that BUILDS the kill set, reading
`browser_pid_registry.on_persistent_profile` the other way round — the same
predicate F-888's adoption rule and the profile-deletion guard already ask, with
no second notion of "ours". The recorded pid then supplies the kill, identity-
checked and start-time fenced exactly as it always was. It applies to the close
path as well as to recovery, because a named profile is a named profile
whichever caller arrived.

**What it costs, and the owner chose it:** a leaked Chrome on a named profile
whose record entry was lost is never reaped automatically. It stays visible in
`stealthy profiles` and is cleared deliberately. The alternative is a scan that
cannot tell a leaked browser of ours from the one the operator is logged into,
ending both.

It also closes F-917's defect through a **second door**, which F-917's own
filter could not reach. When an adoption fails, `browser_reattach.run` falls
back to a reap that protects its ADOPTABLE candidates' pids — and an entry
F-916 SPARED is by construction not one of them, so a spared browser sharing
that profile was ended. Measured: `[6666, 7777]` before, `[7777]` after. What
closes it is the scope rule rather than a second subtraction, because `run`
hands that reap a metadata dict that must declare the profile persistent — the
profile-delete guard reads those same two keys.

`process_cleanup.py` was at 1007/1007 — this lane's own ratchet — so the change
paid for itself: four sites that logged a declined pid in four spellings became
one `_skip_note`. 1007 → 1006.

Details, the before/after table and the defect this fix created in F-917's own
pins are in `audit/stage2/finding_F922_named_profile_reaped_by_directory.md`.

## 2.1.12

### Fixed — F-901: a profile request can no longer name the directory profiles live in

`user_data_dir="."` opened the **clone root** — the directory that holds every
session — and `user_data_dir=".."` opened the **browser-session root**, which
holds that plus the shared profile and its seed. `"./"`, `".\"` and `"..."`
reached the clone root too (Windows folds those components away), and an
absolute path to either root was honoured as though it were a profile. Chrome
was then handed the storage as its own user-data-dir and wrote its profile
files in among every session's.

`anchor` had an `inside(...)` check, but it decides WHICH root to anchor under
— it was never a guard on the answer, and `Path("..").name` is `""`, so no
refusal could see one of these either. A relative request now lands on the
normalised path and must be strictly INSIDE the clone root; the clone root and
the browser-session root are refused through either spelling; and both refusals
name what to pass instead.

Unchanged: every ordinary request (`acme`, `sessions/acme`, `default`, any
other absolute path) lands exactly where it did, and an absolute path is still
returned byte-for-byte. `sub/../acme` is accepted and canonicalised to
`sessions/acme` — the directory it already meant — rather than refused.
`session=` refused all of these before and still does: a session is a name.

**A session directory that is a symlink or a junction to storage elsewhere
still opens**, through either spelling. The walk test reads the path the caller
composed and never where it resolves to, precisely so that configuration keeps
working; the two roots are still compared by resolving, which is what catches a
link pointing AT one. And a refusal that reached a directory through a link now
names the directory it really opens, instead of naming a path inside the clone
root while explaining that it is the clone root.

### Changed — F-896: sessions have a name, and it is never "master"

`spawn_browser` gains **`session`**, the one documented way to ask for a
profile: `spawn_browser(session="acme")`. `user_data_dir` stays as a deprecated
alias and **resolves to the same request** rather than running beside it — one
normalizer (`profile_seed.profile_request`) reads both and answers a single
value, so nothing downstream can develop two opinions about which spelling
wins. Passing both with **different** values raises instead of applying a
precedence you cannot see; passing both with the same value is fine.

`session` takes a NAME and refuses a path — "a session named
`C:\Users\me\profile`" is not a sentence — and says where the path door is:
`user_data_dir`, or `stealthy call spawn_browser --arg user_data_dir=<path>`.

Whitespace around a NAME is not part of it through either spelling, while a
PATH keeps its own characters. A non-empty value that is empty once stripped
(`"   "`, `"\t"`) now **raises** through either spelling instead of being
honoured as a profile request: on Windows `user_data_dir="   "` resolved to the
clone root itself — the directory that holds every session — so it was never
a profile anyone meant. An **empty string is unchanged and still means "not
given"** through either spelling, so a client that sends `""` for an optional
argument gets the ordinary unnamed spawn exactly as it does today.

**`default` is now a session you can open.** F-894 reserved the word as a
refusal, explicitly as a placeholder for this release; it now MEANS the shared
profile every session is seeded from and the one a human logs in to.
`session="default"` and `user_data_dir="default"` both select it, by the same
door an absolute path to it already used, so nothing about which directory an
unnamed spawn picks has changed. It stays reserved in the sense that matters:
it names exactly one directory, so `sessions/default` and any other RELATIVE
spelling that would create a second one under the same word is refused —
including one the filesystem would fold onto it, since Windows strips a
trailing dot or space from a path component and `session="default."` otherwise
lands in `sessions/default`. F-894's trap does not get to come back one
separator away from the word we now teach. `master` and `master-snapshot` stay
refused outright.

A directory of your own whose name simply ends in `default` is **not** affected:
an absolute path is opened as it always was — Chrome's own per-profile folder is
called `Default` — and an existing `sessions/default` from before this release
keeps its contents and stays openable by its absolute path, exactly as RUNBOOK's
recovery paragraph says.

**The words "master" and "snapshot" are retired from every user-facing string**
— tool and parameter descriptions, CLI help and output, error messages, and the
`spawn_diagnostics.profile_selection` payload. What moved, and what it is now:

| was | is |
|---|---|
| `profile_role: "master"` | `profile_role: "default"` |
| `snapshot_dir` / `snapshot_refreshed` / `snapshot_reason` / `snapshot_error` | `seed_dir` / `seed_refreshed` / `seed_reason` / `seed_error` |
| `master_snapshot_path` | `seed_path` |
| `snapshot_error: "master-in-use"` / `"snapshot-in-use"` | `"default-in-use"` / `"seed-in-use"` |
| `clone_source: "master-snapshot"` / `"live-master-fallback"` | `"default-seed"` / `"live-default-fallback"` |
| `source_kind: "explicit-master-snapshot"` / `"explicit-master"` | `"explicit-default-seed"` / `"explicit-default"` |
| `seeded_from: "master-snapshot"` / `"master"` | `seeded_from: "default"` — one name, and one you can pass to `session=` |
| `stealthy profiles` roles `master` / `snapshot` | `default` / `default-seed` |

The old keys are **not** kept readable beside the new ones: the payload is
rebuilt on every spawn and has no persisted or cross-version consumer, so a
duplicate key would be two spellings of one fact bought for nothing.

**What did not move:** the directories are still `master` and `master-snapshot`
on disk, and `BROWSER_MASTER_USER_DATA_DIR` / `BROWSER_MASTER_SNAPSHOT_DIR` are
still their env vars. Renaming either would migrate every existing
installation's profiles or break every existing `.env`, for a word that appears
in a path an operator reads and never types. Clone markers already on disk keep
their old `source_kind`, and are still read correctly.

**CLI.** `stealthy spawn --session NAME`. `--profile` still works for one
release but is undocumented (`--help` does not list it) and prints a line on
stderr naming its replacement; it will be removed. A path stays reachable
through `stealthy call`.

### Fixed — F-899: adopted instances leaked between test files through a process-global store

`in_memory_storage` is a module-level singleton: one per backend process in
production, one per pytest SESSION in a test run. `browser_reattach`'s adoption
pass and `BrowserManager`'s spawn both write it, and both bind it by value at
import time, so `patched_server(in_memory_storage=FakeStorage())` never reached
them. `tests/test_browser_reattach.py` therefore left `i-kept` and `i-held`
behind, and `list_instances` — which merges the manager's instances with this
store — reported them to a later file as `source: "stored"` rows. Running
`test_browser_reattach.py` and `test_tool_failure_visibility.py` together failed 2
of 104; each alone was green. The full lane was green only because
`test_mcp_protocol_surface.py` sorts between them and boots the real transport
unpatched, so `app_lifespan`'s shutdown ran `clear_all()` on the real singleton in
passing.

`tests/conftest.py` grows an autouse `_in_memory_storage_hygiene` that restores
the store after every test, a sibling of the `_stealth_logger_hygiene` fixture
above it, and `tests/test_in_memory_storage_isolation.py` pins it with a two-node
pair that no collection order can mask.

### Fixed — F-899: a cancelled `close_instance` left a permanent ghost row in `list_instances`

Found while reviewing the above, in the same subject. `BrowserManager.close_instance`
popped `_instances` in Phase 1 but removed the in-memory-storage entry in Phase 4,
inside a `try` whose handler is `except Exception` — which an
`asyncio.CancelledError` walks straight past, because it is a `BaseException`. Six
awaits separate the two, and the `close_instance` tool body carries no CDP timeout,
so a client disconnecting mid-close cancelled the request task in that window and
left the manager without the instance and the store with its entry. `list_instances`
then reported it as a `source: "stored"` record — about a browser already being torn
down — for the life of the backend, since only lifespan shutdown clears the store.

The removal now happens in Phase 1, under the same lock and with no `await` between
it and the pop, so the window is closed rather than narrowed. The cancellation still
propagates. `close_instance` keeps exactly one removal site and
`browser_manager.py` stays at its 1485-LOC cap.

### Fixed — F-900: the proxy bridge's inherited read timeout silently dropped its event stream

The stdio proxy's bridge opened `streamablehttp_client(url)` — the mcp SDK's
deprecated client, with no arguments — so it inherited
`Timeout(connect=30, read=300, ...)`. A `read` deadline is a deadline on being
IDLE: the standing GET event stream carries nothing while a session is quiet and
the backend sends no SSE keepalive, so the stream timed out, was retried the
SDK's two times, and was then abandoned for good at DEBUG after ~601 s of quiet.
That is exactly the discriminator F-862's session sweep uses to decide a client
has gone, whose docstring promises a live proxy idle for hours is never touched —
so after ~15 min of continuous idleness a healthy session was reaped. The next
tool call is answered `Session terminated`, and it does not recover: the SDK
answers a 404 by pushing that JSON-RPC error into the read stream and returning
without raising, without closing the stream and without clearing the dead
session id, so the bridge never ends, nothing heals or re-bridges, and every
later call in that Claude Code session answers the same error until the client
is restarted.

The bridge now uses `streamable_http_client` (no more `DeprecationWarning` from
our own call sites, pinned by AST) through `backend_client.http_client`, the one
transport seam, with `BRIDGE_READ_TIMEOUT = None`. What bounds a bridge is left
where it already lives: the F-820 watchdog, `proxy_selfheal`, and each tool
call's own CDP budget. Measured against a real loopback socket: a bounded read
opens the stream twice and then loses it; the new policy holds one.

### Fixed — F-902: reading a cookie no longer kills the tab (CRITICAL)

On **Chrome 153**, `get_cookies` on a page holding a single cookie never
returned AND left that tab's CDP connection dead: every later call hung to its
own deadline and reported *"the browser may have crashed or the connection
dropped"* about a browser that was fine. `get_instance_state` and
`clear_cookies(url=…)` did the same, because both read the cookie jar — the
first is the widest, since it is the tool you call to find out whether anything
is wrong, and on any logged-in page it degraded and then killed the tab it was
asked about.

Chrome 153 stopped sending `Network.Cookie.sameParty` (the removed First-Party
Sets field), and it is the ONLY field it stopped sending — measured off a raw
websocket. nodriver 0.47's generated `Cookie.from_json` reads it
unconditionally, and `Connection._listener` guards its EVENT path but not its
RESULT path, so the `KeyError` ended the listener: the one task that resolves
every future on that connection. This is F-883's failure shape reached by a
parse error rather than a cancellation, so `cdp_transport`'s shield did nothing
for it.

`cdp_transport`'s sentence widens from "awaiting a CDP reply must never be able
to cancel it" to **"DELIVERING a CDP reply must not be able to kill the
listener"**, and gains two halves beside the existing shield:
`Transaction.__call__` now completes the
one unreadable transaction with an error instead of propagating into the
listener — so that command fails, every other pending call still resolves, and
the connection lives — and `Cookie.from_json` supplies retired fields from a
NAMED table carrying its measurement. The first is the general rule (nodriver
0.47 has **1199** unconditional required field reads across its generated
classes; cookies are simply the one Chrome retired first); the second is what
makes `get_cookies` actually work rather than merely fail honestly. Both are
deleted by the nodriver bump that fixes either, and the docstring says which.

**Cookie names and values no longer reach the error path.** nodriver's own
re-raise interpolates the whole reply into its message, so the exception that
killed the listener carried every cookie name and value on the page, and it
escaped as an unretrieved task exception — the asyncio handler, the durable log
and Sentry at once. The replacement reports shape only: the CDP method, the
exception type, the reply's field count, and the missing protocol field when
that is provably all the failure named. Pinned, hermetically and against a real
browser.

Two named limits. `Cookie.to_json` still writes the field, so a cookie read on
Chrome 153 reports `sameParty: false` — a value Chrome never sent. It is
synthesised, `False` is what it meant for every cookie outside a First-Party
Set, and the feature no longer exists. And "delivering" is the exact scope: two
raises upstream of any `Transaction` — `json.loads` and the `mapper.pop` for an
unknown id — still end the listener, are unreachable from this seam (nodriver's
`Connection` metaclass refuses every class-level assignment) and are unreachable
from a real Chrome. Both are named in the module docstring and the finding
rather than covered by a wider claim.

## 2.1.11

### Fixed — F-892: the snapshot staleness witness stated a file Chrome stopped writing in v96

`_snapshot_needs_refresh` decided whether the master snapshot was behind the
master by stating `Default/Cookies`, `Default/Login Data` and `Default/Web
Data`. Chrome moved the cookie jar to `Default/Network/` in version 96.
Measured 2026-09-20 on this machine's live master profile and its snapshot:
`Default/Cookies` is **absent from both** while `Default/Network/Cookies` is
524,288 B. So a login that writes only cookies — Google SSO, Amazon Seller
Central, any SPA that never offers to save a password — never made the snapshot
look stale, and the `pre-clone-stale` refresh depended entirely on `Login Data`
(only when Chrome saves a password) or `Web Data` (autofill). For the most
common login shape the trigger was dead.

The list is now `profile_seed.LOGIN_WITNESSES`, led by the current cookie jar
with the pre-96 path kept beside it, and it is spelled in exactly one place —
not even a docstring may repeat it, which a pin enforces, because a stale prose
copy is how the original claim survived. The pin's fixture is built from the
measured layout of a real Chrome profile, never from our own copier's output.

### Fixed — F-893: a snapshot refresh that copied nothing reported success

`_copy_profile_tree` returned early when the TARGET directory was held by a live
browser, and `_refresh_master_snapshot_if_safe` then set
`snapshot_refreshed: True` regardless — so callers were told the seed carried
the master's logins when not one byte had moved. Refusing is right; reporting
it as a refresh is not.

The precondition was live: measured 2026-09-20, a Chrome had been running on
`master-snapshot` itself, and the residue it left is unambiguous — the
snapshot's `Default/Network/Cookies` is 7 s **newer** than the master's and its
`Local State` is 133,607 B against the master's 90,406, which `shutil.copy2`
from that master cannot produce. `_copy_profile_tree` now reports the refusal
and the refresh answers `snapshot_refreshed: False` with
`snapshot_error: "snapshot-in-use"`. The existing `"master-in-use"` arm is
unchanged.

### Fixed — F-894: `user_data_dir="master"` silently opened a different profile

A bare relative name is anchored under the clone root, so `"master"` resolved to
`<root>/sessions/master`, not `<root>/master`, and was created as a fresh clone.
Measured: that directory exists, 0.46 GB, marker `explicit-master-snapshot`,
created 2026-09-11 — someone asked for the master profile by its documented name
and got a nine-day-old copy, with no warning.

`master`, `master-snapshot` and `default` are now reserved names and raise
`ToolError` naming the word, before anything is created and in front of F-871's
`<name>-2` walk so a reserved name can never come back as `master-2`. The
snapshot PATH is refused too — a browser driven there writes into the seed every
later session copies from, which is how F-893's precondition arose. The master
by absolute path is deliberately still allowed and now selects the master ROLE,
which is what makes `close_instance` refresh the snapshot afterwards.

The refusal is asked at the SPAWN, before F-888's re-attach, not only inside the
resolver. `browser_reattach.adopt_held_profile` runs in front of profile
selection and matches the requested directory against live browsers, so an
absolute snapshot path **with a browser on it** — the exact state F-893 is about
— was re-attached to and the resolver never saw the request. One rule, two sites
that ask it; it is a path decision plus one `Path.resolve` and one `exists()`, so
asking twice costs two stats.

That refusal also runs AHEAD of the headed-visibility guard (F-808). Both are
pre-flight and neither has a side effect, so the order decides only which message
a caller gets when both apply — and on a host with no desktop (every Linux CI
cell, and any backend started outside a desktop session) the headed guard used to
answer first, so a reserved `user_data_dir` came back as "this context cannot
display a window" and the reservation was unreachable through the tool. A
reserved path is refused on every machine there is; "no desktop here" is a fact
about one backend, and answering with it sends a caller after a display they do
not need.

**If you already have a session directory named `master`, `master-snapshot` or
`default`** (one exists on the machine this was measured on, 0.46 GB), nothing is
deleted or hidden: it is still listed by `profiles` and still openable by its
**absolute path**, or rename it to a name that is not reserved. The refusal
message says so when such a directory exists. RUNBOOK's "Disk filling up" carries
the one-time job.

A drive-qualified path that is not absolute (`C:foo`, what a Windows absolute
path becomes once a lenient string layer has eaten its backslashes) is refused
as well. That is not a hypothetical: it reproduces, exactly,
`sessions/stealth-mcp-browser-sessionssessionsstealth-chrome-devtools-mcp-f876e3d7f2ec`
(0.35 GB) — `Path.is_absolute()` is False for a drive-relative path, so the
resolver anchored a fully qualified path as a bare session name. **On every
platform**: a drive is a Windows concept (`PurePosixPath("C:foo").drive` is
`""`, measured), so the first cut of this rule read the host's flavour and let
the identical string through on Linux and macOS. The drive is now read through
`PureWindowsPath` while "would this be anchored rather than opened" stays on the
native flavour the resolver anchors with — one rule, same answer everywhere, and
`anchor` keeps its one home.

### Added — F-895: sessions now say which seed they came from, and whether it has moved on

The clone marker gains `seeded_from` (the seed by name) and `seeded_at`, written
beside the three legacy keys so a 2.1.10 reader still finds what it looks for.
`spawn_diagnostics.profile_selection` and `stealth-chrome-devtools profiles`
both report those plus `seed_changed_since`, computed from F-892's witness list
— the same one, never a second. A marker carrying neither key reads
`seeded_from: "unknown"` with `seed_changed_since: None`, and `created_at` is
deliberately not substituted for `seeded_at`: when a directory was made is not a
claim about which seed it was made from, and the whole point is that a profile
frozen since August must not read as up to date. Sizes, mtimes, roles and a seed
name only — no profile content is read, printed or logged.

### Removed — a documented refresh window that never ran

`clone_storage._clone_needs_refresh` and `_profile_refresh_days` had no callers
anywhere in the tree, and the first carried a second spelling of the clone
marker's filename — breaking the one-home claim inside the commit that made it.
Both are deleted and the marker name joins the grep pin. The
`BROWSER_PROFILE_REFRESH_DAYS` **setting keeps its field**, because `Settings` is
`extra="forbid"` and a `.env` naming a field the model no longer has crashes at
startup — but nothing presents it as live any more: README calls it inert instead
of claiming it refreshes copies after N days, `.env.example` comments it out with
the reason, the two shipped example configs no longer set it, and the field
itself carries the reason. If you have `BROWSER_PROFILE_REFRESH_DAYS` in your own
`.env` or client config it still loads and still does nothing; you can drop it.

### Changed — the seed's own subject has one home

New leaf `embedded/profile_seed.py`: the clone marker (its name, schema, read,
write and the auto/named verdicts), the login witnesses, and where a
`user_data_dir` request lands plus which directories a caller may not name.
`clone_storage.py` keeps a wrapper per name the suite and the CLI call, and its
LOC budget **ratchets down** 1055 → 1054 — the extraction is what paid for these
four fixes rather than a raised cap.

### Fixed — CLAUDE.md's glossary taught a parameter that raises

The "browser session / named session" row said `spawn_browser(session_name=…)`.
There is no such parameter and never has been; it is `user_data_dir`.

### Added — F-891: `stealthy`, one CLI that can also drive the backend's tools

Nothing in a shell could call a tool on the running backend. On 2026-09-19,
recovering a stranded Seller Central login therefore cost a hand-written 40-line
MCP stdio client (`reattach_seller_central.py`) whose only job was to invoke
`spawn_browser(user_data_dir=…)` — a script that had to know the protocol, the
result shape and how to start a proxy, to make one call the product already
supports.

The ops CLI gains six verbs and a new name. `stealthy` and
`stealth-chrome-devtools` are **one CLI under two names** — the same `cli:main`,
one parser, one `_DISPATCH` — and help text names whichever you typed. The
existing verbs are unchanged.

- `stealthy call <tool> [--arg k=v ...] [--json '<object>']` reaches **any** tool.
  There is no per-tool argparse mirror: the tool's own schema on the backend is
  the validation, so a 95th tool is callable the day it is registered and the
  tool count stays derived. `--arg` values are JSON when they parse
  (`headless=false`, `browser_args=["--x"]`) and strings when they do not
  (`C:\Users\me\profile`), split on the first `=` so a query string survives.
- `stealthy ls` / `nav` / `close` / `spawn` are sugar over `list_instances`,
  `navigate`, `close_instance` and `spawn_browser`; instance ids resolve by
  unique prefix, and an ambiguous one names every match instead of picking.
- `stealthy spawn --profile <name-or-path>` is the stranded-login recipe as one
  command, and prints `REATTACHED : yes` with the holder's pid when F-888 gave it
  the browser that was already running. The value is passed through as
  `user_data_dir` untouched and a `spawn` with no `--profile` is whatever
  `spawn_browser()` itself selects; either way the `profile_selection` the
  backend made — role and directory — is printed, so nothing has to be inferred.
  There is deliberately **no** `--master`: `master` as a bare name resolves to
  `sessions/master`, a different profile (F-894), and the vocabulary that will
  name the master profile is `--session`/`--from` with `default` reserved.
- `stealthy tools [--section X]` lists the LIVE backend's surface and states the
  installed build's registry count beside it, because when those two disagree the
  shell and the backend are different builds — which is the answer.

They talk MCP streamable-HTTP straight to the backend, selected **exactly** the
way `status` selects it (`singleton._probe_backend_status`, F-868) and started
through the existing `ensure_server_running` path when none is running, so the
cold-start lock, F-886's step-aside and F-889's adopt-forward apply unchanged;
`--no-start` makes the absence an error. No stdio proxy is spawned per command.
Output is a table on a terminal and JSON in a pipe or under `--json`.

**Exit codes are a closed set**: 0 ok / 1 the tool answered and said no / 2 usage
— including `stealthy` with no subcommand, which prints help and is the same kind
of mistake argparse answers with 2 / 3 no backend, which is also where a transport
failure lands (nothing on the backend saw the request, so there is no answer to
report) / 70 a bug in the CLI itself / 130 `Ctrl-C` / 141 the READER went away.
Every exception out of a tool verb is mapped, so no tool-verb invocation ever pairs
a raw traceback with Python's default exit 1 — the code that means the tool
refused. An ops verb's OWN failure (an `OSError` that is not the reader leaving —
a directory `cleanup --apply` cannot delete) still propagates as it always did;
that is deliberate, and stated in the finding's §6.17.

141 is `128 + SIGPIPE` and it needs its own judgement, asked before the transport
one: a `BrokenPipeError` is an `OSError`, so `stealthy ls | head -1` and `stealthy
tools | less` with `q` pressed early reported *"could not reach the backend"* about
a round trip that had already succeeded — a false statement about the backend,
made by the one function whose job is to keep transport and tool apart, over the
commonest idiom in the shell. The judgement is keyed on the errno and not only the
type, because the same closed pipe is `BrokenPipeError` (EPIPE) on every POSIX and
a bare `OSError(EINVAL)` on Windows — measured through a REAL pipe, where a row
keyed on the type alone still answered 3 on Windows while every hermetic double
passed. It prints nothing (the operator's `head` did what they asked), and stdout
is re-pointed at the null device before returning, because otherwise the
interpreter's own exit flush fails again outside every handler and CPython answers
with exit 120 — a code outside the advertised set.

**Both ways out reach that, and the SUCCESS path is the one you meet first.** A
verb that fails mid-write is the easy half; a verb that succeeds leaves the tail
of any output over the 8 KB buffer unwritten, and it lands at interpreter
finalisation with the reader long gone — so `stealthy call get_page_content |
head -1` exited **120** with `Exception ignored on flushing sys.stdout` while the
docs said 141. Every run now ends with ONE explicit flush in `cli.main`, after
whichever dispatch table answered, and `main` guards the DISPATCH itself with the
same reader-gone judgement — both halves are needed for the eight ops verbs, which
have no handler of their own: the flush catches an answer that fits the buffer,
the guard catches one that crosses it mid-`print` (`stealthy profiles | head -1`
with many sessions exited 120 before the flush and 1-with-a-traceback with only
the flush). It is a second guarded site rather than a row in the exit-code table: it
catches `OSError`, not `BrokenPipeError`, because the measured Windows
finalisation error is `EINVAL` (errno 22), not a pipe error at all, and the table
would have routed that shape to the transport row and answered 3 about a round
trip that succeeded. The redirect-to-null that follows cannot raise either, even
out of descriptors — it runs inside handlers, where an escape would be the
traceback this whole set exists to prevent. The one-line report is
likewise emitted under `contextlib.suppress(OSError)`, because `2>&1 | head -1`
closes stderr too and that print lives inside the handler; and
`asyncio.CancelledError` joined the caught set, being the last `BaseException`
shape that could leave a set advertised as closed.

`--traceback` prints the stack **as well as** the one-line message, and
deliberately does not re-raise: an exception leaving `main` goes past
`sentry_init()`, and `sys.excepthook` would ship a `BackendCallError` carrying the
tool's own payload — exactly what this CLI promises it never sends anywhere.

**The CLI never evicts a live backend.** The one selection is identity-blind, so
a backend built from a different source tree answers and is adopted rather than
replaced — a fingerprint mismatch is the proxy reuse gate's business, not a
one-shot command's. What a cold start can still evict, when nothing answers at
all, is a *wedged* backend of another build owning no live browser: that is the
proxy's own startup path, unchanged and not forked, and `--no-start` is the
opt-out. Both facts are in `--no-start`'s help.

The MCP session each call opens is **terminated on the way out** — pinned on the
happy path, on a tool error, on a mid-call transport failure and on cancellation,
against the real `mcp` SDK driven over a fake in-process HTTP server, rather than
by asserting that a keyword argument was passed. `spawn --url` prints the
instance id **before** navigating, so a failed navigation still leaves the
operator a browser they can name.

Two new leaves: `embedded/backend_client.py` (the session, the call, and the one
reading of an answer — `structuredContent` with EMPTY `content` is the common
shape, measured) and `cli_call.py` (the six verbs' parsers, bodies and dispatch
table). `cli.py` keeps the one parser TREE, the one `main` and both script
names, and calls `cli_call.add_parsers(sub)` once — so there is still exactly
one `--help` and one set of verbs, and a flag now sits beside the code that
explains it. It is **890 LOC against the 1000 budget**, which ratchets down
only; no cap was raised.

## 2.1.10

### Fixed — F-882d: the meta-refresh node named two of that shape's three truthful states

The third sibling of F-882b and F-882c, in the same node, and again not a
product defect. A `meta refresh` is scheduled at the first document's `load`,
which is the milestone `navigate` returns on, so the replacement can commit in
the gap between the wait ending and the post-navigation read: `location.href`
has moved to the landing while the landing has not parsed its `<title>` yet.
That pair is one document at one instant — the read has been a single round trip
since F-882 — and the node rejected it. Measured: CI run 35175574635 job
105056426121 (release-gate integration, Linux/X64) failed with
`assert (False, '') in ((True, ''), (False, 'Nav Landing'))`.

The accepted pairs come from `_meta_refresh_states` now, which names all three
and still excludes every mix of two documents. Naming the third state does not
weaken the node: it proves the refresh was fetched, and that the page ENDS on
the landing carrying its title, read through `execute_script` in one round trip,
so a mid-transition answer cannot be confused with a broken title read. A
hermetic pin in `tests/test_navigate_milestone.py` drives the product into that
state deterministically with a held supersession and asserts its answer is in
the E2E node's own set — so an unnamed state fails on every lane instead of once
in a while on one cell. No `src/` change.

### Fixed — F-887: Sentry drowned by expected events (client disconnects, CDP/navigation budgets, caller input, proactor and nodriver teardown noise)

Triaged live on 2026-09-18 against release 2.1.8, the project's Sentry for the
previous seven days was almost entirely this product working as designed. Six
shapes, ~13 700 events:

| events | shape |
|---|---|
| 6 500 | the bare message `Received exception from stream: ` on `mcp.server.lowlevel.server`, with no exception values at all |
| 6 200 | `starlette.requests.ClientDisconnect`, raised inside `request.body()` under `streamable_http._handle_post_request` |
| 466 | pydantic `ValidationError` for `call[spawn_browser]` — callers sending `window_width=` at a tool whose parameters are `viewport_width`/`viewport_height` |
| 231 | `ConnectionResetError` `[WinError 10054]` from CPython's own `_ProactorBasePipeTransport._call_connection_lost` |
| 151 | `ConnectionRefusedError` out of nodriver's unawaited `Browser.update_targets()`, after its Chrome had already gone |
| 140+ | `ToolError: CDP operation timed out` and `ToolError: Navigation to … timed out`, spread over ~20 separate issues because the instance uuid is in the message |

Every one of them had already been ANSWERED before it was logged: the
disconnected client is gone, the bounded operation's `ToolError` reached the
caller, FastMCP replied to the caller with the validation message, and the two
teardown races belong to CPython and to nodriver. What they cost is the only
thing Sentry is for — an issue list a maintainer can read. Step 0 of the one
`before_send` was written for exactly this and could not see them: its rule was
"drop only when EVERY exception in the chain is our `ToolError`", and a chain
the product produces is `ToolError` <- `TimeoutError` <- `CancelledError`
(`asyncio.wait_for` cancels the coroutine it gave up on and raises from that
cancellation), which is three links and only one of them ours.

The taxonomy is now a module of its own, `expected_events.py`, with five named
classes and one consumer (`observability._expected_event_class`, step 0). Each
class is ONE rule, and every rule is read through a `Link` — a type NAME, a
MODULE and the FRAMES, each spelled the way the SDK spells them — so the live
path (`hint`) and the serialized-payload path cannot drift apart. Three of those
spellings are read out of `sentry_sdk/utils.py` rather than guessed, because all
three differ from the obvious Python answer: `type` is `__qualname__` (so a
class declared inside a function serializes as `outer.<locals>.Name`), `module`
omits `builtins` but **not** `__main__` (and `embedded/server.py` runs as
`__main__` under runpy), and the serialized chain lists the ROOT cause first
where a live `__cause__` walk starts from the reported exception. `Link` is
normalized to outermost-first so a positional rule means one thing on both paths.

Two rules are narrower than the shape they describe, and both narrowings are the
whole point. `error-convention` requires the OUTERMOST link to be ours: the
conversion is always the last raise in a real budget chain, so a `TimeoutError`
that is itself the reported exception is a cleanup path that timed out
unconverted — the missing-convention case — even when a `ToolError` sits behind
it. And `caller-input` requires the outermost link's frames to carry
`fastmcp.tools.tool` `run` directly above `pydantic.type_adapter`
`validate_python`. The logger alone cannot serve there:
`fastmcp/tools/tool_manager.py` wraps `await tool.run(arguments)` in ONE `try`
and `tool.py`'s `type_adapter.validate_python` is INSIDE that `run`, so a
caller's bad kwarg and a `ValidationError` our own code raised produce a
byte-identical logger, message and chain. Measured, a logger-only rule dropped
both of these: the crash from an unknown `STEALTH_MCP_*` key (which makes
`Settings()` raise and fails EVERY spawn, since `get_settings()` is on the spawn
path) and `browser_manager.py`'s `BrowserInstance(...)` with a wrong field type.
The frames do separate them, identically on both paths — a body of ours always
sits between those two.

One rule is deliberately WIDER than its name, and now says so. The message-only
`client-disconnect` arm matches "an exception with no text at
`mcp.server.lowlevel.server`", not "a `ClientDisconnect`": mcp's line 707 is a
`case Exception():` catch-all formatting `str(exc)` with no `exc_info`, so a bare
`RuntimeError()` from our own session handling produces the same event and is
dropped with the 6 500. That event carries no exception values, no frames and no
extra, so there is nothing else in it to read; the trade is taken deliberately
and pinned.

What still ships, and why each was made a test: a `ToolError` raised while
handling an `AttributeError` (the historical `navigate` bug, which arrived on the
very logger the noise arrives on); an unconverted outermost `TimeoutError`;
`ToolError: Failed to spawn browser` over nodriver's plain `Exception`; a bare
`TimeoutError` or `CancelledError`; a `__main__`-defined class merely NAMED
`TimeoutError`; F-883's `InvalidStateError` from nodriver's listener, fixed in
2.1.9 and wanted loudly if it returns; nodriver's `ProtocolException`; a
`ConnectionResetError` from anywhere but that one CPython callback; an unawaited
task of OURS with the same exception type nodriver's has (the rule requires
`nodriver` in the message); our own pydantic `ValidationError` under FastMCP's
own logger, in three shapes; F-827's `capture_lifecycle` proxy messages; and the
sibling of the 6 500 — `Received exception from stream: Received response with an
unknown request ID: … Method not found`, 2 events, which is why that message is
matched by EQUALITY and never as a prefix.

Every drop is now recorded: `_is_expected_tool_failure` logs the class that
recognised the event at DEBUG, so an unexpected fall in Sentry volume traces to
one rule rather than to "the filter".

`observability.py` keeps the two event shapes and the never-raises contract and
loses the taxonomy, but its docstrings grew by more than the taxonomy weighed:
613 → 616 physical lines (521 → 527 non-blank). The new leaf is 610 physical
(507 non-blank), stdlib only, and takes the exception chain and the error base as
arguments, so the lazy `tool_errors` import stays single-homed. Both are under
the 1000-line budget and neither is grandfathered. The measurement trail — how
sdk 2.64.0 serialises each class, the three spelling rules above, and the CPython
and mcp-SDK source lines the message rules cite — is in
`audit/stage2/finding_F887_sentry_expected_noise.md`.

### Fixed — F-889: a starved proxy condemned a healthy backend, then exited

Measured 2026-09-18, 13:30-14:00 UTC on 2.1.8. The machine had **2.4 GB free of
125.7** and 114 stdio proxies whose working sets had been paged out to ~0 MB.
Their 2 s liveness probes timed out on the CLIENT side, the watchdog condemned,
the heals could not complete in the wall time they were given, and the proxies
EXITED — Claude Code rendered that as **"Connection closed"** on every session at
once. The backend they condemned (pid 173824, port 52554) answered an MCP
`initialize` in **227 ms** throughout, with zero errors in its own log for the
whole window. 829 condemnations in seven days. The asymmetry is the finding: the
only process that reported a problem was the one that had no CPU.

Four changes, each in the one home for its question.

**(a) Strikes are earned in fairly scheduled seconds.** A strike run may not
conclude until a `scheduling_lag.FairWindow` of
`interval * (failures_before_teardown - 1)` has been spent. On an idle machine
nothing moves: the per-tick charge is `interval * (1 + probe / nap_actual)`,
which is `>= interval` always, so the remaining ticks always spend the window
and the human-pinned ~12 s hard-down detection window is preserved. Under
starvation it stretches and still terminates, because `MAX_STRETCH` bounds it at
4x its patience in wall seconds. `FairWindow` is consumed, never modified.

**(b) The backend is a witness to its own liveness.** It stamps a wall timestamp
and its pid into a per-port sidecar, `~/.stealth-mcp/heartbeat-<port>.json`, every
3 s **from its event loop**, and a proxy reads it with no HTTP, no socket and no
thread. A fresh self-report against a failed client probe means "I am starved",
not "it is dead", and resets the strike run; a stale one (10 missed stamps) or an
absent one falls through to the confirmation phase exactly as before. The event
loop is the whole design: the failure the watchdog exists for is a backend whose
dispatch loop is dead while its socket stays open, and a heartbeat on a thread
would keep stamping through it. A SIDECAR and not a field on the record, because
`server.json` is written under the cold-start lock and a 3-second heartbeat must
not take it: unlocked, a whole-record read-modify-write is a lost update by
construction. One file per port has one writer, so there is no merge and no
snapshot; the record itself is byte-for-byte what 2.1.9 reads and writes, so the
schema version does not move and an older fleet is unaffected in both directions.
The sidecar is deleted with its entry by both doors out of the record — forgetting
it (`stop`, `cleanup --apply`) and superseding it (a cold start of ours moving to
a new port) — and a stamp for a port the record no longer names is not evidence
about anything, so it can never resurrect a forgotten entry.

**The veto it buys is bounded.** A heartbeat proves the event LOOP is turning, not
that the HTTP listener is reachable, so a fresh stamp may defer condemnation for
at most `HEARTBEAT_VETOES` = 10 completed strike runs — each of which must spend
its own `FairWindow` first, making the budget one of FAIR-time rounds. A starved
proxy spends it slowly; a fairly scheduled one reaches the confirmation gate in
about two minutes and heals from there.

**(c) The proxy never exits because the backend is unreachable.** Where
`proxy_selfheal.drive` used to return — a heal that gave up, three deaths back to
back, or a generation that never became ready — it now backs off (2 s doubling to
60 s, jittered ±25% so a fleet does not converge on one second) and keeps asking
for as long as the client's stdio pipe is open. In-flight calls are still failed
fast with the existing JSON-RPC error, so no call hangs silently, and the client
keeps its MCP server: when a backend comes back, the very next tool call works.
The one lifetime the proxy ever legitimately had is the client's. Stdin EOF is
still the exit — and, because the same outage's cleanup found **116 stale proxy
processes**, so is the client process itself going away: the new
`embedded/client_presence.py` captures the launching process as a
`(pid, create_time)` pair at start and ends the proxy once that exact process is
gone. That is not a decision about the backend, it is noticing nobody is
listening, and every uncertainty about it resolves to "still there" so it can
never disconnect a live session. The process it names is not the direct parent:
above a proxy sit a venv `python` trampoline with an identical command line and a
waiting `uv`/`uvx`, both of which live exactly as long as the proxy does, so it
walks past those (and past our own console-script redirector) to the first
ancestor that is nobody's launcher — otherwise the check could never fire for the
population it exists for. A walk that cannot settle answers "unknown", which
reads as present.

The `proxy: teardown after failed heal` report described a thing that no longer
happens and is now `proxy: backend unreachable, retrying`, carrying the same
`reason` values plus the first delay — shipped **once per outage, not once per
retry** (the retry series is unbounded), and closed by exactly one
`proxy: backend reachable again` carrying `attempts` and `outage_seconds`. Every
individual attempt is still in the proxy's own log file.

**(d) A newer backend of ours is adopted, never evicted.** Two identities on one
desktop each read the other as stale — `fingerprint_mismatch` answers "these
digests differ", never "mine is older" — so each evicted the other on every proxy
start. F-886 stops that only when the loser owns live browsers, and a fleet
mid-upgrade is exactly the population where neither does yet. A recorded version
strictly newer than ours is now ADOPTABLE, through a predicate of its own
(`_adoptable_identity`) read by the reuse gate and nothing else. "Is this entry
mine" stays `_identity_matches` and stays exactly what it was, because the
protection rule and the operator verbs ask it: adopting forward means **step
aside**, never take the port, so a newer sibling that is not answering is still
protected while it owns a live browser, and `restart`/`stop` still target our own
identity's backend. Same version + different digest (issue #14's editable-install
flow) is untouched, an older backend is still evicted when unprotected, and an
unresolvable version on either side is never "newer", so every uncomparable case
falls back to today's cold start.

### Fixed — F-890: an inherited `FASTMCP_*` variable made every backend launch crash at import

For sixty-six minutes on 2026-09-18 (04:26-05:33) every backend spawn died with
a `pydantic.ValidationError` before one line of our code ran, and the only trace
was `backend-boot.log`: the proxy hands the backend the MCP client's environment
whole, and `fastmcp` 2.11.2 builds a `pydantic_settings.BaseSettings` AT IMPORT
whose `env_prefixes` are `["FASTMCP_", "FASTMCP_SERVER_"]`. An inherited
`FASTMCP_PORT=""` is therefore parsed into `port: int` before `--port` exists as
a concept, and `int("")` does not validate. Measured on the installed stack;
the bare `port`/`PORT` names have no effect at all, so a fix written against
them would have shipped as a fix for a bug it did not touch.

The child env is now scrubbed at THE one composition site
(`singleton._start_server_process`) through the new `embedded/backend_env.py`,
which drops the whole `FASTMCP_` family rather than overriding the field that
crashed us — the backend's configuration comes from the argv we build, so every
one of those names is a second input to a decision already made, and
`FASTMCP_STATELESS_HTTP` would not even crash, it would silently give the bridge
a transport it is not written against. The prefix is scrubbed rather than a
field list derived from the library, because the stdio proxy must never import
`fastmcp`; a test pins the constant against `fastmcp`'s own `model_config`, and
a subprocess node proves the crash and its absence after the scrub. The module
also absorbed M8-2's `STEALTH_MCP_NO_AUTO_RECOVERY` pop (same sentence, one
home) — for the CHILD env, which is the only environment that rule was ever
about. The removed NAMES are logged, never their values.

The composer is not the only way this package imports `fastmcp`, so the third
party's names are also dropped from our OWN environment, at the two doors that
reach such an import: the first statement of `server.main()` (`--transport http`
runs `embedded/server.py` in that very process through `runpy`, and
`stealth-chrome-devtools serve --http` delegates to the same function) and
`cli._server()`, which every ops verb but `profiles` goes through — so a stray
`FASTMCP_PORT=""` in an operator's shell no longer kills `status` and `doctor`,
the two commands they would run to find out why nothing starts. **Only the third
party's names**: our own process keeps every `STEALTH_MCP_*` variable it was
started with, including the `STEALTH_MCP_NO_AUTO_RECOVERY` flag that keeps the
read-only verbs read-only. That removal belongs to the spawned backend's
environment and nowhere else. What we drop from our own process is reported at
WARNING rather than INFO, because this runs before logging is configured and
Python's last-resort handler starts at WARNING.

### Added — F-888: persistent named profiles and CDP re-attach after a backend restart

A backend that died took its browsers' logins with it. On 2026-09-18 a backend
went unresponsive under 60 concurrent sessions; the replacement's startup orphan
sweep killed every browser the dead one owned, including a human's logged-in
Amazon Seller Central session. Two more logins were stranded the same week by the
other half of the gap: their backend is still alive, but `stop` and `restart`
both end with its browsers terminated, so there was no verb that finished with
the login intact. F-886 (2.1.9) only stopped a cold start from EVICTING a backend
that owns browsers; a backend that dies for any other reason was still fatal.

**A browser on a persistent profile is now handed over, not killed.** One
predicate, `browser_pid_registry.on_persistent_profile`, applied at the two
places a browser died without a client asking for it. At SHUTDOWN
(`_cleanup_all_tracked`) such a browser is left running and left tracked, so
`stop` and `restart` finish with the login alive. At STARTUP RECOVERY
(`_recover_orphaned_processes`) it is skipped rather than reaped, and picked up
by the new adoption pass. A disposable auto-clone is unaffected in both places —
still killed, still deleted — and `kill-orphans --force` still takes everything,
because an operator asking is the authority the rule otherwise supplies.

**A new backend re-attaches over CDP and keeps the client's instance id.**
`browser_reattach.run`, driven fire-and-forget from `app_lifespan` so it can
never delay a serve. Adoption requires four conditions
and each one alone refuses (`browser_reattach.adoptable`): the owner is not a
live backend of ours (two backends driving one Chrome is F-886's harm), the
profile is persistent, the recorded pid is still that Chrome, and a CDP endpoint
is recoverable. The endpoint has three witnesses — the port this release now
records in `browser_pids.json` (`cdp_port`), `--remote-debugging-port` on the
process command line, then Chrome's own `DevToolsActivePort` — and the last two are
what let a 2.1.8/2.1.9 record, which carried no port at all, be adopted. The
instance is registered under its RECORDED id, with its live url and title read
through `tab_identity` rather than the cached pair, so a client holding an id
from before the restart still reaches the same browser.

**A named profile's persistence is now stated and pinned rather than emergent.**
`user_data_dir=<name or absolute path>` already survived close, the clone GC and
storage cap, `cleanup --apply` and `kill-orphans` in 2.1.9 — but that guarantee
was three hand-written copies of one condition that happened to agree. There is
no new parameter and no new layout: the condition has one home, the four
guarantees are pinned in `tests/test_browser_reattach.py`, and a test fails if
the literal comes back.

**Spawning onto a held profile re-attaches to it**, rather than walking to a
sibling directory (F-871). This is the second entry point into the same rule and
it is the one that matters in practice: the browser this feature exists for has
**no record entry at all** — measured on the real stranded Chrome (pid 115652,
`--remote-debugging-port=9223`), whose owner backend died and whose successor
rewrote `browser_pids.json` without it — so the startup pass, which walks
entries, would walk past it forever. The witness here is Chrome's own process
singleton (`profile_lock.profile_hold`) and the record is consulted only to
refuse a browser a LIVE sibling backend owns. **So recovering a logged-in browser
whose backend died is one call: spawn with the same `user_data_dir`.** A failure
on this path never reaps — a client asked for that browser, and killing it
because we could not attach would be this bug committed by its own fix.

The endpoint ladder now asks the process command line BEFORE
`DevToolsActivePort`, also measured: the stranded Chrome had no such file while
running, so a file-first ladder found nothing to attach to. The file keeps its
rung for `--remote-debugging-port=0`, which the command line cannot answer.

**Which process on that profile is the browser is asked explicitly**
(`browser_cmdline.browser_process`: the one with no `--type`). A profile is held
by a whole process TREE and `profile_hold` names whichever member its witness
iterated first — measured on one real spawn, eleven processes on one profile, of
which only the browser and the six renderers carry `--remote-debugging-port` at
all. Adopting the named member was a coin flip: a `utility` child yielded no port
and the adoption declined, while a `renderer` would have been adopted and stamped
onto `Browser._process_pid`, so the instance would be discarded the moment that
renderer recycled and `close_instance` would kill a renderer while the browser
kept running. Relatedly, "a browser holds this directory and we could not get in"
is now a named `reattach_declined` reason rather than the same silence an empty
directory produces.

A new RUNBOOK playbook ("Recover a stranded login") gives both paths plus the one
case that still needs an operator — a backend that is alive but unreachable,
which must be stopped before its browsers can be taken over.

Also: the nodriver host-and-port pair that makes `uc.start` connect instead of
spawn moved out of `desktop_launch.launch_and_attach` into
the new `cdp_attach` leaf, so the delegated headed launch
(F-810) and a backend adopting a browser now use one door.
`browser_reattach.reap_recorded` is the one home for "reap this entry", shared by
startup recovery and by a failed adoption's fallback — which is exactly 2.1.9's
behaviour for that entry, with the directory still spared. The whole subsystem
takes the `BrowserManager` and the `ProcessCleanup` as arguments
(`spawn_leak`'s precedent), which is what keeps both of those files inside their
grandfathered LOC caps; `browser_manager.py`'s ratchets down 1493 -> 1492.

**The claim that makes adoption safe between processes.** The refusal above —
never take a browser a live backend of ours owns — was a classification followed,
much later, by the ownership stamp a successful attach writes. Between those two
moments every other backend on the machine reads the same record, sees the same
dead owner and reaches the same verdict, so two backends could drive one Chrome:
F-886's harm, reached by the fix meant to prevent it. An `asyncio` lock cannot
help, because the racers are processes. Both entry points now take a CLAIM
(`browser_pid_registry.claim_browser`) BEFORE the CDP door: the check and the
ownership stamp happen inside ONE `update_entries` mutate, under the record's own
file lock, keyed on the PID (the instance id is not stable across the two entry
points, so two backends would mint two ids and both "win"). A failed attach hands
it back. That is also what answers two concurrent spawns onto one held directory
— the second reads a live owner and is refused — and the spawn then proceeds
normally, reporting `spawn_diagnostics["reattach_declined"]` rather than walking
away silently.

**And a lost claim is never a reason to kill a browser.** Holding the claim
across the attach is the new `browser_claim` leaf (`held`), because the guarantee
it needs is about an `await`: the claim is taken inside the block that releases
it, it is shielded (cancelling an `await asyncio.to_thread(...)` does not stop
the worker thread — it still writes), and the task is kept so a teardown can hand
back a claim that landed after the enclosing budget had given up on it. The two
outcomes that are NOT evidence about the browser are named types — `Refused` (a
sibling backend claimed it first) and `Undecided`, its subclass (the record write
itself failed) — and the startup pass now catches them: the entry and the browser
are left exactly as found, with a WARNING naming the reason. Reaping stays
reserved for a browser we claimed and then could not attach to, and only on the
startup pass; the spawn path still never reaps.

**An adopted instance now reports measured values, not the caller's request.**
`headless` comes off the holder's command line and the window is MEASURED through
`window_sizing.measure` — a read that deliberately does not RESIZE, because
`apply_and_measure` would have moved a human's open window to a default they never
asked for. What cannot be read back off a running browser is named
(`spawn_diagnostics["not_restored"]`: `extra_headers`, `timezone_id`,
`user_agent`, `proxy`) instead of being re-asserted as though it had been applied.
The adopted tab also gets this backend's dynamic-hook interception, exactly as a
spawn does; without it a hook created against an adopted instance was registered
and never fired. Spawn arguments that describe a LAUNCH are ignored rather than
refused on the re-attach path, and listed in
`spawn_diagnostics["ignored_spawn_args"]`.

**A browser whose egress proxy died with its backend is adopted and stamped.** An
authenticated `proxy=` spawn points Chrome at a forwarder inside the backend, so
the forwarder dies with it while the launch arg lives on. `browser_cmdline`
connect-probes a loopback `--proxy-server` and the diagnostics carry
`dead_egress_proxy` plus a WARNING. Adopted rather than refused, because on the
record path a refusal routes to the reap — which turns a recoverable login into a
kill.

Smaller, all from the same review: a classification failure now resolves toward
SPARING the entry with a logged warning (it was a bare `contextlib.suppress`,
which dropped the entry out of the spared set and let recovery kill a human's
Chrome with nothing written anywhere); adoption requires BOTH halves of a pid's
identity where reaping is content with one, and the recovered port is JOINED to
the profile, so a recycled pid on a stranger's chrome.exe cannot be adopted and
later killed; the attach runs behind a shield so a timeout cannot strand the
connection it opened; and what a running browser's own command line says about it
is a new leaf, `browser_cmdline`. `process_cleanup.py`'s cap ratchets down
1017 -> 1009.

### Fixed — F-834 stage 2: a Chrome still opening its DevTools endpoint was killed as a failed spawn

nodriver 0.47 gives a Chrome it just launched `0.25 s + 4 × 0.5 s` = **2.75 s of
waiting** to answer `/json/version` (`core/browser.py:411-435`) — a loop count
with no `Config` field behind it. (Wall clock adds what five refusals cost,
which is the platform's: near-instant on POSIX per F-870 §1.3, so ≈2.75 s there;
2023-2060 ms each on Windows, measured locally, so ≈12.9 s — which is why the
Windows cell's cold start does not fail.) Chrome routinely misses it on macOS.
The F-870 cold-start probe, which runs on
every gate cell before the suite with the runner idle and launching exactly ONE
Chrome, measures `ms_to_json_version`:

| cell | gate run | launch 1 (cold) | launch 2 (warm) |
|---|---|---|---|
| macOS/ARM64 | 35304880367 | 3943.6 ms | 581.6 ms |
| macOS/ARM64 | 35316298288 | 4786.0 ms | 1289.7 ms |
| macOS/ARM64 | 35454765486 | 5839.1 ms | 2138.7 ms |
| Windows/X64 | 35316298288 | 4250.0 ms | 344.0 ms |
| Linux/X64 | 35316298288 | 685.8 ms | 232.6 ms |

Two of the three cells have never once answered inside the window on a cold
binary, and the warmest macOS reading already spends 78 % of it on one launch
with nothing else running. What followed was correct and wasted: the attempt
raised "Failed to connect", no `Browser` was handed back, F-860's reap killed
the Chrome that was coming up and logged a WARNING on the backend's durable
channel, and F-834 stage 1 relaunched from cold onto the directory the reap had
just freed. On the six-way fleet (`spawn 26.0s`, 1 lead + 5 in 3 lanes on 3
cpus) a follower lost that race often enough to fail four of the last five
macOS gate runs — `tests/test_e2e_fleet.py`, runs 35454765486 (attempts 1 and
2), 35316298288 (attempt 3) and 35304880367 (attempt 1).

The budget is ours now. `embedded/browser_connect.py` is THE one home for "how
long does a freshly launched Chrome get to open its DevTools endpoint", and
`browser_manager._launch_browser` installs it once ahead of the F-810 branch, so
every launch in the tree is covered. `CONNECT_PATIENCE_SECONDS` is 30.0 — ~5×
the worst measured cold launch — and it is a **ceiling, not a wait**: an
endpoint that opens in 300 ms costs 300 ms, and a live one-Chrome run through
the product measured a healthy headless spawn at 644.5 ms end to end. A launcher
that exits, before the wait or during it, ends the wait at the next refusal, so
the one case this could have slowed (Chrome dies at launch) is faster than
2.1.9, which polls a dead port for its whole window.

**The ceiling is for launches we own.** The `connect_existing` door — F-810's
delegated launch, and F-888's re-attach next — takes nodriver's window
unchanged, because an attach targets an endpoint that is already open and a live
one answers in 0.78 ms median (measured, ten fetches); patience buys nothing
there, while a stale recorded port would have cost 30 s inside a user-facing
call. There is deliberately no second constant for that door. An HTTP *error*
answer is likewise not a closed socket, so a squatter answering 500 still fails
in nodriver's window. What this does cost is named rather than hidden: a Chrome
that starts and then hangs without ever listening now spends the ceiling on each
of `_SPAWN_ATTEMPTS`' three attempts — 32.5 s and 97.6 s measured on the
instant-refusal shape — and the finding's §6.3 argues why clipping the later
attempts was declined. The seam is `HTTPApi.get`, the only call that
sits between "Chrome is spawned" and "the endpoint answers" and nodriver's
single caller of it; this closes the fix F-870 §6 wrote down and declined to
ship for want of exactly the number §7 has since measured. No new knob, no
attach path, no weakened oracle — `spawn_leak`'s warning still fires for a spawn
that genuinely leaves a Chrome behind. `browser_manager.py`'s LOC cap ratchets
down 1493 → 1490. See
`audit/stage2/finding_F834b_connect_deadline.md`.

### Fixed — F-882e: `navigate` failed about a healthy page when the document moved under its landing read

The fourth sibling of F-882b/c/d and the first that is a product defect. The
milestone and the post-navigation read are two round trips, so a document
scheduled to replace itself AT the milestone — the `meta refresh` shape, a
`load`-time `location.replace`, a JS challenge — can commit in the gap. Chrome
then answers the in-flight `Runtime.evaluate` with
`ProtocolException: Inspected target navigated or closed [code: -32000]`, and
that reached the caller as a failed `navigate` while the browser sat on a
perfectly good page. Measured twice on `integration (macOS/ARM64)`, on two
branches a day apart and neither of them related: release-gate runs 35316298288
attempt 1 (2026-09-18) and 35460514255 attempt 1 (2026-09-19), both raised from
`navigation_milestone.landing`, both with the navigation itself
`[accepted, committed]` — which is why nothing retried it, correctly: a failure
after Chrome accepted a navigation is the page's own (F-881).

`landing` now RE-READS, because the document that took the old one's place is
what the tab is showing and one more round trip is still one document at one
instant. Bounded at `LANDING_SWAP_RETRIES` (2) extra reads and keyed narrowly by
`document_swapped` on Chrome's code **and** its message — `-32000` alone is that
browser's generic server error, so every other protocol failure is still raised
from the first read, and the last read sits outside the loop so its error is the
one the caller sees. Deliberately not a second wait for the replacement to reach
the milestone: the milestone belongs to the navigation and was reached, and a
committed-but-still-parsing landing is a truthful answer the oracle already
names (F-882d). `browser_manager.py` is unchanged; the E2E node is unchanged and
stays the real-Chrome witness.

## 2.1.9

### Fixed — F-885: proxy/backend-death tests touched the developer's live `~/.stealth-mcp` record

`tests/test_proxy_backend_death.py::TestProxyExitsOnBackendDeath` ran an
IN-PROCESS proxy with the real state paths unpatched — only
`STEALTH_MCP_BROWSER_SESSION_ROOT` was overridden — so its death-confirmation
step (`_same_identity_backend_ready`, wired in as `confirm_alive`) read
`singleton.SERVER_STATE_FILE` directly, and its subprocess-spawned backend
wrote real boot/backend logs into `~/.stealth-mcp/logs` (measured: a
`backend-<pid>.log` and `backend-<pid>-fault.log` pair landed there from a
single run). Isolated the in-process proxy state the same way
`test_singleton_version_aware.py`'s `isolated_state` fixture does
(`singleton.STATE_DIR`/`PORT_FILE`/`SERVER_STATE_FILE` monkeypatched to a
`tmp_path`), and the subprocess env the same way
`release_gate_harness.gate_workspace` does (`_isolated_env`: HOME/USERPROFILE/
LOCALAPPDATA/APPDATA/log dir/session root/clone dir all redirected into a
throwaway workspace, since a monkeypatch in the test process never reaches a
separate child process).

The sweep this finding called for turned up a substantially worse instance of
the same shape: `tests/test_singleton_fast_handshake.py::
TestEntrypointExitsOnDisconnect::test_stdio_entrypoint_exits_when_stdin_closes`
spawned the REAL stdio entrypoint (`python -m stealth_chrome_devtools_mcp
--singleton-port <port>`, no `--transport` flag) with HOME unredirected. That
entrypoint runs `ensure_server_running()` for real; when the recorded backend's
source fingerprint does not match the checkout under test,
`_select_backend_port()` prefers the port already recorded for this machine's
display context — not the port the test passed — and
`_start_backend_holding_lock()` evicts (kills) whatever backend is listening
there before cold-starting a replacement. On a developer machine with a live
backend other sessions are using, running this test unpatched could kill it
and close every browser it was serving. Two siblings in the same file
(`TestFastHandshakeEndToEnd`, `TestProxyExitsOnClientDisconnect`) share the
same log-writing hazard as the original finding, at the same (lower) severity.
All three were fixed with the same `_isolated_env`-based redirect.

Full before/after measurement, the eviction trace, and the rest of the sweep
are in `audit/stage2/finding_F885_proxy_death_test_touches_real_state.md`.

### Fixed — `navigate` timed out on a loaded page whose document replaced itself (F-882)

A ten-site fleet test on 2.1.8 (Chrome 152, Windows 11) raised
`Navigation … timed out after 30000ms` for three of ten ordinary destinations —
a signed-out `mail.google.com`, `youtube.com` and `reddit.com` — while the
browser was sitting on a fully loaded page. Each of those sites replaces its
first document with a second one (a head-script `location.replace`, a JS
challenge that sets a cookie and re-navigates) BEFORE the first reaches `load`.
The replacement commits under a NEW `loaderId`, fires its own `load`, and
F-881's wait — keyed on the single `loaderId` `Page.navigate` answered with —
had nothing left to wait for and spent the caller's whole budget. Measured on
Chrome 152: the superseding document's commit landed 20.3-22.5 ms after ours
across all four shapes.

`navigation_milestone` follows the FRAME's loader CHAIN now: events are filtered
to the frame `Page.navigate` returned (a same-origin iframe's two loaders carry
the subframe's `frameId` and are ignored), a commit for that frame under a later
loader extends the chain, and the milestone is satisfied when the LATEST document
has reached it. A commit is `Page.lifecycleEvent` name `init` and never `commit`
— because `Page.setLifecycleEventsEnabled(true)` REPLAYS the current document's
whole lifecycle under that name (measured), and the tool sends it immediately
before navigating, so the replay always describes the page being left.

`net::ERR_ABORTED` is answered instead of waited on. It has two measured
meanings: a download (`Content-Disposition: attachment`) commits nothing ever and
leaves the tab where it was, and a page navigating itself away while our
navigation is still pending cancels ours and commits its own 11.6-14.1 ms later
(or, in two of six runs, just before the abort response arrived). So an abort
waits `ABORTED_GRACE_SECONDS` (1 s, ~70x the worst measured gap, clipped to half
the remaining budget) for the document that took our place, and only a grace that
passes with nothing in it raises — naming the download rather than reporting a
30 s timeout about a navigation that was over in 13 ms.

Two smaller truths came out of the same work. The post-navigation read was two
`tab.evaluate` round trips, and on a `meta refresh` page the refresh fired
between them: the tool answered with the FIRST document's `url` and the SECOND
document's `title`, a record no document ever had. It is one `JSON.stringify`
round trip now (`navigation_milestone.landing`). And the one durable record of a
failed navigation was `Navigation attempt 1 failed for <id>: ` — a `TimeoutError`
stringifies to nothing — so the line now carries the exception type plus
`Progress.describe()`: whether Chrome accepted the navigation, whether our
document committed, and how many later documents superseded it. The raised
`ToolError` carries the same clause.

Deliberately unchanged: a document that reaches `load` BEFORE its replacement
commits is still answered about at its own `load` (the `meta refresh` and
self-reload shapes, and Amazon's `title: ""`), because that answer is true at the
instant it is made and waiting past it would be a quiescence wait `navigate` does
not promise. `networkidle` keys to the FIRST commit and F-787 stays open.

New real-Chrome coverage: `tests/test_e2e_navigation_truthfulness.py`
(`integration`, 8 nodes) drives all seven shapes plus the same-origin-iframe case
against local fixture routes, with the page's own sentinel and the fixture
server's request ledger as oracles independent of the tool under test.

### Fixed — F-885b: two more tests wrongly cleared by F-885's sweep, plus the retention consequence

An independent reviewer of F-885 found two sites its §4 sweep had wrongly
cleared, both the same unisolated-subprocess shape: `tests/test_tool_registry.py::
TestCountTripwire::test_list_sections_printed_total_matches_registry` spawned
`python -m stealth_chrome_devtools_mcp --transport http --list-sections` with
no `env=` override at all, on the theory that `--list-sections` "exits before
serving" — it does, but `embedded/server.py`'s `__main__` calls
`bootstrap_backend_process_logging()` as its first statement, six lines before
that branch, so the process still writes a "backend process starting" line
and a `-fault.log` into the real `~/.stealth-mcp/logs/`. And
`tests/test_singleton_version_aware.py::TestStaleBackendEvictionEndToEnd::
test_clear_stale_backend_terminates_real_backend` spawned a real
`--transport http` backend with `env = dict(os.environ)` plus only
`STEALTH_MCP_BROWSER_SESSION_ROOT` — byte-for-byte the pattern F-885 replaced
elsewhere, missed here because the file's `isolated_state` fixture (which this
one test does not use) made the whole file look covered. Both fixed with the
same `release_gate_harness._isolated_env`-based redirect.

The reviewer also identified that the class is worse than log pollution:
every `configure_logging()` call ends by calling `prune_old_logs()`, which
unlinks every `*.log*` file in the resolved log dir beyond the newest 50 or
older than 7 days — so an unisolated test subprocess applies the product's own
retention policy to the developer's real backend/proxy logs and deletes the
oldest of it. Measured directly: running the two unpatched tests against the
real `~/.stealth-mcp/logs/` (294 files beforehand) left it at 295, not 298 —
`prune_old_logs` had already reaped older real entries to make room for the
four new files the unpatched subprocesses wrote. Running the fixed tests left
the directory's file set byte-identical.

A repo-wide re-sweep of `tests/` for the same shape (any spawn of product code
with an env that does not redirect HOME/USERPROFILE or `STEALTH_MCP_LOG_DIR`)
found no further instances. See §6 of the finding doc for the full writeup.

### Fixed — F-883: `execute_script` never awaited, so a Promise was `{}` and `await` was a SyntaxError

Measured through the shipped MCP on 2.1.8 (Chrome 152, nodriver 0.47):
`return fetch(u).then(r => r.text())` answered `{"success": true, "result": {}}`,
`return Promise.reject(new Error('boom'))` answered the **same** `{}` — a failure
reported as a success — and `const v = await …` died with `SyntaxError: await is
only valid in async functions`. The tool's own docstring forbade every blocking
wait and told callers to use `await fetch(url)`, which is the one thing it refused
to run.

The one `Runtime.evaluate` was sent without `awaitPromise`, so Chrome answered
with the Promise **object** and `returnByValue` serialized it by its own
enumerable properties — a `Promise` has none, hence `{}`, indistinguishable from
a script that genuinely returned an empty object. And F-812's retry wrapper was an
ordinary arrow function, so a top-level `await` was never even a case it
recognised.

Three changes, at a new leaf `embedded/script_evaluation.py` — THE one home for
"run a caller's JS in the page and read its answer" (`dom_handler.py` was at 997
of its 1000-LOC budget; it is 853 now, and `DOMHandler.execute_script` is a
one-line delegation): `await_promise=True` on THE one send, so a returned Promise
answers with the value it resolves to and a rejection becomes the
`exceptionDetails` F-795's reader already refuses; an **async** wrapper on both
wrap paths (the retry's and the `args` path's), so `await` is legal on both call
shapes; and a retry keyed on **two** compile complaints — Chrome's illegal-`return`
and its top-level-`await` — each paired with the phrase the error message uses to
name it, so a caller whose `await` was the trigger is not told about a `return`
they did not write. A script that fails for any other reason still keeps its error
and is still evaluated exactly once, and a top-level `var`/`function` still lands
on the page unwrapped, because the source is always evaluated as written first.

A rejection reason is page-authored and unbounded, so `tool_errors._require_js_value`
— THE one place a thrown script becomes the error convention — now clamps the
detail to 200 characters with a visible `…`, the same bound
`js_aspect_answer.MAX_ERROR_CHARS` and `page_storage.BLOCKED_REASON_CHARS` carry.

The same defect through the other door: `inject_and_execute_script` and
`call_javascript_function` / `execute_function_sequence` already sent
`await_promise=True`, but the **wrappers** their caller's code runs inside were
synchronous, so an async page function's Promise landed in `result` as `{}` with
`success: true`. Both wrappers are `async` now and their inner calls awaited, in
their own home — no second evaluate path.

One behaviour is deliberately slower: a Promise that never settles used to answer
`{}` instantly and is now killed at `timeout_ms` (`_clamp_timeout` +
`_with_cdp_timeout`, the one home for that clamp — no second deadline inside the
eval seam), reported as a timeout. The tab is usable immediately afterwards.

**And every CDP reply is now shielded from its caller's cancellation, which
closes TWO open HIGH findings — F-788 and F-794 — as well as the crash the
review made reachable.** Cancelling an await — ours on a timeout, or the
client's `notifications/cancelled` — cancelled nodriver's `Transaction` while it
was still registered in `Connection.mapper`; when Chrome answered LATE, the
connection's listener task `set_result`-ed a cancelled future, died of the
`InvalidStateError`, and every later call on that tab timed out with the generic
"the browser may have crashed" — about a browser that was fine. That is
**F-788** (a navigation timeout wedges the instance, HIGH, open since W10) and
**F-794** (a cancelled call wedges the instance, HIGH, open since W13), and
F-883's `awaitPromise` simply made `execute_script` a third way to reach it.

The fix is one line at the transport layer: the new
`embedded/cdp_transport.py` wraps `Transaction.__await__` in `asyncio.shield`,
so the cancellation lands on a throwaway future and the registered one stays
pending for the listener to resolve. It is installed once from `tool_runtime`
and covers every CDP send in the tree — ours and nodriver's own — including the
deadlines inside `browser_manager.navigate`, which is untouched. `_with_cdp_timeout`
still CANCELS the operation it bounds: a cancelled request must stop the rest of
the body, and an earlier attempt that shielded there instead made a cancelled
`navigate` navigate anyway (caught by the wire lane, reverted).

Measured on Chrome 152: a Promise settling after `timeout_ms`, a `navigate` that
times out while Chrome commits late (F-882's reported shape), and a client
cancellation of a confirmed in-flight request — all three now leave the instance
usable. Both characterization pins that recorded the wedge are inverted in this
change (`tests/test_resilience.py`, `tests/test_wire_semantics.py`), MQ-128 is
promoted to satisfied and MQ-141 now waits on F-791 alone. What is NOT recalled:
a timed-out `navigate` has already handed `Page.navigate` to Chrome, so the page
may still land — the instance survives, the navigation is not undone.

The F-812 retry is keyed on the raw `exceptionDetails` now, not on the message:
class `SyntaxError` AND no stack frame in the description (measured: a compile
complaint's description is bare; every thrown Error's carries `\n    at`). So
`throw new Error("Illegal return statement")` — page-authored — no longer re-runs a
side-effecting script twice. A page that also overwrites `.stack` still can, and
the finding says so.
Everything else is byte-identical: a plain sync return, a nested object/array
(including falsy leaves), `0`/`""`/`false`/`null`/`undefined`, `Infinity`, `args`,
a synchronous throw, a non-serialisable value and a cycle all answer exactly as
they did on 2.1.8 — measured, `audit/stage2/finding_F883_execute_script_never_awaits.md` §2d.

### Fixed — F-882b: a slow fixture document recorded itself into the NEXT test's ledger

`tests/fixture_routes.py`'s `/nav/slow-doc` route slept first and appended to
the server-side `nav_paths` ledger afterwards, where every other `/nav/*` route
records on arrival. The one caller of that route pre-empts the navigation, so
the browser abandons the request while the handler thread goes on sleeping for
the full 2.5 s; by the time it records, the next test has already called
`/e2e/reset` and the entry lands in a ledger that belongs to a different
navigation. Measured: CI run 35150887345 (release-gate integration, Windows/
X64, PR #123) failed the subframe node of
`tests/test_e2e_navigation_truthfulness.py`, whose ledger must be exactly three
paths, with the previous node's `/nav/slow-doc?ms=2500` at its head. The route
records before it sleeps now, and a hermetic pin in
`tests/test_fixture_dynamic_routes.py` holds the request open at the delay and
reads the ledger while the response is still withheld — no browser and no
wall-clock budget.

### Fixed — F-882c: two navigation nodes asserted a fetch their milestone does not cover

The sibling of F-882b, in the same ledger oracle and also not a product defect.
`tests/test_e2e_navigation_truthfulness.py`'s meta-refresh and self-reload nodes
asserted an exact fetch sequence at the instant `navigate` returned, for a
second document their own page schedules AT `load` — the very milestone the
tool returns on. Both nodes accept an answer about the FIRST document, and in
that arm the server legitimately has not been asked for the second yet, so the
oracle was racing the page it was meant to witness. Measured: CI run
35157444236 (release-gate integration, macOS/ARM64, PR #126) failed the
meta-refresh node with `['/nav/meta-refresh']` against an expected two paths.

Both now wait through `_await_fetched`, a bounded poll of the ledger (5 s
deadline, 50 ms interval) that returns whatever it has at the deadline; the
exact-sequence assertions are unchanged, so a wrong order and a short ledger
fail exactly as before. The other six ledger assertions in the file still read
once, deliberately — each asserts on a fetch the tool's own answer proves
already happened, and that classification is argued in the helper's docstring.

### Fixed — F-834 stage 1: a `spawn_browser` whose first attempt failed got no second attempt unless it was already on a clone

Two measured shapes, one hole. A NAMED follower on the coverage gate's
macOS/ARM64 cell (run 35150887345, attempt 2) failed with
`ConnectionRefusedError` while alone on its own un-walked directory: Chrome had
STARTED there — the F-860 reaper found its pid running and killed it — and had
simply not opened its DevTools port inside nodriver 0.47's connect deadline,
0.25 s plus five 0.5 s naps, a constant that does not scale with load, on a
3-vCPU runner taking five launches at once. And three concurrent unnamed spawns
all select the master profile, so two of them lose Chrome's own process
singleton. In both shapes `_fallback_profile_selection` answered `None` for
every role that was not `clone`, so the three-attempt loop re-raised on the
first failure. The retry budget was always there; the spawn had nowhere to spend
it. Meanwhile the product's own contention hint was advising the caller to
"retry this one once the others have settled".

The fallback now covers all three roles, and only a `clone` re-clones. A named
profile retries **the same directory** — never a clone and never `<name>-2`,
because the caller asked for that profile's cookies and logins, and the one
place that walk may happen is the resolver, which reports it (F-871). A master
retries the same directory too when nothing holds it, and falls through to a
reserved clone when a sibling took it, since retrying a directory another Chrome
owns fails the same way again. What frees the directory in time is the failed
attempt's own F-860 reap.

Master itself is still never reserved: the release would have to live on the
close path, and a leaked reservation would silently force every later spawn to
clone forever. The attempt count is unchanged, and the retry does not wait for
the sibling wave — `_spawns_in_flight` is decremented in the `finally` around
one attempt, so a spawn deciding its retry is not counted in it (measured: the
count reads 0 at every retry decision), and every member of a failing wave would
read a number excluding all the waiters and be released together.

Measured hermetically through the real tool body, with Chrome's process
singleton modelled once and read from both sides:

| | before | after |
|---|---|---|
| three concurrent unnamed spawns | 1 of 3 live, 1 directory | 3 of 3 live, 3 directories |
| serialised lead + five named followers | 0 of 6 live | 6 of 6 live, every follower on the directory it asked for |

A pre-existing leak is closed in the same pass, because the widening is what
exposes it to an ordinary unnamed spawn: the last attempt asked for a
re-selection that nothing could ever drive, so a fully failed spawn copied one
extra profile tree and left that directory protected from the storage sweep for
the life of the process. Before this fix a master-role spawn that failed every
attempt made zero clones and leaked nothing; the `clone` role always reached it.
The handler now skips the re-selection once the budget is spent, so the loop's
`else` stays the one exhaustion raise and the caller's joined error text is
unchanged.

Two residuals are named rather than hidden. An `explicit` or untaken `master`
selection drives the same directory on all three attempts with no overall
deadline, so a permanently unusable profile costs about 8.25 s of nodriver's own
connect naps instead of 2.75 s — the same budget the clone role always spent.
And the hermetic measurement establishes that the retry happens on the right
directory, not that a real retry wins the race against that deadline on a
saturated runner; nothing hermetic can establish the latter.

`clone_storage.py` is still 1055 lines, its grandfathered cap.

That three concurrent selections all still answer `master` is unchanged and is
now pinned as characterization: the master branch of
`clone_storage.resolve_profile_selection` asks `_profile_has_running_browser`,
which is a LIVENESS check and never a reservation, and every concurrent spawn is
pre-launch when it asks. That reading is the design; what was broken was what
happened to the callers who then lost.

### Changed — F-834: the contention hint names the count it measured, not a mechanism it did not

The paragraph appended to a failed concurrent spawn said those spawns "contend
for the same Chrome profile". The only fact that module has is an integer, and
after both F-834 layers concurrent spawns are handed distinct reserved clone
directories — so the sentence was frequently false, and measurably false for the
macOS follower above, which was alone on its own directory. The paragraph now
says which part of it is measured, offers both causes without picking one —
Chrome's profile singleton, and what N simultaneous launches cost a small runner,
including a browser that starts and still misses nodriver's fixed connect
deadline — and keeps the one remedy that serves either. The `no_sandbox`
disclaimer is unchanged.

### Tests — real-Chrome E2E coverage for F-873…F-881

Every defect in the 2.1.7/2.1.8 set was found by driving the shipped release against
real sites with several browsers at once, after CI was green. This adds the coverage
that would have been red first. No `src/` change.

- **`tests/test_e2e_fleet.py` (new)** — six headless browsers: one lead, then five
  spawned at once, then six navigations and six tool calls in one `asyncio.gather`
  each, over a mix of page shapes (plain, a page whose `load` is held open, an app
  shell whose document cannot scroll, a form), with every answer checked against the
  page's own state in JavaScript. Half the fleet is UNNAMED, which is the advertised
  path and the manual run's own shape — one run covers all three profile roles
  (`master`, `clone`, `explicit`). The lead spawns alone because nothing reserves the
  master profile — measured, three concurrent `resolve_profile_selection(None)` calls
  against a free master all return the SAME directory, which is how the macOS/ARM64
  gate cell first failed this node. F-834 stage 1 has since made that survivable
  (the loser retries instead of raising), so the serialization is now about cost
  rather than survival: each loser burns a Chrome launch plus nodriver's whole
  ≈2.75 s connect deadline before the fallback is asked. The five that follow the
  lead spawn in as many lanes as the cell has cores (minimum two, printed in the
  node's own diagnostic line): nodriver 0.47 gives a launching Chrome a
  fixed ≈2.75 s to answer `/json/version`, and five simultaneous cold starts on a
  3-vCPU runner lost one — a named directory nothing else wanted, whose Chrome was
  alive when the reaper found it, so capacity rather than contention. No test-side
  retry: retrying is the product's job and F-834 stage 1 is where it now happens.
  Two members move
  their page WITHOUT the `navigate` tool (a click that retitles, and a `switch_tab`),
  which is what makes the `list_instances` block red against F-874 rather than
  decorative. Asserts six live titles with `partial: false`; that of the six profile
  directories the product said it used, exactly the named ones survive the close
  (both halves of `spawn_browser`'s documented promise — every disposable auto-clone
  reclaimed, driven through `cleanup_deferred_profiles` rather than waited for; every
  claim scoped to what this fleet was given, because the temp root is shared across
  worktrees); and that the backend logged nothing at WARNING while the fleet was
  DRIVEN beyond the one named, lane-structural clone-seed fallback. A six-way
  concurrent close on Windows may add exactly three named teardown warnings (a Chrome
  kill over `settings.close_kill_timeout` and its worker's `did not die after force
  kill` — two ends of one slow kill — plus a profile a dying Chrome still holds open;
  all measured), tolerated only because the node has already proved their
  consequence repaired: the same poll that reclaims the clone directories also waits
  for all six instances to leave the product's tracked-pid record, which is the
  product itself vouching that every Chrome is dead — `close_instance` answers True
  on the timeout path by design, so it cannot. The spawn `gather` collects exceptions
  so a partial spawn failure closes whatever did start. Measured `spawn 5.2s,
  navigate 1.3s, actions 2.3s, total 9.0s`.
- **`tests/test_e2e_load_milestone.py` (new)** — F-881 made red by construction: a
  page that commits at once and holds its `load` on a slow `<img>` for 1.8 s, whose
  title and `readyState` flip only at `load`. Plus the `domcontentloaded` control that
  keeps the three milestones told apart.
- **`tests/test_cli_backend_records_e2e.py` (new)** — F-880 through the REAL
  `stealth-chrome-devtools` console script as a subprocess, against a hand-written
  `server.json` in an isolated HOME: `doctor` names the `(dead record)` and the
  `no port recorded` entry and writes nothing; `cleanup --apply` forgets exactly the
  dead one and keeps the other. Both nodes assert the developer's real
  `~/.stealth-mcp` was not touched.
- **`tests/test_e2e_scroll_page_verification.py`** — the whole F-878 twelve-fixture
  matrix is now asserted against the finding's own "right answer" column, not only the
  four fixtures a candidate heuristic gets wrong. Re-measured 12/12.
- **`tests/test_browser_integration.py`** — F-874's third record shape (`partial: true`
  + `detail_error`, and NO `current_url`/`title` key) against a real instance.
- **`tests/test_e2e_type_text_verification.py`** — a control that REWRITES what it
  receives (a `dd-dd` mask) still succeeds, holding open F-873 §6's rule that the
  check is "did anything change" and never "does it contain what I typed".
- **`tests/test_wire_semantics.py`** — a self-calibrating overlap probe on the real
  stdio wire: three `tools/call` in flight add ONE server-side hold, not three
  (measured baseline 0.15 s, held 2.18 s over a 2.0 s hold). The backend does not
  serialize concurrent calls.
- **`tests/fixture_routes.py`** — six `cov_*` routes appended at EOF for the above, and
  the module docstring's determinism rule now names them as its second deliberate
  exception (they sleep on a `?ms=`, capped, never as a synchronization point).
- **`tests/conftest.py`** — `STEALTH_MCP_BROWSER_SESSION_ROOT` is redirected to a temp
  directory at conftest IMPORT time, beside the `STEALTH_MCP_CLONE_OUTPUT_DIR` line that
  already used the idiom, so no test can reach the operator's real browser-session root.
  A per-test fixture provably cannot do this — `get_settings()` is `lru_cache`d and every
  E2E module's autouse `_warmup` spawns a browser before any function-scoped root fixture
  is set up, which is how a fleet node declaring `tmp_empty_root` still wrote six 108 MB
  profiles into the real root. Closes the structural gap F-841 left open; that finding is
  updated with the measurement.

### Tests — lifecycle resilience E2E (disconnects, browsers closing)

`tests/test_e2e_lifecycle_resilience.py` — eight nodes that drive a REAL fleet (the
installed console launcher over stdio JSON-RPC, a detached backend on an isolated
`HOME` and an OS-assigned port, real headless Chrome) and assert, after each stress:
the recorded backend pid is unchanged; every browser spawned before the stress is
still alive AND answers a CDP round trip; every `tools/call` on a surviving proxy got
exactly one response frame, not an error, and no proxy's stdout reached EOF; and ZERO
lifecycle incidents were written to the proxy/backend logs. The mixed-fingerprint
fleet is the one deliberate exception to that set: it churns a backend by
construction, so it runs in its own workspace, asserts browsers on the pid captured
at spawn only (the winner rewrites `browser_pids.json`, so the registry cannot be its
oracle) and is exempt from the zero-incident rule — the eviction line IS its
measurement. The incident vocabulary is the
product's own log lines (`confirmed unusable`, `confirmed gone after a lost
connection`, `backend healed: re-bridging`, `backend unhealable after`, `times in a
row`, `backend stale (source changed), evicting`) — read from the logs rather than
from `capture_lifecycle`, which is a no-op under the suite's
`STEALTH_MCP_NO_ERROR_REPORTING=1` — and one hermetic node asserts each of those
substrings still occurs in the module that emits it, so a reworded line turns the
vocabulary red instead of making every stress node vacuous. A watchdog STRIKE is
deliberately not an incident (F-820) and is counted and printed instead.

Stresses, one node each, with the measured wall time and the numbers asserted on:
CPU saturation (20 s, `os.cpu_count()*2` normal-priority busy loops, two proxies
calling every ~1 s. The strike count varied across runs on a 32-core box (0 in
five, 6 in one where answered calls also fell from 76-80 to 28; the *unstressed*
60 s soak logged 2 in another), so no node asserts a count or a floor — every
node asserts the IMPLICATION instead: if a full strike run is ever reached on a
port, THAT proxy's confirmation phase must have answered `was busy, not dead`
— never silence, never `confirmed unusable`. Keyed on `(log file, port)` because
every proxy here shares one backend, so a port-only key would let a sibling's
verdict close another proxy's run; and a full run that is the last watchdog line
its proxy wrote counts as pending, because the confirmation may legitimately run
for `REUSE_PATIENCE_SECONDS` without logging. Vacuous when the load does not
bite, an end-to-end F-820 oracle when it does; the longest consecutive run is
printed so which of the two happened is readable. It has not fired on any real
run yet — seven runs, the limit never reached — so both paths are exercised
hermetically on synthetic log lines instead); a hard-killed sibling proxy; 30 `initialize`+DELETE liveness
sessions plus five clean proxy connect/disconnect cycles; a session idle past
`session_hygiene.ABANDONED_AFTER_SECONDS + SWEEP_INTERVAL_SECONDS` (the longest
periodic reaper in the tree, derived from those constants rather than typed); a
three-proxy 60 s soak of navigate/scroll/type/screenshot against the new
`/life/lifecycle.html` fixture route; and two proxies whose package source
fingerprints differ, sharing one state dir.

**One finding, pinned as `xfail(strict=True)` and not fixed here: a
source-fingerprint eviction CLOSES ANOTHER SESSION'S BROWSER, silently.** Measured
six times with no exception. The evicting proxy's cold-start lock calls
`singleton._clear_stale_backend` → `_terminate_backend` on the running backend, and
afterwards the browser that backend owned is gone (measured on the pid captured at
spawn; which of the two candidate mechanisms kills it — dying with the terminated
backend, or being reaped as unowned by the replacement's orphan recovery, since
`browser_pid_registry` stamps the BACKEND as owner — was not isolated). The
other session never asked for that and is never told: its proxy log carries no
condemnation, no heal and no teardown — only transient strikes that reset themselves
(usually one `probe failed 1/3`; once `2/3`). The proxy's FAST death witness is
port-only: `backend_watchdog.watch_liveness` probes with
`singleton._backend_http_ready`, which asks the port and not the identity, and the
replacement binds the SAME port and answers it — so the three strikes that would open
the confirmation phase never accumulate and the confirmation that IS identity-scoped
(`_same_identity_backend_ready`) is never reached; the streamable-HTTP bridge is
per-request, so nothing "breaks" for `_confirm_bridge_verdict` either. What actually
died is the MCP SESSION, which nothing watches, leaving `proxy_selfheal`'s entire
recovery unreachable on the most common way a backend goes away. The client-visible
half varies (5 of 6 runs: every later call answers
`{"code": 32600, "message": "Session terminated"}`; 1 of 6: the session kept answering
over a backend that no longer had its browser), so the node asserts the half that did
not vary. The eviction itself converges — exactly one wave in all six runs, and the
fleet ends on exactly one live recorded backend, asserted by a sibling node whose
fixture first proves with the product's own `_source_fingerprint` that the two sides
really differ — but only because the loser never notices, not because any rule makes
a ping-pong impossible. Two proposed universal rules are in `CONTRIBUTING.md`.

`tests/release_gate_harness.py`'s `_pick_free_port` no longer hands an isolated
workspace whatever loopback port the OS assigned: it refuses the product's default
singleton port and every port the developer's REAL `~/.stealth-mcp/server.json`
records, and retries (raising after 32 picks rather than guessing). The ephemeral
range covers both, and a throwaway backend squatting a live backend's recorded port
while that backend is down would be adopted by the developer's next real proxy and
die at workspace teardown — the suite handing out the very `CONNECTION_CLOSED` it
exists to eliminate. The record parse is now ONE helper (`_backend_entries`) shared
with `_backend_pid_from_state`, and `tests/test_release_gate_harness_ports.py` pins
the pick hermetically.

### Fixed — concurrent selector resolution on one tab crashed with `-32000` (F-884)

Three concurrent `wait_for_element` calls against ONE tab raised
`ProtocolException: DOM agent hasn't been enabled [code: -32000]`. Deterministic, not
flaky: at three concurrent resolutions 5 of 15 failed on every round, at five 15 of 25,
and through the real tools a mixed batch of fifteen CSS/XPath/wait calls lost **14**.

Three links. `DOM.getDocument` does not merely read the document — it resets this CDP
session's node-id bindings (measured on Chrome 152: a session's own re-fetch kills the
ids it just handed out, while a *second* connection to the same target leaves them
alone, so the table is per session, which is per `Tab`). Every resolution is
`getDocument` followed by a query using the id it returned, so two overlapping ones
always lose. nodriver then answers that `ProtocolException` by sending `DOM.disable()`
*before* re-raising, and that send fails with `"DOM agent hasn't been enabled"` once a
sibling already disabled the agent — **replacing** the stale-node text. And
`element_resolution`'s bounded retry, which would have absorbed the first link on its
own, classifies on that text and so never ran.

The fix is not a wider marker list — the wording is Chrome's and not a closed set (the
XPath path raises `"DOM agent is not enabled"` from the same cause). It is to stop
generating the race: `element_resolution` now gives each tab one `asyncio.Lock`, held
across each resolution ATTEMPT, so `getDocument` and the query that uses its node id are
atomic per tab. Scope is the tab object because the state it guards is; the key is
`id(tab)` with a `weakref.finalize`, because nodriver's `Connection` defines `__eq__`
without `__hash__` and a `Tab` is therefore unhashable.

A fourth site had to move for the fix to hold: `Element.update()` is a `DOM.getDocument`
too, and `dom_handler.query_elements` called it once per returned element — one listing
of twenty elements reset the table twenty times while a sibling was mid-query. Both
`update()` call sites now go through `element_resolution.refresh_element`, the one home,
under the same lock. A lock inside `element_resolution` alone was measured and still
lost 1 of 15.

**The waiting moved out of the lock**, and that half matters as much as the lock. nodriver
bundles a poll loop into `select`/`find`/`select_all`/`xpath`, so holding the lock across
one froze every other DOM call on that tab for its 10 s default no matter what the caller
asked — measured: a `wait_for_element` given a **one second** timeout held the tab 10.5 s,
a sibling `query_elements` went 0.25 s → 10.56 s, four concurrent absent resolutions blew
the tool's own 30 s budget, and a waiter answered `False` about an element a concurrent
click *would have created*, because it starved that click. So `element_resolution` now
owns the wait: one locked single-shot query, then a poll with the lock released, bounded
by the caller's own deadline. `wait_for_element` passes `timeout=0` inward because its own
loop is the wait — it used to nest nodriver's 10 s default inside a 1 s budget.

The result beats the pre-fix baseline on every axis measured: three concurrent resolutions
of a present selector 0.122 s → 0.005 s with failures 5/15 → 0/15; the starved waiter
`False @ 10.71 s` → `True @ 1.02 s`; the sibling query 0.25 s → 0.01 s; four concurrent
absent resolutions from 2 answers + 2 crashes to 4 answers at ~10.2 s each. Two costs are
named rather than hidden: one locked round-trip pair at a time still delays a sibling by a
slow query's own duration, and a genuinely absent **XPath** now waits 10 s rather than
nodriver's 2.5 s, because CSS and XPath were given one budget. Full argument, matrices and
residuals in `audit/stage2/finding_F884_concurrent_dom_queries.md`.

One swallow had to be re-made rather than inherited. Resolving XPath through
`find_elements_by_text` instead of `Tab.xpath` sheds that method's `dom.enable()`
prologue, **not** its `dom.disable()` — traced on Chrome 152, the XPath path's commands
are `getDocument, performSearch, getSearchResults, discardSearchResults, disable`, and
that disable is nodriver's own last statement, sent bare, with the answer already built.
`Tab.xpath` wraps exactly that call in `try/except ProtocolException: pass` and comments
that it "sometimes raises"; calling `find_elements_by_text` directly lost the guard, so a
failing disable would have discarded a resolution that SUCCEEDED — the same masking shape
this finding removes. `_xpath_matches` now catches it, named and bounded to ONE repeat of
the search (the answer went with the exception, and the search is an idempotent read whose
own `getDocument` re-enables the agent). It is deliberately not a `_STALE_NODE_MARKERS`
entry and not a `recoverable_race`: that error is the mask, it says nothing about whether
the query raced, and treating it as one would restore the old behaviour by the other door.
Also on this pass, `resolve_elements` gained the same `timeout` its three sibling
resolvers have — it could not offer one before, because the wait was `select_all`'s and
bundled into the query.

**F-805 is half fixed as a side effect**, and its finding and strict-xfail node now say
which half. `wait_for_element(timeout=2000)` against a selector that never resolves cost
~10.5 s and now costs ~2.03 s, because the tool's own loop is the wait and it asks
`element_resolution` for exactly one query. The xfail does not flip, because its other row
calls `click_element` with no timeout and so spends that tool's own 10000 ms default —
honoured, not ignored (measured: `timeout=2000` answers in 2.03 s). The one branch that
still ignores a timeout its caller declared is `click_element(text_match=...)`, which
reaches `resolve_by_text` with none, so a declared 2000 ms costs 10.19 s; that is now
named in the finding as what remains open. Separately, six tools expose no `timeout` at
all and inherit the 10 s default — a surface question, not a defect.

### Fixed — a cold start no longer closes another session's browsers (F-886)

**The two symptoms operators report — "my browsers randomly closed" and "the MCP
server disconnected mid-session" — were one cause.** A stdio proxy starting up
next to a backend it would not adopt terminated it, and the decision consulted
only IDENTITY: `backend_registry.fingerprint_mismatch` answers "these two source
digests differ", never "that one is busy". Two clients running the same released
version off different source bytes — a `uvx @latest` session beside a `uv tool`
install, an editable checkout beside either — each read the other as stale, and
whichever started second killed the one already working.

The browser did NOT die with its backend. Measured at 0.25 s resolution: the
incumbent backend was terminated at t+9.11 s and its browser was still running
4.43 s later, until the REPLACEMENT's orphan recovery reaped it
(`process_cleanup.recovery: Killed 1 orphaned browser processes`) — an owner we
had just killed ourselves is indistinguishable from one that crashed last week.
Meanwhile the surviving proxy never learned: both of its death witnesses are
PORT-scoped and the replacement binds the same port, so the watchdog's strikes
never reached three and the per-request bridge never broke. Its later calls
answered `{"code": 32600, "message": "Session terminated"}` with no condemnation,
no heal and no teardown in its log.

**The rule, new module `embedded/backend_eviction.py`:** a backend that is one of
ours, running, of an identity we would not adopt, and still owning at least one
live browser is PROTECTED — never terminated, never bound over. The arriving
client spawns its own on a fresh port and both sessions keep their browsers.
`singleton` asks it at the bind site (`_select_backend_port`) and again at the
kill site (`_clear_stale_backend`). An IDLE stale backend is evicted exactly as
before, so the edit-source-get-a-fresh-backend flow (issue #14) is unchanged in
the case it actually happens in; `stop` and `restart` are deliberately ungated,
and our own wedged backend is still replaceable.

`server.json` is now **schema v3** — `backends` is a LIST, so one display context
can hold one backend per identity. v2 (every 2.0.4-2.1.8 record, key still
authoritative for the display context) and v1 still read, so an upgrade adopts
the running backend rather than evicting it. `record_backend` supersedes by port
and by (display context, identity), so our own respawn still replaces our own
entry and nothing accumulates. `forget_backend` is deleted — `forget_entries` was
already the entry-precise sibling, and `stop_backend` now forgets the one entry
it stopped, so a sibling identity survives a `stop`.

Port selection picks the backend recorded for **our own identity** on this
desktop, falling back to that context's first entry only when we have none.
Ours-first is load-bearing rather than tidy: on a two-identity desktop the first
entry is the stranger's by construction, and targeting it made `restart` step
aside from the stranger, spawn a third backend on an OS-assigned port and leave
your own wedged backend running — with its record entry then superseded away, so
`status`, `doctor` and `cleanup` could not see it either.

`status`'s `others` line now compares entries on (display context, port) rather
than display context alone, so the second backend on your own desktop is named
rather than silently omitted, and each is labelled `context:port`.

The proxy-lifecycle report for a source-change eviction is sent after the
decision, not before it, so a refusal to evict no longer reaches the log and
Sentry as an eviction that never happened.

**Upgrading a machine, not just a session.** The protection lives in the
*arriving* client, so a 2.1.8-or-older install on the same machine still
terminates a backend that is serving — and it reads the new `server.json` as no
backends at all, which routes it straight to that eviction. Until every install
on a machine is 2.1.9 or newer you will still see browsers close. Check with
`uv tool list` and any pinned `uvx` version in your MCP client configuration; the
symptom and the check are in `RUNBOOK.md` under "Two backends on one desktop".

Measured on the real fleet (`tests/test_e2e_lifecycle_resilience.py`, `S5`):
before, 7 of 7 runs evicted a backend, killed the loser's browser and left it
answering `Session terminated`; after, zero eviction waves, zero lifecycle
incidents, both browsers alive and both sessions served. The `xfail(strict)` on
`S5b` is removed. Full write-up:
`audit/stage2/finding_F886_eviction_kills_sibling_browsers.md`.

## 2.1.8

### Fixed — `navigate(wait_until="load")` returned before the page had loaded (F-881)

Two Windows full gates failed the same way on unrelated PRs: `navigate` to a `data:`
page whose `<title>` is in its own URL answered `title: ""`. Measured on Chrome 152:
the tool's `load` wait was `tab.wait(cdp.page.LoadEventFired)`, and nodriver 0.47's
`Tab.wait(t)` takes a *duration* — a class is truthy, so the whole wait was skipped
(0.02 ms). `domcontentloaded` was the same no-op. What stood in for a wait was the
`tab.get(url)` before it: `Page.navigate` plus a `Tab.wait()` that, with nothing having
enabled the `Page` domain on the tab, saw no event and slept a flat 0.5 s. Every
navigation paid that half second, and on a loaded runner it was not enough for the
parser to reach `<title>` before `document.title` was read.

`navigation_milestone` is the ONE home for `wait_until` now. It arms a
`Page.lifecycleEvent` listener BEFORE sending `Page.navigate` (the response and the new
document's events are not ordered — `DOMContentLoaded` was measured 0.3 ms before the
response, `load` 0.3 ms after), and returns when the event named for **that response's
`loaderId`** has been seen, whether it arrived before the response or after. An older
document's `load` does not count; a same-document navigation (`loaderId: null`, no
events at all) returns at the response; a navigation Chrome could not perform commits
its error page under the same `loaderId` and fires `load` for it, so the F-802/F-833
`chrome-error://` detector is unchanged. `networkidle` is still F-787's fixed sleep —
now after the committed document rather than after the 0.5 s — and that finding stays
open. An unknown `wait_until` raises naming the three accepted values instead of silently
meaning `load`.

Both directions of the change are visible. Faster: a `data:` navigation answers in
~20 ms instead of ~525 ms, and after `load`. Slower, deliberately: a page whose `load`
takes longer than ~0.5 s now makes `navigate` wait for it, up to the budget; and a
committed page that never reaches `load` — an open transfer, a hanging subresource —
under the default `wait_until="load"` used to return `success: true` at ~0.5 s and now
times out with the existing message. That second class is pinned hermetically but is
**untested on Chrome**: the resilience suite characterizes the hang-after-headers route
under `networkidle` only. Likewise `net::ERR_ABORTED` (a download, or a navigation
superseded before commit, including a JS/meta redirect that commits before the first
document's `load`) commits nothing for the loader being waited on, so it runs to the
budget and raises the timeout instead of answering with the previous page's url and
title.

What a timeout costs is drawn at acceptance. `navigate`'s one stale-tab recovery (a
`TimeoutError` on attempt 1 → `_replace_main_tab`, which CLOSES the caller's tab and
re-navigates with a full second budget) now applies only to a `Page.navigate` Chrome
never answered — the hang-before-headers shape, where the tab may indeed be stale. A
timeout after `Page.navigate` answered is the page's own — slow, never loading, or a
download — and is reported once, on the caller's tab, within one budget; retrying it
would have discarded a page that exists, triggered a download twice, and cost 60 s by
default.

### Fixed — `scroll_page` returned `true` for a scroll that had not happened (F-875)

`DOMHandler.scroll_page` ended in one `tab.evaluate`, a fixed
`asyncio.sleep(0.5 if smooth else 0.1)` and an unconditional `return True`,
documented as "True if scrolled successfully". `True` reported that the evaluate
had not thrown. Measured against real Chrome 152 on 2.1.6, three different
states wore that one word: the scroll arrived; the scroll was **still in
flight** (4910 of 7039 px on an 8016 px document when the nap ended, 1747 of
1815 on a real stackoverflow page); and there was **nothing to scroll at all** —
a Cloudflare interstitial exactly one viewport tall, `scrollY` `0` before and
after, `true` returned. A caller that read elements after it read the wrong
viewport, and the shortfall grew with the page, which is exactly the
lazy-loading case `direction="bottom"` exists for.

The nap is a **settle** now and the bool is a **record**. `scroll_page` reads
the page's scroll offsets and extent before the scroll, then scrolls and waits
for the page itself to say the scroll finished — bounded, not slept through —
and answers
with `scrolled` (the scroll OFFSET changed), `at_edge` (the page is as far as
`direction` goes), `settled` (the offset stopped moving inside the budget),
`settle_seconds`, the requested `direction`/`amount`/`smooth`, and
`scroll_x_before`/`scroll_y_before`/`scroll_x_after`/`scroll_y_after`/
`max_scroll_x`/`max_scroll_y`. Both axes, because `direction="right"` moves X
and a Y-only record would call a working horizontal scroll a no-op.

Offsets and extent are never compared together — a lazy-loading page grows its
document while standing perfectly still, so comparing whole readings would call
that growth a scroll (with identical `scroll_y_before`/`scroll_y_after` in the
same record) and would stop a still-loading page from ever settling. The extent
reported is the final read's.

A page with nothing to scroll is **reported**, never raised — `max_scroll_y: 0`,
`scrolled: false`, `at_edge: true` — because a one-viewport document is a
legitimate page. `ToolError` is still raised only for operational failure, and
only once (the leaf's own message is no longer re-wrapped): an invalid
direction, a **negative `amount`** (both rejected before any round trip —
`amount` is a distance and `direction` is the only thing that carries a sign, so
the old `down, -500` that silently scrolled up and the old `up, -500` that died
as a JS syntax error are now one clear refusal), and an evaluate that did not
answer with the JSON the read asks for.

**What ends the wait is `scrollend`, not the reads.** Stopping when two
consecutive reads agree on the offset is a guess about timing, and it is wrong:
a plain document smooth scroll runs on Chrome's compositor thread while
`window.scrollY` is read on the main thread, so a blocked main thread makes the
reads go stale while the scroll keeps going. Measured on Chrome 152, the run of
agreeing mid-flight reads lasts exactly as long as the renderer's long task
(120 ms → 121 ms, 250 → 250, 400 → 400) — unbounded, so no read count and no
fixed quiet window can see through it. The scroll now arms a one-shot
`scrollend` listener in the same round trip that performs it, and the position
read reports that latch: `scrollend` latches, so jank can only delay when the
end is observed, never fake it. A scroll that moves nothing fires no `scrollend`
at all, so the same round trip also answers "will this move anything",
synchronously — which is what keeps an instant scroll and a one-viewport page on
the 0.12 s fast path.

The read and the settle live in the new leaf `embedded/scroll_position.py`,
which also holds the one table for what a direction means (its axis, its edge
and its JS). The read is one `JSON.stringify` round trip off
`document.scrollingElement` — the element CSSOM View says `scrollTo` moves, and
now the same element `bottom` targets, so the destination and the reported
`max_scroll_y` cannot disagree. Measured with the fix on an 8000 px `data:` page
in a 977 px viewport: a smooth scroll to the bottom answers after 1.59 s at
7023/7023 instead of at 0.5 s and 70 % of the way, an instant scroll answers in
0.126 s (against the old nap's 0.109 s), and the one-viewport page answers in
0.119 s with `scrolled: false`.

This is a tool **schema change** — `scroll_page`'s `output_schema` in
`tests/goldens/tool_surface.json` moves from FastMCP's `_WrappedResult`
`{result: boolean}` to the `{type: object}` every other dict-returning tool
already serves. A caller that treated the old `true` as proof must read
`scrolled` / `at_edge` / `settled` instead.

### Fixed — `paste_text` and `click_element` reported a dispatch, not a result (F-876)

The two tools F-873 named and left. Measured through the product code path on
Chrome 152.0.7977.83, headless, on a throwaway profile.

`paste_text` sent one `Input.insertText` and returned `True` without asking the page
anything. Five of seven controls take that insert and move nothing — `readonly`,
`range`, `color`, an `<input type="date">` on this build, and a non-editable `<div>` —
and the tool answered `True` for all five. It now reads the field back through the same
`text_entry` leaf `type_text` uses, with the baseline taken **after** the clear, and
raises naming the selector and two counts and never the pasted text (the field may be a
password box). An empty paste is not a refusal and is not checked.

`click_element` returned `True` for a click the target never received, in six shapes:
an overlay above it ate the click (the page logged the overlay, not the button); a
`disabled` control, a `pointer-events:none` target, a zero-size target and a
`visibility:hidden` target each received nothing; and an off-viewport target took a
click at negative coordinates that reached nobody. A `display:none` target went
further — `Element.mouse_click` raises there, so the tool silently fell back to the
in-page `el.click()`, an **untrusted** click, in a tool whose whole point is trusted
input.

**`click_element` now returns a record instead of a bool** — `{"selector",
"dispatch", "point", "size", "target", "hit", "hit_is_target", "reason"}`. `dispatch`
is `"coordinate"` or `"synthetic"`, so the fallback is labelled rather than silent;
`hit` is the tag/id/classes (never the text) of whatever `document.elementFromPoint`
returned at the exact point the click went to; `reason` is `null` when the click
reached the target and otherwise one of `not-rendered`, `off-viewport`, `zero-size`,
`not-visible`, `pointer-events-none`, `covered`, `disabled`. The point is the centre of
`getClientRects()[0]`, which is byte-equal to the point nodriver clicks — the bounding
box's centre is 28.5 px off for an element wrapped over several line boxes and would
name a point the click never used. The synthetic fallback is kept, because for a
`display:none` element it is the only thing that reaches it at all.

The tool deliberately gains **no** "did the page react" oracle and raises for none of
the six shapes: a navigation, a mutation or a fetch may all legitimately be absent
after a correct click, so the record reports what is decidable and the caller decides.
A new leaf `embedded/click_target.py` owns the one read and the closed reason set.

Also corrected: the refusal message shared by both text tools says "entered" rather
than "typed" (`paste_text` reaches the same controls through an insert, not keys) and
no longer names `date` — the local build refused digits into a date field but PR #110's
gate measured all three CI cells accepting them, so it is build-dependent and is not
claimed.

### Fixed — `select_option` selected nothing and said it had; `upload_file` counted the request, not the files (F-877)

Measured on Chrome 152.0.7977.83 through the product code path, on a throwaway
profile. The two tools F-876 named and left unmeasured, and the same sentence a third
and fourth time: the tool reported the success of its own dispatch, not of the
interaction.

`select_option` answered `True` for **eleven** cases that did not select what was
asked, and three of them were worse than a silent no-op. A `value=` that names no
option does not merely fail — `select.value = …` sets `selectedIndex` to `-1`, so it
**cleared the selection the page already had**, fired a `change` announcing it, and
reported success. A selector pointing at an `<input type="text">` had its `value`
**written**, because nothing checked that the element was a `<select>` at all. And the
`text=` arm was `send_keys`, so its real consumer was Chrome's `<select>` typeahead —
a live buffer with a ~1 s timeout shared with the previous call, searching from the
option after the current one, resolving a `label=`/text collision onto the wrong
option — which on a `<select disabled>`, a control that cannot take focus, sent the
characters wherever focus already was: measured, a request for the disabled select
moved a **different** select on the page, with a trusted `input`+`change` pair, and
the tool answered `True`. An out-of-range `index=`, a `text=` matching nothing and an
empty `<select>` each changed nothing and answered `True` too.

**`select_option` now returns a record instead of a bool** — `{"selector", "by",
"selected_index", "selected_count", "option_count", "multiple", "changed"}`. It reads
the options first, resolves the criterion to an index in one stated rule (exact
`option.text`, then exact `option.label`, then a case-insensitive prefix over either,
skipping `disabled` options — the prefix tier is kept because the typeahead had it and
callers may rely on it), refuses before writing anything when nothing matches, then
sets `selectedIndex` and dispatches `input` **and** `change` (the pair, and the order,
Chrome's own typeahead produces; the shipped arms fired `change` alone, and fired it
even when nothing moved). The control is read back after those handlers have run, so a
page that resets the select inside its own `change` handler is caught. Nothing types,
so a request can only ever move the control it named. `changed: false` is a success —
"it was already on that option" and "it refused" are different facts a bool could not
tell apart.

`upload_file` was honest in ten of eleven measured cases — every "resolved to the wrong
thing" case already raised — but its answer was composed from the caller's own argument
list before the CDP call and regardless of it. Two paths into an input with no
`multiple` attribute: `DOM.setFileInputFiles` **succeeds** (the raw call reports no
error) and Chrome keeps only the first file, while the tool reported `count: 2`.
**It now returns** `{"selector", "requested", "attached", "multiple", "total_bytes"}`,
read from the input's own `FileList`, and raises when the input holds a different
number of files than were sent. The old `uploaded` field is gone: it echoed **absolute
local paths**, which name the operating user, into a value that travels to the client.

No message either tool raises carries an option's text or value, a file path or a file
name — a `<select>` is frequently a list of account numbers, and a raised `ToolError`
reaches the caller, the debug ring and Sentry at once. A new leaf
`embedded/control_state.py` owns both reads, the matching rule and both verdicts.

**What this costs, plainly.** The `text=` arm's events are now **untrusted**
(`isTrusted: false`) where real keystrokes produced trusted ones — there is no trusted
alternative that is not the typeahead being removed, and the `value=`/`index=` arms
were already untrusted, so a page gating on `event.isTrusted` was already unreachable
through two of three arms and is now unreachable through all three. A caller relying on
the typeahead's **wraparound** — asking for a prefix that matches the option already
selected in order to advance to the *next* match — now gets the current option and
`changed: false` instead of a move; that behaviour was never documented and is not
kept. And an `index=` inside a `<select>` with more than 2000 options cannot be
resolved, because the option read is bounded; that case gets its own message naming the
cap, never "no option matches".

Also fixed in passing, the same leak class one guard earlier: `upload_file`'s
"File not found" now reports the path's position, the count, its length and its suffix
instead of the absolute path, which named the operating user in an error that reaches
the client, the debug ring and Sentry.

Deliberately unchanged, and named in the finding: a `multiple` `<select>` still cannot
be driven past one selection (the signature takes one criterion — the record now says
so); a `disabled` `<option>` stays reachable by `value=`/`index=` and unreachable by
`text=`, which is what Chrome does; and `upload_file` still attaches to a `disabled`
input and still ignores `accept=`, because CDP does.

### Fixed — `scroll_page` could not move an app shell (F-878)

A page laid out as `body{overflow:hidden}` plus one scrolling `div` — the
default of every SPA starter template, and where infinite feeds, virtualised
lists and lazy loading live — is not scrolled by `window.scrollTo` at all.
F-875 made that visible (`scrolled: false`, `max_scroll_y: 0`) rather than
silently wrong; this makes it work. `scroll_page` now picks the page's **real**
scroller and drives that.

Which element is "the" scroller when several overflow was measured before it was
decided: twelve `data:` fixtures through the product path against real headless
Chrome 152. `document.scrollingElement` alone — what the tool used until now —
is right **4 times out of 12**. The two obvious heuristics score 9 and 8: the
largest-area rule with a coverage floor finds nothing in a three-column mail
layout and lets a scrollbar's width (0.8 %) rank a `body` that can move 20 px
above a shell that can move 7023; the element-under-the-viewport-centre rule
walks INWARD to a page's own data grid and is blind behind a `position:fixed`
scrim. The rule that ships scores 12/12: **if the document scroller can move on
the requested axis it IS the scroller** (so a plain page, a quirks-mode page, a
scroll-snap page and a nested box inside a scrolling document are all unchanged,
and the scroll call generated for them is the same `window.scrollTo` /
`window.scrollBy` it always was), otherwise the
element with the largest viewport-clipped area that can move on that axis, near
ties broken by the larger extent. The axis comes from the direction, so
`direction="right"` now finds a horizontal-only strip that neither heuristic
could see.

The record grew two fields, because the pick is a judgement and a caller is
entitled to see it: `scroller` (`{tag, id, classes}` of the element that was
actually driven — shape only, bounded in the page itself) and
`scroller_is_document`. The `scroll_page` **description** in
`tests/goldens/tool_surface.json` changes with them; the input and output
schemas do not, and no other tool moved.

Driving a nested element also moves the end-of-scroll latch F-875 introduced, and
where it goes is not a matter of taste: measured on Chrome 152 across the same
twelve fixtures, an ELEMENT scroll's `scrollend` fires **at that element** — for
smooth and instant alike, on both axes, including a scroll-snap container — and
does **not** bubble to `window` or `document`, while a DOCUMENT scroll's fires at
`document`/`window` and never at `document.scrollingElement`. There is one right
target per scroller kind and both wrong choices fail the same silent way: the
listener never fires, the settle burns its whole 10 s budget, and the tool reports
`settled: false` about a scroll that finished in a second. So the listener is armed
on the very expression that receives the scroll. No scroller kind needs the
degraded read-agreement path; that still exists only for a browser without
`onscrollend`. Re-measured end to end on all twelve fixtures after the change:
every one picks correctly, settles, and lands at its true final offset, in 0.50 s
to 1.61 s.

### Fixed — the headed desktop hand-off could send a `/TR` schtasks truncates (F-879)

`schtasks /Create` stores at most **253** characters of `/TR`, drops everything
past that and **exits 0** — measured under F-867 on Windows 11 10.0.26200, and
eight characters short of the "~261" the documentation gives. F-867 guarded the
backend's own scheduler rung against it and left the headed browser hand-off
(F-810, `desktop_launch`) named as the remaining exposure: that path composed its
`/TR` with no length check at all.

What it cost, had a machine hit it: the task is created and reported successful,
then names a launcher script whose path lost its tail, so at run time it fails
with Last Result 2 and writes to no log anywhere. `launch_and_attach` then polls
for the whole 20 s readiness deadline and raises an error blaming the DevTools
port — the one component that was never involved.

Chrome's own command line was never the problem and has not moved: the launcher
*script* already carries its argv, which is why a 400-character profile path and
a proxy's worth of switches cost `/TR` nothing. What spends the budget is the
state dir, and that is what is now checked — before the launch directory is
created, so an impossible layout costs no directory, no scheduled task and no
deadline. The error names the measured cap, the actual length and the path that
is long.

The cap has one home and it is the `schtasks` seam itself: `TR_MAX_CHARS`,
`TOKEN_CHARS` and `tr_overflow` now live in `desktop_launch` beside `_schtasks`,
and `backend_launch` reads them there at call time exactly as it already reaches
there for `_schtasks`, `_cleanup` and `_read_pid`. It carries no `253` of its
own, and the comparison is single-homed too, so the two composers cannot drift on
the cap or on its inclusive boundary. The headed path *raises* where the backend
rung *drops a rung*: the backend has a plain spawn to fall to and a killable
backend beats none, while a delegated headed launch has no fallback at all. The
per-attempt token here is 12 hex characters now rather than 32, which is 20 more
characters of headroom and one spelling of the token length instead of two.

Not verified without a real `schtasks`: the 253 figure is F-867's measurement,
carried over unchanged. Everything this change adds is asserted hermetically
against the faked seam — no test creates a scheduled task.

### Fixed — nothing ever forgot a dead backend record (F-880)

`~/.stealth-mcp/server.json` had a writer for every backend that arrived and none for
any that left. On the maintainer's machine it held three entries: a live backend
(`win-session-1`, 2.1.6) beside two whose ports had no listener and whose recorded pids
had not existed for days (`win-session-2` at 2.1.1, `headless` at 2.1.3). The only rule
that ever removed anything was `record_backend`'s supersede-by-port, which by
construction only touches the port being claimed — a dead sibling on another port stayed
for good. F-868 had already stopped such an entry being *reported* as the backend;
what was left was a record that only grows, a cold-start probe against ports nobody
listens on, and a `doctor` listing naming backends that do not exist.

`backend_liveness` gains the ONE deadness rule (`survey` / `dead_entries` /
`forget_dead`), and it takes **two witnesses**: the port must probe `down` AND the
recorded pid must not be a running backend of ours. Neither alone will do. A backend is
recorded at Popen time, *before* it binds, so a sibling is `down` for the whole of its
cold start while its process is alive — the socket alone would race every cold start on
the machine. And "the pid is gone" says nothing about whether the port is free (F-868
§6's stated objection), which requiring `down` answers directly: the port has just been
observed to hold no listener at all. A **wedged** backend is therefore never dead — it
holds its port, `restart` is its verb, and its record is how the eviction path finds a
pid to kill — and an entry whose `port` is not an int is reported, never forgotten.

The write is `backend_registry.forget_entries`, which re-reads the record and drops only
entries still matching on display context **and** port **and** pid, so a context
re-recorded while the probe was running survives. It never unlinks the file: forgetting
the last entry leaves a readable empty record, exactly as `forget_backend` does.

Two operator-facing changes. `cleanup` prints a `backend records:` line — how many are
recorded and how many are dead — and `--apply` forgets them; it is the disk-hygiene verb
and a record naming nothing is residue. `doctor` marks each dead line `(dead record)`,
names them in one summary line and points at `cleanup --apply`; it stays read-only.
Both read the SAME survey pass, so a wedged sibling costs one probe per run rather than
one per question.

Display context is deliberately not consulted: a `down` entry belonging to another
desktop is forgotten like any other. Adoption's asymmetry (F-808) exists to stop a
client *reusing* a foreign desktop's live backend; it has nothing to say about one whose
process is gone.

`cli._probe_recorded_backend` is deleted — its whole content was the ladder plus the
word `no port recorded`, and that word is `backend_liveness.NO_PORT` now.

## 2.1.7

### Fixed — `list_instances` reported the last navigation, not the instance (F-874)

Measured on 2.1.6 over real stdio with ten headed browsers (Chrome 152, Windows
11): `list_instances` said `current_url: "https://www.youtube.com/"` /
`"YouTube"` for an instance whose active tab was at
`…/results?search_query=lofi+hip+hop+radio`, and `get_active_tab` on that same
`instance_id` answered correctly in the same second. Three more instances the
same way — a tab that had been `switch_tab`'d away from, a title the page set
after load (`title: null` for a titled Amazon page), and a `data:` URL whose
`title` was still the Wikipedia page before it. `BrowserInstance.current_url` /
`.title` had exactly two writers in the tree, the spawn and the `navigate` tool,
so every other way a page can move — an in-page click, a script navigation, a
redirect, `switch_tab`, a late `document.title` — left the tool reporting a
moment that had passed. Nothing raised; the record was well-formed and the field
was named `current_url`.

The read now has one home. `embedded/tab_identity.py` owns the
`{tab_id, url, title, type}` record and the `Target.getTargets` refresh in front
of it, and `list_tabs`, `get_active_tab` and `list_instances` all go through it —
the first two had been writing the same four expressions out by hand, and the
third had not been asking at all. The refresh is a real round trip rather than a
read of `tab.target` as it stands, because that metadata is only as fresh as
whatever `Target.targetInfoChanged` nodriver has already processed;
`get_active_tab` loses its `await tab` in the trade, which refreshed nothing,
cost a 0.5 s floor and raised `TypeError` on a rediscovered target (F-771).

An `active` record now carries the LIVE `current_url`/`title` and
`partial: false`. If that read fails it carries `partial: true`, a
`detail_error`, and the last navigation's values under the names
`last_navigated_url` / `last_navigated_title` — and deliberately **no**
`current_url` key, because a cached value under that name is the defect. The
`stored` tier and `get_instance_state`'s two partial records, neither of which
has a live browser to read, carry the same honest pair. `BrowserInstance`'s
fields are renamed to match what they have always held. Each entry is bounded by
the CDP budget and the entries are gathered concurrently, so one wedged browser
costs its own row and one budget for the whole listing, not one per instance.

A second defect on the same cache is fixed with it: `update_instance_state`
guarded both fields on truthiness, so `navigate` reporting `title: ""` — Amazon
sets its title late, a bare `data:text/html` document never sets one — left the
previous page's title standing. It is `is not None` now; an empty title is what
the page has.

`scroll_page` returning `true` for a scroll that has not happened was measured
in the same session and is a different defect with a different remedy; it is
fixed separately, below.

### Fixed — `type_text` reported success for text it never entered, and for an Enter that could not submit (F-873)

Measured on 2.1.6 over real stdio transport, headed Chrome 152: `type_text` returned
`{"result": true}` when the characters never reached the page (Amazon's and Gmail's
search boxes, `.value` still `""` afterwards, three runs each), and `parse_newlines`'s
trailing newline never submitted the form it was typed into. Two defects, one shape —
the tool reported the success of its own dispatch, not the success of the interaction.
The Enter was a `KeyboardEvent` constructed *inside the page* by `element.apply`; an
event a script constructs is `isTrusted: false` and carries no `charCode`, and a
form's implicit submission is performed by Blink on the **keypress** of a trusted
Enter. Measured against a one-input form with a submit listener: the synthetic
keydown submits 0 times, a trusted `rawKeyDown` (which fires no keypress) submits 0
times, and only a `keyDown` carrying `text="\r"` submits — while adding a separate
`char` event on top fires a second keypress and submits **twice**. And nothing
between "dispatch the events" and `return True` ever asked the page whether the
characters had landed: measured on the same Chrome, `readonly`, `range` and `color`
controls each accept every key event and leave their value exactly where it was, and
the tool answered `True` for all three. (`<input type="date">` did the same on this
machine's Chrome 152, but that one is build- and locale-dependent — CI's headless
Chrome accepted the digits on Windows, macOS and Linux alike — so it is pinned as the
invariant rather than as a refusal: never a success over a value that did not move.)
Key presses and the "did the page
take it" check now live in `embedded/text_entry.py`: every key goes out as one
`Input.dispatchKeyEvent` `keyDown` carrying `text` plus a `keyUp` (so `keydown`,
`keypress` and `input` all fire, all trusted — the shipped path sent a lone `char`
event per character, so a page whose autocomplete or shortcuts are bound to `keydown`
saw a value appear with no key pressed), and after each line's characters the
element's own text is read back and compared against the baseline taken just before
them. A control that took every event and moved nothing now raises `ToolError` naming
the selector and the counts — never the typed text, since the raised error reaches the
debug ring and, as the exception itself, the caller and Sentry, and the field may be a
password box. The verification asks "did anything change" rather than "does it contain
exactly what I typed", deliberately: an input mask, an autocomplete that rewrites and a
`number` field that normalises all DID receive the input, and a stricter test would have
turned each into a new false alarm; the cost of the looser rule, named in the finding,
is that a control whose value is legitimately identical afterwards now raises.
`type_text`'s clear fallback also stopped being a no-op — it sent WebDriver's
private-use codepoints (U+E009 for Ctrl, U+E017 for Delete) through `send_keys`, which
CDP has never understood, so all three characters landed verbatim and nothing was
cleared, corrupting the field it was asked to empty; it and `paste_text` now share the
one CDP select-all + Delete.

## 2.1.6

### Fixed — `get_instance_state` reported empty storage as if it were the truth (F-869)

On any page that actually has localStorage or sessionStorage entries,
`get_instance_state` (and the `browser://{id}/state` and `browser://{id}/console`
resources) returned `"local_storage": {}`, `"session_storage": {}` and
`"partial": false`. Measured on 2.1.5 against `https://www.google.com/`: 28
cookies came back, both stores came back empty, and the record declared itself
complete. `nodriver`'s `Tab.evaluate` always sends deep `SerializationOptions` and
hands back `deep_serialized_value.value` raw, so `Object.keys(localStorage)`
arrives as `[{'type': 'string', 'value': 'alpha'}, …]` — measured against Chrome
152 — and the per-key loop raised `TypeError: unhashable type: 'dict'` when it used
one of those nodes as a dict key. An `except Exception` then logged it at INFO as
"Storage access unavailable", the sentence meant for opaque origins, and let the
empty record through; INFO is not error-reported, so the failure reached neither
the caller nor error reporting. The read now lives in `embedded/page_storage.py`
and asks the page for one `JSON.stringify` of both stores — the same idiom F-844
applied to the viewport eleven lines below — which also retires the
`localStorage.getItem('{key}')` string interpolation and 2N+2 CDP round trips. Only
a page that genuinely refuses the read still reports empty storage; anything else
propagates and `get_instance_state` answers with `partial: true` and a
`detail_error`, as its docstring always promised, with a WARNING and a traceback in
the backend log.

### Fixed — `status` reported a dead sibling's record as "the backend" (F-868)

On a machine whose `server.json` recorded three display contexts — two dead
(`win-session-2` on 7169, `headless` on 19222) and one healthy backend serving 56
proxies (`win-session-1` on 52554) — `stealth-chrome-devtools status`, run from that
same session-1 shell, printed `backend : not running` and `pid : 89892`, the pid of a
process that had been gone for hours. `singleton._probe_backend_status` selected the
record with `backend_registry.first_backend`, which under schema v2 is dict insertion
order and carries no preference of its own, while discovery had been walking
`adoption_candidates` — the one home for "which backend would THIS client use" — all
along. The probe now walks that same list and reports the first candidate that answers
(wedged over down when none does), which fixes `status`, `doctor`'s summary lines,
`stop` and `kill-orphans`'s live-backend guard at once, since all four already consumed
it. The CLI status block now also selects once and passes the answer down: the pid and
log lines read the entry on the port just reported (`backend_on_port`) instead of making
their own `first_backend` read, and `doctor`'s port-occupant line takes the same port —
two independent record selections deleted rather than a third added. `status` gained one
`others      :` line naming the display contexts it is NOT speaking about when the record
holds more than one, so a summary over a multi-context record no longer reads as "this is
all there is".

Two things the same selection bug was hiding are fixed with it. The socket→`initialize`
ladder is now `singleton._probe_port`, one home with three callers, instead of four lines
copied into `cli._probe_recorded_backend` under a comment justifying the copy with a claim
about `_probe_backend_status` that this release makes false. And `restart` now reports
that ladder's verdict for **the port it spawned on**: it took its `status` from the
record-wide walk while its `pid` came from the spawned port, so a responsive sibling could
report "responsive" beside the pid of a backend that had just come up wedged — both halves
of one return describing two processes.

Stale records are still pruned by nobody; see `audit/stage2/finding_F868_cli_status_reports_a_dead_record.md` §6.

### CI only — every gate run now measures Chrome's cold start (F-870)

No product change. `tools/chrome_cold_start_probe.py` runs on the `integration`,
`transport`, `offline-stealth` and `install-smoke` cells (Windows included, as
the control) and records how long that runner's Chrome takes to open its
DevTools endpoint over two back-to-back launches — the one number
`audit/stage2/finding_F870_posix_ci_nodriver_connect_failures.md` could not get
from any failure log, because nodriver abandons a launched Chrome after a
hardcoded 2.75 s and discards its stderr.

### Fixed — four cloner aspects returned nested transport nodes, not values (F-872)

`extract_element_structure`, `extract_element_events`, `extract_element_assets` and
`extract_related_files` handed back payloads whose nested lists held Chrome's BiDi
`{'type': …, 'value': …}` serialization records instead of the values they describe.
Measured against real headless Chrome: `structure["children"][0]` was
`{'type': 'object', 'value': [['tag_name', {'type': 'string', 'value': 'span'}], …]}`
rather than `{'tag_name': 'span', …}`, `class_list` was a list of `{'type': 'string'}`
nodes, `assets["images"][0]` and `related_files["stylesheets"][0]` the same — and an
empty JS object (`events["framework_handlers"]`) decayed into an empty **list**.

nodriver's `Tab.evaluate` sends `serialization="deep"` on every call and returns the
deep-serialized value verbatim, so a returned JS object arrives encoded at *every*
depth and `return_by_value` cannot undo it. The engine's tolerance,
`_convert_nodriver_result`, unwrapped the **top level only**: its `"array"` branch
returned the raw list of nodes. Nothing raised — the answer looked right and was
wrong below the first level, which is why the E2E tier (which string-searches the
JSON, and the values ARE in there) stayed green, and why the unit fixture, which fed
a plain dict a real tab can never produce, short-circuited the conversion entirely.

The four aspect scripts now end in `JSON.stringify` and are read back with
`json.loads` — the idiom `extract_element_animations` (F-846), the viewport read
(F-844) and `window_sizing` already use, since a string is the one shape deep
serialization leaves alone. The four per-aspect ladders collapse into one reader in
the new `embedded/js_aspect_answer.py` leaf, shared with the animations aspect;
`_convert_nodriver_result` is deleted. Schemas and field names are unchanged — only
the depth at which the values are real. Also corrupted, and also fixed: the nine
tools composed from these four (three `*_to_file`, `clone_element_complete`,
`extract_complete_element_to_file`, `clone_element_to_file`,
`clone_element_progressive` and its `expand_children` / `expand_events` slices) —
13 of 94 in all. `cdp_element_cloner.py` 1012 → 947 LOC; its grandfathered cap
ratchets down to match.

### Fixed — a JS error inside an extraction script was reported as a wrong type (F-872)

Found while fixing the above, on the same line of code. Every aspect began with
`if hasattr(raw, "exception_details")`, a branch that cannot fire against nodriver
0.47: `Tab.evaluate` returns the `ExceptionDetails` record *itself* in the value's
place, and that class has no `exception_details` attribute. So a genuine JS error in
an extraction script reached the caller as
`Unexpected return type: <class 'ExceptionDetails'>` rather than as the error.

The decode moves into the new leaf, and the message is built from
`.exception.description`, not `.text` — measured against real Chrome, `.text` is the
literal string `"Uncaught"` for *every* throw there is (ReferenceError, TypeError, an
explicit `throw new Error(…)`, a SyntaxError), so a message built from it names
nothing. A caller now sees
`JavaScript error: ReferenceError: nosuchthing is not defined … (line 0, column 13)`,
clamped to 200 characters of Chrome's text — the text is Chrome's, but its length is
the page's. The only test covering this was a hand-built double asserting the
product's own mistaken belief about the library type; it is rebuilt from nodriver's
own constructors, which flipped all five of its cases from green to red before the
fix.

### Fixed — a named profile is no longer silently swapped for `<name>-2` (F-871)

Whether a Chrome profile was busy used to be decided by asking whether
`SingletonLock`, `SingletonSocket` or `SingletonCookie` exists in it. None of
those names means what that assumed. Chrome's lock is a SYMLINK whose target is
the string `<hostname>-<pid>` — a claim about a pid, not a path — so
`Path.exists()`, which follows symlinks, reported the one artefact that names an
owner as ABSENT; while `SingletonSocket` points into a per-launch `/tmp`
directory that a killed browser never gets to clean up, so its target outlived
the browser and reported "busy" forever. After F-860's reaper killed a Chrome
that a failed spawn had leaked, the next `spawn_browser` on the same NAMED
profile therefore read the leftovers as a running browser and walked the caller
to `<name>-2` — a different identity (a fresh clone of the master snapshot the
first time, and thereafter whatever an earlier walk left at that name) for a
profile that exists
precisely to keep its cookies and logins — with nothing in the answer saying so
(measured on the 2.1.5 release gate: `ci-warmup`, `ci-warmup`, `ci-warmup-2`
across three attempts that all passed `user_data_dir="ci-warmup"`). The question
now has one home, `embedded/profile_lock.py`, which reads the lock the way
Chromium's own `ParseProcessSingletonLock` does — a lock naming a dead pid is
orphaned and holds nothing, exactly as Chrome concludes before unlinking it and
starting — and the socket and cookie are not consulted at all, because Chrome
writes them after the lock. A live browser's lock is also visible for the first
time, so a held profile can no longer be handed to a second Chrome. When a walk
does happen the answer now says so, and says it where a caller will actually
read it: `spawn_diagnostics.profile_selection` gains `requested_user_data_dir`,
`walked_to` and `walk_reason` (e.g. "Chrome's SingletonLock is held by live pid
4242") whenever the caller did not get the profile they asked for, and that
reason is prepended to the named-profile `warning` rather than left sitting
beside it. On Windows, where Chrome writes no readable lock and the process scan
is the only witness there is, a scan that cannot be read now resolves toward
"held" instead of "free" — the same direction an unreadable pid already took.

## 2.1.5

### Fixed — the backend escapes the MCP client's Job Object (F-867)

The 2.1.4 publish run's first attempt went red in the Windows transport cell with
the shape F-859 had been chasing for weeks: twelve cold sessions, one backend, and
0.4 s after uvicorn's `Application startup complete` every proxy at once logging
`backend connection lost` — no traceback, no `Shutting down`, a 0-byte fault log.
It was the first such red to carry the backend's own log (F-859 §12), and the
timestamps named the killer: the backend died at the instant the first three of the
twelve sessions finished `tools/list` and closed.

The reference MCP Python SDK (`mcp/os/win32/utilities.py`) puts every stdio server
it starts inside a Windows **Job Object** with `KILL_ON_JOB_CLOSE` and no
`BREAKAWAY_OK`. Our per-session stdio proxy IS that server. The proxy that wins the
cold-start lock spawns the shared backend, and job membership is inherited by
children — `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` say nothing about jobs. So
when that ONE session ends, `TerminateJobObject` — or merely the last `CloseHandle`
— kills every member, and the backend serving every other session is a member.
Verified 3/3 by experiment on both paths. F-866 could not have caught this: it
removed the job the venv **redirector** created, and a job the **client** wraps
around the proxy is a different one, outside the product's reach from inside. POSIX
is immune: clients kill a process GROUP, and the backend is spawned with
`start_new_session=True`, so it is in its own.

How the backend is created is now a subsystem of its own,
`embedded/backend_launch.py`, which climbs three rungs on Windows.
`CREATE_BREAKAWAY_FROM_JOB` is asked for first — free, and correct for any client
whose job permits it. A successful call is **not** accepted as proof: under a nested
job chain the flag leaves the innermost job only, so the new process is asked whether
it is in ANY job. One that is gets discarded microseconds old — but only after the
next rung is known to be available, because a backend that escaped one job beats one
that escaped none; where there is no scheduler rung it is kept, at WARNING, as
`breakaway-partial`. Second,
the creation is handed to Task Scheduler, the job-free intermediary the product
already owns (F-810): a one-shot task runs a stdlib-only launcher under
`pythonw.exe` — no console, so nothing can flash, and the BASE interpreter's, never
a venv's redirector (F-866) — which reads the argv, the entire child environment and
the boot-log path from a JSON spec in the user-private state dir, starts the backend
detached, and hands the SERVING pid back through an atomically written pid file. It
appends the backend's stdout **and** stderr to `backend-boot.log`, so F-303's
property — an import-time crash leaves a trace — survives the hand-off. That rung is
taken only when the spawning process is already in the logged-on console session, so
the backend lands where it would have landed anyway and the display context recorded
for it stays true (F-808: the tool still never PICKS a session). Third and last is
today's detached spawn, unchanged, for a runner with no console session — carrying the breakaway
bit anyway when rung 1 proved it permitted — and its line names F-867 and says the
backend is inside this client's job. Every rung is logged in one spelling, so a
post-mortem reads which one served.

`singleton._start_server_process` keeps the env building, the boot-log rotation and
the state record, and makes one call. `tests/test_backend_escapes_client_job.py`
builds the SDK's job with `ctypes`, has a helper spawn a backend from inside it,
ends the job both ways, and asserts the backend is alive and in no job — RED before
this change, GREEN after, on the `scheduler` rung.

What users see: a Claude Code session ending no longer takes the backend every other
session on the machine is using down with it. What this does **not** fix, named so
nobody reads more into it: a proxy whose session is not the console session (SSH, a
service, session 0, some RDP layouts) falls to the `plain` rung and remains exposed; a
backend that dies for any other reason still **kills** the browsers it owned on the
way back up (unchanged from 2.1.4); and Claude Code's own node client was never
captured using a job — the Python SDK case is the proven one. The Windows herd rate
F-859 measured (~9 % per cell) is *expected* to fall to zero on cells where the
scheduler rung serves; that is a prediction to be re-measured over the next gate
runs, not a result.

Measured on CI (gate run 34842210967, 2026-09-14): the escape pin passed on all three
Windows unit cells and named rung `scheduler` on each, for both the handle-close and
the `TerminateJobObject` session ends. GitHub-hosted Windows runners do have a
logged-on console session, so the pin did not skip and the fix is verified there, not
merely unfalsified. The Windows herd cell passed in 4m58s. That is one green run, not a
rate.

Full write-up: `audit/stage2/finding_F867_backend_inherits_the_clients_job_object.md`.

## 2.1.4

### Fixed — the backend is spawned on the real interpreter, not the venv redirector (F-866)

On 2026-09-13 at 13:32 the backend serving 24 Claude Code sessions — 32 hours
up, its last line a routine hygiene tick — stopped existing: no traceback, no
uvicorn shutdown, an empty fault log, no eviction. Its replacement's orphan
sweep then killed the one browser open on it. The cause was the spawn, not the
backend. In a Windows venv `sys.executable` is CPython's venv **launcher**, not
an interpreter: it re-spawns the real `python.exe` as a child, holds it in a
Job Object with `KILL_ON_JOB_CLOSE`, and — being console-less itself after our
`DETACHED_PROCESS` — hands that child a brand-new console, which the default
terminal shows as a **visible Windows Terminal window titled with the python
path**. Every detach flag, and F-839's SIGBREAK immunity, applied to the
redirector; the process that served every session had its lifetime tied to a
redirector nobody knew existed and to a terminal window anyone could close.
`server.json` recorded the redirector's pid, not the backend's.

The backend is now launched on `sys._base_executable` directly, with
`__PYVENV_LAUNCHER__` naming the venv — the launcher's own hand-off, which
`getpath` honours and CPython then drops from the environment before any code
runs. No redirector, no job, no console, no window; the recorded pid is the
serving process. POSIX venvs have no redirector and are unchanged.
`tests/test_backend_spawn_no_redirector.py` pins the command, the environment,
and — against a real spawn — job membership and console absence.

Not fixed by this, and named so nobody reads more into it: a backend that dies
for any other reason (an upgrade's source-change eviction, a deliberate
`restart`) still **kills** the browsers it owned on the way back up rather than
re-adopting them.

Full write-up: `audit/stage2/finding_F866_backend_spawned_through_venv_redirector.md`.

### Internal — a failed startup herd hands over the backend's own log (F-859 §12)

Three Windows wedge reports in a row (F-859 §9-§11) showed two proxy logs and no
`backend-<pid>.log`, and §9 recorded the absence as a fact about the workspace. It
was the harness: `release_gate_harness._backend_logs` printed the two NEWEST log
files, which in a twelve-proxy herd are always two proxy logs. The one question the
finding could not settle — what the backend saw during the 120 s the proxies starved
in its readiness gate (§7.3) — was answerable every time and lost with the throwaway
home. Test-side only, no runtime change:

* `_backend_logs` includes every `backend-*.log` (per-boot logs and the boot log a
  crash traceback lands in), then the two newest other logs; a journey's report is
  unchanged.
* New `workspace_proxy_warnings`: every proxy's WARNING-or-worse lines, by file,
  newest 120 kept — the fleet's view, which separates "one connection dropped" from
  "all twelve lost the backend in the same second".
* `tests/test_startup_herd.py` attaches the same evidence block (booted-backend
  census, backend logs, proxy digest) to EVERY failure shape, including an exception
  out of a session — the "backend died mid-flight" red of §11 ended as a bare
  `McpError` with nothing attached.
* `tests/test_release_gate_harness_logs.py` pins the selection and the digest.
* Finding §12: the release PR's attempt 1 was red in BOTH Windows cells, the `main`
  push for the 2.1.3 merge is red (Windows herd wedge + a macOS nodriver
  "Failed to connect to browser" that is not the herd), and today's measured rate
  across every attempt: Windows herd cells 5/54, Linux+macOS 0/81, four of the five
  between 22:07 and 23:27 UTC.

## 2.1.3

### Fixed — the wheel installs the dependency versions the gate tested (F-865)

Every direct dependency was already an exact pin, but four packages the source imports
arrive only transitively — `mcp` (through `fastmcp`), `anyio`, `starlette`, `httpx` — and
`requests` was a range. `uv.lock` is a universal resolution, one version per package for
every Python the project supports, so a transitive dependency that needs a newer pin on a
newer Python is held back for ALL Pythons in the lock; a user's installer re-resolves the
wheel's metadata for one Python and takes the newest version that fits. On 2026-09-11 every
gate cell ran `mcp 1.27.1` while `uv tool install stealth-chrome-devtools-mcp==2.1.2` on
Python 3.12 installed `mcp 1.30.0` — a library whose private session manager the F-862
reaper subclasses, and whose 1.30 line changes session behaviour (a session is forgotten on
the client's DELETE, a 4 MiB request-body cap, a 30-minute idle timeout) that no test had
seen. The five packages are now pinned in `[project.dependencies]` at the locked versions,
so what installs is what the gate ran. `tools/check_pinned_imports.py` (pre-commit and the
quality cell) fails when a third-party import is unpinned or its pin disagrees with
`uv.lock`; CI installs with `uv sync --locked`, so a lock that lags `pyproject.toml` fails
too. Moving a dependency is now a deliberate commit: change the pin, run `uv lock`, and
the gate tests the new version before anyone installs it.

## 2.1.2

### Fixed — the backend no longer keeps every abandoned MCP session forever (F-862)

A backend serving 62 Claude Code sessions reached 6.7 GB resident in 18.5 h with five
browsers, empty body stores and bounded request rings. The growth was MCP sessions:
every stdio proxy's watchdog opens a throwaway session on the backend every 2 s (a real
`initialize`) and DELETEs it best-effort, and the MCP layer NEVER unlists a session: a
DELETE only marks its transport terminated (so the id answers 404) and the list is pruned
solely on an idle timeout FastMCP never sets. Every probe therefore left 2–7 KB listed for
good, and every probe whose DELETE was lost under load — or any proxy that died — left the
whole session behind at 0.12 MB (0.4 MB with a `tools/list`). At 31 probes a second that is
two million listings a day: the 6.7 GB, with no lost DELETE assumed. Measured hermetically
with `tools/probe_backend_memory.py`: abandoned sessions grow the backend linearly at 0.12
MB each, DELETE'd ones at a few KB each; navigation and tool-call churn barely move it.

The new `embedded/session_hygiene.py` leaf makes the backend defend itself:
`HygienicSessionManager` sweeps every 30 s and unlists any session that has no
standing GET event stream (the MCP client opens one right after `initialize` and holds it
for the session's life, so a live proxy — even one idle for hours — is never touched) and
has made no request for five minutes — both the terminated transports the layer kept and
the sessions whose client simply vanished. Reaping goes through the transport's own
`terminate()` (idempotent), so a reaped id answers 404 exactly as a deleted one. On a
source-built backend the first sweep past the window reaped 916 sessions and RSS stayed
flat for the seven minutes that followed. `install()` binds the
class to the name FastMCP constructs by module attribute, called from `server.py`'s http
branch before `mcp.run()`; `tests/test_session_hygiene.py` pins the sweep against fake
transports with an injected clock, the install seam, and an in-process end-to-end run
over real streamable HTTP. Universal; no knob. The probes' churn itself is a separate
follow-up (F-864).

### Fixed — `execute_cdp_command` types a caller's JSON onto the CDP wrapper's parameters (F-861)

A caller sending what the CDP docs show — `Input.dispatchMouseEvent` with
`button: "left"`, `Browser.grantPermissions` with `["geolocation"]`,
`Browser.setWindowBounds` with `windowId: 7` — got `'str' object has no attribute
'to_json'` (Sentry STEALTH-CHROME-DEVTOOLS-MCP-4T, 74 events; -79) or its `int`
twin. nodriver's generated wrappers take their own typed classes (`MouseButton`,
`PermissionType`, `WindowID`, `Bounds`, …) and call `.to_json()` on whatever they
are handed, so a raw value crashed one frame inside nodriver with nothing to say
which parameter wanted what.

The new `embedded/cdp_params.py` leaf (`typed`) builds each argument into the type
the wrapper's OWN signature declares, read from its resolved type hints — nothing is
typed by hand, so a nodriver upgrade is covered the moment it lands. `from_json` for
the generated classes, through `Optional[..]` and `List[..]`; primitives and
already-typed values pass through untouched, so every frame F-816 pinned is
byte-identical. A value the type cannot take is now a `ToolError` naming the param
and the type (dropped by `before_send` as an expected failure), instead of
nodriver's AttributeError shipping to Sentry. Wired at the one composition site,
`build_cdp_call`, after the F-816 name folding. `tests/test_cdp_params.py` pins the
three Sentry shapes plus the dataclass, str-newtype, list and enum cases; the
executor's cap ratchets 1012 -> 1004.

### Docs — the fleet story, with the numbers behind it

Docs and one test constant; no product code. The README, the package description,
`DESIGN.md` and `RUNBOOK.md` now say what the architecture was built for and what it
measures: a Claude Code session costs a thin stdio proxy (≈ 60 MB resident), not a
browser, so memory scales with the Chromes a fleet actually spawns rather than the
sessions it opens. The new README section *Built for fleets: 50+ Claude Code
sessions, one backend* carries the measurements it rests on — 62 sessions attached
at once on one workstation, ≈ 3.7 GB of proxies against ≈ 46 GB had each session run
its own Chrome, one backend per desktop context, and the startup herd's own cold-start
and warm-join times. The backend's own footprint is deliberately not quoted as a
constant: it depends on what the sessions do with it.

The startup herd (`tests/test_startup_herd.py`) now runs 50 sessions on a
workstation instead of 40, so the scale the docs claim is the scale the gate proves;
the CI fleet stays at 12 (hosted runners) and every invariant is unchanged.

### Internal — the startup herd's wedge now speaks, and the integration cell lets it (F-859 §7.1/§7.2)

Test and CI only; no product code. The Windows integration cell had been going red
at a 15–22% rate since 2026-09-02 with one shape every time: the 40-session (12 in
CI) startup herd wedged at its own 240s backstop and died as a bare `TimeoutError`
with nothing attached — the pytest-timeout of 180s killed the process before the
test could report, so there was no test name, no `junit.xml`, and a second red from
`release_evidence`'s `pytest: null` that was pure consequence. The full
investigation is committed as
`audit/stage2/finding_F859_windows_herd_stall_rate.md`.

* **§7.1 — the wedge fails by name.** `tests/test_startup_herd.py` now catches the
  herd's own timeout and fails with which sessions never came back, the phase each
  one stopped in (`spawning proxy` / `initialized@Ns, awaiting tools/list` / `done`),
  the usual percentile summary, and the backend's own log — exactly what the
  neighbouring asserts already attach for the shapes they catch. The phase marker
  is what settles the finding's open question on the next red: whether
  `initialize` completed and `tools/list` never returned (§3.1), or the proxy
  never came up.
* **§7.2 — the integration cell's `pytest --timeout` is 300, not 180.** Above the
  herd's 240s backstop, like the transport cell already was, so the report from
  §7.1 can actually be written. Same defect class as F-780.
* **§7.3 — the confound is separated.** Draft PR #85 re-ran the gate on the last
  pre-F-856 main (`2da61c0`) three times on today's runner pool: 6/6 Windows herd
  samples green, against 15–22% red on post-F-856 trees the same days. The
  evidence points at F-856's 240.0s == 240.0s backstop identity, not the runner
  pool. §7.4 (decoupling the herd backstop from
  `REUSE_PATIENCE_SECONDS × MAX_STRETCH`) is a product decision and is
  deliberately NOT made here.

### Fixed — a spawn that failed after Chrome launched no longer leaks it (F-860)

A `spawn_browser` that failed AFTER nodriver had started Chrome but BEFORE it
handed a `Browser` back — nodriver's own *"Failed to connect to browser"* after
its `/json/version` polls, or the websocket's *"timed out during opening
handshake"* — left that Chrome running, untracked and invisible to
`list_instances`. The failure handler only ever stopped a `Browser` it held, and
`process_cleanup.kill_browser_process` returned early because tracking happens in
`_apply_post_launch`, after a successful launch. On a clone the cost was a stray
process tree until the next backend start's orphan reap. On `master` it was
worse: the leaked Chrome held the profile, so every later spawn cloned, the
master snapshot never refreshed, and nothing in `list_instances` explained why —
until a backend restart. Observed once under a 12-way concurrent spawn burst; the
evidence and the independent audit of the mechanism are in
`audit/stage2/finding_F860_failed_spawn_leaks_untracked_chrome.md`, committed
here.

* **The attempt's profile directory identifies the process.** The orchestrator
  holds no pid for a launch that raised; what it still knows is the
  `--user-data-dir` it launched on. The new `embedded/spawn_leak.py` leaf
  composes `process_cleanup`'s existing cmdline scan and escalating kill with the
  ONE fact that makes the reap safe: **only a browser that started at or after
  the attempt began is ours**. An explicit `user_data_dir` may name a profile a
  real Chrome already holds (its singleton is then exactly why the launch
  failed); that process predates the attempt and is spared. A browser on any
  other directory is never touched.
* **One teardown for both failure phases.** The cancel and error handlers in
  `spawn_browser` carried the same eleven lines twice; they now share
  `_teardown_failed_spawn`, which stops a held `Browser` as before and otherwise
  reaps by profile. The reap never raises: the caller still sees the launch
  failure, never a cleanup failure in its place, and a Chrome that refuses to die
  is logged, not fatal.
* Left alone on purpose: a failed clone attempt's directory is still released to
  the `auto_clean` sweep rather than deleted inline (bounded, and the sweep is
  the one home for that), and nothing changes for a spawn that failed before the
  launch was reached.

`browser_manager.py`'s LOC cap ratchets DOWN 1532 → 1529 (cap == actual): the
shared helper was paid for by collapsing four boilerplate `Args:/Returns:`
docstring blocks that only restated their signatures.

### Internal — three more sections leave `embedded/server.py` (plan_SERVERSPLIT slices 4–6)

No behaviour change and no tool renamed, added or removed: the served surface stays
byte-identical to `tests/goldens/tool_surface.json` at every commit, and all three
loaded identities of `server.py` (canonical import, bare-name spec load, runpy
`__main__`) keep building a full 94-tool app.

- **`tool_sections/dynamic_hooks.py`** (slice 4, 10 tools) — the only section with
  SYNCHRONOUS tool bodies, which is what this slice proves: `tool_registry`'s
  `_surrogate_safe_returns` branches on `inspect.iscoroutinefunction`, and its sync
  wrapper is now applied by `server.py`'s binding loop to a function whose
  `__globals__` is a section module. Zero shared dependencies beyond
  `rt.dynamic_hook_ai`, so the sync branch is proven in isolation. `server.py`:
  3014 → **2839** LOC (174 bodies plus the now-unused `dynamic_hook_ai` alias
  import).
- **`tool_sections/progressive_cloning.py`** (slice 5, 10 tools) — the first
  golden-backed section. Its two goldens
  (`tests/goldens/progressive_expand_styles.json`,
  `progressive_list_stored_elements.json`) belong to
  `progressive_element_cloner`, one layer BELOW the tool bodies, so a pure move
  cannot touch them; they are re-run byte-unchanged as part of the slice to prove
  exactly that. `server.py`: 2839 → **2660** LOC (178 bodies plus the now-unused
  `progressive_element_cloner` alias import).
- **`tool_sections/network_debugging.py`** (slice 6, 10 tools) — the first
  section to take a module CONSTANT with it: `_CAPTURE_OFF_NOTE`, which sat
  wedged between two tool bodies in `server.py`, now sits at the top of the module
  whose three `capture_note` tools are its only readers (the string is
  byte-identical; only its position moved). It is also the heaviest raw-`.fn`
  test coupling in the plan — `tests/test_server_network_tools.py` reaches fifteen
  call sites as `server.<tool>.fn(...)` — and every one of them is a
  module-attribute read the binding loop still satisfies, so none needed
  re-pointing. `server.py`: 2660 → **2391** LOC.

After slice 6, 43 of the 94 bodies have moved and `server.py` is down 951 lines
from the slice-0 baseline (3342), i.e. 1020 from the 3411-line god file the plan
started against. The LOC cap ratchets DOWN to the measured actual in every
commit and `tests/source_scan.py`'s floor ratchets UP by one, so a section module
dropped from `SECTION_MODULES` reads as a collapsed source set rather than a
legitimately smaller one.

### Internal — the extraction sections leave `embedded/server.py` (plan_SERVERSPLIT slices 7–9)

No behaviour change and no tool renamed, added or removed: the served surface stays
byte-identical to `tests/goldens/tool_surface.json` at every commit, and all three
loaded identities of `server.py` (canonical import, bare-name spec load, runpy
`__main__`) keep building a full 94-tool app.

- **`tool_sections/file_extraction.py`** (slice 7, 9 tools) — the to-file twin of
  `element-extraction`. The nine bodies were physically SPLIT in `server.py`, two
  above `extract_complete_element_cdp` and five below it, because that
  element-extraction body was misfiled among them; here they close up in their
  registration order and the stray is left for slice 8. The section's two goldens
  (`tests/goldens/file_based_structure_to_file.json`,
  `extract_element_structure_list_convert.json`) belong to
  `file_based_element_cloner`, one layer BELOW the tool bodies, so a pure move
  cannot touch them; they are re-run byte-unchanged to prove it. `server.py`:
  2391 → **2116** LOC (274 body lines plus the now-unused
  `file_based_element_cloner` alias import).
- **`tool_sections/element_extraction.py`** (slice 8, 9 tools) — the inline half
  of the cloner surface, and the plan's SECOND stray relocation:
  `extract_complete_element_cdp` was physically filed among the file-extraction
  bodies in `server.py` while registering into `element-extraction` all along,
  and now lives in its own section's module. Three of these bodies return
  through the SYNCHRONOUS `response_handler.handle_response` — awaiting it
  raises `TypeError` and silently broke these very tools once (F-202) — so this
  is the slice that proves that guard follows the bodies: it re-derives its AST
  scan from `tests/source_scan.py` instead of being hard-wired to `server.py`.
  The section's three goldens (`tests/goldens/extract_element_styles.json`,
  `cdp_complete_element.json`, `canonical_engine.json`) belong to
  `cdp_element_cloner`, one layer BELOW the tool bodies, and are re-run
  byte-unchanged. `server.py`: 2116 → **1748** LOC (367 body lines plus the
  now-unused `cdp_element_cloner` alias import).
- **`tool_sections/cdp_functions.py`** (slice 9, 13 tools) — the one section a
  runtime GATE switches off. Both gate sites stay in `server.py` (the
  module-scope `xpool_safe_mode` branch and the `__main__` block's
  `--xpool-safe` / `--disable-cdp-functions`) and both still run AFTER the
  binding loop, because `apply_disabled_sections` works by `mcp.remove_tool` and
  so can only remove tools that are already registered — plan_SERVERSPLIT R6.
  Verified against a REAL backend subprocess over HTTP rather than in process:
  control serves 94, `--xpool-safe` 81, `--disable-cdp-functions` 81 and
  `XPOOL_SAFE_MODE=1` 81 — exactly thirteen removed by each, identical to the
  pre-move baseline. `server.py`: 1748 → **1371** LOC (376 body lines plus the
  now-unused `cdp_function_executor` alias import).

After slice 9, 73 of the 94 bodies have moved and only `browser-management`
(8 tools) and `element-interaction` (12) are left in `server.py`, which is down
1971 lines from the slice-0 baseline (3342). As in every earlier slice the LOC
cap ratchets DOWN to the measured actual and `tests/source_scan.py`'s floor
ratchets UP by one (8 → 11), so a section module dropped from `SECTION_MODULES`
reads as a collapsed source set rather than a legitimately smaller one.

### Internal — the last tool bodies leave `embedded/server.py` (plan_SERVERSPLIT slices 10–11)

No behaviour change and no tool renamed, added or removed: the served surface stays
byte-identical to `tests/goldens/tool_surface.json` at every commit, and all three
loaded identities of `server.py` (canonical import, bare-name spec load, runpy
`__main__`) keep building a full 94-tool app.

- **`tool_sections/browser_management.py`** (slice 10, 8 tools) — the browser's own
  lifecycle. `spawn_browser` is the plan's largest single tool and carries the
  F-808/F-810 headed-visibility guard, which runs BEFORE its `try` and outside it so
  a spawn nobody could ever see refuses without first cloning a profile dir onto
  disk; it reads `rt.display_context.display_context()` for the refusal message and
  reaches `desktop_launch.can_deliver_headed_window()` through a function-local
  import, carried rather than hoisted. `spawn_browser` and `close_instance` are the
  two ends of the on-disk profile/clone lifecycle, so six calls land in
  `rt.clone_storage` — resolved at call time, which keeps
  `tests/test_clone_storage.py`'s "patch it THERE, not on `server`" pin true of a
  body that has left `server.py`. `get_instance_state`'s `# F-164 non-CDP` marker
  moved byte-unchanged with it, and `tests/test_cdp_timeout.py` follows it into the
  new file through `tests/source_scan.py` — load-bearing exactly here, because after
  this slice `server.py` contains no `asyncio.wait_for` at all. `server.py`:
  1371 → **986** LOC (377 body lines plus seven imports it was the last consumer of).
- **`tool_sections/element_interaction.py`** (slice 11, 12 tools) — the largest
  section and the last to move. `execute_script` is why it went last: it is the only
  body that reads three runtime knobs at once (`rt._script_rejection_reason` and
  through it `MAX_USER_SCRIPT_BYTES`, `rt.EXECUTE_SCRIPT_TIMEOUT`,
  `rt._clamp_timeout`), all resolved against `tool_runtime` at CALL time so
  `patched_server` still reaches a guard whose caller no longer lives in `server.py`.
  `take_screenshot`'s two function-local imports (`io`, `PIL.Image`) are carried as
  they stood, keeping Pillow off the module's import graph at binding time.
  `server.py`: 986 → **524** LOC (452 body lines plus thirteen imports it was the
  last consumer of).

**`embedded/server.py` now holds no tool bodies.** What remains is `mcp`, the
registry, the binding loop that registers all 94 functions once per execution of its
module body, `app_lifespan`, the four `@mcp.resource` handlers, the xpool-safe gate,
`build_arg_parser` and the `__main__` block — plus the migration alias block that
slice 12 deletes. The file is down 2887 lines from the 3411-line god file the plan
started against. Its LOC cap ratcheted DOWN to the measured actual in both
commits; `tests/source_scan.py`'s floor ratcheted UP to its FINAL value 13
(`server.py` + `tool_runtime.py` + all eleven section modules), and §5.5's
`MIGRATION_ALIASES` floor ratcheted DOWN 14 → 11 → 5, the five aliases left all
having a named reader. `SECTION_MODULES` is complete, so `SECTION_TOOLS`' key order
has settled back to the canonical one. The xpool gate was re-measured against a real
backend subprocess over HTTP — control 94, `--xpool-safe` 81,
`--disable-cdp-functions` 81, `XPOOL_SAFE_MODE=1` 81 — still exactly thirteen
removed by each, unchanged from the pre-move baseline.

### Internal — the split is complete; the scaffolding is gone (plan_SERVERSPLIT slice 12, closing)

**plan_SERVERSPLIT is COMPLETE.** All 94 tool bodies live in
`embedded/tool_sections/`, and this closing slice removes the migration machinery
that made the move reviewable. No behaviour change, no tool renamed, added or
removed: the served surface is still byte-identical to
`tests/goldens/tool_surface.json`, and all three loaded identities of `server.py`
still build a full 94-tool app.

- **The migration alias block is deleted.** Through slices 0–11 `server.py`
  re-exported a shrinking set of `tool_runtime` names, so a fake handed to
  `tests/conftest.py`'s `patched_server` had to be written into two places at once
  (a dual-patch guarded by an alias-identity pin). `server.py`'s own non-tool
  readers — `app_lifespan`, the four `@mcp.resource` handlers and the `__main__`
  block — now read `rt.<name>` exactly as a tool body does, so the dual-patch and
  the pin go with the block. `tool_runtime` is the one patchable home again, with
  no era to keep in step.
- **"One home" is a guard now, not a convention.** A new parametrized test asserts
  the four constructed singletons (`browser_manager`, `network_interceptor`,
  `dom_handler`, `cdp_function_executor`) are **not** attributes of `server`.
  An alias that came back would fail nothing else: every test would stay green
  while a `setattr` on `tool_runtime` silently stopped reaching whatever read the
  `server` copy. `tests/test_observability.py`'s four `_with_cdp_timeout` calls and
  two `debug_logger` reads are re-pointed to `tool_runtime`, and
  `tests/test_tool_module_reload.py`'s shared-runtime check now asserts through
  each identity's own `rt` binding.
- **`embedded/server.py` leaves `GRANDFATHER`.** The row is *deleted*, not merely
  satisfied: a grandfathered cap is a standing permission to exceed the budget, and
  the file — **523 LOC**, down from the 3411-line god file the plan started against
  — is governed by the 1000-LOC default like every other module. What is left in it
  is `mcp` and the registry, the binding loop that registers all 94 functions once
  per execution of its module body, `app_lifespan`, the four `@mcp.resource`
  handlers, the xpool-safe gate, `build_arg_parser` and the `__main__` block. The
  one import that is not a runtime read is `clone_storage`, kept as the positive
  delegation handle `tests/test_clone_storage.py`'s F-201 negative-surface pin needs.
- **Docs.** `CLAUDE.md`'s `server.py` / `tool_runtime.py` / `tool_sections/` rows
  and its "Tool count = 94" paragraph now state the finished shape (the count
  derives from `SECTION_TOOLS`, filled by `server.py`'s binding loop over
  `SECTION_MODULES`); `DESIGN.md` §8 gains the section-module corollary — *a module
  that holds tool bodies must not register them either*, with the zero-registration
  failure mode spelled out; `CONTRIBUTING.md` gains an "Adding a tool" section
  saying what that now means: a function in a section module and an entry in its
  `TOOLS` tuple.

### Internal — the first tool bodies leave `embedded/server.py` (plan_SERVERSPLIT slices 1–3)

No behaviour change and no tool renamed, added or removed: the served surface is
byte-identical to `tests/goldens/tool_surface.json` at every commit, and all three
loaded identities of `server.py` (canonical import, bare-name spec load, runpy
`__main__`) keep building a full 94-tool app.

Slice 0 shipped the machinery; these three slices are the first bodies to use it,
ordered smallest-and-most-isolated first so the mechanism is proven on three tools
before it is trusted with four hundred lines.

- **`tool_sections/cookies_storage.py`** (slice 1, 3 tools) — the smallest section
  that still exercises the whole shared-dependency set a body reaches for
  (`browser_manager`, `network_interceptor`, `_with_cdp_timeout`, `_require_tab`,
  `ToolError`). `server.py`: 3342 → **3248** LOC.
- **`tool_sections/tabs.py`** (slice 2, 5 tools) — the first section to use
  `_require_browser`/`_require_landing_ok` and the first to read a tuned knob
  DIRECTLY (`new_tab` hands `CDP_OPERATION_TIMEOUT` to `_require_landing_ok`),
  which is what proves a knob stays patchable from a section module: it is
  resolved against `tool_runtime` at call time, not bound at import. `server.py`:
  3248 → **3144** LOC (103 bodies plus the now-unused `_require_browser` import).
- **`tool_sections/debugging.py`** (slice 3, 5 tools) — the first slice that
  RELOCATES rather than only moves: `validate_browser_environment_tool` sat among
  the element-extraction bodies in `server.py` while registering into
  `debugging`, and joins its own section here, which is what makes module ↔
  section a clean 1:1. It is also the first slice to move a string a source-TEXT
  guard is pinned to (`MSG_EXPORT_TIMEOUT`, in `export_debug_logs`): slice 0's
  derived file set carries the pin into the new module, where an
  `inspect.getsource(server)` pin would now have passed over a file the message
  had already left. `server.py`: 3144 → **3014** LOC (128 bodies plus the two
  now-unused `platform_utils` imports).

After slice 3, 13 of the 94 bodies have moved and `server.py` is down 328 lines
from the slice-0 baseline; the LOC cap ratchets DOWN to the measured actual in
each commit, and `tests/source_scan.py`'s floor ratchets UP by one, so a section
module dropped from `SECTION_MODULES` reads as a collapsed source set rather than
a legitimately smaller one.

### Internal — the mechanism for splitting `embedded/server.py` (plan_SERVERSPLIT slice 0)

No behaviour change and no tool moved: the served surface is byte-identical to
`tests/goldens/tool_surface.json`, a new HARD golden taken in this commit.

`embedded/server.py` is a 3411-line god file whose 94 tool bodies are due to move
into one module per section. Slice 0 ships only the machinery that makes that move
safe, and the guards that prove it:

- **`embedded/tool_runtime.py`** — the one home for what a tool body reaches for
  beyond its own arguments (the four constructed singletons, the re-exported
  module singletons, the four tuned knobs and the script/timeout guards). A body
  resolves `rt.<name>` at CALL time against a module that is loaded once, so the
  whole surface has exactly one patchable seam no matter which file it ends up in.
- **`embedded/tool_sections/`** — the subpackage the bodies move into, carrying
  the contract that a section module never decorates its own tools.
  `SECTION_MODULES` is empty in this slice.
- **`server.py`'s binding loop** — registration is driven from `server.py`'s
  module body, so all three loaded identities (canonical import, bare-name spec
  load, runpy `__main__`) each build a full 94-tool app. A section module that
  decorated itself would register into the first execution only — and the existing
  94-count tripwire cannot see that, because `SECTION_TOOLS` is shared and would
  still read 94.
- **`tests/test_tool_module_reload.py`** — the detector for both failure modes,
  each demonstrated red in place: removing the registry's idempotent append yields
  `282 tools ... (3x registration)`, and moving one tool into a self-decorating
  section module leaves the second and third identities at 93 while the count
  tripwire stays green.
- **`tests/source_scan.py`** — four guards read `server.py`'s source TEXT
  (F-164 CDP-timeout discipline, F-202 call convention, the uvicorn run-config,
  the export-timeout message pin). A source-text guard does not fail when the code
  it polices leaves the file; it passes, over an emptier file. All four now derive
  their file set from one helper carrying a floor assertion.

`server.py`: 3411 → **3342** LOC, cap ratcheted down to the measured actual.

### Changed — the cloner subsystem joins the one error convention (F-858)

Every tool in this package reports failure by raising `ToolError`, so a client
sees a real error. The cloner subsystem did not: 29 sites across the engine, the
progressive adapter and the to-file adapter answered a failure by RETURNING
`{"error": ...}`, which reaches an MCP client as a **successful** call whose
result happens to contain the word "error". Those 29 now raise. Message text is
byte-preserved everywhere, so nothing a caller reads got worse.

Two of them were more than cosmetic:

* **`clone_element_to_file` with malformed `extraction_options`** returned an
  error dict while its sibling `clone_element_complete` — same argument, same
  `json.loads`, same failure — raised. Whether a bad option string was an error
  depended on which of two tools you called.
* **`*_to_file` swallowed a failed extraction.** It wrote the engine's error
  payload to a JSON file and answered with the normal
  `{file_path, extraction_type, summary}` shape and an all-empty summary — a
  file claiming to be a clone of an element that was never extracted, and a
  summary indistinguishable from a genuinely empty element. A failed extraction
  now propagates and writes nothing.

What deliberately did NOT change: a complete clone still survives one bad aspect.
`extract_complete_element` already gathered its six aspects with
`return_exceptions=True`, so a raising aspect lands in the same embedded
`{"error": ...}` record it landed in before — one isolation mechanism, not two.
The other five aspects still populate.

### Fixed — an edit recipe now says WHERE, and stops guessing WHY (F-857)

Three follow-ups recorded by the animation-v2 adversarial audit (PR #73), all in
the same seam: the payload knew the answer and printed a guess instead.

* **D6 — three causes, one wrong sentence.** A `var()`-indirect value, an
  animation declared in the element's own `style=""`, and a rule that lives in
  an adopted constructed stylesheet all degraded with *"its stylesheet is likely
  cross-origin, so there is nothing here to find/replace"* — a guess, and a
  wrong one in two of the three, which sends a weak model hunting through a file
  that is not involved. Each is now decided from a fact the collector already
  sent: the declared value names the custom property (`var(--dur)` →
  "change `--dur`'s own declaration"), `element.inline_properties` names the
  attribute, and a *witnessed* `cross_origin_stylesheet` warning — not the
  absence of a rule — is what licenses saying "cross-origin" at all, with the
  href it was witnessed on. With no such witness the message says what is
  actually left: a constructed sheet adopted at runtime, a shadow root's own
  `<style>`, or JavaScript.
* **Openable source location.** `rule_span` computed the offset of a rule inside
  the sheet and threw it away, so the strongest thing a recipe could say was
  "find this string somewhere in `<style> #0`". A recipe that carries a `find`
  now also carries `char_offset`, `line` and `column`, and its `sources` entry
  carries `open`: the url an editor opens (a linked sheet's href, else the
  DOCUMENT that contains the `<style>`) plus `offsets_in`, because offsets into
  a `<style>` element's text are not offsets into the HTML around it. A
  constructed sheet gets no `open` at all — a url there would be a fabricated
  address. Resolving a url to a path on disk stays with `extract_related_files`,
  the one URL→file answerer; the payload surfaces the halves rather than growing
  a second one.
* **D7 — `editable` was whole-record, and its absence read as yes.** A record
  whose keyframe declarations were applicable while every timing knob was a
  pointer emitted no verdict at all, so a reader that stopped at the flag
  concluded it could retime an animation it cannot. `editable` still answers
  "can ANY recipe here be applied"; `not_editable` now names the ones that
  cannot, and is omitted where nothing is a pointer.

Payload shape (SOFT golden, updated in the same commit): animation and
transition records always carry `editable`; `not_editable` is new; the recipe
`note` for "no rule declares this" is now short and defers to the record's
`not_editable_reason`, which carries the discriminated cause. `edit_protocol`
gains one `open` paragraph, stated once rather than per recipe.

`embedded/animation_source.py` is new — THE one home for *where a declaration
lives* (the locating, the openable location, and the three causes when it cannot
be located), extracted from `animation_edits.py` because the additions took that
file past the 1000-LOC budget. No cap was raised.

## 2.1.1

### Fixed — a starved machine no longer manufactures its own backend death (F-856)

On 2026-09-02, on a machine at 3,445 processes with the CPU pegged at 100%,
every Claude Code session sharing one backend saw `CONNECTION_CLOSED`. Nothing
had died. The product's own logs show the failure assembling itself out of two
halves that each look reasonable alone:

* **A timeout was treated as evidence by a prober that was not being
  scheduled.** F-820's confirmation gate correctly held off six times across
  eleven minutes — `backend on port 52554 was busy, not dead` — and then, on the
  seventh strike run, spent its whole 60-second patience without hearing the
  backend and returned `confirmed unusable`. The backend had not changed; the
  prober's 60 wall-clock seconds no longer contained 60 seconds of the attention
  the window was sized for.
* **The recovery that followed had a deadline the same starvation guaranteed it
  would miss.** The replacement backend was born at 12:37:47 and did not serve
  until ~12:39:41, because `process_cleanup`'s orphan reap ran *inside* the
  lifespan the first `initialize` awaits — ~90 seconds of it, several 5s
  force-kill waits per orphan — while the healing proxy held a 45s readiness
  budget. The heal could not have succeeded at any level of patience.

Both halves are fixed, and neither adds a knob or a second recovery path:

* **Patience is now spent in fairly scheduled seconds.** The reuse gate's own
  naps are the measurement — ask for 0.25s, wake at 0.25s + delta — so the
  process reads its own lateness with a monotonic clock and no system polling.
  Elapsed time is discounted by that lag before it is charged, bounded at 4x, so
  a window widens *only* in the conditions that make a narrow window wrong; on an
  idle machine the measurement is 1.0 and the behaviour is line-for-line what it
  was. One home (`embedded/scheduling_lag.py`), applied at one site — the gate
  three callers already share (the F-820 watchdog's confirmation, F-843's bridge
  verdict, F-807's cold-start grace). `proxy_selfheal` is untouched: starvation
  moves *when* a verdict is reached, never what happens after one.
* **Readiness comes before reaping.** `process_cleanup.activate()` still arms
  its atexit/signal handlers synchronously — one armed after the first browser
  exists was not there when it counted — and hands the reap to
  `embedded/serve_startup.py`, which runs it off the first-serve path. Nothing
  about the reap needed to precede the first tool call: ownership, not timing, is
  what makes it safe to kill a browser (F-808), the registry write drops reaped
  ids *by name* through the shared read-merge-write, and the temp-profile sweep
  already skips anything a live browser holds or anything younger than the orphan
  age.
* A **`proxy: patience extended under starvation`** lifecycle report (F-827)
  fires at most once per window, and only on material lag, so the
  CONDEMNED/HEALED/TEARDOWN series finally has a denominator for how often
  starvation nearly caused one.

Rejected on the way: "the recorded pid is alive, therefore not dead" as a veto
over a condemnation — a *wedged* backend (dispatch loop dead, socket open,
process resident) passes that forever, which is the exact failure F-301/F-501
exist for. That signal already has its one sound use inside the gate, as the
fast-fail for the genuinely dead. Also rejected: simply raising
`REUSE_PATIENCE_SECONDS` and `HEAL_ATTEMPT_SECONDS`, which buys starvation
tolerance by making every real hard-down slower to detect on every machine
forever.

Internal: the proxy's liveness watchdog moved out of `singleton.py` (at its LOC
budget) into `embedded/backend_watchdog.py`, taking both probes as arguments so
it stays a leaf; `singleton._watch_backend_liveness` remains as the wiring that
knows which probes are ours. `process_cleanup.py`'s budget was ratcheted DOWN,
1023 → 1017.

## 2.1.0

Animation extraction is rebuilt as **schema v2** (F-846..F-855), and the result
was then audited adversarially before shipping. The new capability plus the
breaking payload change for `extract_element_animations` is why this is a minor
bump rather than a patch.

### Fixed — five defects schema v2's own tests could not see (audit of PR #72)

An independent adversarial review audited the animation-parsing work from a
clean worktree, under the rule that nothing changes on a code-read alone: every
fix below is here because a new test reproduced it first, red on the merged code
and green after. All five share one signature — the payload was **confident and
wrong**, which F-850's own premise ranks below saying nothing — and in each case
the fact needed to catch it was already *in* the payload, just never read.

- **A `none` slot shifted every list after it.** Filtering
  `animation-name: fade, none, spin` down to the live names dropped the slot
  *index* along with the slot, so `spin` was handed slot 1's duration, delay,
  easing and iteration count, and its edit recipe addressed the wrong comma item
  of `animation-duration: 1s, 2s, 3s` — at `confidence: "high"`. F-847's
  list-cycling rule was applied correctly, to the wrong index. The list index
  now stays the CSS slot, while a record's `id` counts records, because
  `build_waapi` numbers live records from the record count and a hole here would
  collide with them.
- **`!important` was swallowed by the replace template.** For
  `animation-duration: 2s !important` the token was `2s !important` and the
  replacement `animation-duration: {{NEW_VALUE}}`, so applying the recipe
  silently dropped the priority — turning a retime into a cascade edit, and
  breaking F-852's promise that `replace` carries the rest of the declaration.
  Not a rare path either: `winning_rule` ranks `!important` first, so an
  important rule is the one a recipe most often points at. Spans are now taken
  against the value without its priority, at both knob branches and in the
  keyframe recipe.
- **`@layer` reversed the cascade unnoticed.** Ranking went `!important` →
  specificity → document order and never read `at_rule_context`, which the
  recipe itself carries. An unlayered declaration beats a layered one however
  specific the layered selector is, so a layered `#hero` outranked an unlayered
  `.hero` here and lost in the browser — and the computed-value cross-check
  cannot catch it when both declare the same value. Layer *order* is not
  recoverable (a bare `@layer a, b;` statement has no `cssRules`, so the
  collector's walk never sees it), so candidates spanning different layers now
  degrade to a rule pointer at `confidence: "low"` — the vocabulary the module
  already had for `:is`/`:where`.
- **The 20-recipe edit cap truncated silently.** `caps.truncated` reports
  animations and keyframes only, so 20 of 38 recipes arrived with nothing said —
  against F-853 and F-855, and leaving a model to conclude that the keyframes it
  can find no recipe for are not editable. A per-record `edit_cap_reached`
  warning now names a remedy the reader actually has: `EDIT_CAP` is not
  caller-settable, so the message points at the `@keyframes` block's
  `source_ref` and at narrowing the selector, rather than inviting a raise.
- **A shadow host was reported as static.** `getAnimations({subtree: true})`
  does not cross a shadow boundary and `document.styleSheets` does not list a
  shadow root's `<style>`, so a host whose shadow content was visibly animating
  returned `has_motion: false`, zero animations and not one warning. It does not
  have to be captured; it has to be admitted. A named
  `shadow_root_not_traversed` warning now says how many roots were skipped and,
  where the root is open, how many elements inside them are animating right now.

**Reported, not fixed** — both degrade safely, so neither emits a wrong value:

- Three distinct causes — a value behind `var()`, an inline `style=""`
  animation, and an adopted constructed stylesheet — all share the one fallback
  reason "likely cross-origin; edit that instead", which sends a reader hunting
  for a file that is not involved. The payload already carries the facts that
  tell the three apart.
- `editable` is a whole-record verdict, so a record whose timing knobs are all
  pointers but whose keyframes are editable emits no `editable: false`. Pinned
  as a test rather than changed.

**Tests:** 50 new. `tests/test_animation_v2_audit.py` adds 28 hermetic — 9
proved the defects above by failing on the merged code, and the other 19 pin
contracts that were already correct, each proved falsifiable by briefly mutating
the product (the red condition is documented in the file). Notably, `var()`
degradation survived removing `token_verdict` alone: it is defense-in-depth, and
only lied once the computed-value cross-check was also removed.
`tests/test_e2e_animations_edge.py` adds 22 against real Chrome, and its
load-bearing test checks **every** `find` literal against
`tests/fixture_app/animations_edge.html`'s own bytes read off disk, never
against a string the test composes — the one check Chrome's re-serialization
cannot pass by accident.

### Changed — the animations payload is sized for the model that reads it (F-853)

The caps added in F-849 bounded the payload and still produced something no
model could read. A page with 400 animated children returned **4,910,005 bytes**
— roughly 1.2M tokens, about 24.5 KB per animation record. A cap that prevents
unboundedness but yields an unconsumable payload has met the letter of the rule
and missed its purpose.

The same page now returns **31,646 bytes**, a 155x reduction, from three changes
each sized by measuring where the bytes actually went rather than by picking
round numbers:

* **The default caps drop to 25 animations and 20 keyframes** (from 200 and 60).
  An element with more than 25 animations *of its own* does not exist in
  practice — that cap is really a subtree bound — and 20 keyframes is four times
  over the `0/25/50/75/100` vocabulary, while keyframes were 75-83% of every
  oversized payload measured. Both are overridable per call as
  `max_animations` / `max_keyframes` — but only on the engine path,
  `clone_element_complete(extraction_options={"animations": {...}})`, and not as
  arguments to the two `extract_element_animations` tools (see the known
  limitation below). They are module constants and deliberately **not**
  `STEALTH_MCP_*` env knobs, being payload shape rather than deployment config.
* **The edit protocol is stated once for the payload** instead of once per
  recipe. The `how` sentence and `replace_placeholder` were 6,960 bytes of a
  single 24,759-byte record — 36% of its `edits` block — repeated verbatim. They
  now live in a top-level `edit_protocol`, and the per-call recipe cap halves to
  20.
* **Records the caller did not select are summarized.** The blowup is always the
  subtree: ask about a grid and every animated cell arrives at full weight. A
  descendant keeps its identity, timing, semantics and trigger — enough to decide
  whether to look closer — and drops keyframes, checkpoints and edit recipes.
  This is stamped in the record as `detail_level: "summary"`, never left as a
  silent shape difference: a model seeing `edits` on one record and none on
  another would otherwise conclude the second is not editable.

Truncation stays loud, and now fires far more often, so each warning says how
many were dropped and names a remedy the reader can actually act on: narrow the
selector, which works from every path, and — where the caller is on the engine
path — raise the cap. A truncated list also no longer reports stagger groups; a
stagger inferred from an arbitrary prefix of the animations is an artifact of
the cap, not a fact about the page.

A test pins a busy page's payload under a stated byte budget, so the sizing
cannot silently regress the way it did here.

The truncation message names a remedy the reader can actually act on. It first
said "raise it by passing `max_animations` to this tool", and the two tools a
model actually calls — `extract_element_animations` and its `_to_file` twin —
do not accept that parameter; a truncated payload is precisely when a model is
most motivated to follow the advice, so a remedy that gets rejected is worse
than none. It now leads with narrowing the selector (the only lever those
callers have) and names the option against the path it is genuinely settable
on. A test reads the real tool signatures, so the message and the parameters
cannot drift apart in either direction.

**Known limitation:** `max_animations` / `max_keyframes` are settable only on
the engine path — `clone_element_complete(extraction_options={"animations":
{...}})` — and not as arguments to the two `extract_element_animations` tools.
`server.py` is at its grandfathered line budget with no room to add them; they
become tool arguments when that module is split.

**Internal:** `animation_analysis.py` reached its 1000-line budget, so the live
`Animation` half moved to a new leaf, `animation_waapi.py` (timeline typing,
live timing and keyframes, and the declared-vs-live reconciliation). The shared
value types it and `analyze()` both need — `Caps`, `cap_message`, `warn` — moved
down to `animation_facts.py`. Behaviour is unchanged by the move.

### Fixed — an edit recipe now names the token to change and the rule that wins (F-852)

Two defects that compounded, both worse than a missing recipe.

**Every timing knob got the same `find`.** For a rule written as

    animation: pulse 2.4s cubic-bezier(.68,-0.55,.27,1.55) 0.3s infinite alternate both;

the duration, delay, easing, iteration-count and name recipes all carried that
entire declaration as `find`, each at `confidence: "high"`, differing only in
`current`. A model told "find this, replace it with the new duration" turns
`.card { animation: fade 2s ease; }` into `.card { 3s; }` — a file-corrupting
instruction delivered at the highest confidence the schema has.

A recipe now carries three things instead of one: the author's whole declaration
as `find`, the single `token` inside it that this knob owns, and `replace` — the
same declaration with only that token swapped for `replace_placeholder`
(`{{NEW_VALUE}}`). Applying it cannot drop the rest of the declaration, because
the rest is carried in the replacement. Times are read positionally, the way CSS
defines the shorthand (first `<time>` is the duration, second is the delay), and
the identified token is checked against what the browser computed before it is
offered. Anything ambiguous — an animation literally named `ease`, say — degrades
rather than rewriting the wrong part of a working declaration.

**The recipe could point at a rule that cannot change the rendering.** The rule
was chosen as the last document-order rule declaring anything `animation*`,
ignoring specificity and `!important`, while `current` came from computed style.
With `.card { animation: fade 2s ease }` before `#hero { animation-duration: 5s }`
it reported `current: "5s"` with a `find` inside `.card`. Selection is now
per knob and follows the cascade: `!important`, then specificity, then document
order. Two situations degrade instead of guessing. The first is a selector whose
specificity cannot be computed exactly from its own text, because it is borrowed
from something that text does not resolve: `:is`/`:where`/`:not`/`:has(S)` and
`:nth-child(An+B of S)` take it from their argument, and `&` takes it from the
parent rule — so under CSS nesting a best-effort count made `& .card` tie with a
bare `.card`, and document order then handed the win to a rule that cannot change
the rendering. The second is a winner whose declared value disagrees with what the
element computes, which means an inline style, a UA sheet or an unreadable
stylesheet is in charge. (Plain `:nth-child(2)` stays decidable.) Recipes
also carry `rule_selector` and `at_rule_context`, so the rule-scoped
`find_unique_in_rule` claim can actually be checked.

Transitions get the same treatment rather than an `editable: false`, and the
collector no longer ships every `@keyframes` block in the document — only those
something on or under the element references.

**Breaking:** the recipe shape gains `token`, `replace`, `replace_placeholder`,
`how`, `rule_selector` and `at_rule_context`; the prose note "change the X
component within `find`" is gone, superseded by `token`/`replace`. The
`extract_element_animations` docstring no longer describes `find` as "verified
unique in its rule" — that claim only ever held for the recipes reporting
`find_unique_in_rule: true`.

### Changed — every derived animation field now carries the confidence it was derived with (F-850)

Ten separately reported defects in the animations schema shared one root cause:
confidence was stamped **after** the fact. The code wrote
`easing_confidence: "high"` onto whatever a heuristic returned — including a
fall-through branch that had decided nothing — the edit recipes defaulted to
`"high"`, and the live-animation path hardcoded `warnings: []`. The honesty rule
says every derived field carries a confidence or is omitted; the code said it and
then routed around it.

Derivations now return their value and their confidence together, so a branch
that reached no conclusion cannot inherit a caller's optimism. Where the honest
answer is "I do not know", the field is **omitted** rather than emitted hedged.

**Breaking:** `semantics.motion_kind` and `semantics.easing_class` are now
claim objects — `{"value": "overshoot", "confidence": "high"}` — instead of bare
strings, as is `transitions[].easing_class`. `semantics.easing_confidence` is
gone; the confidence lives inside the claim it belongs to.

Concrete consequences:

- `cubic-bezier(0.1, 0.9, 0.9, 0.1)` (fast at both ends) was classified
  `"linear"` at `"high"` confidence. It has no name in this vocabulary, so it
  now has none.
- `semantics.easing_class` read only the animation-level curve. An animation
  whose keyframes each declare their own `animation-timing-function` reported
  that unused curve confidently while its own `checkpoints[].between.segment_easing`
  said otherwise. Segments that disagree now report `"per-keyframe"`.
- `transform: translateX(10px) scale(1.2)` was classified `"scale"` alone
  (first-match-wins substring scanning), and `matrix3d(...)` was asserted to be
  a `"translate"`. Transform functions are now read as functions; a matrix is
  not decoded rather than guessed at.

### Fixed — caps, checkpoints, delays, triggers and stale options (F-851)

- **Caps are enforced and surfaced.** `include_subtree` defaults on, so one call
  can pull in hundreds of live animations; that path had no cap at all while
  `caps.truncated` reported `false`, and keyframes were cut to the cap silently.
  Both now stop at the cap and say so in `warnings`.
- **Checkpoints respect `animation-direction`.** With `reverse` or
  `alternate-reverse`, `time_ms` claimed offset 0 renders at t=0 — where the
  element actually shows the 100% keyframe. A non-zero iteration-start now omits
  `time_ms` entirely with a warning naming why.
- **A negative delay is not a wait.** `animation-delay: -0.5s` produced
  `active_start_ms: -500` and the prose "after a -0.5s delay". It starts
  immediately, already 500ms in, and now says so.
- **`pending_animations` no longer advises an impossible edit.** For
  `.gallery .card` matched against a `.card` outside any `.gallery`, it said "add
  the 'gallery' class to run it" — which cannot make that rule match. Only a
  class on the element's own compound selector is offered; an ancestor or sibling
  requirement is described as one.
- **`prefers-reduced-motion: no-preference` no longer fires the reduced-motion
  warning.** The check matched the feature name as a substring, so a block that
  applies only when motion IS allowed got a warning whose remedy is backwards.
- **Staggers that are not staggers.** Identical delays across siblings produced
  `{uniform: true, delta_ms: 0.0}`, and an unreadable delay was coerced to 0
  before differencing, turning an unknown into a confident invented spacing.
  Neither is reported now.
- **`editable` is mandatory wherever there are no usable recipes**, whatever the
  record's kind — absence read as "editable". Transitions now carry edit recipes
  of their own, or an explicit `editable: false` with a reason.
- **A retired extraction option degrades instead of destroying the whole clone.**
  `extraction_options={"animations": {"analyze_keyframes": True}}` (a real v1
  option) bound against the new signature and raised `TypeError` at the call
  site — before any coroutine existed, so the per-aspect isolation never saw it —
  turning one stale string into a single error payload for the entire complete
  clone, losing structure, styles, events, assets and related_files. Unknown
  per-aspect options are now dropped and reported in that aspect's `warnings`,
  naming the option and its replacement.

### Fixed — edit recipes pointed at text that is not in your stylesheet (F-849)

The `find` literals in `edits[]` were built from Chrome's `cssText`, which is a
re-serialization rather than the author's source. For a rule written as

    animation: pulse 2.4s cubic-bezier(.68,-0.55,.27,1.55) 0.3s infinite alternate both;

Chrome reports it with the animation NAME moved to the end, `.68` expanded to
`0.68`, spaces added after commas and a `running` keyword injected — so the
recipe advertised `confidence: "high"` for a string that occurs zero times in
the file. Same for values: `opacity: .8` became `opacity: 0.8`. A find/replace
found nothing, which is the failure mode the honesty rule exists to prevent
(the edit fails safely, but M10's entire value proposition was gone).

Recipes are now resolved against the author's real bytes, read from
`<style>` elements via `ownerNode.textContent` — nothing is re-fetched over the
network. A `find` is emitted only when the declaration was located in that text,
scoped to the rule's own span (and, inside `@keyframes`, to the individual
keyframe block, so a recipe for the `100%` frame stops returning the `0%`
frame's declaration). Where the author's text is unavailable (a linked or
cross-origin sheet) or the rule is ambiguous (the same selector declared twice),
the recipe degrades to a rule pointer with `confidence: "low"` and no `find`;
Chrome's form is still carried, but as `computed_declaration`, which is never a
find target. A `sources[]` entry now reports `source_text_available` and carries
the author's `source_text` when it is readable.

Two related honesty fixes: an animation applied through a pseudo-element rule
(`#hero::before`) came back with `edits: []`, no `editable: false` and no reason
— silence where its rule was sitting in the same `<style>` block; it now gets
real recipes, and any animation that genuinely has nothing to edit says so.
`keyframes[].raw_css_text` is renamed to `computed_css_text`, because it was
never raw author text and the name invited exactly the mistake above.

**Breaking:** `keyframes[].raw_css_text` → `computed_css_text`;
`sources[].css_text` → `computed_css_text`.

### Fixed — animation keyframes arrived as unusable serialization garbage (F-846)

`extract_element_animations` returned a nested object from `tab.evaluate`, and
`tab.evaluate` deep-serializes anything non-primitive: `keyframe_rules` reached
callers as `{"type": "object", "value": [["pulse", {...}]]}` rather than as
keyframes. This is the same hazard class as the fixed F-844 viewport bug, and
it is why nothing downstream could depend on the field. The page now builds one
`JSON.stringify` payload and the engine `json.loads` it, so a keyframe arrives
as a real parsed object with a numeric `offset` and a property map.

`Infinity` is normalized to the string `"infinite"` in the page *before*
stringify: `JSON.stringify` silently turns it into `null`, and a null reads as
"unknown" rather than "forever".

### Fixed — keyframes came back empty whenever an element had 2+ animations (F-847)

The lookup compared the whole computed `animation-name` list — the literal
string `"pulse, spin"` — against each `CSSKeyframesRule.name`. It therefore
matched nothing in exactly the case the feature exists for. Animations are now
split into one record per animation, with the CSS list-cycling rule applied
where it belongs (a single `animation-delay` against two names now gives both
animations that delay, instead of dropping one). The UA-default `all 0s ease
0s` transition every element reports is suppressed, so `transitions` carries
motion rather than noise.

### Added — animation extraction rebuilt for models that have to edit the CSS (F-848)

Schema v2, aimed at a consumer that will open the stylesheet and change it. Per
animation: a generated `summary`; `semantics` (motion kind, easing class);
`timeline` typed `time`/`scroll`/`view`; `timing` with every duration as a
`*_ms` number beside its `*_raw` CSS token; `derived` (cycle, active window,
total, stagger deltas) so no arithmetic is needed; `trigger` attribution;
resolved `keyframes`; `checkpoints`; and `edits` — per knob, the file plus a
`find` literal *verified unique in its rule*, which turns an edit from CSS
comprehension into find/replace. Alongside them: `interactions[]`, the
precomputed conflicts with remedies (a scroll-driven animation whose duration
edits would do nothing; two animations writing one property; an inline style
that overrides the rule you were about to edit), `sources[]` with stylesheet
href and rule path, and named `warnings[]` where a cross-origin sheet used to
be swallowed by a bare `catch {}`.

`getAnimations({subtree: true})` is now covered, so a running
`element.animate()`, a `::before` animation and descendant animations are
reported at all — previously the payload said an element was static while it
was visibly moving. A live animation with no CSS declaration is marked
`editable: false` with the reason, because editing CSS would not affect it.

Every derived field is emitted only when it is mechanically decidable, carries a
`confidence`, or is omitted — no value is interpolated by this tool and
presented as if it were measured.

**Breaking:** `css_animations`, `css_transitions` and `keyframe_rules` are
removed (the last of these only ever carried the F-846 garbage). The
`include_css_animations` / `include_transitions` / `include_transforms` /
`analyze_keyframes` arguments are replaced by `include_subtree` and
`include_waapi`; the removed flags gated v1 keys that no longer exist, and the
stylesheet walk they toggled now also feeds sources, triggers and edit recipes.

## 2.0.9

### Fixed — a backend that dies with a call in flight now heals instead of disconnecting (F-843)

The F-838 self-heal had a blind spot that turned out to be THE remaining
user-visible disconnect: it only healed deaths the ~12 s liveness watchdog got
to condemn. A backend that died *while a client call was in flight* broke the
HTTP bridge in milliseconds, and the bridge's own teardown cancelled the
watchdog before it could reach a verdict — so the proxy took the pre-F-838
exit and the session was dead ~1 s after the kill, every time (idle sessions
healed fine, which is why the gap survived testing). F-838's own motivating
incidents — the OOM crash, the CTRL_BREAK — were fast deaths it never covered.

The discriminator is now "did the backend leg end for a reason recovery
answers", not "did the watchdog condemn it". A bridge that breaks after the
backend had genuinely served us runs the same identity+readiness confirmation
the watchdog's own verdict phase uses: confirmed gone → the existing heal loop
(same `ensure_server_running`, same cold-start lock, same flap budget);
still alive → re-bridge quietly. Client-gone and never-became-ready keep their
honest exits. The F-827 lifecycle reports gain a `cause` field
(`watchdog` / `connection_lost` / `connection_reset`) so the two witnesses are
distinguishable in telemetry — closing the observability blind spot where a
fast death tore down before reporting anything at all.

### Fixed — `get_instance_state` reported `partial: true` on every call (F-844)

Three stacked bugs, each able to spoil the whole state read: the cookie
collection called `.get("cookies", [])` on nodriver's already-deserialized
`list[Cookie]` (an `AttributeError` on every call since the nodriver
migration); behind it, the viewport `tab.evaluate` of an object literal comes
back CDP deep-serialized (`[['width', {...}], ...]`) rather than as the object,
failing `PageState` validation; and a fractional `devicePixelRatio` (Windows
125 % scaling, Retina) failed the `dict[str, int]` viewport annotation.
Cookies now serialize via `Cookie.to_json()`, the viewport read asks the page
for a `JSON.stringify(...)` string (the one shape deep serialization passes
through), and the viewport model admits floats. Verified over real Chrome +
real stdio: `partial: false` with genuine cookies and viewport.

### Fixed — closing the active tab no longer strands the instance on a dead tab (F-845)

`close_tab` sent `Target.closeTarget` and returned without re-pointing the
stored active tab, so every subsequent tab-scoped tool on that instance hit
Chrome's DevTools endpoint with the dead target id — surfacing as the baffling
`server rejected WebSocket connection: HTTP 500` until a manual `switch_tab`.
Closing the active tab now re-points to the first surviving target under the
instance lock (the closed id excluded explicitly — nodriver's own target list
drops it only on a racing `Target.targetDestroyed` event); closing a
non-active tab pays nothing; closing the last tab leaves the typed
"no tab" error instead of a lie.

### Documented — python `mcp`-SDK clients tree-kill the shared backend on close (F-842)

The official python SDK's stdio client terminates the launched server's whole
process tree on close. Our detached shared backend is still in the
lock-winning proxy's tree, so closing that one session kills the backend under
every other session. Claude Code does not tree-kill, so production is
unaffected — but every python-SDK harness and third-party integration is.
Filed with a proposed double-spawn fix direction
(`audit/stage2/finding_F842_python_client_tree_kill_kills_shared_backend.md`);
not fixed in this batch.

## 2.0.8

### Fixed — the boot log no longer grows without bound (F-830)

`~/.stealth-mcp/logs/backend-boot.log` reached **794 MB** on a single developer
machine: ~13 million uvicorn HTTP access-log lines — mostly the client
watchdog's ~2 s probe of every live stdio proxy — appended to one shared file
across every backend the machine had ever started. Nothing could rotate it: the
file is a raw `Popen` stdout/stderr redirect, so the running backend holds its
descriptor for life, an in-process rotating handler never sees those bytes, and
an external rename either fails (Windows) or leaves the child writing to the
old inode (POSIX). The age-based log pruner skipped it too, because a live
backend refreshes its mtime continuously.

Fixed on both sides. The backend's uvicorn run-config now sets
`access_log=False`, so the per-request spam is never emitted — tool calls are
still logged, with a correlation id and a duration, by the existing
`stealth.backend` logger into the size-rotated per-pid file. And the launcher
rolls the boot log aside when it exceeds 16 MB, keeping two numbered siblings:
`singleton._start_server_process` does it on the line where it opens the file
for a *new* backend, which is the only moment in the system's life at which
that rotation is safe. Both the setting and the rotation live in
`logging_setup.py`, the observability spine. An existing oversize file is
rolled at the next backend spawn; the `.1` sibling it becomes can then be
deleted freely, because nothing holds it open.

### Fixed — the log pruner no longer destroys crash post-mortems (F-840)

`prune_old_logs` swept purely on recency, and that is backwards for exactly
the files that matter: a *live* backend keeps refreshing its log's mtime, while
a *dead* one never does, so the sweep preferentially deleted the logs of
processes that had crashed. On 2026-08-30 an OOM-killed worker's
`backend-<pid>.log` and `backend-<pid>-fault.log` were gone by the next
morning and the investigation started blind — the fault log especially, since
`faulthandler` writes it at the C level for exactly the hard crashes that leave
no other trace.

Dead-backend logs now get a retention exemption. The three most recent backend
log sets (the per-pid log, its rotations and its fault log) are kept whatever
their age, and no `*-fault.log` younger than 14 days is ever pruned. The
exemption is deliberately narrow: proxy logs, older surplus backend sets and
`backend-boot.log`'s own rotations are still swept exactly as before, so this
cannot re-open F-830. The backend's startup line also now records its `argv`,
so a post-mortem can tell a console-attached `serve --http` birth from the
detached spawn path.
### Fixed — the stdio proxy reports to Sentry: condemnations, heals, teardowns and evictions now ship (F-827)

`sentry_init()` had exactly two callers — the HTTP backend and the ops CLI. The
stdio proxy had none: the thin entrypoint's stdio branch returns after
`run_stdio_proxy()` and never reaches the `runpy` load that would have brought
the backend's own init into the process. So the one component that owns the
liveness watchdog, the eviction decision and (since F-838) the heal loop — the
component that *decides to disconnect you* — reported nothing at all. The whole
2026-08-30 disconnect saga (F-820 / F-829 / F-838 / F-839) had to be
reconstructed from local log files on one machine; every other install produced
silence.

The proxy now initializes error reporting in its own branch, and only there:
placing it at the top of `main()` would run a *second* init in the same process
the moment `runpy` loads the backend, which is a class of bug this repo has
already paid for once. Because `sentry_sdk` costs ~1.5–2.5 s to import and
initialize — and the proxy's whole value is answering the client's `initialize`
locally and instantly — the init runs on a daemon thread instead of ahead of
the handshake, the same way the backend cold start already does. The proxy's
log handler is now installed in the same place, before the cold-start thread
that writes to it.

Four transitions now ship as structured events: a backend **condemned** by the
watchdog (with the strike timing), a **heal** that re-bridged onto a
replacement (old port → new port, generation), a heal that failed and **tore
the session down** (the only user-visible disconnect left after F-838, reported
as an error), and a backend **evicted for a genuine source change** — never for
F-829's unreadable digest. Every existing log line keeps its exact level and
text; the reports piggyback, they do not replace. Reporting still honours
`STEALTH_MCP_NO_ERROR_REPORTING`, still passes through the one PII scrubber,
and no capture path can raise: a telemetry call that throws cannot cost you a
backend.

Full write-up: `audit/stage2/finding_F827_proxy_invisible_to_sentry.md`.

### Fixed — a file the OS could not read no longer counts as "you edited the source" (F-829)

Reuse identity is the package version plus a SHA-256 of the package's `*.py`
source, and the hash used to return `""` for any OS read error — the same value
the reuse gate reads as "does not match". So a single unreadable file (this
tree lives under OneDrive, where a file being synced is briefly locked) made a
healthy shared backend look source-stale: the next session's cold-start lock
terminated it, disconnected everyone on it, and logged `backend stale (source
changed), evicting` for an edit that never happened. A backend that was
*spawned* during such a hiccup recorded `""` and was then guaranteed to be
evicted by the following session.

The fingerprint now has the third state it always needed. A failed read is
retried three times, 50 ms apart, and only then yields `None` — "unreadable",
which `backend_registry.fingerprint_mismatch` (the one reading of that field)
treats as *unknown*, never as a mismatch: nothing is evicted, no rival backend
is started, and the WARNING names the real cause (`source fingerprint
unreadable: <error>`) instead of blaming a source change. A backend recorded
while the source was unreadable stamps the sentinel deliberately, so the same
"unknown" rule applies to it later. A genuine digest mismatch, a version
mismatch, and a legacy record with no digest all still evict exactly as before.

Full write-up:
`audit/stage2/finding_F829_transient_fingerprint_read_evicts_healthy_backend.md`.

### Fixed — a closing terminal can no longer take the shared backend down with it (F-839)

On 2026-08-30 at 18:43:57 the backend serving every live session — healthy,
its last tool call completed normally two and a half hours earlier — logged
`Received signal 21, initiating cleanup...` and shut down cleanly. Signal 21
on Windows is **SIGBREAK**, a console control event: it can only arrive from a
shared console. Four proxies then confirmed a genuinely dead backend within
thirteen seconds and every attached Claude session disconnected at once. This
is the residual "stealth randomly disconnects" left after 2.0.7's F-820 fix:
that one stopped *false* condemnations of a busy backend; this one stops the
backend actually dying for a reason it was never supposed to be reachable by.

Nothing in the product sends SIGBREAK. Eviction and the `stop` / `restart`
verbs all terminate through `TerminateProcess`, which runs no signal handler
at all — so honoring SIGBREAK served only accidental, session-scoped killers:
a terminal closing, or a client killing its child process tree on exit. One
process shared by N sessions had its lifetime tethered to one of them.

The backend now installs `SIG_IGN` for SIGBREAK. SIGTERM and SIGINT keep the
F-809 hand-off unchanged, so every deliberate stop — including Ctrl+C on a
foreground `serve --http` — behaves exactly as before.

### Fixed — a dead backend no longer takes your session with it (F-838)

When the liveness watchdog confirmed the shared backend was genuinely dead (not
merely busy — that distinction is F-820's and is untouched), the stdio proxy
logged `backend became unreachable; tearing down for reconnect` and exited, on
the assumption that the MCP client would respawn it. Clients do not reliably
respawn a stdio server mid-session, so every real backend death — an OOM crash,
a console `CTRL_BREAK` on a backend born through the foreground `serve --http`
path — showed up as a dead `stealth` server until you reconnected by hand.

The proxy now **heals in place**. On a confirmed death it obtains a replacement
through the very same startup path it used at boot — the same reuse gate, the
same F-808 adoption order, the same cold-start lock — and re-bridges onto it
with a fresh `initialize` handshake, while your stdio connection never drops.
When a shared backend dies and every proxy on it reacts at once, that lock does
what it already does for a startup herd: one cold-starts, the rest adopt.

Calls that were in flight when the backend died are answered with a clear error
naming the method, and are deliberately **not** replayed against the
replacement. Healing is bounded (two attempts, and at most three back-to-back
recoveries before a long-lived generation earns the budget back); when it is
spent, the pre-existing teardown runs exactly as before. Net effect: one slow or
failed call instead of a dead server. New home: `embedded/proxy_selfheal.py`.

Full write-up: `audit/stage2/finding_F838_proxy_exits_instead_of_healing.md`.
### Fixed — concurrent `spawn_browser` calls no longer kill each other's browsers (F-834)

Under an agent fleet (several clients spawning against one backend) most
spawns failed with nodriver's `Failed to connect to browser … you need to pass
no_sandbox=True`, and — worse — a spawn occasionally returned `state: "ready"`
with an `instance_id` whose browser was dead by the very next call. The client
had done nothing wrong: it was the product's own cleanup doing the killing.

The retry/fallback profile clone was named `{base}-{os.getpid()}-{suffix}`.
That pid is the **backend's**, identical for every concurrent spawn in the
process, and the only guard — "does a browser already run in this directory?" —
is false for *all* of them during their pre-launch window. Every loser of the
master-profile race therefore copied into and launched Chrome from the **same**
directory; then a deferred profile delete fired against that shared path and
removed it out from under the one attempt already reported ready.

Three layers, so no single one has to hold alone:

- **Per-attempt directories.** `clone_storage` now stamps a monotonic
  per-attempt token into the name, and consults the existing in-flight
  reservation set (`_protect_clone_dir`) as well as liveness when choosing a
  directory. Liveness is a check, not a reservation — the `-{pid}` /
  `-{pid}-{index}` ladder had the same hole and is gone with it.
- **Cleanup ownership, re-asked at fire time.** `cleanup_deferred_profiles`
  deferred these deletes arbitrarily long ago, so it no longer trusts the
  live-profile snapshot it took at sweep start, and no profile directory a
  *live tracked instance* owns is deleted for another instance's sake — the
  skip is logged.
- **Honest error text.** A spawn that raced siblings now says so and explicitly
  disowns nodriver's root/`no_sandbox` advice, which is a red herring for this
  failure mode and cost two independent diagnosing agents real time. New leaf
  `embedded/spawn_contention.py`, appended at the same one composition site as
  F-811's exhaustion hint.
### Fixed — a page whose JavaScript throws no longer crashes `get_page_content` (F-822)

nodriver's `Tab.evaluate` **returns** the CDP `ExceptionDetails` record in the
value's place when the evaluated JS throws, instead of raising — and returns a
bare `RemoteObject` whenever the value is falsy. F-795 installed the one guard
for that on the `execute_script` path; `dom_handler.get_page_content` calls
`evaluate` three more times, unguarded, so on any page where `document.body` is
null (a bare XML/JSON document, a page caught mid-navigation, a CSP-blocked
eval) a CDP dataclass landed under `text` — and the large-response handler's
very first act, `json.dumps`, died on it:
`TypeError: Object of type ExceptionDetails is not JSON serializable`. The CDP
work had already succeeded; the call died while *measuring* the answer.

The fix is one conversion at the transport boundary, not a per-tool check:
`response_handler.json_safe` returns a payload unchanged when it is already
pure JSON data and otherwise converts every foreign object to plain data,
preferring the object's own `to_json()` so a converted record still carries its
real `text` / `exception` / `className`. `handle_response` applies it once,
before the size estimate, covering both exits (inline and spilled) and all six
call sites. `estimate_tokens` and the spill write also take `default=str`:
measuring or storing a payload must never be able to fail the tool that
produced it. Deliberately a *converter*, not `tool_errors._require_js_value`'s
raise — a page whose `innerText` threw still has real HTML, URL and title to
return. Details in
`audit/stage2/finding_F822_estimate_tokens_crashes_on_cdp_objects.md`.

### Fixed — responses too big to deliver are no longer too small to divert (F-837)

The inline/file threshold sat *above* the MCP client's practical token ceiling,
so there was a dead band. Measured live on 2026-08-30: a **59,734-char**
response came back inline and the client rejected it with "result exceeds
maximum allowed tokens", while 138.91 KB and 282.83 KB diverted to file
correctly. The caller got neither the content nor a file path.

Two compounding errors: the 20,000-token ceiling was too high, and the
`len // 4` estimate is optimistic for the markup-heavy payloads this handler
carries — the rejected response estimated at just 14,933 tokens. The new
`INLINE_TOKEN_CEILING = 10_000` is derived from that failure rather than
rounded to it: the rejection proves under ~2.4 chars/token against a 25,000-token
client cap, so taking 2.0 chars/token as the worst case and budgeting 20,000
real tokens (80% of the cap) gives 10,000 estimated tokens, about 40,000 chars.
The regression size now clears the threshold by 49%; the two already-diverting
sizes still divert; small responses are untouched. Pinned by tests using the
measured 59,734-char size. Details in
`audit/stage2/finding_F837_inline_threshold_above_client_ceiling.md`.

### Fixed — half an emoji in page content no longer destroys the whole tool result (F-823)

Sentry, on `execute_script`: `PydanticSerializationError: Error serializing to
JSON: UnicodeEncodeError: 'utf-8' codec can't encode character '\ud83d' in
position 5811: surrogates not allowed`. `\ud83d` is the *high half* of an emoji
pair — what a page hands back whenever a JS `slice`/`substring` (which indexes
by UTF-16 code unit) cuts between the two halves of a `😀`, or content
arrives mis-decoded. Python's `str` stores it happily; UTF-8 has no encoding
for an unpaired surrogate, so FastMCP's serializer
(`pydantic_core.to_json(data, fallback=str)`) raised and the entire result was
lost — including the ~5,800 characters of good content in front of it.

The repair is one string policy at the one boundary every tool return travels:
`response_handler.surrogate_safe` rewrites each unpaired surrogate to a single
U+FFFD REPLACEMENT CHARACTER and returns the payload **unchanged, by identity**
when there is nothing to repair, and `tool_registry.section_tool` — already the
single registration chokepoint — now applies it to the return of all 94 tools.
It could not live in the large-response handler: `execute_script` and 86 other
tools never touch it, and F-822's `json_safe` probe (`json.dumps`) *succeeds*
on a lone surrogate, because serializable and encodable are different
properties. `json_safe` now calls the same helper, so the file-fallback spill
path and its caller-supplied metadata get one policy rather than a second
implementation.

Deliberately narrower than `json_safe`: this touches strings only and returns
every other leaf by identity, so no tool's payload shape changes. Valid emoji
and other astral characters pass through byte-identically — CPython stores them
as one code point, never as a pair, so a surrogate in a payload is always
broken. U+FFFD rather than `errors="replace"`'s `?`, so the loss is visible and
not confusable with a question mark the page really contained. Details in
`audit/stage2/finding_F823_lone_surrogates_crash_tool_returns.md`.
### Fixed — advertised XPath selectors now work in every tool that advertises them (F-831, [#15](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/issues/15))

Seven element-interaction tools document their `selector` parameter as "CSS
selector or XPath" — `query_elements`, `click_element`, `upload_file`,
`type_text`, `paste_text`, `get_element_state`, `wait_for_element`. Only
`query_elements` honoured one, and it did so through its own
`selector.startswith("//")` branch calling `tab.xpath` **inside the tool path**.
The other six handed the XPath string to `DOM.querySelector` as if it were CSS
and failed. A user reading the tool schema had no way to tell which was which.

Choosing between the two selector *languages* is part of resolving a selector,
so it now lives in `element_resolution` — the one home selector resolution
already routes through:

* `xpath_expression` / `is_xpath` are the one place the choice is made, purely
  syntactically. An explicit `xpath=` prefix (case-insensitive, stripped before
  dispatch) is XPath; otherwise a selector whose first non-space character is
  `/` or `(` is XPath — `//a`, `/html/body`, `(//div)[1]`, and everything the
  deleted branch accepted. No CSS selector may begin with either character, so
  nothing is taken from CSS. `#id`, `.cls`, `div > a` stay CSS, including
  `./div` (use `xpath=./div` for the relative form).
* `resolve_element`, `resolve_elements` and `query_selector_all` all dispatch,
  and all run the XPath call inside the **same** retry/race-recovery the CSS
  paths use — one classifier, one loop, one bound, both languages. An XPath in
  `query_elements` previously got none of that recovery, so a DOM mutation
  mid-query surfaced a raw CDP `-32000` to the caller.
* The `query_elements` branch is **deleted**; it now resolves like every other
  tool. Its returned shape is unchanged and pinned.

Also fixed in passing: `select_option`'s `value`/`index` arms re-resolved the
selector a second time with `document.querySelector(...)` in the page. They now
act on the element already resolved, so they cannot silently match nothing and
still report success.

### Fixed — the nodriver race classifier now reaches `navigate` (F-824)

F-817 taught `element_resolution` to recover from nodriver's two known races:
the CDP `-32000` stale-document error, and the `KeyError(<cdp event class>)`
that `Tab.wait`'s bare `del self.handlers[evt_dom]` raises when two waits
overlap on one tab. Every selector-driven tool inherited that recovery —
**but `navigate` never resolves a selector**, so it never passed through it.
It classified its own errors from a substring list ("connection dropped",
"target closed", …), and a `KeyError` whose argument is a CDP event class
matches none of them: `STEALTH-CHROME-DEVTOOLS-MCP-3N` is exactly that error
escaping `navigate` → `tab.get(url)` → `Tab.wait` → `remove_handler` on the
first of its two attempts, as a raw `KeyError` at the caller.

The fix is reach, not a second classifier. `element_resolution.recoverable_race`
is now the public, single home for "is this one of the known nodriver races",
and `BrowserManager._is_recoverable_navigation_error` asks it instead of
re-listing the signals. Retry budgets are untouched on both sides: `navigate`
still makes at most two attempts, `element_resolution` still re-resolves at
most three times. A genuinely fatal navigation error is still refused on the
first attempt.

### Fixed — a stale document that says "DOM Error while querying" is now recovered (F-828)

The stale-document classifier matched one exact CDP message, "Could not find
node with given id". Chromium has a second reply for the same situation: when
the query reaches the renderer and fails there, Blink answers with its blanket
`DOM Error while querying`. Two live Sentry issues are that message
(`STEALTH-CHROME-DEVTOOLS-MCP-3F`, `-23`) — one of them carrying no numeric
code at all, because nodriver only fills `ProtocolException.code` when the CDP
error object supplies it. Both escaped the retry and crashed the calling tool
(`wait_for_element`, `query_elements`).

The one classifier now matches either message, on message text and never on the
numeric code, so both code-carrying and code-less variants are recovered. The
bound is unchanged: after `_MAX_RESOLVES` re-resolves the original
`ProtocolException` surfaces to the caller exactly as before.
### Fixed — `execute_script` returns your value, not a CDP envelope (F-832, closes #17)

`execute_script` evaluated through `nodriver`'s `Tab.evaluate`, which asks Chrome
for a **deep-serialized** result — a BiDi-shaped graph of `{"type": …, "value": …}`
nodes, capped at depth 10 — and then reads it back with two truthiness tests. Two
things went wrong, from one root cause: the value was *inferred* rather than read.

An object came back as an envelope to unwrap instead of its JSON (the reported
issue), and anything past depth 10 was gone. Worse, `if remote_object.value:`
cannot tell "there is no value" from "the value is falsy" — so a script
evaluating to `0`, `""`, `false` or `null` failed that test and fell through to a
bare `RemoteObject` husk in place of the number, string or boolean you asked for.

The eval now goes through a raw `Runtime.evaluate` with `return_by_value=True`
(no `serializationOptions`, which CDP documents as *overriding* it; `userGesture`
and `allowUnsafeEvalBlockedByCSP` are carried over so CSP-strict pages and
activation-gated handlers do not regress), and the answer is read with an
explicit None-vs-absent check: `undefined` → `None`, `null` → `None` by its own
branch, a present value verbatim however falsy, `Infinity`/`NaN` as their token,
and anything Chrome could not send by value as its description — a `RemoteObject`
is never handed to the transport.

The two behaviours that ride on this path are unchanged: a script that **throws**
still raises, through the same single `_require_js_value` guard (F-795), now fed
the `exceptionDetails` explicitly rather than in the value's place; and a
top-level `return` still gets exactly one retry as a function body (F-812) — by
value too, since that is the path most agent-written scripts actually take.
Details and residuals in
`audit/stage2/finding_F832_execute_script_shallow_serialization.md`.
### Fixed — `set_cookie(same_site=…)` could not set a cookie at all (F-821)

Every `set_cookie` call that supplied `same_site` failed with
`Failed to set cookie: 'str' object has no attribute 'to_json'`; omitting the
attribute worked, which is why the tool looked healthy. nodriver's CDP commands
build their request frame while being advanced and serialise `same_site` with
`.to_json()`, so the plain string an MCP argument carries killed the command
before it reached Chrome. The one CDP cookie boundary
(`network_interceptor.to_cookie_same_site`) now converts it to the real
`cdp.network.CookieSameSite` enum, accepting `Strict` / `Lax` / `None` in any
case and raising a `ToolError` that names the valid values for anything else.

Fixes `STEALTH-CHROME-DEVTOOLS-MCP-3P` (9 events).

### Changed — `search_network_requests` says which argument filters the URL (F-825)

Its summary line was "Search network requests with advanced filters and
pagination", which names no filter at all, so a caller reaching for the obvious
one guessed `url=` or `pattern=` and got a validation error. The summary now
names `url_pattern`, and every filter states how it actually matches
(case-insensitive substring for `url_pattern` / `response_contains` /
`payload_contains` / `resource_type`, whole-value for `method`, exact for
`status_code`).

### Fixed — the environment validator no longer recommends args the stealth filter strips (F-836)

As root or in a container, `validate_browser_environment_tool` returned
`recommended_args: ["--no-sandbox", "--disable-setuid-sandbox", …]` — the exact
flags the stealth filter blocks out of caller-supplied `browser_args`. Anyone
who followed the advice got "Stripped 4 detectable arg(s)" and no effect. The
recommendation is now derived *through* the filter, so it can never name a
blocked flag again, and the flags the environment genuinely needs are reported
in `recommendations` as what they are: applied automatically by `spawn_browser`
after the filter runs, not something to pass by hand. The launch policy itself
is unchanged, and on a normal desktop the tool's output is identical to before.
### Fixed — error reports no longer carry OAuth tokens or email addresses (F-826)

Error reporting is on by default, and this product's exceptions quote the thing
that failed — which is page content. The 2.0.4 scrubber removed what a machine
leaks about *itself* (its hostname, the username in every path) but let the
exception's own message through verbatim. The project's Sentry consequently held
OAuth authorization URLs with `access_token` still in the query, and email
addresses, from third-party installs as well as the maintainer's machine.

`_scrub_event` — still the one `before_send`, still one walk over the event —
now applies two more rules to every string it visits:

* **email addresses** become `[redacted-email]`;
* **URLs** keep their scheme, host and path and lose their secrets:
  `user:pass@` becomes `[redacted]@`, the query becomes `?[redacted-query]`, the
  fragment becomes `#[redacted-fragment]` (the implicit OAuth flow puts the token
  there).

The redaction is deliberately **targeted, not wholesale**: a message that has
been blanked is an issue nobody can act on, which is a slower way of turning
reporting off. `GET https://api.example.com/v1/instances/42 returned 500` is
untouched, and so are the `@`-shaped things that are not addresses —
`/opt/homebrew/opt/python@3.11/…`, `sentry-sdk@2.64.0`, `@sentry/browser`.

Two behaviours are unchanged: an expected `ToolError` is still dropped whole
before any of this runs (F-815), and the username in a path is still `~` rather
than a new spelling. One behaviour is tightened: if the scrub walk itself fails,
the event is still sent — with its type, module, mechanism, frames and tags —
but the free-text fields it could not vouch for are dropped instead of shipped
raw.

No new environment knobs; `STEALTH_MCP_NO_ERROR_REPORTING=true` still disables
reporting entirely.

### Fixed — a tool that fails is now visible in `get_debug_view` (F-835)

After 24 consecutive failed `spawn_browser` calls — a total spawn outage —
`get_debug_view` still reported `total_errors: 0`. The surface an operator
watches to answer "is this thing healthy" said it was, while nothing could
launch. The raised `ToolError`'s only copy went to the MCP client; the failure
path emitted INFO lines and nothing ever turned "this call raised" into an
error record.

The fix is at the one wrapper every registered tool passes through
(`logging_setup.with_correlation_id`, the `section_tool` chokepoint), so it is
**all 94 tools**, not just spawn: an escaping exception is recorded in the debug
ring — filed under component `tool` with the tool's own name, the message, and
the call's correlation id — and then re-raised **unchanged**. Recording never
transforms the error, never records the same exception twice, and can never
break a tool call (a throwing debug ring loses only the recording). Repeats
still dedup as they always have, so 24 identical failures read as one entry with
`stats["tool.spawn_browser.errors"] == 24` — what can no longer happen is `0`.

Scope, deliberately: the **in-memory ring only**, not the backend log file. A
failure message echoes the caller's own arguments, and F-782's condition for
logging it — redact the record first — is unanswered (F-826). The ring is
process-local and returns only to the client that already holds those bytes; the
log is durable and Sentry-bridged, so it stays clean and F-782 keeps its log
half.

`clear_debug_view` now also forgets the de-duplication signatures. Without that,
"clear the view and watch" — the operator loop for a live outage — would show an
empty ring forever, because each repeat was deduped against a signature whose
entry had just been cleared.

### Fixed — `go_back`, `go_forward`, `reload_page` and `new_tab` stop reporting success over a Chrome error page (F-833)

`navigate` has been truthful since F-802: a navigation Chrome could not perform
still *completes* — Chrome commits a `chrome-error://` page and every
Python-side step around it succeeds — so the tool raises instead of answering
success over it. The four other tools that move a tab never got that guard. A
dead history entry, an offline reload, and a new tab whose initial URL will not
load each landed on the same error page, and each still said it worked; the
caller's next `query_elements` or `get_page_content` then described Chrome's
error page as the page they thought they were on.

All four now raise, through the *same* guard `navigate` uses — one error-page
detector, five call sites — naming which move failed and what it landed on. The
loaded-page half is unchanged and asserted alongside it: a 404, a redirect,
`data:` and `about:blank` are landings, not failures. `new_tab` closes the tab it
could not land, so the honest error does not cost you a stranded tab.

Two things this deliberately does not change: `reload_page(ignore_cache=…)` is
still dropped (F-800, open), and a `go_back` with no history entry behind it
still reports success for a move that never happened — a different kind of
untruth, pinned as characterization and written up in
`audit/stage2/finding_F833_navigation_tools_lack_truthfulness_guard.md` with the
residual settle race.

## 2.0.7

### Fixed — the watchdog no longer disconnects every session when the shared backend is briefly slow (F-820)

This is the user-reported "stealth randomly disconnects", and it was neither
random nor per-session. The stdio proxy's liveness watchdog tore itself down
after three consecutive misses of its 2s `initialize` probe — about six
seconds of slowness. Under a multi-session fleet the one shared backend
answers that probe in more than 2s for stretches of 20–40s while serving
everyone perfectly well, and because every proxy probes the *same* backend
they all reached the same wrong verdict in the same second: production logs
for 2026-08-30 show four waves (7, 30, 3 and 10 proxies) torn down while
backend pid 52396 went on serving `navigate` and `screenshot` throughout, with
the strike counters visibly resetting in between — the signature of slow, not
gone.

Three strikes now open a **confirmation phase** instead of passing sentence,
and the verdict comes from the gate the cold-start lock already trusts
(`_same_identity_backend_ready`, F-807) rather than a second busy-vs-dead
policy: **busy** answers inside the existing 60s patience window and the proxy
stays up; **dead** fails on the first refused connection and buys none of it,
so hard-down detection keeps its ~12s window; **wedged** (socket open, nothing
answering) is still condemned, only now confirmed first — which costs it up to
60s more. The fast loop, its interval, its timeout and its strike counter are
unchanged. Details and residuals in
`audit/stage2/finding_F820_watchdog_condemns_busy_backend.md`.

### Fixed — the masked User-Agent no longer advertises a Chrome version the browser no longer has (F-806)

The stealth mask renders the browser's major version into a `--user-agent=`
launch flag, and that version was probed once and cached on the executable's
**path**. Chrome updates in place, so under the long-lived backend the cache
could not see an upgrade: the mask kept claiming `Chrome/150` while the browser
it was masking — and the `sec-ch-ua` client hints Chrome generates from its own
build — said `151`. A User-Agent that contradicts its own client hints is a
sharper tell than the headless token the mask exists to remove. It turned the
macOS stealth-gate cell red against byte-identical product code.

Three defenses now.

**The Windows probe reads the binary.** It used to list the version-named
directories beside `chrome.exe` and take the newest. Chrome's updater lands that
directory long before it swaps the launcher stub, and the browser keeps running
the old build until it next restarts — days on a workstation — so during that
whole window the probe answered with a version Chrome would not run, and the
first spawn of every fresh backend shipped a skewed UA. It now reads
`chrome.exe`'s own embedded file-version resource, which is the executable
answering for itself; the directory scan remains as the fallback for a binary
whose resource cannot be read, so no machine gets a worse answer than before.
(Windows still does not shell out: `chrome.exe --version` hands the flag to an
already-running Chrome instead of printing.)

**The memo expires with the binary.** The version probe is memoized on the
executable's on-disk identity — `(mtime_ns, size)` — rather than on its path, so
an in-place upgrade expires it while an unchanged binary is still probed only
once.

**The launched browser has the last word.** Every spawn reads CDP
`Browser.getVersion` after launch and writes the *actual* launched version back,
so a version that changed between probe and launch corrects every later spawn.
`Browser.getVersion`'s `product` field is not rewritten by `--user-agent=`,
which is what makes it authoritative — the regression test re-measures that on
every run rather than assuming it.

That third defense is now **bounded**. This fix shipped in 2.0.3 and was pulled
back out of it: the post-launch read was the first await of every spawn and had
no timeout, so against a stale or dead CDP connection a probe that must never
even *fail* a spawn could hang one indefinitely. It waits 10 seconds — the same
bound the pre-launch version probe already uses for the same question — then
cancels the read, logs, and leaves the mask exactly as the pre-launch probe set
it. A spawn is never delayed by more than that, and never fails because of it.

### Fixed — CI: the image's Chrome is frozen at run start, so a red macOS cell means red (F-819)

No product change. `tools/resolve_chrome.py` — the one home of the expected Chrome
identity — gains `--freeze-updater`, and every CI invocation now passes it.

GitHub's macOS runners let Google's Keystone updater upgrade Chrome Stable in place
*while a job is running*. The gate resolves the image's Chrome identity at the top of
each browser job and then trusts it minutes later, when the browser actually launches,
so an upgrade in between makes the two readings describe two different binaries. PR
#64 showed it twice on byte-identical trees: CDP `Browser.getVersion` reported
`Chrome/151.0.7922.76` against a resolved identity of `150.0.7871.187`, and on the
re-run the same swap surfaced through the UA-coherence gates instead. No product fix
can close that — F-806 already narrowed the product-side window as far as measurement
allows; a program cannot make a binary hold still between two measurements.

So the run environment is frozen instead. Before the version is read, the flag
neutralises the OS's updater: on macOS it unloads and deletes Keystone's launchd jobs
and both `GoogleSoftwareUpdate` trees, then leaves each path as a root-owned
unwritable stub inside a root-owned parent — Chrome re-registers Keystone every time
it launches, and this run launches Chrome, so removal alone would not survive the very
act it has to survive. On Windows it stops and disables the two Google Update
services, disables the machine update tasks, and sets the enterprise policy that
forbids updates. On Linux it does nothing and says so — the images ship no background
updater.

Every sub-step is best-effort: exit codes are recorded, never checked, and an absent
service, task, plist or directory is the normal case on at least one OS. The freeze
narrates to stderr; stdout stays the identity JSON alone. Without the flag, behaviour
is byte-identical to before, and `resolve_chrome()` itself remains side-effect-free,
so importing the module can never touch a developer's machine.

## 2.0.6

### Fixed — a failed spawn tells you the machine is out of process capacity (F-811)

When Chrome could not launch because the machine had run out of process capacity,
`spawn_browser` surfaced nodriver's raw
`ToolError: Failed to spawn browser: --- Failed to connect to browser ---` with no
indication that hundreds of browser processes were live. The caller — usually an
agent — read an opaque string and retried, which made the exhaustion worse.

Everything needed to make that error actionable was already present and simply not
consulted: the CLI ships the remedies, `browser_pid_registry` knows which browsers
are tracked, and psutil is already a dependency. A failed spawn on a machine showing
an exhaustion signal now appends a paragraph naming the live Chromium-family process
count and the tracked-browser count, then the two remedy commands in order —
`stealth-chrome-devtools kill-orphans --force` (with why `--force` is required: the
command refuses while a backend is alive, and a spawn failure is by definition raised
by a live one), then `stealth-chrome-devtools cleanup --apply` to reclaim the profile
directories — and the honest limit that processes we do not track are not ours to
reap.

Below the threshold the error is byte-identical to before. Nothing is killed,
throttled, or retried differently, and nothing runs on the success path: the
measurement happens exactly once, on a spawn that has already failed. The threshold
is a module constant, not a new `STEALTH_MCP_*` knob, and it fires on the measured
signal rather than on nodriver's message text, so a nodriver upgrade cannot silently
switch it off.

## 2.0.5

### Fixed — a headed spawn just works, even from a backend with no desktop (F-810)

2.0.4 made an invisible headed spawn impossible by **refusing** it (F-808). Refusing
is honest, but it is not what you asked for: you asked for a browser you can see.

On Windows, `spawn_browser(headless=False)` from a backend whose display context
cannot show a window now delegates Chrome's **process creation** to Task Scheduler —
a one-shot task that runs "only when the user is logged on" — so **Windows itself**
puts the process in the logged-on user's interactive session and the window is
visible by construction. The same backend then attaches to it over CDP, so there is
still exactly one backend, the instance appears in `list_instances` like any other,
and all 94 tools work unchanged. No env knob, no installer, on by default.

This amends the F-808 ruling in mechanism, not in spirit: the tool still never picks
or enters a session, and `display_context.py` is unchanged and still observational.
The one new OS read answers "is anyone logged on at all", never "which session".

The F-808 refusal is now the **fallback** — it fires only when delegation is
impossible (not Windows, nobody logged on) or fails, and its message says so. That is
exactly the situation a loud error is correct for.

### Fixed — a clean backend stop no longer ships ERROR noise to Sentry (F-809)

Stopping the backend cleanly (`stealth-chrome-devtools stop`, or SIGTERM) produced
1-3 ERROR-level Sentry events per shutdown: process_cleanup's signal handler
**replaced** uvicorn's own handler and `sys.exit`-ed from inside it, unwinding the
event loop abnormally. Every clean stop looked like a crash in the error stream,
burying real errors.

The handler now records the disposition that was installed before it and **hands
the signal back** after cleanup, and uvicorn gets a positive graceful-shutdown
timeout so in-flight requests drain instead of erroring. A clean POSIX stop now
exits via SIGTERM's default disposition with zero ERROR lines — pinned by an e2e
test that accepts *only* exit 0 or `-SIGTERM` (a crash still fails it).

Closing the loop on Ctrl+C: handing SIGINT back meant uvicorn re-raised it as a
`KeyboardInterrupt` that escaped `main()` — a traceback and an unhandled-exception
Sentry event per Ctrl+C, on both the HTTP backend and the default stdio `serve`
(where Ctrl+C is the only way out). The entry-point shim now converts it to a
quiet exit 130; the interrupted frame is still logged at DEBUG so an interrupt
during a wedged startup keeps its diagnosis.

## 2.0.4

### Fixed — a headed spawn opens a browser you can actually see, or says why not (F-808)

`spawn_browser(headless=False)` could return `state: "ready"`, `headless: false`
and `window_size.measured: true` while producing a browser that was **permanently
invisible**. Chrome inherits its parent's window station, so visibility is decided
by whoever launched the backend — not by the `headless` flag and not by the caller.
One session cold-starting the shared backend from an SSH login or a Windows service
session (Session 0, isolated since Vista) poisoned headed browsing for **every**
session on the machine, including the ones running on the physical desktop. Every
signal the server had said success, because none of them observed a window: CDP
attaches to the process, and `take_screenshot` captures the compositor surface
whether or not it is displayed. This was a regression from 1.0.0, where each client
ran the server in-process and Chrome was always a descendant of the session that
asked.

The fix has two halves, and the first is the one that closes the report.

- **The backend a session adopts now depends on where windows can be shown.**
  `server.json` records one backend per **display context** — an observed token
  naming the desktop a process could put a window on (`win-session-N`,
  `wayland-…`, `x11-…`, `aqua-<uid>`, or `headless` / `unverified`). Discovery
  prefers a window-capable backend, and adoption is deliberately asymmetric: a
  client that cannot prove it has a desktop adopts **any** backend, window-capable
  first — which is exactly what makes an SSH session's headed spawn land on the
  desktop backend and open on the real screen — while a client that can prove one
  adopts only its own context's backend. Nothing tries to *find* the interactive
  session: on the reporting machine the active console session was 2 while the
  user's desktop was session 1, so every "pick the interactive session" heuristic
  is wrong on somebody's machine. The cost is one extra backend process on a
  desktop box that is also SSH'd into, which is the correct trade against invisible
  browsing.
- **Where no window-capable backend exists, the spawn raises.** A headed spawn in a
  context that cannot display a window now fails with a `ToolError` naming the
  context and both remedies — start a backend from a desktop session, or pass
  `headless=True` — instead of handing back a browser nobody can see. It refuses
  before cloning a profile directory, so a doomed spawn costs no disk. There is no
  silent headed→headless degradation; that is the same defect wearing a different
  hat. **Headless spawns are unaffected from any context**, which is what CI
  depends on.

`stealth-chrome-devtools doctor` now prints one line per recorded backend with its
display context and whether that context can show a window, plus an explicit remedy
line when none of them can.

Fixes `STEALTH-CHROME-DEVTOOLS-MCP-K` — 66 nodriver "Failed to connect to browser"
events on 2.0.3, all from headed spawns driven over the magent/psmux SSH path
against a backend that had no desktop to put a window on. That is F-808's signature
seen from the other end, and it is closed by the adoption fix above.

### Changed — `server.json` is schema v2, and your existing record is not evicted

The record grew from a flat `{port, version, pid, source_fingerprint}` to
`{"schema": 2, "backends": {"<display context>": {…}}}`, so one machine can hold a
headless backend and a desktop backend at once. **Records written by 2.0.3 and
earlier still read**, as one backend classified `unverified` — which every client
treats as adoptable — so upgrading does not evict the backend you are currently
using. The v1 entry is superseded in place the first time a 2.0.4 backend records
itself on that port: recording supersedes any other entry claiming the same port,
because only one process can hold a loopback listener, so a second entry naming it
is by construction a leftover. Without that rule the stale entry would sort first
forever and force a kill-and-respawn of the shared backend on every proxy start.
Reading the record belongs to `embedded/backend_registry.py`; nothing outside it
branches on the schema.

### Fixed — concurrent backends stop erasing each other's tracked browsers

`browser_pids.json` was read and rewritten whole by every writer, so two backends
running at once — which schema v2 now makes an ordinary state — clobbered each
other's entries, and the loser's browsers became untrackable orphans nothing would
ever reap. Every write now read-merge-writes under a sibling lock
(`browser_pids.json.lock`), with the record's schema, its owner stamp, and that
protocol living in one new module, `embedded/browser_pid_registry.py`.

Entries also carry the identity of the backend that started them (`owner_pid` and
`owner_create_time`), and recovery reaps only browsers whose owner backend is
**dead** — the distinction the old `create_time` guard could not draw, since every
already-running backend's browsers predate a starting backend's import. `kill-orphans`
now drops only the entries it actually reaped, leaving other backends' entries
alone, and `--force` bypasses the ownership check as well as the live-backend
refusal, so it still does what an operator reaches for it to do. There is
deliberately **no schema bump**: an entry with no owner keys is a 2.0.3 entry, and
that absence is exactly what makes it a reclaimable orphan after an upgrade.

### Fixed — the window-size clamp is attributed to the right desktop (corrects F-804)

2.0.1 reported window sizes truthfully but explained the clamp as headed Chrome
fitting "the desktop work area", read as an ordinary monitor limit. That reasoning
concluded a workstation driving an RTX 3080 had a ~1024x768 screen. The real clamp
was **Session 0's small default desktop** — the same root cause as F-808. The
remedy is unchanged (`spawn_diagnostics.window_size` still reports `requested`,
`actual`, `inner_viewport` and `clamped`); the docstrings on `spawn_browser` and
`window_sizing` now say the clamp is to the **launching** context's desktop,
which is the user's monitor only when the backend runs on it.

### Fixed — test runs no longer ship injected failures to the real Sentry

Error reporting is on by default and `LoggingIntegration` forwards every
ERROR-level log, so a local test campaign — which deliberately injects failures —
pushed roughly 50,000 noise events into the live project in 15 hours. `conftest.py`
now sets `STEALTH_MCP_NO_ERROR_REPORTING=1` as a session-wide default alongside its
existing env guards, and because the singleton strips only
`STEALTH_MCP_NO_AUTO_RECOVERY` from a spawned backend's environment, real-Chrome
integration backends inherit the mute too. An explicitly-set value still wins, so a
CI cell that *wants* reporting keeps it.

### Changed — error reports no longer carry your username or machine name

Error reporting is on by default, and this release is the one that stopped
pretending it only ever runs here. Events arriving from third-party installs of
2.0.3 carried **their** Windows usernames — in stacktrace frame paths, in the
recorded command line, and inside exception messages such as
`No such file or directory: 'C:\Users\<name>\…'` — plus **their** machine names, as
Sentry's `server_name`.

The reporting stays: it is how two real bugs on machines nobody here owns were
found. What changes is that every event now passes through a scrubber before it
leaves your machine. `server_name` is dropped, and the home-directory segment of
every path is replaced with `~` — `C:\Users\~\…`, `/home/~/…`, `/Users/~/…`, plus
UNC shares and the `/var/home` layouts — regardless of which OS produced the
event, since a Windows maintainer receives Linux users' reports and the reverse.
Account names containing spaces are handled too.

Separately, **local variables are no longer captured**. The SDK records every
frame's locals by default, and in this product a local can hold a proxy
password, an `Authorization` or `Cookie` header, or a script you passed in —
values that are secret in themselves, which no amount of path scrubbing would
have fixed. The project's own canary suite already treats those classes as
release blockers on every other surface; error reports now match.

What a maintainer actually debugs from is deliberately untouched: the release,
the environment, the exception type and mechanism, the failing source line, and
the module path *after* the home segment.

This is universal — there is no maintainer-only exemption and no way to opt back
into sending the identifying fields. The README now discloses what a report
contains and how to switch it off (`STEALTH_MCP_NO_ERROR_REPORTING=true`).

### Known gaps

Recorded, not fixed here; each is a row in the `DESIGN.md` §10 known-debt ledger.

- A clean shutdown on Linux and macOS still logs at ERROR, so every graceful stop
  ships Sentry events (`STEALTH-CHROME-DEVTOOLS-MCP-1J`, `-1H`). Two independent
  causes: our signal handler replaces the HTTP server's rather than handing control
  back to it, and FastMCP pins a zero-second graceful-shutdown budget, which always
  times out. Windows is unaffected. (F-809)
- A cold-start lock **loser** can poll a port the winner never bound for up to 120 s
  before self-healing, when a foreign process squats the preferred port (F-509 A2).
- Handled tool errors reach Sentry at full volume: the durable debug log is
  deliberately un-deduped, and the same records feed error reporting. One user-script
  `SyntaxError` produced 132 events (`STEALTH-CHROME-DEVTOOLS-MCP-P`).
- `debug_logger` records emitted inside a **stdio proxy** reach no log file — only the
  backend role has a handler for them.
- A single unreproduced WebSocket 404 during post-launch window measurement
  (`STEALTH-CHROME-DEVTOOLS-MCP-1E`); measurement is already guarded, so the visible
  cost is `measured: false`.
- `~/.stealth-mcp/server.port` is still written and still has no reader.
- A clone directory shielded from the storage sweep stays shielded if its spawn dies
  before the instance exists.

## 2.0.3

### Fixed — the shared backend no longer absorbs the host project's `.env` (#56)

`Settings` read `.env` from the **current working directory**, and MCP clients
launch this server with cwd set to whatever project folder the user opened. The
backend therefore configured itself from that project's application config. Under
the model's `extra="forbid"` schema this was fatal, not merely wrong: a folder
whose `.env` held nothing but `DATABASE_URL` and `NEXT_PUBLIC_*` killed the
backend at startup with a `ValidationError`, for **every** session connected to
it — an ordinary Next.js repo was enough. The same read silently adopted a host
`PORT=3000` or `DEBUG=true` as this server's own.

The `.env` file is now read from `~/.stealth-mcp/.env` — the state dir that
already holds the logs, the port file and `server.json`. A project-local `.env`
is never read. `extra="forbid"` is kept deliberately: with the file scoped to our
own state dir, strictness protects the operator from typos in a file they wrote
instead of punishing them for one they did not. Operators who had put keys in a
project `.env` must move them to `~/.stealth-mcp/.env` (see `.env.example`).

### Changed — error reporting is on by default and reads no `SENTRY_DSN` (#55)

`SENTRY_DSN` is the single most common key in a product repo's `.env`, so the
opt-in knob for *our* error reporting was in practice a switch the host project
flipped: the backend adopted the app's DSN and shipped this tool's crashes into
someone else's project. There is no `sentry_dsn` setting any more.

Reporting now goes to this project's own hardcoded DSN — the one previously
published in the README, and public by design, since a DSN is an ingest address
and not a credential. `sentry-sdk` moved from the `[sentry]` extra into the
package's dependencies (a default-on feature that only works if you remembered to
install something is not on), and the extra is kept, empty, so existing
`pip install stealth-chrome-devtools-mcp[sentry]` command lines keep resolving.
`sentry_init()` can no longer raise: a missing SDK degrades to a logged warning,
where it used to abort startup with a `RuntimeError`. Opt out with
`STEALTH_MCP_NO_ERROR_REPORTING=true`.

## 2.0.2

### Fixed — multi-session cold start can no longer evict the backend it is racing (F-807)

The singleton's cold-start lock used to be released when the backend's socket
bound, while the reuse gate demands an answered MCP `initialize` on a single 2s
probe. A session acquiring the freed lock inside that gap — or while the backend
was busy absorbing a fleet of simultaneous startups — concluded "not reusable",
**terminated** the healthy backend everyone else was using, and double-spawned.
The winner now holds the lock until the backend is genuinely MCP-ready, and a
lock-holder gives a same-identity backend (version AND source fingerprint both
match) up to 60s of retried probes before it may evict. A stale record still
evicts immediately (upgrades take effect now), and a dead one (no socket, no
live process) skips the wait entirely, so crash-recovery cold starts stay fast.

### Added — startup-herd scale test

`tests/test_startup_herd.py` starts **40 real stdio launcher processes at
once** against a cold isolated workspace and requires every session to finish
`initialize` + `tools/list` within 30s, with exactly one logical backend
spawned, plus a warm-join bound for a 41st session. Measured on a Windows
workstation: full 40-session cold herd usable in **7.9s**, warm join **1.0s**.

## 2.0.1

### Fixed — two tools no longer report success for an operation that failed

Both defects had the same shape: the operation failed *at the browser* while every
Python-side step around it succeeded, so the tool assembled a payload whose `success`
said it worked.

- **`navigate` raises instead of reporting a Chrome error page as a success (F-802).**
  Navigating to a host that does not resolve (or refuses the connection, or fails the
  TLS handshake) used to return `{"url": "chrome-error://chromewebdata/", "success":
  true}`. It now raises a `ToolError` naming the requested URL and the error page. A
  page that merely answered 404/500, a redirect to a different final URL, `about:blank`
  and `data:` URLs are **not** failures and are unaffected.
- **`execute_script` raises when the script throws (F-795).** `nodriver`'s
  `Tab.evaluate` returns the CDP `ExceptionDetails` record *in the value's place*
  instead of raising, so a throwing script came back as `{"success": true, "error":
  null}` with the exception nested inside `result`. It now raises a `ToolError`
  carrying the exception text. The success envelope is unchanged.

Callers that branched on `result["success"]` will now see a raised tool error where they
previously saw a success they could not act on.

### Fixed — spawn can no longer hang forever on a silent client (F-790)

The default (unnamed) `spawn_browser` path sends a `roots/list` request to the MCP
client and awaited the answer with no deadline. MCP roots is an *optional* client
capability, so a conforming client that never answers parked the tool call forever.
The round trip is now bounded by `STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS`
(default 5 s, `0` = never ask); on expiry the spawn falls back to the same local
seed chain an unsupported client already used. Clients that answer are unaffected.

### Fixed — network capture rows are typed, filterable, and free of browser noise (F-803)

- `resource_type` was `null` on every captured request in every prior release:
  nodriver's CDP dataclasses spell the field `type_`, and the interceptor read
  `event.type` behind a `hasattr` guard that turned the permanent miss into `None`.
  It is now populated (Document, XHR, Fetch, Script, …).
- Consequently `list_network_requests(filter_type=…)` could never match anything;
  it now works, case-insensitively.
- Browser-internal traffic (`chrome://`, `chrome-extension://`, `devtools://`,
  `chrome-error://`, `about:`) is no longer captured by default — it drowned real
  requests 24-to-1 on an ordinary page load. Opt back in per instance via
  `set_network_capture_filters(capture_internal_urls=True)` or process-wide via
  `STEALTH_MCP_NETWORK_CAPTURE_INTERNAL_URLS`.

### Fixed — window size is reported truthfully (F-804)

Headed Chrome clamps its window to the desktop work area; the spawn result echoed
the *requested* size as if applied (1920x1080 requested, ~1028x617 delivered).
Spawn diagnostics now report `requested`, measured `actual`, the real inner
viewport, and a `clamped` flag; `instance.viewport` is the measured size.
Headless remains unclamped and exact.

### Added — real-transport soak coverage

A 62-operation soak journey (`tests/test_soak_stability.py`) drives one instance
over real stdio with a hard per-call deadline: navigations (including deliberately
unresolvable hosts), throwing scripts, tab churn, screenshots, cookies. Any overdue
reply fails the suite by name; the journey ends with a clean close and a
no-leftover-Chrome-children assertion. It also characterized **F-805** (a selector
that never resolves costs nodriver's default 10 s regardless of the caller's
timeout) as a strict xfail pending its fix.

## 2.0.0

The first release since the foundational audit. It carries ~85 commits since 1.2.0 and
fixes four defects that were present in every prior release and invisible to the old
test suite, because none of them can be reproduced through the in-process test seam —
they only appear over the real stdio transport a client actually uses.

### ⚠️ Breaking

- **`STEALTH_MCP_SESSION_STORAGE_CAP_GB` is now `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`.**
- **`--session-cap-gb` is now `--browser-session-cap-gb`.**

  There is **no back-compat alias**. The old names are simply not read, so if you set
  either one it stops taking effect **silently** on upgrade — your storage cap reverts to
  the default. The rename removes a genuine ambiguity: "session" meant three different
  things across this codebase (an MCP protocol session, a Claude Code session, and a
  profile-backed browser session), and only the last one was ever meant here.

  Note the environment namespace is strict: an unrecognised `STEALTH_MCP_*` variable is
  rejected at startup rather than ignored, so a stale name fails loudly at the *next*
  restart even though the setting itself silently lapsed.

### Fixed

- **Browsers were destroyed every ~2 seconds over real stdio.** FastMCP runs the server
  lifespan once per *MCP session*, and the liveness watchdog's probe sessions each re-ran
  orphan recovery and its destructive teardown — killing every live browser instance
  belonging to the real session. Anyone driving this server the normal way (stdio proxy →
  detached backend) had instances disappear underneath them. The lifespan is now
  session-reentrant.
- **`list_tabs` raised a bare `TypeError` after any `close_tab`.** nodriver re-adds
  rediscovered targets as raw `Connection` objects, which are not awaitable, and the tool
  awaited each one. Once a tab had been closed the failure was permanent for that
  browser, not transient.
- **Every navigation after a `close_tab` silently switched tabs and leaked one.** The
  same root cause, but swallowed by a broad exception handler: the tracked tab was found
  correctly, the liveness check on it raised, and the handler concluded the tab was
  "missing or invalid" and replaced it — without closing the original. No error surfaced;
  navigation simply happened in a different tab each time, and the abandoned tabs
  accumulated.
- **`close_tab` returned `False` for a closeable tab**, and **`switch_to_tab` failed to
  activate**, for the same class of rediscovered target. Both now address the target by
  id through CDP, which works regardless of object type.
- **Headless mode advertised `HeadlessChrome` in its User-Agent.** That is the cheapest
  bot check that exists — one server-side substring test, before any JavaScript runs —
  and it contradicted the product's central claim. A default headless spawn now presents
  the same User-Agent the same binary presents headed, on the page, on the wire, and at
  the CDP level. An explicitly supplied `user_agent` still wins. See *Known limitations*
  for what this does **not** fix.
- **Every spawn enabled catch-all network interception, even with no hooks defined.**
  Chrome paused every request and waited for a resume that only the hook handler would
  send, so all traffic paid a pause plus a CDP round-trip for no benefit. Interception is
  now armed only when there is something to intercept — and, relatedly, a hook created
  *after* spawn now arms interception through the same path instead of relying on the
  catch-all's accidental coverage.
- **Selector resolution could hit stale-node `-32000` errors under DOM churn**, because
  nodriver's `select`/`find`/`query_selector` are not atomic. All selector resolution now
  routes through a single resolver that survives document-node invalidation.
- A Tier-A pass on silent-correctness and "lying success" defects — cases where a tool
  reported success without having done the thing (PR #41).

### Added

- A **three-OS release gate** (Ubuntu x64, Windows x64, macOS ARM64) that exercises the
  real stdio transport against real Chrome, asserts the exact Chrome binary identity, and
  gates on a single aggregate check. Previous releases were verified on Ubuntu only.
- **Build-once packaging.** The distribution is built exactly once per commit, hashed,
  verified, and installed from that same artifact in smoke tests; the publish step
  downloads those bytes, re-checks their SHA-256, and uploads them without rebuilding.
  What was tested is what ships.
- A **deterministic offline stealth suite** asserting anti-detection invariants against a
  vanilla-Chrome control, so a regression that reintroduces an automation tell fails the
  build.
- The **source distribution shrank from 15 MB to 592 KB.** It had been shipping 12.8 MB
  of demo media and 2.8 MB of internal audit documents. The wheel — what `pip` and `uvx`
  actually install — is unchanged at 192 KB; this only affects installing from source.

### Known limitations

Stated explicitly rather than by omission.

- **macOS: navigation is unverified.** On GitHub-hosted macOS/ARM64 runners, Chrome
  launched by the detached backend completes no network navigation (reproducible 11/11);
  a connection to a *closed* port hangs rather than being refused, so the request never
  reaches the network stack. The cause is unknown and it has **never been reproduced on a
  real Mac** — hosted runners differ in ways that plausibly matter. The gate therefore
  excludes the macOS transport cell and runs macOS install-smoke without navigation. This
  release makes **no claim that macOS navigation works, and none that it is broken.**
  Linux x64 and Windows x64 are verified.
- **Headless is not "undetectable".** The User-Agent fix closes the cheapest and most
  widely deployed check, but supplying a User-Agent override makes Chrome blank its
  high-entropy client hints (`architecture`, `bitness`, `platformVersion`,
  `uaFullVersion`, `fullVersionList`). Low-entropy hints and every `sec-ch-ua*` header on
  the wire remain correct and coherent, so the residue is reachable only from JavaScript
  that explicitly calls `getHighEntropyValues()` — a strictly smaller and more expensive
  tell than the one it replaces, but a real one.
- **`switch_to_tab` can still store a rediscovered target** as an instance's main tab.
  Activation is fixed; the storage path is not. It fails loudly if it fires.
- **The HTTP transport is unauthenticated and loopback-default by design.** All
  verification here covers stdio; stdio evidence licenses no HTTP claim.
- **Not evidenced in this release:** scheduled drift observation, deterministic
  site-breadth corpus, manual-QA parity tripwire, performance and resource budgets,
  fault-injection/resilience, runnable-documentation checks, the security/trust-boundary
  matrix, wire concurrency/cancellation and independent-client interoperability,
  upgrade/migration smoke, failure-observability, and worker/PWA/internationalized site
  shapes. These are planned work that has **not** been performed — do not read their
  absence as a passing result.
- **Per-tool verification depth varies, and the release contract says so per tool.** All
  94 tools have end-to-end coverage driving real Chrome against a local fixture,
  enforced by a set-equality tripwire (94 covered, 0 exempt). But only `set_cookie`,
  `get_cookies` and `clear_cookies` are additionally verified over the **real stdio
  transport** your client actually speaks; the rest are exercised through an in-process
  test seam that bypasses the wire. That distinction is not academic — every one of the
  transport bugs fixed above was invisible to the seam. It is a gap in test placement
  rather than a known defect in those 91 tools, which is why the contract calls them
  *served* rather than *release-qualified*.
- A known flake exists in one packaging smoke cell (`install-smoke (sdist Linux/X64)`) on
  Chrome cold-spawn; it passes on re-run.

### Upgrading from 1.x

1. Rename `STEALTH_MCP_SESSION_STORAGE_CAP_GB` → `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`
   and `--session-cap-gb` → `--browser-session-cap-gb` wherever you set them.
2. Pin the new version, e.g. `uvx stealth-chrome-devtools-mcp==2.0.0`.
3. Restart the backend. Code changes apply via a fresh backend process — the singleton is
   version-gated, so an old backend is evicted rather than reused.

## 1.2.0 and earlier

Not tracked in this file; see the repository history.
