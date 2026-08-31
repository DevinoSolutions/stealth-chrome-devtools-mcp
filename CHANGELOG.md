# Changelog

## Unreleased

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
