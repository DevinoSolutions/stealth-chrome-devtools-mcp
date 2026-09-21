# F-930 — the one thing the fence cannot give a child has six hand-rolled homes

**Status:** FILED, not fixed
**Found by:** writing `tests/test_seed_refresh_atomicity.py::TestARealProcessDeath`
for F-925 — a pin that has to kill an interpreter mid-copy, and therefore has to
run the product in a subprocess
**Severity:** convention 4 ("a change that introduces a second way to do
something already done is a defect") plus a missing enforcement. **Not
data-loss, and not F-903's class.** A full census at `9377c5b` found **zero**
unfenced tests in the shipped suite — see §5. What is wrong is that six authors
each independently built the same guard, because the mechanism that would have
given it to them does not exist.

---

## 1. The mechanism, and its two subjects

`tests/operator_fence.py` is the one home for keeping a test run out of the
operator's real `~/.stealth-mcp` and real browser-session root. `conftest.py`
installs it at import time — ahead of collection and of every fixture of every
scope — and it works by wrapping filesystem primitives **in the process that
imports it**. A child interpreter imports no `conftest.py`, so it has its own
`os` module, its own unwrapped primitives and its own `os.environ`, and none of
that wrapping is present in it.

That much was the obvious reading. It is not the useful one, because **the two
subjects the fence protects reach a child completely differently.** Measured at
`origin/main` (`9377c5b`), read from source:

| subject | how the fence holds it | what a CHILD inherits |
|---|---|---|
| browser-session root | `_fence_session_root` **FORCES** `STEALTH_MCP_BROWSER_SESSION_ROOT` into the env it is handed, and **POPS** the three derived vars | **free**, for any env descended from `os.environ` |
| state dir (`~/.stealth-mcp`) | nothing — it is `Path.home()`-derived and there is no env var at all | **never free, by construction** |

The session-root half is free because of one argument at the call site:
`tests/conftest.py:144` passes **`env=os.environ`** — the real mapping object,
not a copy — so `install` mutates the parent's own environment. `setenv` and
deliberately not `setdefault`, and the fence's docstring carries the incident
that settled it: `setdefault` "cannot tell the gate redirecting the suite from
the OPERATOR'S OWN root arriving in an inherited environment", and an agent
shell that had inherited `STEALTH_MCP_BROWSER_SESSION_ROOT` from the MCP
client's config "ran the whole E2E tier against the real root". It also pops
`_DERIVED_ROOT_ENV` — `BROWSER_MASTER_USER_DATA_DIR`,
`BROWSER_PROFILE_CLONE_ROOT`, `BROWSER_MASTER_SNAPSHOT_DIR` — because an
inherited one of those "would still name the operator's real master profile
under a redirected root". **Three vars, not one**: a fix that sets the root and
leaves the derived three is not a fix.

The state-dir half cannot be free. `backend_registry.STATE_DIR` is
`Path.home() / ".stealth-mcp"`, there is **no `Settings` field** for it — so
pydantic's env source never looks for one — and `STEALTH_MCP_STATE_DIR` occurs
**0 times** anywhere in the tree. The only lever on a child is `HOME` /
`USERPROFILE`, read by `Path.home()` at the child's own import time, and the
fence deliberately does **not** set those. That refusal is measured, not an
oversight; `operator_fence.py` argues it under a heading, **"Why HOME is not
redirected"**, for two reasons:

1. `release_gate_harness._reserved_ports()` calls `Path.home()` to find the
   ports the operator's **real** backends hold. Under a redirected HOME it would
   read the empty fence record, exclude nothing, and an isolated backend could
   bind the port a live backend is serving on — "the fence would have created
   the collision it exists to prevent".
2. It "would also not have fenced the session root at all on Windows, where the
   product's default is the hardcoded absolute `C:\stealth-mcp-browser-sessions`
   and owes nothing to `Path.home()`".

**So the residual is exact, and it is the opposite way round from the intuition.**
The subject with an env var is handed to every child for free. The subject with
no env var can never be handed to one at all. Every subprocess test that needs
the state dir fenced has to do it itself — which is why there are six of them.

## 2. What was measured, and what it does NOT show

`TestARealProcessDeath` hands its child `{**os.environ, ...}` plus four explicit
session-root overrides. To see what happens when those do not apply, the four
were removed and the child run directly:

```
child exit: 1
child said: refusing to run outside C:\definitely-not-here:
            C:\stealth-mcp-browser-sessions\master
tombstone written: False
```

The resolved path is the operator's **real** master profile, and the refusal is
the self-fence added to that test (§4), not the operator fence's. Without it the
child proceeds to `profile_copy.replace_tree`, whose first act inside the `try`
is `staging.mkdir(parents=True, exist_ok=True)` — so **"the copy is patched out,
it would have written nothing" is false**, and worth stating because it is the
reassuring answer. It would have created
`C:\stealth-mcp-browser-sessions\master-snapshot.stealth-staging-<pid>-<n>`.

**What this does not show is that the shipped test was ever exposed**, and the
finding must not be read that way. Because that env is built from `**os.environ`
it carries conftest's FORCED session root, so the child is fenced twice over —
once by inheritance it never asked for, once by its own overrides. The probe
removed the inherited value along with the explicit ones, which models a test
that builds an env **from scratch**, not one that merely forgets an override.
The honest claim is narrower and still worth having: **the free protection is
contingent on the env-construction shape, and nothing enforces that shape.**
`{**os.environ}` and `dict(os.environ)` carry it; a hand-built dict does not;
no rule distinguishes them and no test would fail if a future author picked the
second.

The operator's live root was checked directly after the F-925 work: its
directories are `master`, `master-snapshot`, `probe`, `sessions`, with **no**
`stealth-staging` or `stealth-previous` entries. Nothing of F-925's reached it.

## 3. Why the fence's own pin cannot see this

`tests/test_operator_fence.py` measures the enumeration by importing the package
and asserting every derived global is redirected. That is the right shape for
the threat it was built for — a new module binding `STATE_DIR` a second time —
and it runs **in the pytest process**. A child interpreter has its own `os`, its
own primitives and its own environ; no in-process assertion can observe it. The
gap is not a hole in what that pin measures, it is a boundary the measurement
cannot cross.

The fence's docstring already knows this and says so — "Child processes keep
their own HOME redirection (`release_gate_harness._isolated_env`), which is a
different mechanism for a different process and is untouched." **So this is an
enforcement gap, not a knowledge gap.** The mechanism is documented; what is
absent is any exported symbol a new subprocess test is obliged to reach for, and
any rule that fails when it does not.

## 4. What F-925 did about its own instance

`TestARealProcessDeath`'s child fences itself: before anything else it resolves
`master_profile_dir()`, `master_snapshot_dir()` and `clone_root_dir()` and exits
non-zero unless all three are under the tmp root the parent passed. That closes
the one instance and is deliberately **not** offered as the fix — it is a
per-test guard re-implemented at the call site, which is the seventh copy of the
thing this finding is about.

It passes **four environment variables rather than a `HOME`**, and that shape is
the evidence for §6's fix: `HOME` alone cannot reach the session root on Windows
(hardcoded absolute, owing nothing to `Path.home()`), and the session-root var
alone cannot reach the state dir (no such var exists). Neither lever is
sufficient and they are not interchangeable.

## 5. Scope — ESTABLISHED, and it is zero

A full re-census at `9377c5b` (220/220 test files, AST pass plus an independent
grep cross-check) found:

* **25** test files spawn a subprocess
* **17** of those run product code in it
* **0** are unfenced

Both of the highest-risk files are covered: `test_stealthy_cli_e2e.py`, which
drives the installed console script for real, uses `_isolated_env` and
additionally self-checks the operator's real `server.json` bytes before and
after; `test_package_entrypoints.py` — itself the F-903/F-904 fix artifact —
hand-rolls the same override.

**There is no defect in the wild.** No shipped test reaches the operator's
directories, and this finding claims none. An earlier pass reported
`tests/operator_fence.py` as absent entirely; it had scanned a working tree 186
commits stale, and every number above is from the re-run at current main.

One near-miss checked and cleared rather than counted:
`tests/test_clean_shutdown_noise.py:392` sets `HOME` and **not** `USERPROFILE`,
which on Windows is the defect exactly, since `ntpath.expanduser` reads
`USERPROFILE` and ignores `HOME`. It is not one — the test is
`@pytest.mark.skipif(sys.platform == "win32")`, so it runs only on POSIX, where
`HOME` is precisely what `Path.home()` reads.

## 6. The finding: six homes for one mechanism

What the census establishes is not an exposure but a **population**. Six
independent implementations of "fence a child interpreter", none of them reached
through a shared exported symbol:

| # | site | technique |
|---|---|---|
| 1 | `release_gate_harness._isolated_env` | canonical, 12+ consumers |
| 2 | `test_singleton_fast_handshake._isolated_subprocess_env` | own copy |
| 3 | `test_sigbreak_immunity._isolated_env` | own copy, same name, different module |
| 4 | `test_clean_shutdown_noise` | inline, `HOME` only, POSIX-only |
| 5 | `test_backend_escapes_client_job` | **in-child `STATE_DIR` reassignment** — a different technique entirely |
| 6 | `test_package_entrypoints` | inline `env.update(...)` |

Six authors solved one problem six ways and not one of them was directed to an
existing answer, because there is nothing to direct them to. That is convention
4 read literally, and #5 is the sharpest evidence for it: reassigning `STATE_DIR`
inside the child is not a variant of the env approach, it is a second mechanism
— so the set does not even agree on what the technique IS.

**The fix, named and narrow:** one exported child-side installer in
`operator_fence` — the mechanism's existing one home — carrying **both** levers
and the derived-var pop: `HOME`/`USERPROFILE` for the state dir,
`STEALTH_MCP_BROWSER_SESSION_ROOT` forced for the session root, and
`_DERIVED_ROOT_ENV` popped. The five hand-rolled copies collapse onto it; the
canonical `_isolated_env` becomes its caller rather than its competitor. A
child-side installer is not a convenience here — it is the only shape that can
carry the state-dir lever at all, since no env var exists for that subject and
an in-process `monkeypatch` of module globals cannot cross a process boundary.

Deliberately **not** `sitecustomize`/`PYTHONSTARTUP`, which would cover a test
nobody remembered to annotate but puts fence installation where no reader of
`conftest.py` will look, and changes the interpreter for everything else the
suite spawns.

## 7. What is NOT claimed

* **Not that anything reached the operator's real root.** It did not — checked
  directly, §2.
* **Not that any shipped test is unfenced.** Zero, measured, §5. The severity is
  lower than the first draft of this finding held open for, and saying so is the
  point: a finding that claims harm it cannot demonstrate is the one nobody
  trusts next time.
* **Not that a constructed env is unfenced for the session root.** The realistic
  shape in this suite descends from `os.environ` and carries the forced value;
  only a from-scratch dict loses it, §2.
* **Not that F-903 was wrong.** Its fence does what it says for the process it is
  installed in, and it argues its own HOME decision from measurement. This is a
  door that process cannot see through.
