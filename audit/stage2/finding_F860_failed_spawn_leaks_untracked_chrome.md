# F-860 — a spawn that fails after Chrome is launched leaks the browser; on `master` it pins the profile

**Status:** open (product defect; leak observed on 1 of 4 failed spawns under 12-way concurrent load; mechanism confirmed in source and independently audited 2026-09-11 — see §4)
**Opened by:** concurrency test of 2026-09-10 (4 independent MCP clients x 3 simultaneous `spawn_browser`), requested by the maintainer
**Source at:** `origin/main` = `ca43bff` (installed build: PyPI 2.1.1 via `uv tool`; `clone_storage.py` and `process_cleanup.py` unchanged since v2.1.1)
**Severity:** HIGH for the leak (a live, untracked Chrome holding the *master* profile, invisible to `list_instances`, blocks snapshot refresh and pins every later spawn to clones until a backend restart's orphan reap). MEDIUM for the load behaviour (spawns time out and one client lost all responses at 12 in flight).
**Local times below are America/Toronto (UTC-4), as written by the logs.** Backend log: `~/.stealth-mcp/logs/backend-41364.log` (backend pid 41364, port 52554).

---

## 1. Primary defect — the leak

### 1.1 What was observed

Burst of 12 `spawn_browser` calls from 4 independent stdio clients, gate-released at 15:59:09.

| fact | evidence |
|---|---|
| 12 spawns started 15:59:09.65–15:59:11.22, all 12 ended 15:59:25.5–15:59:26.6 | `backend-41364.log`, `tool spawn_browser start/end` lines; peak in-flight = 12 at the tool level. Launches themselves were **staggered**: the per-spawn `Platform:` lines (emitted after profile selection) are 1.2–1.6 s apart, i.e. profile selection is serialized under the manager lock |
| only 8 were tracked | 8 `process_cleanup.track_process: Tracking browser process ...` lines at 15:59:26; 4 spawn ends have no tracking line (`9897bbd5f0a6`, `51a235fcca7f`, `b3d916f50836`, `8e45935d06e8`) |
| all 8 successes were **clones** (`profile_role: clone`, dirs `amind-72d4743e01f1{,-41364-13,15,16,17,19,20,21}`) | client-side `spawn_diagnostics.profile_selection` |
| a Chrome tree on **`master`** was alive afterwards with an **empty registry** | psutil census ~16:05: main pid 38488, `--user-data-dir=C:\stealth-mcp-browser-sessions\master`, **created 15:59:10**, parent pid 41364 (the backend); `~/.stealth-mcp/browser_pids.json` = `{"browser_processes": {}}` (read live at ~16:05; the file has since been rewritten by later sessions and this state cannot be re-verified from artifacts); `list_instances` = `[]` |
| the master browser was therefore launched by a spawn that **failed** | no success used master; birth time is the burst's first second. Attribution to `9897bbd5f0a6` (15:59:09.65) is **circumstantial**: the backend log never names the profile at spawn time; it is the only request past profile selection (`Platform:` line 15:59:10.873) before 15:59:12.35, matching a Chrome born 15:59:10 |
| the client-visible failure for one of the four was a CDP connect timeout | `ToolError: Failed to spawn browser: timed out during opening handshake` (client-side text; the backend does not log it, so it cannot be tied to a specific correlation id) + F-811 hint: "306 Chromium-family processes are live on this machine and 0 browser(s) are tracked in the shared record" |
| the leaked tree survived >= 6 minutes; removed manually | `taskkill /PID 38488 /T /F` at ~16:06; chrome.exe 159 -> 141; `master\lockfile` released |
| 3 attempt directories were also left behind | `sessions/amind-72d4743e01f1-41364-14`, `-18`, `-22`; marker `created_at` 19:59:15Z / :20Z / :25Z (5 s apart), `auto_clean: true`. Chrome **did launch** on all three (Chrome-written `Local State`, `Default/Preferences`, `CrashpadMetrics-active.pma`, last mtime 15:59:25.4–25.97) and was gone by ~15:59:26 (no `lockfile`/`DevToolsActivePort`). No code path kills these, so either the failure occurred after `_launch_browser` returned (then `_stop_browser` runs) or Chrome exited on its own; not established. **The leak therefore reproduced on 1 of the 4 failed spawns — the one on master.** |

### 1.2 Mechanism (source, `origin/main`)

1. `browser_manager._launch_browser()` returns `await uc.start(config=config)`. The `Browser` handle exists only if `start()` returns.
2. nodriver `core/browser.py::Browser.start()` (site-packages, line 343): `create_subprocess_exec(chrome ...)` -> sets `self._process_pid` -> polls `/json/version` 5 x 0.5 s -> on no answer `raise Exception("Failed to connect to browser ...")`; the later `Connection(...)` websocket connect is what raises `timed out during opening handshake`. **Neither path kills `self._process`.**
3. `browser_manager.spawn_browser()` registers the pid only in `_apply_post_launch()` via `process_cleanup.track_browser_process(...)`, i.e. **after** a successful launch.
4. Its `except Exception` handler does `if browser is not None: await self._stop_browser(browser)` — skipped, `browser` is still `None` — then `process_cleanup.kill_browser_process(instance_id)`.
5. `process_cleanup.kill_browser_process()` (line 763) begins `metadata = self.browser_processes.get(instance_id); if metadata is None: return False`. The instance was never tracked, so nothing is killed.
6. The spawn loop (`tool_sections/browser_management.py` at `ca43bff`; in the installed 2.1.1 wheel the same loop lives in `embedded/server.py:415-450`): for `profile_role == "clone"` a failed attempt gets `_release_clone_dir()` (protection only, no delete) and a fallback selection; for `profile_role == "master"` `_fallback_profile_selection()` returns `None` and the tool raises. Nothing addresses the process that step 2 started.

Net: any spawn whose Chrome launches but fails CDP connect leaks a live, untracked browser. On a clone the cost is a stray process tree until the next backend start's orphan reap; on `master` it additionally makes `_profile_has_running_browser(master)` true for every later caller (all spawns clone, `_refresh_master_snapshot_if_safe` never runs) with nothing in `list_instances` to explain why.

### 1.3 What is NOT claimed

* Not a race in profile selection. Selection was correct in every observed case (see 3.1).
* Not Windows-specific detection breakage. The PID scan in `_profile_has_running_browser` works on Windows (verified against a live master from an external process, and by a live reproduction that correctly cloned while master was held). The POSIX-only marker fallback (`SingletonLock/SingletonSocket/SingletonCookie`; Windows Chrome writes `lockfile`, held exclusively) is a latent gap but did not fire here.
* Whether nodriver's own retry-until-`Failed to connect` (2.75 s) or the websocket handshake (`opening handshake`) was the raise point for the master attempt is not known — the backend does not log the error text. Either raise point leaves `browser is None` and so takes the same path in step 3–5; but only the master attempt is *evidenced* to have leaked (see 1.1, last row).
* The three clone failures are not shown to be independent CDP timeouts. Their `spawn_browser end` lines share the same millisecond (15:59:25.979/25.980/25.980) with launch-to-end spans of 10.8 s / 5.4 s / 0.5 s, and no retry ran although `_fallback_profile_selection` offers one for clones. That pattern fits **cancellation** of one client's three in-flight calls (the client that received no responses) better than three timeouts.

### 1.4 Proposed fix

In `spawn_browser`'s failure handler, when `browser is None` and an attempt directory is known, kill every browser process whose `--user-data-dir` equals that directory — `process_cleanup._get_browser_pids_for_profile(dir)` is already the right primitive and is exactly what `_profile_has_running_browser` uses. For `master` this is safe by construction: master is only selected when nothing holds it, and a concurrent caller that saw the just-launched Chrome would have cloned, never opened master. Delete the attempt clone dir on failure as well (or leave it to the auto_clean sweep — bounded, but three dirs per failed spawn under load). Regression test: fake `uc.start` to spawn a real (or fake-pid) process and raise; assert no process with that `--user-data-dir` survives and `list_instances` / registry stay empty.

---

## 2. Load behaviour at 12 concurrent spawns (4 independent clients)

| metric | serialized (subagents, 3.2) | 12 in flight |
|---|---|---|
| spawn wall time | ~1.8 s each | 15.9–16.9 s each |
| failures | 0/12 | 4/12 (1 client got a `ToolError`; 1 client got **no response at all** on 3 calls — client timeout at 150 s while the backend had logged `spawn_browser end` for all 12 by 15:59:26.6) |
| chrome.exe at peak | ~30 | 306 (tool's own hint) |
| fleet impact | none | **every** proxy on the machine (~55 `proxy-*.log`, i.e. all other Claude sessions) logged `probe failed 1/3 ... 3/3 on port 52554` between 15:59:11 and 15:59:23; none condemned (F-820 gate held). Observed live; the proxy logs have since rotated (earliest surviving line 2026-09-10 19:58) so this row is no longer re-verifiable from artifacts |
| backend warnings | none | 11 `server._client_session_seed` lines: 4 x `List roots not supported`, 7 x `TimeoutError awaiting roots/list` (15:59:11–24) |
| cleanup of the 8 successes | clean | 8/8 navigate, 8/8 close, all 8 `Stopped tracking` at 15:59:33.5–33.7, registry empty, `list_instances` empty. Caveat: 4 of the closes (`106580`, `34240`, `83036`, `152100`) logged `Skipping fallback PID ... create_time mismatch (recycled PID)` only ~5 s after tracking |

The lost responses for one client are **unexplained**: proxies log only at WARNING and none shows a re-bridge; reproducing with `get_settings().log_level` at DEBUG on the proxy is the next step. Practical ceiling on this box: ~3–4 concurrent spawns per backend before CDP connects start timing out.

---

## 3. Secondary observations

### 3.1 The "vanished first instance" was not a selection bug (false lead, corrected)

Instance `6e061ba4` (Chrome pid 35128) spawned on master 14:24:59; a second spawn was granted master 14:25:54. The `master-snapshot` copy taken at 14:25:55 (`before-master-open`) carries `Default/Preferences` `profile.exit_type: "Normal"` and Local State `stability.exited_cleanly: true`, both last written **14:25:49**. `exit_type: Normal` is written only on Chrome's clean-shutdown path (high confidence); `exited_cleanly` is a weaker signal (on Windows it is also mirrored to the registry and the two can disagree — medium confidence). The snapshot read here was the 14:25:55 copy, inspected at ~14:3x; `master-snapshot` has since been refreshed (2026-09-10 18:45) and now reads `exit_type: Crashed` / `exited_cleanly: true`, so the 14:25 state is no longer re-verifiable from disk. The backend log independently shows no kill/close line for `6e061ba4`. The first browser had exited cleanly 5 s before the second spawn; the selector was right. Chrome's process singleton corroborates it: a live master would have made the second Chrome hand off and exit, failing nodriver's connect, and spawn 2 completed in 1.7 s.

### 3.2 Claude Code subagents do not model independent sessions

Four subagents each firing three "parallel" spawns produced **peak in-flight 1 for every tool** (12 spawns strictly sequential, 15:54:25 -> 15:54:58, end-to-next-start gaps 0.12–2.57 s) while *different* tools overlapped (peak 3). Subagents share the parent session's single MCP proxy and same-named tool calls are serialized there. Any concurrency test must use separate MCP clients/proxies (see appendix).

### 3.3 Dead instances are noticed lazily

`6e061ba4` exited 14:25:49; untracked only at 14:28:10 (during another spawn's cleanup pass) and discarded by `list_instances` at 14:30:05 ("browser process is not running"). A browser that dies on its own stays `ready` in the list for minutes.

### 3.4 `ready (stored)` for an instance mid-close (transient)

`list_instances` at 15:55:10.824 reported `23853639` as `state: "ready (stored)"`, `source: "stored"` — inside that instance's own `close_instance` window (15:55:10.125–15:55:11.183). `list_instances` unions `browser_manager.list_instances()` with `in_memory_storage.list_instances()`; the storage entry is removed later in the close path (`browser_manager.py:203`, `:958`), so for ~1 s a closing instance is labelled ready. Cosmetic, but misleading.

### 3.5 Failed attempts leave clone directories

See 1.1, last row. `_release_clone_dir` only lifts sweep protection; deletion waits for the storage-cap / orphan sweep. Bounded by `auto_clean: true`; in this run it was one directory per failed clone attempt (three).

### 3.6 Latent: POSIX-only marker fallback

`_profile_has_running_browser` falls back to `("SingletonLock", "SingletonSocket", "SingletonCookie")`, none of which Windows Chrome creates; it writes `lockfile` (held exclusively — `open(..., "a")` raises `PermissionError` while Chrome runs; 0 of 80 idle profiles carried a stale one). `_REGENERABLE_PROFILE_NAMES` in the same module already lists `lockfile` / `LOCK`. Only matters if the PID scan raises.

---

## 4. Independent verification (2026-09-11)

A second agent with no access to the author's reasoning checked 21 claims against `backend-41364.log`, the surviving `proxy-*.log` files, `browser_pids.json`, the profile directories, and the source at `ca43bff` plus the installed 2.1.1 wheel.

* **Confirmed:** spawn/tracking counts and timestamps (1.1 rows 1–2); every source step of the mechanism (1.2 steps 1–6), including that nodriver 0.47.0 `start()` does not kill on failed connect and that nothing else in the failure path kills by profile directory; the `browser is None` handler; `kill_browser_process` early return; `_fallback_profile_selection` returning `None` for master; the seed warnings; the 3.1 timeline; the 3.2 serialization (peak in-flight 1 for `spawn_browser`); the 3.4 `(stored)` window; the 3.6 tuple. Source in the repo and the installed wheel are byte-identical for the files cited.
* **Qualified (edits applied above):** master-Chrome attribution is circumstantial; Chrome launched on the three clone attempt dirs and did not survive (cause unknown), so the leak is evidenced for **one** failed spawn only; the three clone failures look like cancellation, not timeouts; "clean" close of the 8 successes carries a recycled-PID caveat; launches were staggered, not simultaneous.
* **No longer verifiable from disk:** the empty registry and the fleet-wide proxy probe failures (observed live at ~16:05 and 15:59; registry since rewritten, proxy logs rotated), and the 14:25 snapshot `exit_type: Normal` reading (snapshot since refreshed; now `Crashed`).
* **Verdict as delivered:** the mechanism is sound; the master-leak inference is the best explanation of the census but rests on an observation that can no longer be reproduced from artifacts. A regression test that forces `_launch_browser` to raise after Chrome starts is the way to make the claim durable.

---

## 5. Environment notes relevant to reading the evidence

* Two backends were alive during the tests (pids 41364 and 32428, both claiming port 52554 in cmdline; `server.json` listed 32428 as `win-session-1`). All test traffic landed in `backend-41364.log`.
* 58 zero-byte `backend-*-fault.log` files exist — a long restart history on this machine.
* ~180 `stealth-chrome-devtools-mcp` proxy processes were present, 52 parented by `claude.exe --continue` sessions; an unrelated `python -m src.main browser-scraper --workers 9` was also launching Chrome (`tmp*` profiles) during the window. Neither is part of this finding but both add to the process pressure the F-811 hint reports.

---

## Appendix — reproduction (independent clients; run from the repo venv)

```python
# multiclient.py — 4 stdio clients (one launcher/proxy each), 3 spawns each, gate-released together.
import asyncio, json, os, re, time, collections, warnings
warnings.filterwarnings("ignore")
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

LAUNCHER = r"C:\Users\amind\.local\bin\stealth-chrome-devtools-mcp.exe"
LOG = r"C:\Users\amind\.stealth-mcp\logs\backend-41364.log"   # adjust to the live backend pid
N_CLIENTS, PER_CLIENT = 4, 3

def parse(res):
    if getattr(res, "data", None) is not None:
        return res.data
    try:
        return json.loads(res.content[0].text)
    except Exception:
        return res.content[0].text if res.content else ""

async def call(c, tag, tool, args, timeout=150):
    t = time.time()
    try:
        return {"tag": tag, "ok": True, "secs": round(time.time() - t, 2),
                "r": parse(await c.call_tool(tool, args, timeout=timeout))}
    except Exception as e:
        return {"tag": tag, "ok": False, "secs": round(time.time() - t, 2),
                "err": f"{type(e).__name__}: {str(e)[:300]}"}

async def one_client(idx, gate, results):
    tag = f"C{idx}"
    transport = StdioTransport(command=LAUNCHER, args=[], env=dict(os.environ))
    async with Client(transport, init_timeout=120) as c:
        await c.list_tools()
        await gate.wait()
        spawns = await asyncio.gather(*[call(c, f"{tag}.{j}", "spawn_browser", {"headless": True})
                                        for j in range(PER_CLIENT)])
        results["spawn"] += spawns
        ids = [s["r"]["instance_id"] for s in spawns
               if s["ok"] and isinstance(s["r"], dict) and "instance_id" in s["r"]]
        results["navigate"] += await asyncio.gather(*[call(c, i[:8], "navigate",
                                                            {"instance_id": i, "url": "https://example.com"})
                                                       for i in ids])
        if idx == 0:
            results["list_mid"] = await call(c, tag, "list_instances", {})
        await asyncio.sleep(2)
        results["close"] += await asyncio.gather(*[call(c, i[:8], "close_instance", {"instance_id": i})
                                                    for i in ids])
        if idx == 0:
            await asyncio.sleep(3)
            results["list_end"] = await call(c, tag, "list_instances", {})

async def main():
    log_off = sum(1 for _ in open(LOG, encoding="utf-8", errors="replace"))
    results = collections.defaultdict(list)
    gate = asyncio.Event()
    tasks = [asyncio.create_task(one_client(i, gate, results)) for i in range(N_CLIENTS)]
    await asyncio.sleep(25)          # let every proxy connect, then release all at once
    gate.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    for s in sorted(results["spawn"], key=lambda x: x["tag"]):
        if s["ok"] and isinstance(s["r"], dict):
            ps = s["r"].get("spawn_diagnostics", {}).get("profile_selection", {})
            print(s["tag"], s["secs"], s["r"].get("instance_id", "?")[:8], ps.get("profile_role"),
                  os.path.basename(ps.get("user_data_dir", "?")), ps.get("spawn_retries"))
        else:
            print(s["tag"], s["secs"], "FAILED", s.get("err") or s.get("r"))
    print("navigate ok", sum(1 for x in results["navigate"] if x["ok"]),
          "close ok", sum(1 for x in results["close"] if x["ok"]))
    print("list mid", results.get("list_mid"))
    print("list end", results.get("list_end"))
    lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()[log_off:]
    pat = re.compile(r"^(\S+ \S+) INFO \d+ \[(\w+)\] stealth\.backend: tool (\w+) (start|end)")
    ev = [(m.group(3), m.group(4)) for l in lines if (m := pat.match(l))]
    def peak(tool):
        n = best = 0
        for tl, k in ev:
            if tl != tool:
                continue
            n += 1 if k == "start" else -1
            best = max(best, n)
        return best
    print("backend peak in-flight spawn_browser:", peak("spawn_browser"))
    print("backend WARN/ERR:", [l[11:160] for l in lines if "WARNING" in l or "ERROR" in l][:12])
    # Then take a psutil census of chrome.exe main processes grouped by --user-data-dir and compare
    # with ~/.stealth-mcp/browser_pids.json: any tree on a directory the registry does not know is the leak.

asyncio.run(main())
```
