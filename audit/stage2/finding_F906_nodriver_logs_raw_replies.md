# F-906 — nodriver logs raw CDP replies, and only the inherited log LEVEL stood between them and our sinks

**Status:** fixed
**Date:** 2026-09-21
**Area:** `embedded/logging_setup.py` (the observability spine)
**Follows:** F-902 (`cdp_transport`'s parse guard), whose §6 named this as the residual
**Measured against:** nodriver 0.47.0, websockets 16.0, sentry-sdk 2.64.0, CPython 3.13

---

## 1. The finding

`nodriver` writes the **whole raw CDP reply** into its own log text, and
`websockets` writes the frame under it. Cookie names and values ride in both.
Copied from the installed source:

| Site | Level | What it interpolates |
|---|---|---|
| `nodriver/core/connection.py`:445 | `DEBUG` | `logger.debug("got answer for (message_id:%d) => %s", tx.id, message)` — `message` is the **entire parsed reply**, so `Storage.getCookies` puts every cookie name and value in it |
| `nodriver/core/connection.py`:451-455 | `INFO` | `logger.info("%s: %s  during parsing of json from event : %s" % (type(e).__name__, e.args, message), exc_info=True)` — the **whole event message**, and pre-interpolated with `%`, so it is already inside `record.msg` with `record.args` empty |
| `nodriver/core/browser.py`:824 | `DEBUG` | `"saved cookie for matching pattern '%s' => (%s: %s)"`, `cookie.name`, `cookie.value` — a cookie's name and value **outright** |
| `nodriver/core/browser.py`:869 | `DEBUG` | the same, on the load path |
| `websockets/protocol.py`:609 | `DEBUG` | `logger.debug("< %s", frame)`. `Frame.__str__` truncates past ~75 characters (measured), so a **short reply is printed whole** — the same payload one layer down |

F-902's implementer and reviewer were right that none of these can reach a
handler in this product **as shipped**. What they did not state is that the
protection is the **LEVEL and nothing else**, and the level is **root's to give
away**: those loggers carry no level of their own, so `getEffectiveLevel()`
walks up to root. One `logging.basicConfig(level=DEBUG)` anywhere in the
process — a test, a notebook, a caller embedding this backend — hands it away
for every library at once.

Two things make that more than theoretical here. The backend's **stderr is
redirected into `backend-boot.log`**, a durable file that `roll_boot_log` keeps
up to 48 MB of; and Sentry's `LoggingIntegration` breadcrumb handler sits at
**INFO**, which is exactly where `connection.py`:451 is.

**What this table is NOT.** It is the lines that carry a raw CDP *reply*, all of
which sit below WARNING, which is why a WARNING floor is the whole fix for them.
It is not a census of everything nodriver can put a page's data into:
`element.py`:537/:624/:633 interpolate an element whose `__repr__` renders its
descendant TEXT, at **WARNING**, i.e. *above* this floor and reachable with no
`basicConfig` at all. That is §5.2 and F-907, and this fix does not close it —
stated here so the table is never read as a completeness claim.

## 2. The matrix — MEASURED, not reasoned

Harness: each log call above emitted with its own marker, against a real
`RotatingFileHandler` on a tmp log dir, the real `debug_logger` ring, a real
`LoggingIntegration` + capturing transport, and a capture handler on the root
logger. `✗` = the payload arrived.

Sink (d) is spelled **"a root handler"** rather than "stderr", because stderr is
only ever reached *through* a handler: in production root carries none, so
`callHandlers` falls through to `logging.lastResort` (a stderr handler at
WARNING); under a caller's `basicConfig` it is that caller's `StreamHandler`.

### Before

| Configuration | nodriver effective | (a) `backend-<pid>.log` | (b) debug ring | (c) Sentry | (d) root handler / stderr |
|---|---|---|---|---|---|
| shipped backend | WARNING | — | — | — | — |
| shipped proxy | WARNING | — | — | — | — |
| backend `--debug` | WARNING | — | — | — | — |
| `STEALTH_MCP_LOG_LEVEL=DEBUG` | WARNING | — | — | — | — |
| caller `basicConfig(DEBUG)` **before** our init | DEBUG | — | — | **✗** `connection.py`:451 | **✗ all four** |
| caller `basicConfig(DEBUG)` **after** our init | DEBUG | — | — | **✗** `connection.py`:451 | **✗ all four** |
| caller `basicConfig(DEBUG)` **+ `--debug`** | DEBUG | — | — | **✗** `connection.py`:451 | **✗ all four** |

The last row exists because of how the ring column could otherwise be read
(review S3): every other row asserts `—` for the debug ring across exactly the
cells that could not have reached it anyway — `--debug` alone admits no record,
and a caller's `basicConfig` alone leaves the ring off. Putting both knobs on at
once measures the column under *flowing* records. The ring stays empty for a
structural reason rather than a lucky one: `debug_logger.enable()` sets a flag
and echoes to stderr, and registers no handler on any logger, so no library
record has a route into it.

### After

Every cell `—`, in all seven configurations, with `nodriver` and `websockets`
both at WARNING regardless of root.

### Three answers the matrix settles

* **`--debug` sets NEITHER the root level nor ours.** It is
  `server.py`:452 → `rt.debug_logger.enable()`, i.e. the in-memory ring plus
  its own stderr echo. It never touches stdlib levels, so it was never a door.
* **`STEALTH_MCP_LOG_LEVEL` is ours and stays ours.** `configure_logging`
  applies it to `stealth.<role>`, never to root, so `DEBUG` there does not
  enable a single library record.
* **(a) and (b) are unreachable by construction, before and after.** Our file
  handler is on `stealth.<role>` with `propagate = False`, and the debug ring is
  a structure of ours that no library writes to. A nodriver WARNING does not
  reach `backend-<pid>.log` either — it never did, and this change does not
  alter that.

## 3. The fix, and why this home

`logging_setup.apply_payload_log_floor()` sets an **explicit** WARNING level on
the two family roots, `nodriver` and `websockets`, called as the **first**
statement of `configure_logging` — ahead of the idempotency guard and ahead of
everything that can raise `OSError`, because a process whose log directory could
not be created still has stderr and still has Sentry.

**Why `logging_setup`.** It already owns log-WRITING configuration: handlers,
formatters, levels, retention. A logger level is log configuration. The obvious
alternative, `cdp_transport.install()`, already patches nodriver and already
owns "what nodriver may do with a reply" — but what it owns is *behaviour on the
reply path*, not logging, and putting a level there would give this repo two
places that configure logging one import apart (convention 4).

**Why a level and not a filter or a `before_breadcrumb`.** Measured:
`LoggingIntegration.setup_once` patches `logging.Logger.callHandlers`, which
`Logger.handle` reaches only for a record `isEnabledFor` has already admitted.
A level is therefore **upstream of every sink at once** — file handler, root
handlers, stderr and Sentry — so one mechanism closes all four cells, and the
`before_breadcrumb` rule the brief anticipated is not needed *in addition*; it
would be a second home for one decision, and it would only ever matter if the
level floor were removed. A `logging.Filter` was rejected for a harder reason:
`connection.py`:451 pre-interpolates with `%`, so the payload is inside
`record.msg` with `record.args` empty — any filter would be a pattern-match
against text nodriver is free to reword in the next release.

**Why it holds in both orders.** `basicConfig` only ever sets the **root**
logger's level, and `getEffectiveLevel` stops at the first ancestor carrying a
non-`NOTSET` level. An explicit level on `nodriver` therefore wins whichever way
round the two calls happen, and keeps winning. Pinned in both orders.

**Why WARNING.** It is not a new policy — it is the effective level every
shipped configuration already had, which is why the floor changes nothing an
operator sees. It is also the level at which these libraries stop quoting
payloads and start reporting faults: `connection.py`:483's callback WARNING
names the callback and the event **class**, never the message.

**Why `websockets` too.** Capping `nodriver` alone leaves the identical payload
reachable one layer down. Measured: a short frame is printed whole.

**Why not `uc`.** That is only the local alias this codebase imports `nodriver`
under. Both libraries build loggers from `__name__` (measured:
`nodriver.core.connection.logger.name` **is** `nodriver.core.connection`), so no
logger is ever named `uc`, and capping a name that does not exist would be a
claim the evidence does not support. Pinned.

## 4. Pins

`tests/test_nodriver_payload_logging.py` — 25 nodes. RED-first, re-measured by
neutralising `apply_payload_log_floor` alone (so the RED is the FIX's absence
and not a missing constant): **9 failed, 16 passed**. Six of the nine are
behavioural — the three caller-root-DEBUG configurations × the all-sinks
assertion and the named downstream/Sentry assertion — and three are the
mechanism (explicit level, a later root DEBUG cannot lower it, survives a failed
handler install). The remaining sixteen are invariants that were already true
and must stay true, which is why they are green on both sides. The pins drive
the real stdlib level machinery, a real `LoggingIntegration` and a real
`RotatingFileHandler`, because each sink is a property of how those three
compose.

* every payload line, every sink, every shipped config **and** caller root DEBUG
  in both orders;
* a nodriver WARNING still reaches the process's handlers and Sentry;
* our `stealth.*` levels are untouched, including `STEALTH_MCP_LOG_LEVEL=DEBUG`;
* the floor survives a failed handler install;
* **premises**: the two known payload sites are still below WARNING in the
  installed nodriver; the family roots are the ones we name; and — the one that
  matters most — **Sentry patches nothing UPSTREAM of `isEnabledFor`**. That
  last is asserted in both directions (review S1): `callHandlers` is present,
  `Logger.handle` / `Logger._log` / `makeRecord` are absent, and `setup_once`
  binds **exactly one** name. A presence-only assertion would stay green through
  a bump that kept the `callHandlers` patch and ADDED a lower hook — i.e. it
  would outlive the premise it stands for.

F-902's own guard that a `CdpReplyError` carries no payload is
`tests/test_cdp_transport.py::test_no_cookie_name_or_value_reaches_the_report`
and is unchanged and green — deliberately not duplicated here.

Two measurement traps, both hit and both recorded in the test file so nobody
re-derives them: `logging.basicConfig` binds `sys.stderr` into its
`StreamHandler` at **creation** time, so a `redirect_stderr` entered afterwards
reads as a false negative; and pytest's own root capture handler suppresses
`lastResort`, so a stderr assertion under pytest measures the test runner rather
than the product.

## 5. Residuals

1. **A caller who names the library still wins.**
   `logging.getLogger("nodriver").setLevel(DEBUG)` overrides the floor. This is
   deliberate: that is someone asking for this library's payloads by name, which
   is a different act from turning DEBUG on globally. Not pinned as a
   prohibition; named here as the door left open.
2. **`element.py`:537/:624/:633 leak page TEXT at WARNING — above this floor,
   and reachable in the SHIPPED configuration. This is F-906's open neighbour,
   not a speculative one, and it is raised as F-907.** An earlier draft of this
   section filed it as "worth its own finding *if* element attributes are judged
   sensitive". Both halves of that were wrong, and both were measured (review
   M1, re-measured independently here):

   * **`Element.__repr__` renders descendant TEXT, not just tag and attributes.**
     `element.py`:1136-1144 recursively `str(child)`s every child, and
     :1146-1152 makes a text node return its `node_value` **bare**, before the
     tag is ever composed. So the record carries whatever the element is
     displaying — a `<form>` takes every `value="…"` with it. "Are attributes
     sensitive" is not the question to leave a successor.
   * **It needs no `basicConfig`.** Measured in a fresh process with the floor
     applied: `nodriver.core.element` effective **WARNING**, WARNING admitted
     **True**, root handlers `[]`, so `callHandlers` falls through to
     `logging.lastResort` (stderr at WARNING) — and for the backend stderr **is**
     `backend-boot.log`, the same durable file §1 names as what makes this more
     than theoretical. A Sentry breadcrumb takes it too. Confirmed end to end:
     a marker inside the interpolated element reached the captured stderr.

   And it is on a **hot path**: `dom_handler.py`:273 calls `element.mouse_click()`
   on every `click_element`, and `element.py`:536-537 is
   `if not center: logger.warning("could not calculate box model for %s", self)`
   — precisely the zero-size / not-rendered case `click_target` exists to
   classify.

   It stays out of scope HERE for one reason and it is not severity: this
   finding's mechanism is a floor, refusing to silence nodriver's WARNINGs is a
   deliberate line, and raising the floor to cover it would trade a payload leak
   for the loss of every real diagnostic that library emits. **F-907 is in
   progress on a separate branch; do not fix it here.**

   **One thing to carry forward rather than re-derive:** §3's argument against a
   `logging.Filter` — "it could only pattern-match text the library is free to
   reword" — is true of `connection.py`:451, which pre-interpolates with `%` and
   leaves `record.args` empty, but is **NOT** true of these three. They pass the
   element through `record.args` (measured), so an args-side rule is available to
   F-907 where it was unavailable here.
