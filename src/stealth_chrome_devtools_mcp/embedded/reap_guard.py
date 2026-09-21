"""THE one home for the rule startup orphan recovery kept breaking: **an answer
we could not establish must resolve toward NOT KILLING** (F-916, F-917, F-918).

Recovery runs on every backend cold start, and the thing it is deciding about
may be a human's logged-in Chrome — the one piece of state reconnecting cannot
rebuild, because a killed browser does not flush its session. So every witness
this path consults has two ways of answering "no": the process is provably not
ours, which is a DECISION, and the process could not be read at all, which is
not one. Three places collapsed those two into the same value, and all three
resolved toward the kill.

Three pieces, one sentence:

* :data:`UNDECIDED` — the CLASSIFICATION's third answer.
  ``browser_reattach._adoptable_entry`` returns it where it used to return a
  bare ``None``, so "we could not establish this entry's verdict" stops looking
  like "this entry is a disposable auto-clone" (F-916).
* :func:`spared_pids` — the KILL SET's filter. The reap builds its set by
  scanning the ``user_data_dir``, while the spare is matched by
  ``instance_id``; on a SHARED profile — which is what the master is — one
  stale entry's reap therefore reached a browser another entry had just
  protected (F-917).
* :func:`killable` — the ACT's permission. A pid whose ``name()`` cannot be
  read is not a pid we may end (F-918).

**What the rule costs, named rather than hidden.** Sparing what we could not
classify means a browser we can neither adopt nor reap stays running and stays
recorded. That is deliberate and it is bounded: the entry is re-classified on
every later cold start, and the moment its Chrome actually exits the witness
becomes readable, the entry is reaped and it leaves the record. An operator who
wants it gone sooner has ``kill-orphans --force``, which skips this whole
classification by design. The alternative — a leaked Chrome traded against a
killed login — is the trade F-888 already made once, and it is the same trade
every ``resolves toward`` in this tree makes: ``profile_lock._browser_pids``
reads an unaskable process table as HELD, ``spawn_leak._started_after`` spares
a pid whose start time it cannot read, and ``backend_eviction`` refuses to
evict a backend it cannot prove is idle.

A leaf: ``psutil`` and stdlib only. The Chromium-family name test arrives as an
ARGUMENT (``process_cleanup._is_browser_process_name``, the one home for what
counts as a browser we launch), so this module adds no second answer to it, and
nothing here writes a log line — :class:`Verdict` carries the wording and the
caller writes it on its own component name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Collection, Mapping


class Undecided:
    """We could not establish this entry's verdict. Spare it; do not adopt it.

    A distinct TYPE rather than a second ``None``, because the whole defect was
    that one value stood for two statements. There is exactly one instance,
    :data:`UNDECIDED`, and callers compare with ``is``.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "UNDECIDED"


UNDECIDED = Undecided()


def spared_pids(
    entries: Mapping[str, Mapping[str, object]], spare: Collection[str]
) -> frozenset[int]:
    """Every pid the entries named by *spare* record — what a reap may not end.

    The bridge between the two things that are matched differently: recovery
    decides what to SKIP by instance id, and the reap decides what to KILL by
    directory. Two entries may legitimately share one ``user_data_dir`` — that
    is what a shared profile IS — so the fix cannot be to match the spare by
    directory instead; it is to carry the spared entries' pids over to the kill
    set and subtract them there.

    An entry whose pid is not an int names nothing killable, so it contributes
    nothing rather than raising.
    """
    protected: set[int] = set()
    for instance_id in spare:
        entry = entries.get(instance_id)
        if entry is None:
            continue
        pid = entry.get("pid")
        if isinstance(pid, int):
            protected.add(pid)
    return frozenset(protected)


@dataclass(frozen=True)
class Verdict:
    """What one pid turned out to be, and what a reap may do about it."""

    # Nothing is there to end. The caller's reap has already succeeded.
    gone: bool
    # Identified as a browser of the family we launch, so it may be ended.
    may_kill: bool
    # The line to log when it may NOT be ended; None when it may.
    refusal: str | None


def killable(
    pid: int, instance_id: str, is_browser_name: Callable[[str], bool]
) -> Verdict:
    """May this reap end *pid*, and if not, what does the operator need told.

    Four outcomes and they are deliberately distinguishable, because the two
    that mean "do not kill" have opposite causes:

    * the process is GONE — nothing to do, and the reap counts as done;
    * it is a process of ours — kill it;
    * it is something else entirely — refuse, which is the guard that already
      worked;
    * **its name could not be READ** — refuse. Windows answers
      ``AccessDenied`` for a process this account may not open, and before
      F-918 that landed in a blanket ``except`` that logged "Could not verify
      process {pid}" and then fell through to ``terminate()``. An unidentified
      pid was ended on the strength of a record entry alone.

    What refusing costs is stated in the module docstring and is the point of
    the trade: an orphan we cannot identify is left running.
    """
    try:
        name = psutil.Process(pid).name()
    except psutil.NoSuchProcess:
        return Verdict(gone=True, may_kill=False, refusal=None)
    except Exception as error:  # noqa: BLE001  PERMANENT(anything psutil cannot answer with is the same answer: we do not know what this pid is, so we do not end it)
        return Verdict(
            gone=False,
            may_kill=False,
            refusal=(
                f"Could not verify process {pid} for {instance_id} "
                f"({type(error).__name__}: {error}); it was NOT killed, because a "
                f"pid we cannot identify may be a browser holding a login"
            ),
        )
    if is_browser_name(name):
        return Verdict(gone=False, may_kill=True, refusal=None)
    return Verdict(
        gone=False,
        may_kill=False,
        refusal=f"PID {pid} is not a browser process ({name}), skipping",
    )
