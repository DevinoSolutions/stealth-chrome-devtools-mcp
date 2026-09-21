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

### After

Every cell `—`, in all six configurations, with `nodriver` and `websockets`
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

`tests/test_nodriver_payload_logging.py` (renamed `tests/test_payload_log_floor.py`
by F-908, which added a third family to the same floor and so would otherwise
have opened a second home for one function's evidence) — 22 nodes. RED-first: 4 behavioural
failures (both `basicConfig` orders × the all-sinks assertion and the named
stderr/Sentry assertion) plus 5 mechanism/surface failures, against 13 already-
green invariants. They drive the real stdlib level machinery, a real
`LoggingIntegration` and a real `RotatingFileHandler`, because each sink is a
property of how those three compose.

* every payload line, every sink, every shipped config **and** caller root DEBUG
  in both orders;
* a nodriver WARNING still reaches the process's handlers and Sentry;
* our `stealth.*` levels are untouched, including `STEALTH_MCP_LOG_LEVEL=DEBUG`;
* the floor survives a failed handler install;
* **premises**: the two known payload sites are still below WARNING in the
  installed nodriver; Sentry still patches `callHandlers`; the family roots are
  the ones we name.

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
2. **`element.py`:537/:624/:633 log `"could not calculate box model for %s", self`
   at WARNING**, and `Element.__repr__` renders the element's tag and attributes
   — page content, above the floor. Out of scope: this finding is about raw CDP
   *replies*, and silencing nodriver's WARNINGs is the cost this fix explicitly
   refuses. Worth its own finding if element attributes are judged sensitive.
3. **`browser.py`:393 INFO-logs the full Chrome command line**, which carries
   `--user-data-dir=<path>` and so the operating user's home directory. Below the
   floor, therefore closed by this fix in passing — but it was never a *reply*,
   and `observability._scrub_event`'s home-segment rule would have handled it had
   it shipped.
4. **The floor is only applied where `configure_logging` is called.** That is
   both shipped processes (backend via `bootstrap_backend_process_logging`, proxy
   via `run_stdio_proxy`), and only the backend ever imports nodriver. A third
   party importing `browser_manager` directly without calling `configure_logging`
   gets today's inherited behaviour. Moving the call to import time was rejected:
   a module that mutates global logging state on import is exactly the thing this
   finding complains about a dependency doing.
5. **A nodriver bump can move a payload line above the floor.** Pinned as a
   premise, so it fails in CI rather than in Sentry.