3. **`browser.py`:393 INFO-logs the full Chrome command line**, which carries
   `--user-data-dir=<path>` and so the operating user's home directory. Below the
   floor, therefore closed by this fix in passing — but it was never a *reply*,
   and `observability._scrub_event`'s home-segment rule would have handled it had
   it shipped.
3b. **`util.py`:4599 and :4603 INFO-log the proxy URL, immediately after parsing
   the username and password out of that same string** (`util.py`:4594-4595 read
   `url.username` / `url.password`, then :4599
   `"socks proxy with authentication is requested : %s" % proxy_server` and :4603
   `"which forwards to %s" % proxy_server`). Also below the floor and also closed
   in passing — recorded separately from 3 because the **sensitivity class
   differs** and the argument in 3 does not transfer: a home-directory path is
   covered by `observability._scrub_event`'s home-segment rule, whereas a proxy
   password rides in the URL's userinfo and is only covered by that scrubber's
   F-826 URL rule if the event reaches the scrubber at all. The floor is what
   keeps it out of the log in the first place.
4. **The floor is only applied where `configure_logging` is called.** That is
   both shipped processes (backend via `bootstrap_backend_process_logging`, proxy
   via `run_stdio_proxy`), and only the backend ever imports nodriver. **The
   `stealthy` CLI never calls it and so has no floor** — harmless, and an
   unpinned premise rather than a gap: no CLI verb imports nodriver, because they
   all drive tools over HTTP. A third party importing `browser_manager` directly
   without calling `configure_logging` likewise gets today's inherited behaviour.
   Moving the call to import time was rejected: a module that mutates global
   logging state on import is exactly the thing this finding complains about a
   dependency doing.
