"""THE one home for "put this process's logging back the way you found it".

A pin that measures what reaches a SINK has to own the process's logging for the
duration of the test: `logging` is global, so every handler, level, filter and
record factory another module installed is in the way. Both F-906's and F-907's
pin files therefore start from a clean floor — and for a while both of them left
the process on that floor afterwards.

**That is the defect this module exists to close, and it was MEASURED.**
`reset_logging()` stripped every logger and never put anything back, so a later
module ran with levels, propagation and handlers that were not its own; run in
natural order, `tests/test_observability.py`'s secret-canary pin — one of the
two labelled RELEASE BLOCKER in its own assertion — reported three canaries
disclosed via **stderr**. Reversed, the same slice was green. F-906's review
named the residual (N2) and said "if random test ordering is ever introduced,
snapshot-and-restore instead"; a second file in the slice depending on the
process's logging made it load-bearing with no ordering plugin at all.

**The restore is a DIFF, never an assignment, and that is the whole subtlety.**
The obvious version — snapshot `logger.handlers`, assign it back in teardown —
is wrong here and was measured wrong: pytest's own logging plugin runs each
phase of each item inside its own `catching_logs`, so the handler set at fixture
SETUP is not the set at fixture TEARDOWN (a different `LogCaptureHandler` each
time). Assigning the setup-phase list back at teardown resurrects a handler
pytest had already retired and drops the one it currently holds. So instead:

* every handler the snapshot saw that is no longer attached is **re-attached**;
* every handler attached now that the snapshot did NOT see is **removed and
  closed** — unless it is pytest's, which is the one other party that adds
  handlers between our setup and our teardown.

`_pytest` is named explicitly rather than inferred. It is a test-only module and
pytest is the only other writer of root's handler list inside a test; a rule
that could not say so would have to guess, and guessing is what produced the
version above.

Two more rules, each a thing that went wrong or could:

* **Close only handlers the TEST created.** The first `reset_logging` called
  `handler.close()` on its way past, which for a `RotatingFileHandler` another
  module owns is destructive and is not undone by re-attaching the object. So
  closing is decided at RESTORE time, by identity against the snapshot. What
  the test opened is closed (a file handler on a `tmp_path` Windows cannot
  remove while it is open); what predates the test comes back OPEN.
* **A logger the test CREATED is left inert rather than removed.**
  `logging.Logger.manager` has no removal API, so the closest available answer
  is NOTSET / propagating / no filters / no handlers — what a logger nobody has
  configured looks like.

Deliberately its own module and not a corner of `fakes.py`: that file is THE
home for the DOM/tab/browser doubles, this is one question about a stdlib
global, and a 2 000-line file is not where the next person looks for it. One
home for BOTH pin files, because two copies of a restore routine are two things
that can drift, and the one that drifts is the one nobody re-measures.
"""

from __future__ import annotations

import contextlib
import logging

#: Per logger: handlers, level, propagate, filters, disabled.
_LoggerState = tuple[list[logging.Handler], int, bool, list[object], bool]

#: The one other party that adds and removes handlers between our setup and our
#: teardown. Its handlers are never ours to close and never ours to drop.
_PYTEST = "_pytest"


def _is_pytests(handler: logging.Handler) -> bool:
    return type(handler).__module__.partition(".")[0] == _PYTEST


def _live_loggers() -> list[logging.Logger]:
    """Root plus every logger that exists right now.

    `loggerDict` also holds `PlaceHolder` objects for the gaps in a dotted
    hierarchy; those carry no state and are skipped rather than materialised.
    """
    named = [
        logging.getLogger(name)
        for name, obj in list(logging.Logger.manager.loggerDict.items())
        if isinstance(obj, logging.Logger)
    ]
    return [logging.getLogger(), *named]


def snapshot() -> dict[str, object]:
    """Everything :func:`reset` is about to destroy."""
    return {
        "factory": logging.getLogRecordFactory(),
        "loggers": {
            logger.name: (
                list(logger.handlers),
                logger.level,
                logger.propagate,
                list(logger.filters),
                logger.disabled,
            )
            for logger in _live_loggers()
        },
    }


