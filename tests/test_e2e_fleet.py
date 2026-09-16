"""The CI stand-in for the manual multi-browser fleet run.

Every defect this branch adds coverage for — F-873 through F-881 — was found
the same way: by driving a LIVE release against several real browsers at once
and comparing every answer with the page. The committed suite was green
throughout, because each of its nodes drives one browser through one tool and
then asserts something about that tool's own vocabulary. Four things that fleet
run did which no committed node did are what this module reproduces:

* **several browsers at once**, so an answer that is really about "whichever
  instance the manager looked at last" cannot hide behind there being only one;
* **the ADVERTISED profile path as well as the named one.** The manual run was
  one master plus nine auto-clones, because `user_data_dir` is documented as
  "leave UNSET for normal use". Half this fleet is unnamed for that reason, and
  it is also the only half that can assert the disposable-profile promise;
* **a mix of page shapes in one run** — a plain page, a page whose ``load`` is
  held open, an app shell whose document cannot scroll, and a form — so a tool
  that quietly assumes the ordinary shape is visible next to one that does not;
* **every answer checked against the page itself**, in JavaScript, rather than
  against the tool's own record. A record that agrees with itself is what
  F-873, F-875, F-876 and F-877 each shipped.

It is one node on purpose. The fleet is the unit: six browsers spawned in ONE
``gather``, navigated in ONE ``gather``, driven in ONE ``gather``, and only
then asked — collectively — whether ``list_instances`` can still name what each
one is showing. Split into six nodes it would cost six fleets and stop being
the shape that found the defects.

**Two members move their page WITHOUT the ``navigate`` tool**, and that is what
makes the listing block evidence rather than decoration. F-874 §2's mechanism is
that ``BrowserInstance``'s cached pair had exactly two writers, ``spawn_browser``
and ``navigate`` — so on a fleet where every page arrived through ``navigate``
the stale cache would have been RIGHT and the block would pass against the
defect. The ``click`` member's own click handler retitles its page (§1 row 1) and
the ``tabswitch`` member ends up on a tab it opened afterwards (§1 row 4).

Sizing. Six members, measured locally (Windows 11, Chrome 152): ``spawn 3.6s,
navigate 1.3s, actions 1.7s, total 6.7s`` on a quiet machine and ``spawn 5.2s,
navigate 1.3s, actions 2.3s, total 9.0s`` beside other agents' browsers, over
``roles ['clone', 'explicit', 'master']`` — all three profile kinds in one run,
which is the manual fleet's own shape. Six rather than four because each
member owns exactly ONE of the six (page shape, tool) pairs the finding set
needs, and folding two onto one member would make those two serial. The phase
times are PRINTED and nothing asserts them: on a loaded 2-core runner a wall
budget is the one thing in this file that could go red for a reason that is not
about the product, and a diagnostic that always prints is worth more than an
assertion nobody would trust.

Isolation. The browser-session root is redirected for the whole test session at
``tests/conftest.py`` import time, BEFORE any fixture runs — see the comment
there for why a per-test fixture cannot do it (``get_settings`` is ``lru_cache``d
and the module's own autouse ``_warmup`` spawns a browser ahead of any
function-scoped root fixture, which is how an earlier revision of this very file
wrote six 108 MB profiles into the developer's real session root). This node
therefore does NOT declare ``tmp_empty_root``: it would be decorative, and the
disk assertions below are made against ``clone_storage.clone_root_dir()`` — the
root the product actually used — rather than against a temp directory nothing
wrote to.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    instance_entry,
    integration_pytestmark,
    runtime,
    sandbox_kwargs,
    warmup_once,
)
from fixture_routes import (
    COV_CLICKED_TITLE,
    COV_FORM_TITLE,
    COV_PLAIN_SENTINEL,
    COV_SHELL_TITLE,
    COV_TITLE_AFTER_LOAD,
)

pytestmark = integration_pytestmark()

#: One member per (page shape, tool) pair. See the module docstring for why
#: this is six and not four.
FLEET_SIZE = 6

#: How long ``/cov/slow_load.html``'s ``load`` is held open for the member that
#: carries F-881's question INTO the fleet: shorter than the standalone node's
#: hold (``test_e2e_load_milestone``) because here it runs beside five other
#: browsers and the claim is only that the fleet still waits for it.
HELD_MS = 1_200

#: How far the app-shell member is asked to scroll. Inside its 6000 px filler,
#: so the scroller has somewhere to go and is nowhere near its far edge.
SCROLL_AMOUNT = 800

#: How long an auto-clone's directory may take to disappear after its instance
#: closes. `process_cleanup._cleanup_profile_for_metadata` removes it on close
#: (gated on the tracked entry's `auto_clone` flag), but a Windows file lock
#: defers the delete to `cleanup_deferred_profiles`, so this is a bounded poll
#: that DRIVES that retry rather than a sleep that hopes. Measured locally:
#: gone before the first poll.
RECLAIM_BUDGET_SECONDS = 15.0

#: The only backend warnings the CLOSE phase may emit, matched on component,
#: operation AND sentence. All are Windows contention under a six-way
#: concurrent teardown, all have a reaper behind them, and all are tolerated
#: only because a later assertion in this node proves the repair happened — see
#: the gate at the end of the test for the argument. The first and third are
#: the two ends of ONE slow kill: `close_instance` giving up waiting on its
#: worker thread after `settings.close_kill_timeout`, and that worker's
#: `_kill_process_by_pid` finding the pid still present two seconds after
#: `process.kill()` (measured: the pid was gone moments later). What earns
#: both is the tracked-pid witness in the reclaim poll — the product untracks
#: an instance only once it has seen the pid go — so a kill that genuinely
#: wedged still fails the node there, before this tuple is consulted. The
#: `kill_process` prefix ends at the variable and, at WARNING under that
#: operation, matches that one sentence: its neighbours begin ``PID ``,
#: ``Could not verify process `` and ``Failed to terminate process ``, and the
#: successful kills are logged at INFO. Neighbours from the other two
#: operations (``Blocking teardown failed``, ``browser.stop() coroutine
#: failed``, ``Proxy forwarder close failed``) are deliberately NOT here: they
#: are exceptions, not timeouts, and nothing reaps after them.
TEARDOWN_WARNINGS_WITH_A_REAPER = (
    "browser_manager.close_instance: Chrome kill for ",
    "process_cleanup.cleanup_profile: Failed to remove temp profile for ",
    "process_cleanup.kill_process: Process ",
)


def _stealth_warnings(records):
    """The BACKEND's own durable channel at WARNING and above, nothing else."""
    return [
        record
        for record in records
        if record.name.startswith("stealth.") and record.levelno >= logging.WARNING
    ]


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


