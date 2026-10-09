# F-956 — session-lock waiters are served in the order they asked, and the line is visible

**Severity:** Low-Medium (feature; closes a fairness gap in F-952's advisory lock).
**Files:** `embedded/session_lease.py` (queue, hand-off, `status(session, owner=None)`),
`embedded/tool_sections/session_lock.py` (descriptions, `get_session_lock_status(owner=)`),
`tests/goldens/tool_surface.json` (soft golden, regenerated deliberately), `NAVMAP.md`,
`RUNBOOK.md`, `CHANGELOG.md` (`## Unreleased`). Tests: `tests/test_session_lease.py`
(24 -> 37 test functions: 12 queue tests, 1 tool-surface test), one shape update in `tests/test_fleet_session.py`.
**Branch:** `feat/f956-fifo-session-lock`, from `origin/main` (38f42d9, 2.1.26).

---

## 1. What was wrong

`acquire` with `wait_seconds>0` polled every 0.25 s; after a release, whoever polled first won
(the module docstring said so). Several agents waiting on `fleet` therefore got it in an arbitrary
order, and a `wait_seconds=0` newcomer could take a lock the instant it freed, ahead of agents
that had waited for minutes. There was also no way to see who was waiting.

## 2. Design

* **One queue per session** (`_queues: dict[str, list[_Waiter]]`), each `_Waiter` = owner, enqueue
  time (monotonic), and an `asyncio.Event`. All tool calls run on one loop, so no thread lock.
* **Only the head may take** a lease that is free or expired (`_may_take`). A newcomer takes a free
  lock only if the queue is empty; otherwise `wait_seconds>0` joins the back and `wait_seconds=0`
  is refused naming the holder (or "free but N queued"), the expiry, and the position it would
  have. The holder renewing its own lease is checked first and is immediate.
* **No polling.** `release` and a departing head call `_wake_head`; the head also sleeps with a
  timeout equal to the time left on the lease, so an EXPIRED lease nobody releases still goes to
  the head. The rest of the line sleeps until it becomes head or its own deadline.
* **Leaving the line is a `finally`:** success, deadline and `CancelledError` all remove the waiter;
  if it did not take the lock the new head is woken, so a dead caller cannot block the queue.
* **One place per owner:** a second `acquire` by an owner already waiting raises, naming its
  position. The holder asking again is a renewal, never an entry.
* **`status(session, owner=None)`** adds `queue_length` and `waiting` (`owner`, 1-based `position`,
  `waiting_seconds`) always; with `owner`, `your_position` (0 holder, 1..N queue, null neither).
  `spawn_browser`'s already-running answer embeds `status(...)` via `fleet_session.reuse_answer`, so
  it gets the two new fields; a free lock is now `{session, locked: False, queue_length: 0, waiting: []}`.
* **`reset()`** clears queues and wakes every waiter with `dropped=True`; they raise a `ToolError`
  ("lock state was reset") instead of waiting on a queue that no longer exists.
* `POLL_SECONDS` is gone (its only reader was the poll loop). `MAX_WAIT_SECONDS` stays 60.

No deviation from the brief. Follow-up (review): the finally wakes the new head unconditionally, so when the head TAKES the lock the next waiter re-arms its timer on the new holder's expiry instead of sleeping to its own deadline (pinned by `test_the_next_waiter_re_arms_on_the_new_holders_expiry`). One choice the brief left open: a waiter whose own deadline passes
while it is not head simply leaves; its refusal reports the position it had reached then.

## 3. Evidence

RED on the pre-change `session_lease.py` (swapped in from `HEAD`, new tests in place): 10 of the 12
queue tests failed. The two that passed on the old code are the expired-lease-to-head test (the old head polled first anyway; the sharper just-expired-newcomer test is the RED one) and the cancelled-head test (a cancelled poller simply stops polling). The load-bearing three:

* order (a): `ToolError: Session 'fleet' is locked by 'b', not 'a'` — on the old poll, `b` (a
  different phase of the 0.25 s timer) won before `a`;
* newcomer (b) and just-expired newcomer: `DID NOT RAISE` — the jumper took the lock.

The tests stagger waiters by 0.1 s so the old poll cannot satisfy them by timer luck.

GREEN: see the commit message for pass counts. Mutation (runtime rebinding, no source edit):
`_may_take` -> `held is None` and `_wake_head` -> wake everyone, newest first, makes the order test
fail with `locked by 'c', not 'a'`.

## 4. Soft golden

`tests/goldens/tool_surface.json` changed only in the descriptions of `acquire_session_lock` and
`get_session_lock_status` and the new optional `owner` parameter of the latter. The tool count is
unchanged (97). Regenerated with `PYTHONUTF8=1 python tools/dump_tool_surface.py --write` and the
diff read before commit.

## 5. Not done

A tool call carries no caller identity, so the owner label is still a typed string: a caller can
queue under any label. The queue is as advisory as the lease. Queues are in memory and per backend,
like leases.