5. **A nodriver bump can move a payload line above the floor.** Pinned as a
   premise, so it fails in CI rather than in Sentry.
6. **Residual 1 is slightly stronger than stated, for `websockets` only.**
   `websockets/protocol.py`:108 snapshots `self.debug = logger.isEnabledFor(DEBUG)`
   at connection construction and guards the frame log with it (:608). So a
   caller who re-enables by name *after* a connection is open gets no frames on
   **that** connection. It fails in the safe direction; noted so nobody reads the
   floor as weaker than it is.
7. **The biggest payload logger this floor does NOT reach is
   `sse_starlette.sse`:362** — `logger.debug("chunk: %s", chunk)`, the serialised
   tool **RESULT**, whole. It is on the live path:
   `mcp/server/streamable_http.py` defaults `is_json_response_enabled: bool = False`
   (:141) and marks `_create_json_response` `# pragma: no cover` (:327), so every
   `tools/call` response leaves as an SSE chunk. Measured **DEBUG — admitted**
   with this fix applied and root at DEBUG. That is `get_cookies` output,
   `get_page_content` HTML, `get_instance_state` localStorage. Out of scope here
   — it is not nodriver and not a CDP reply — and named so a successor starts
   from a measurement.

   **A structural warning for that successor:** `mcp/shared/session.py`:383-384
   uses module-level `logging.warning` / `logging.debug`, i.e. the **ROOT**
   logger, and :383 is at WARNING. No family-root cap can ever reach it, so
   "cap the `mcp` family" is not a plan that works there.