def _clone_root():
    """The clone root the PRODUCT resolved, not one a test hoped for."""
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    return clone_storage.clone_root_dir()


def _dirs_in(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}


def _selection(spawn_result: dict) -> dict:
    """The profile the spawn actually got: role, and the directory on disk."""
    return spawn_result["spawn_diagnostics"]["profile_selection"]


async def _drive_shell(iid):
    """F-875 + F-878 inside the fleet: the nested scroller, truthfully."""
    scroll_page = get_fn("scroll_page")
    return await scroll_page(instance_id=iid, direction="down", amount=SCROLL_AMOUNT)


async def _drive_type(iid, marker):
    """F-873 inside the fleet: a trailing newline submits a one-field form."""
    type_text = get_fn("type_text")
    return await type_text(
        instance_id=iid,
        selector="#cov-field",
        text=f"{marker}\n",
        parse_newlines=True,
    )


async def _drive_click(iid):
    """F-876 inside the fleet: where did the click actually go.

    The click also retitles the page, which is this member's SECOND job — see
    the module docstring on F-874 §1 row 1.
    """
    click_element = get_fn("click_element")
    return await click_element(instance_id=iid, selector="#cov-button")


async def _drive_select(iid):
    """F-877 inside the fleet: the Python matching rule, over a label."""
    select_option = get_fn("select_option")
    return await select_option(instance_id=iid, selector="#cov-select", text="Gamma")


async def _drive_slow(iid):
    """F-881 inside the fleet: the page's own ``load``, under contention."""
    return await eval_js(iid, "document.readyState")