def reset() -> None:
    """Every logger to the floor: no handlers of ours, NOTSET, propagating,
    root at WARNING — the state a fresh interpreter has before anything
    configures it.

    Pytest's own handlers stay attached: they are not part of what a pin
    measures (a pin that needs a bare root, like F-907's ``lastResort`` one,
    strips them locally and puts them back itself), and detaching them here is
    how the first version of this came to fight the plugin.

    It REMOVES and never CLOSES: whether a handler is this test's to close is a
    question only :func:`restore` can answer.
    """
    for logger in _live_loggers():
        for handler in list(logger.handlers):
            if not _is_pytests(handler):
                logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        logger.filters = []
        logger.disabled = False
    logging.getLogger().setLevel(logging.WARNING)
    # The factory is part of the floor, not an afterthought: a redaction
    # factory another module left installed rewrites the very arguments a pin
    # here is measuring. MEASURED — without this line, F-907's "the unredacted
    # leak IS visible" pin went green-by-accident in one file order and RED in
    # the other, which is a pin measuring nothing rather than a pin failing.
    logging.setLogRecordFactory(logging.LogRecord)


def restore(before: dict[str, object]) -> None:
    """Put back exactly what :func:`snapshot` saw, by DIFF — see the module
    docstring for why an assignment is wrong."""
    loggers: dict[str, _LoggerState] = before["loggers"]  # type: ignore[assignment]

    for logger in _live_loggers():
        state = loggers.get(logger.name)
        was = list(state[0]) if state else []
        was_ids = {id(handler) for handler in was}

        # What arrived while we owned the block and is not pytest's is the
        # test's: detach it, and close it so a tmp_path file handler does not
        # hold the directory open on Windows.
        for handler in list(logger.handlers):
            if id(handler) not in was_ids and not _is_pytests(handler):
                logger.removeHandler(handler)
                with contextlib.suppress(Exception):
                    handler.close()

        # What the snapshot held and is no longer attached comes back, OPEN.
        attached = {id(handler) for handler in logger.handlers}
        for handler in was:
            if id(handler) not in attached:
                logger.addHandler(handler)

        if state is None:
            # Created during the test. `Logger.manager` cannot forget it, so
            # the closest thing to absent is a logger nobody configured.
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
            logger.filters = []
            logger.disabled = False
            continue
        _, level, propagate, filters, disabled = state
        logger.setLevel(level)
        logger.propagate = propagate
        logger.filters = list(filters)
        logger.disabled = disabled

    logging.setLogRecordFactory(before["factory"])  # type: ignore[arg-type]


def drift(before: dict[str, object]) -> list[str]:
    """What is still different from the snapshot, named rather than counted.

    Pytest's own handlers are excluded on both sides: it adds and removes one
    per phase, so they differ between a fixture's setup and its teardown for
    reasons that are none of our business.
    """
    loggers: dict[str, _LoggerState] = before["loggers"]  # type: ignore[assignment]
    out: list[str] = []
    if logging.getLogRecordFactory() is not before["factory"]:
        out.append("record factory")
    for logger in _live_loggers():
        state = loggers.get(logger.name)
        if state is None:
            continue
        was, level, propagate, _filters, disabled = state
        mine = [h for h in logger.handlers if not _is_pytests(h)]
        expected = [h for h in was if not _is_pytests(h)]
        if [id(h) for h in mine] != [id(h) for h in expected]:
            out.append(f"{logger.name or 'root'}.handlers")
        if logger.level != level:
            out.append(f"{logger.name or 'root'}.level")
        if logger.propagate != propagate:
            out.append(f"{logger.name or 'root'}.propagate")
        if logger.disabled != disabled:
            out.append(f"{logger.name or 'root'}.disabled")
    return out


@contextlib.contextmanager
def owned():
    """Own the process's logging for the block, and hand it back unchanged.

    The `finally` is the whole point: a pin that fails mid-test must still
    leave the process as it found it, or one red node becomes a file-ordering
    puzzle in another module.

    **The check is HERE and not in a test node**, and that is the second
    lesson this module cost. A node asserting "root is as I found it" runs
    INSIDE this block, where `reset` has deliberately put everything on the
    floor — so it measures the floor, agrees with the baseline only when the
    baseline happened to be the floor, and is green for the wrong reason in one
    file order and red in the other (measured, both). After `restore` is the
    one moment the claim is about, so the claim is made there, for every test
    in both pin files rather than once at the end of one.
    """
    before = snapshot()
    reset()
    try:
        yield
    finally:
        restore(before)
        drifted = drift(before)
        assert not drifted, (
            f"logging state not restored: {drifted}. A pin that owns the "
            f"process's logging must hand it back, or a later module runs on "
            f"state that is not its own."
        )
