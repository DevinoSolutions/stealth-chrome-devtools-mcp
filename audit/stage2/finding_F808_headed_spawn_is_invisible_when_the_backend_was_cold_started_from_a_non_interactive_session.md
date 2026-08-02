# F-808 — a headed spawn is invisible when the singleton backend was cold-started from a non-interactive Windows session

**Status: FIXED** in 2.0.4 on branch `fix/F808-headed-visibility`.
**Severity: HIGH. Regression from 1.0.0.**
`spawn_browser(headless=False)` returns `state: "ready"`, `headless: false`,
`window_size.measured: true` — and produces a browser the user **cannot see and
never will**. Chrome is running and fully driveable over CDP; its window is on a
desktop Windows will not display.

This is a regression. On 1.0.0 the same SSH workflow produced a visible browser.

## Reproduction (measured 2026-08-01, Windows 11 Pro 26200, Chrome 151.0.7922.72)

Driving the released 2.0.3 backend over psmux/SSH:

```
spawn_browser(headless=False) → {"state":"ready","headless":false,
   "window_size":{"requested":{1920,1080},"actual":{1044,788},
                  "inner_viewport":{1028,617},"measured":true,"clamped":true}}
```

Polling for the browser it just launched:

```
t=3s … t=18s   stealth-chrome procs=8   visible-windows=0   sessionId=0
```

Eight live Chrome processes, stable, **every one with `MainWindowHandle = 0`**.

## Mechanism

Windows puts services and non-interactive logons in **Session 0**, which since
Vista has been isolated: a GUI created there is never composited onto the
logged-in user's desktop. A child process inherits its parent's session and
window station, so Chrome's visibility is decided by **whoever launched the
backend** — not by the `headless` flag, and not by the caller.

Measured on this machine:

| Process | Session |
|---|---|
| `explorer.exe` (the user's desktop) | **1** |
| backend `python … --transport http --port 55296` (pid 39536) | **0** |
| its ancestry: `python ← uvx ← claude.exe` (pid 109196) | **0** |
| `claude.exe` instances | **41 in session 0**, 9 in session 1 |

The backend was cold-started by a Session 0 client (an SSH/psmux Claude Code
session). Because the backend is a **singleton**, the nine Session 1 clients —
running on the real desktop, which on their own would launch a visible Chrome —
**reuse that Session 0 backend** and inherit its invisible window station. One
unlucky cold-start poisons headed browsing for every session on the machine,
including the interactive ones.

## Why 1.0.0 worked

There was no shared backend. `embedded/singleton.py` does not exist at `v1.0.0`
(`git cat-file -e v1.0.0:src/…/embedded/singleton.py` → absent); each Claude Code
session ran the MCP server **in-process**, so Chrome was always a descendant of
the client that asked for it and always landed in that client's session. The
2.x singleton is what decoupled "who asked" from "where the window goes".

## Why it is silent

Every signal the server has says success, because none of them observe a window:

* CDP attaches to the browser **process**; navigation, `execute_script`,
  `query_elements` and cookies are all genuinely correct.
* `take_screenshot` captures the compositor surface, which exists regardless of
  whether the window is displayed — so screenshots look perfect.
* `window_size.measured: true` is measured against Session 0's default desktop.

## Corrects F-804

F-804 attributes the clamp (`requested 1920x1080 → actual 1044x788`) to headed
Chrome clamping to *the desktop work area*, reading it as an ordinary monitor
limit. On this machine that reasoning produced the absurd conclusion that a box
driving an RTX 3080 has a ~1024x768 screen. The real clamp is **Session 0's
default desktop**, not the user's monitor. F-804's remedy (report
requested/actual/clamped truthfully) stands; its stated *mechanism* is wrong and
should cite this finding.

## Proposed fix

1. **Make the interactive session part of backend identity.** The reuse gate
   (`_same_identity_backend_ready` / `_source_fingerprint` in
   `embedded/singleton.py`) already refuses a backend whose source differs; a
   backend whose **window station differs** is just as unusable for headed work.
   Extend that one home — do not add a parallel path. Consequence: at most one
   backend per Windows session, which is the correct cost.
2. **Until then, fail loudly.** A headed spawn served by a backend in a
   non-interactive session must raise `ToolError` naming the cause and the
   remedy, instead of returning `headless: false` and an unseeable browser.
   Silence here is what cost a user their trust in the tool.
3. Headless spawns are unaffected and must keep working from Session 0 — CI
   depends on exactly that.

## Workaround before 2.0.4

Stop the Session 0 backend and let one cold-start from a client in the
interactive session; every session then shares a backend whose windows are
visible on the real desktop, SSH-driven spawns included.

## The fix (2.0.4)

Both halves of "Proposed fix" landed, and the shape changed in one deliberate way:
display context did **not** join the reuse gate as an equality test, because that
would have refused the desktop backend to the very SSH client that needs it.

| Commit | What |
|---|---|
| `a1b3075`, `8a78561` | `embedded/display_context.py` — the observational token and `can_show_windows()` |
| `efed9d0`, `663da1a` | `embedded/backend_registry.py` — the `server.json` record moved out of `singleton.py` (pure refactor) |
| `7989dee`, `32b3185`, `62d4813` | schema v2: one backend per display context; v1 records still read as `unverified`; supersede-by-port |
| `85f7fe6`, `09433a0`, `f02334c`, `4e2ede3` | discovery prefers a window-capable backend; asymmetric adoption; `unverified` is never a port conflict; restart terminates only the port it is about to bind |
| `2b22fe1`, `d209b46` | `spawn_browser(headless=False)` raises in a non-capable context instead of returning a ghost; the F-804 docstring clamp correction |
| `172e014`, `60e48da`, `69e48ad`, `d84323e` | `doctor` reports one line per recorded backend with its display context, and an explicit remedy when no live backend can show a window |
| `29f02a0` | test runs no longer ship injected failures to the real Sentry |
| `f437cab`, `38aa897`, `f4e58f3`, `22529ad`, `3aea184`, `977566c` | `browser_pids.json` gets one home, owner identity per entry, and a read-merge-write protocol, so concurrent backends stop erasing each other's tracked browsers |
| `3fe6b37`, `9c539d3`, `7d08c5d`, `08002f3` | the tests stop assuming a platform implies a desktop: F-804's headed nodes gate on display capability, and an integration twin asks the machine (via `EnumWindows`) whether a headed spawn is really visible — the one assertion the pre-2.0.4 signals could not make |

The reported symptom is closed by the **adoption** half, not the refusal half: an
SSH client now converges on the desktop backend and its headed spawn opens a
visible window. The refusal is the floor under the case where no such backend
exists anywhere.

**Acceptance** (plan_F808 Task 8 step 5): on the reporting machine, with a backend
cold-started from the desktop session, an SSH-driven `spawn_browser(headless=False)`
puts a visible window on the physical desktop.