async def _drive_tabswitch(iid, base):
    """F-874 §1 row 4: end up on a tab this member OPENED, not one it navigated.

    Reads its own plain page first — the control that the fleet's most ordinary
    member is fine — then opens a second tab and switches to it. ``new_tab``
    and ``switch_tab`` are not writers of the cached url/title pair, so after
    this the only way to name what this browser is showing is to ask Chrome.

    One timing dependence, named rather than hidden: ``new_tab`` returns when
    nodriver's ``Browser.get`` settles, which does not itself promise the new
    document's ``<title>`` has been parsed, and the listing assertion wants
    that title. It is a served ``<title>`` on a tiny page and five awaits
    (the ``switch_tab``, the gather's other five members, then the listing's
    own ``Target.getTargets``) intervene, so the risk is small — but if this
    member ever reports the app shell's URL with an empty title, that is the
    race and not a regression in ``tab_identity``.
    """
    new_tab = get_fn("new_tab")
    switch_tab = get_fn("switch_tab")
    sentinel = await eval_js(iid, "document.getElementById('sentinel').textContent")
    opened = await new_tab(instance_id=iid, url=f"{base}/cov/app_shell.html")
    assert await switch_tab(instance_id=iid, tab_id=opened["tab_id"]) is True
    return sentinel


async def test_a_fleet_of_six_browsers_answers_truthfully_about_every_page(
    fixture_app_server, caplog
):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    list_instances = get_fn("list_instances")
    close = get_fn("close_instance")

    # The backend's own durable-warning channel. `debug_logger.log_warning`
    # writes here, which is how a degraded `list_instances` row (F-874), a
    # storage read the page refused (F-869) or any other quiet fallback would
    # announce itself. Holding it empty for the whole driving phase is what
    # stops this node passing on six browsers that all half-worked; the two
    # named teardown warnings a concurrent six-way close may add are gated
    # separately at the end.
    caplog.set_level(logging.WARNING, logger="stealth.backend")

    #: (page, what to do with it, whether the profile is NAMED). Three of each:
    #: the unnamed half is the advertised path and the only half whose profile
    #: is disposable; the named half is what an explicit `user_data_dir` does.
    plan = [
        ("/cov/app_shell.html", "shell", False),
        ("/cov/form.html", "type", True),
        ("/cov/form.html", "click", False),
        ("/cov/form.html", "select", True),
        (f"/cov/slow_load.html?ms={HELD_MS}", "slow", False),
        ("/cov/plain.html", "tabswitch", True),
    ]
    assert len(plan) == FLEET_SIZE
    kinds = [kind for _, kind, _ in plan]
    clone_root = _clone_root()

    # `return_exceptions=True` is load-bearing, not defensive style. Without it
    # `gather` re-raises the FIRST failure while the other five spawns run to
    # completion, so `ids` would never be bound, the `try` below would never be
    # entered and its `finally` would never close them: one failed spawn would
    # leak up to five live headless Chromes — and a clone-role leak also holds
    # its directory, so the NEXT node in the session inherits it. A spawn phase
    # running six concurrent Chrome launches is the single most likely place in
    # this suite to fail on a loaded runner (measured on this machine with 118
    # foreign Chrome processes live: `ConnectionRefusedError [WinError 1225]`),
    # so this path is exercised, not hypothetical. Collect first, bind `ids`
    # from whatever DID start, and re-raise from INSIDE the try.
    started = time.monotonic()
    spawned = await asyncio.gather(
        *(
            spawn(
                headless=True,
                **({"user_data_dir": f"fleet-{kind}"} if named else {}),
                **sandbox_kwargs(),
            )
            for _, kind, named in plan
        ),
        return_exceptions=True,
    )
    spawn_seconds = time.monotonic() - started
    ids = [result["instance_id"] for result in spawned if isinstance(result, dict)]

    try:
        failures = [result for result in spawned if isinstance(result, BaseException)]
        if failures:
            raise failures[0]

        assert len(set(ids)) == FLEET_SIZE, f"the fleet shares instance ids: {ids}"

        # Every member got its OWN profile, and got the KIND of profile it asked
        # for. Three concurrent unnamed spawns resolving to ONE directory would
        # be six browsers on three profiles, and every disk claim below would be
        # about something other than what ran. If this line ever fails, read it
        # as a PRODUCT finding before a test bug: `resolve_profile_selection`
        # reserves a clone directory (`_protect_clone_dir`) but nothing reserves
        # `master`, so two unnamed spawns can both read it as free before either
        # Chrome exists.
        selections = [_selection(result) for result in spawned]
        roles = {
            kind: selection["profile_role"]
            for kind, selection in zip(kinds, selections, strict=True)
        }
        used_dirs = {
            kind: Path(selection["user_data_dir"]).name
            for kind, selection in zip(kinds, selections, strict=True)
        }
        # Every attempt a spawn made beyond its first is recorded here; a retry
        # re-enters the clone path, so this is what bounds the seed-warning
        # count at the end of the node.
        retries = sum(
            len(selection.get("spawn_retries") or ()) for selection in selections
        )
        assert len(set(used_dirs.values())) == FLEET_SIZE, used_dirs
        for _, kind, named in plan:
            if named:
                assert roles[kind] == "explicit", (kind, roles[kind])
            else:
                assert roles[kind] in {"master", "clone"}, (kind, roles[kind])
        # The advertised path really is exercised: at least one disposable clone.
        # (The FIRST unnamed spawn takes the master profile when it is free,
        # which is exactly the manual run's one-master-plus-clones shape.)
        assert "clone" in set(roles.values()), roles

        nav_started = time.monotonic()
        navigations = await asyncio.gather(
            *(
                navigate(instance_id=iid, url=f"{base}{path}")
                for iid, (path, _, _) in zip(ids, plan, strict=True)
            )
        )
        nav_seconds = time.monotonic() - nav_started

        # Every navigation answered about ITS OWN page. A fleet is the only
        # place this can be wrong: with one browser, "the last page navigated"
        # and "this browser's page" are the same string.
        for iid, (path, _, _), result in zip(ids, plan, navigations, strict=True):
            assert result["url"].endswith(path), (iid, path, result["url"])

        # The held page, in the fleet: its load fired before navigate answered.
        slow_index = kinds.index("slow")
        assert navigations[slow_index]["title"] == COV_TITLE_AFTER_LOAD, (
            "a navigation in the fleet answered before its page's own load "
            f"({navigations[slow_index]['title']!r}) — F-881"
        )

        id_by_kind = dict(zip(kinds, ids, strict=True))
        marker = "fleet-typed"
        act_started = time.monotonic()
        answers = await asyncio.gather(
            _drive_shell(id_by_kind["shell"]),
            _drive_type(id_by_kind["type"], marker),
            _drive_click(id_by_kind["click"]),
            _drive_select(id_by_kind["select"]),
            _drive_slow(id_by_kind["slow"]),
            _drive_tabswitch(id_by_kind["tabswitch"], base),
        )
        act_seconds = time.monotonic() - act_started
        by_kind = dict(
            zip(
                ["shell", "type", "click", "select", "slow", "tabswitch"],
                answers,
                strict=True,
            )
        )

        # ── Each answer, against the page's own state ────────────────────────
        # F-875/F-878: the app shell's DIV was driven, and the record's
        # `scroll_y_after` is that div's real `scrollTop`, read independently.
        shell = by_kind["shell"]
        assert shell["scrolled"] is True, shell
        assert shell["settled"] is True, shell
        assert shell["scroller_is_document"] is False, shell
        assert shell["scroller"]["id"] == "shell", shell
        real_top = await eval_js(
            id_by_kind["shell"], "document.getElementById('shell').scrollTop"
        )
        assert round(real_top) == shell["scroll_y_after"], (real_top, shell)
        # And the DOCUMENT did not move, which is what makes the pick the claim.
        assert await eval_js(id_by_kind["shell"], "window.scrollY") == 0

        # F-873: the tool said the text landed, and the form says it submitted.
        assert by_kind["type"] is True
        typed = await eval_js(
            id_by_kind["type"], "document.getElementById('cov-field').value"
        )
        assert typed == marker, typed
        assert await eval_js(id_by_kind["type"], "window.__covForm.submits") == 1

        # F-876: the click record names the target, and the page reacted.
        click = by_kind["click"]
        assert click["dispatch"] == "coordinate", click
        assert click["reason"] is None, click
        assert click["hit_is_target"] is True, click
        assert click["target"]["id"] == "cov-button", click
        assert (
            await eval_js(
                id_by_kind["click"],
                "document.getElementById('cov-clicked').textContent",
            )
            == "CLICKED"
        )

        # F-877: the option the rule picked is the one the control holds, and
        # the page's own change listener saw it.
        selection = by_kind["select"]
        assert selection["by"] == "text", selection
        assert selection["changed"] is True, selection
        assert selection["selected_index"] == 2, selection
        assert (
            await eval_js(
                id_by_kind["select"], "document.getElementById('cov-select').value"
            )
            == "c"
        )
        assert (
            await eval_js(
                id_by_kind["select"], "JSON.stringify(window.__covForm.selected)"
            )
            == '["c"]'
        )

        # F-881: the held page is complete by the time anything else ran.
        assert by_kind["slow"] == "complete"
        assert await eval_js(id_by_kind["slow"], "window.__covLoaded") is True

        # The control member read its own plain page before opening a tab.
        assert by_kind["tabswitch"] == COV_PLAIN_SENTINEL

        # ── F-874: the listing names what each browser is showing NOW ────────
        # Six live titles at once, each read fresh from its own browser. TWO of
        # the six moved without `navigate` — `click` retitled its own page and
        # `tabswitch` is on a tab it opened — so the pre-fix cached pair is
        # wrong for those two and this block goes red against the defect. The
        # url is asserted beside the title per member so a listing that mixed
        # two members up passes neither.
        listing = await list_instances()
        want = {
            "shell": (COV_SHELL_TITLE, "/cov/app_shell.html"),
            "type": (COV_FORM_TITLE, "/cov/form.html"),
            "click": (COV_CLICKED_TITLE, "/cov/form.html"),
            "select": (COV_FORM_TITLE, "/cov/form.html"),
            "slow": (COV_TITLE_AFTER_LOAD, "/cov/slow_load.html"),
            "tabswitch": (COV_SHELL_TITLE, "/cov/app_shell.html"),
        }
        for kind, (title, path) in want.items():
            entry = instance_entry(listing, id_by_kind[kind])
            assert entry["partial"] is False, entry
            assert entry["title"] == title, (kind, entry)
            assert path in entry["current_url"], (kind, entry)

        # Every answer is in. Everything logged from here on belongs to the
        # teardown, which is held to a different (named) standard — see the gate
        # at the end of the node. This line belongs INSIDE the `try`, after the
        # last answer assertion: if an answer fails it is never bound, but the
        # gate is never reached either, and moving it earlier would move
        # driving-phase warnings into the tolerated teardown window.
        answers_end = len(caplog.records)

        # What the product's tracked-pid record held while all six were live.
        # Read now so the post-close witness below is provably non-vacuous: an
        # empty intersection after close means "left the record", not "never
        # in it".
        tracked_while_live = set(ids) & set(
            runtime.process_cleanup._load_tracked_pids()
        )

        print(
            f"\nfleet of {FLEET_SIZE}: spawn {spawn_seconds:.1f}s, "
            f"navigate {nav_seconds:.1f}s, actions {act_seconds:.1f}s, "
            f"total {time.monotonic() - started:.1f}s "
            f"(roles {sorted(set(roles.values()))})"
        )
    finally:
        closed = await asyncio.gather(
            *(close(instance_id=iid) for iid in ids), return_exceptions=True
        )

    assert all(result is True for result in closed), closed

    # Nothing of ours is live any more.
    still_live = {
        entry["instance_id"]
        for entry in await list_instances()
        if entry.get("source") == "active"
    }
    assert still_live.isdisjoint(ids), still_live

    # ── The disposable-profile promise, on the path that makes it ───────────
    # `spawn_browser`'s docstring says an unset `user_data_dir` "automatically
    # clones a disposable session from the master profile and deletes it when
    # the instance closes", and the same docstring says a NAMED profile "is NOT
    # auto-cleaned and persists on disk indefinitely". Both halves are asserted,
    # because a fleet of only named profiles could not have caught a broken
    # reclaim and a fleet of only unnamed ones could not have caught a named
    # profile being eaten. The delete is
    # `process_cleanup._cleanup_profile_for_metadata` (gated on the entry's
    # `auto_clone` flag) via `_cleanup_profile_dir`'s `shutil.rmtree`.
    #
    # EVERY claim here is scoped to a directory THIS fleet was given. The clone
    # root is shared — other worktrees, other pytest processes and this
    # machine's other agents all write into it — so a bare "what appeared since
    # we started" diff is not a fact about this test, and a spawn RETRY adds a
    # second, abandoned clone directory that nothing reclaims until a cap sweep.
    # Both were assertions here and both are gone; what remains is the one shape
    # that is genuinely ours: of the six directories the product told us it
    # used, exactly the named ones survive.
    clone_dirs = {used_dirs[kind] for kind in kinds if roles[kind] == "clone"}
    named_dirs = {used_dirs[kind] for kind in kinds if roles[kind] == "explicit"}

    # A Windows file lock makes `_cleanup_profile_dir` give up after
    # `_MAX_CLEANUP_RETRIES` and leave the entry tracked for
    # `cleanup_deferred_profiles` — which nothing drives in the in-process lane,
    # since the idle reaper is not running. Driving it inside the poll turns
    # "deferred" into "eventually" without masking a real leak: the deadline
    # still decides, and a directory that is never reclaimed still fails.
    #
    # The same poll waits for the PROCESS witness, for all six members. The
    # shared pid record (`browser_pids.json`, read through
    # `process_cleanup._load_tracked_pids`) keeps an instance until the product
    # itself has seen its Chrome die: `kill_browser_process` untracks a
    # named/master entry only after its kill succeeded, and `finalize` /
    # `cleanup_deferred_profiles` untrack a clone entry only once
    # `psutil.pid_exists` is False AND its directory is gone. A reclaimed clone
    # directory already implies a dead Chrome (Windows will not `rmtree` a
    # profile a live one holds), but a named profile is meant to survive, so for
    # those three members this record is the ONLY fact the node has about the
    # process — and it is what earns the `Chrome kill … exceeded` tolerance
    # below: `close_instance` answers True on that path by design and the
    # manager's dict forgets the instance either way, so neither can vouch that
    # the worker thread's kill ever landed. A kill that wedges keeps its pid
    # tracked, `cleanup_deferred_profiles` skips a live pid, and the deadline
    # turns that into a failure here rather than a Chrome that outlives the
    # session.
    def _lingering() -> set[str]:
        return set(ids) & set(runtime.process_cleanup._load_tracked_pids())

    deadline = time.monotonic() + RECLAIM_BUDGET_SECONDS
    leftover = clone_dirs & _dirs_in(clone_root)
    lingering = _lingering()
    while (leftover or lingering) and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        runtime.process_cleanup.cleanup_deferred_profiles()
        leftover = clone_dirs & _dirs_in(clone_root)
        lingering = _lingering()
    assert not leftover, (
        f"disposable auto-clone(s) still on disk {RECLAIM_BUDGET_SECONDS:.0f}s "
        f"after close: {sorted(leftover)}"
    )
    assert not lingering, (
        f"instance(s) still in the product's tracked-pid record "
        f"{RECLAIM_BUDGET_SECONDS:.0f}s after close, i.e. a Chrome the product "
        f"has not seen die: {sorted(lingering)}"
    )
    # …and the record really was the fleet's, so the line above is a fact about
    # six departures and not about a record that never mentioned them.
    assert tracked_while_live == set(ids), sorted(set(ids) - tracked_while_live)

    survivors = set(used_dirs.values()) & _dirs_in(clone_root)
    assert survivors == named_dirs, (
        f"of the profiles this fleet was given, the survivors should be exactly "
        f"the named ones {sorted(named_dirs)} — got {sorted(survivors)}"
    )

    # ── No browser in the fleet degraded quietly on the way through ──────────
    # The gate is split at the moment the last answer was in, because the two
    # phases are held to genuinely different standards. While the fleet is being
    # DRIVEN nothing on the backend's durable channel is acceptable at all, bar
    # one named property of the lane. While six Chromes are torn down AT ONCE on
    # Windows, the named warnings in `TEARDOWN_WARNINGS_WITH_A_REAPER` are — and
    # the assertions above are what earn them.
    #
    # The channel is the BACKEND's own (`stealth.*`), not the process's:
    # nodriver's websocket teardown puts `asyncio` ERROR records ("Task
    # exception was never retrieved", `ConnectionClosedOK`) on every run of this
    # tier, and those belong to the library's shutdown, not to any answer a tool
    # gave.
    answer_records = caplog.records[:answers_end]
    teardown_records = caplog.records[answers_end:]

    # The driving phase's ONE exception, which is a property of the LANE and not
    # of the run. An auto-clone's directory name is seeded from the MCP client's
    # roots (`clone_storage._client_session_seed`), and the in-process E2E tier
    # has no MCP client at all, so `get_context()` raises, the warning is logged
    # and the documented `codex_workspace`/`claude_project_dir`/`pwd`/`getcwd`
    # fallback chain answers. Named rather than filtered out by level or by
    # logger — and pinned to the level AND the logger it is expected on, so a
    # hypothetical ERROR that happened to mention the symbol is not excused.
    seed_fallbacks = [
        record
        for record in answer_records
        if record.name == "stealth.backend"
        and record.levelno == logging.WARNING
        and "_client_session_seed" in record.getMessage()
    ]
    others = [
        record
        for record in _stealth_warnings(answer_records)
        if record not in seed_fallbacks
    ]
    assert others == [], [record.getMessage() for record in others]
    assert seed_fallbacks, (
        "no clone-seed fallback was logged, so no member took the auto-clone "
        "path — this fleet is no longer covering the advertised profile path"
    )
    # One per clone-path ATTEMPT, not per surviving clone: a retry re-enters
    # `_fallback_profile_selection`, which re-resolves and so seeds again. The
    # lower bound is the real claim (each surviving clone got its name from the
    # seed); the upper bound is what stops the tolerance growing silently.
    assert len(clone_dirs) <= len(seed_fallbacks) <= len(clone_dirs) + retries, (
        f"{len(seed_fallbacks)} clone-seed fallbacks for {len(clone_dirs)} "
        f"surviving auto-clone(s) and {retries} recorded spawn retr(ies) — "
        "expected one per clone-path attempt"
    )

    # The teardown phase. Closing six browsers at once is where Windows contends
    # with itself, and the product says so everywhere it can: a Chrome whose
    # blocking kill outruns `settings.close_kill_timeout` is handed to
    # `process_cleanup` (and that worker may itself report the pid still present
    # two seconds after `process.kill()` — the same slow kill, seen from its
    # other end), and a profile directory a dying Chrome still holds open is left
    # tracked for `cleanup_deferred_profiles` (measured across runs: `[WinError
    # 5] Access is denied` on a `Trusted Icons` png, one kill over the 5.0 s
    # budget, one `did not die after force kill` whose pid was gone moments
    # later). All are tolerated ONLY because the assertions above have
    # already proved each one's consequence was repaired: every disposable clone
    # directory was gone inside the reclaim budget — which this node DROVE
    # rather than waited for — and every one of the six instances had left the
    # product's tracked-pid record, which is the product vouching that each
    # Chrome is dead (`closed` being all True and `still_live` being empty are
    # NOT that witness: both hold by design on the timeout path). The match is
    # component + operation + sentence, so
    # the neighbouring warnings from those same operations are not excused;
    # `Blocking teardown failed` and `browser.stop() coroutine failed` have no
    # reaper behind them and should fail this node.
    unrepaired = [
        record
        for record in _stealth_warnings(teardown_records)
        if not record.getMessage().startswith(TEARDOWN_WARNINGS_WITH_A_REAPER)
    ]
    assert unrepaired == [], [record.getMessage() for record in unrepaired]
