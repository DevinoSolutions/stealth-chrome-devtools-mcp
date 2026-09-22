"""THE one home for "which tracked browsers keep their logins, and which of
those profiles is open right now" (F-921).

`kill-orphans --force` deletes F-888's persistent-profile spare, so it is the
one verb in the tree that can end a browser holding a human's logins. What it
is about to end has to be countable BEFORE it runs — and counting it is not a
CLI question. It is a reading of `browser_pids.json` plus Chrome's own idea of
who holds a profile. `cli.py` keeps the PHRASING; this module owns the ANSWER,
so the counts have a home a test can reach without going through a parser.

Extracted from `cli.py` at F-921's first review, and the reason is worth
keeping: that file stood at 999 of the 1000-LOC default and the two-line fix
for an empty record did not fit, so the budget was deciding the code rather
than the other way round. The first version of this finding wrote "the next
change should extract this" into its own §6 — a rule left where the person who
needs it will not read it, which is F-921's own defect one level up. The
precedent is `dom_handler.py` at 997 producing `script_evaluation`.

**Persistent is `browser_pid_registry.on_persistent_profile`, never a second
spelling.** That predicate is THE one home for "this directory outlives its
browser" (F-888) and it is the very thing `--force` skips, so a warning built
on it cannot name a different set from the one the reap takes. Deliberately
NOT `browser_reattach.Classified.spare`, which answers a different question:
the spare is what recovery would protect, and `--force` skips the
classification entirely, so every persistent entry is at risk and not just the
unadoptable ones. Deliberately NOT `clone_storage.clone_is_auto` either — that
reads the on-disk MARKER rather than the RECORD, and could disagree with the
kill it is describing.

**Open is the CALLER's**, handed in as ``is_open`` on `backend_liveness`'s
pattern: answering it needs `profile_lock` through `clone_storage`'s adapter
and `process_cleanup`'s process scan, and importing either would make this a
node in the lifecycle graph instead of a leaf. It must never be a presence
test — F-871 exists because `Path.exists()` over Chrome's `Singleton*` read a
reaped browser's residue as busy and could not see a dangling symlink at all.

**Counted by DIRECTORY, and the count is a FLOOR.** The reap kills every
browser on a recorded entry's ``user_data_dir``
(`process_cleanup._kill_processes_for_metadata`), so two entries on one profile
end one profile — and so a directory the RECORD calls disposable has every
browser on it ended, including one a human started there by hand. Such a
browser is absent from this answer, because the record is the only thing that
knows which directories a reap will touch and it classifies them by how WE made
them, not by what is inside. **F-922 is the decided follow-up**: directory-wide
reaping is being restricted to auto-clone directories, and on a named or
persistent profile only recorded pids will be killed, which is what closes the
gap. Until it lands a caller must phrase this as the record's scope and never
as a death toll.

A leaf: `browser_pid_registry` (itself a leaf) and stdlib. It reads no file,
takes no lock and probes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded.browser_pid_registry import (
    Entries,
    on_persistent_profile,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class AtRisk:
    """What one pass over the record found.

    ``tracked`` counts persistent DIRECTORIES rather than entries, for the
    reason the module docstring gives. ``open_names`` are the basenames of the
    ones a browser currently holds, sorted — names and counts only, never a
    path, because a caller PRINTS this and a path names the operating user
    (F-869/F-877 discipline).
    """

    tracked: int
    open_names: tuple[str, ...]


def assess(entries: Entries, *, is_open: Callable[[Path], bool]) -> AtRisk:
    """The persistent profiles *entries* names, and which of them are open."""
    directories: dict[str, Path] = {}
    for entry in entries.values():
        recorded = entry.get("user_data_dir")
        # The recorded string is itself the de-duplication key: it arrives
        # normalized from `browser_pid_registry.normalize_entries`, which is
        # the same normalization the reap's own directory match uses.
        if isinstance(recorded, str) and on_persistent_profile(entry):
            directories.setdefault(recorded, Path(recorded))
    return AtRisk(
        tracked=len(directories),
        open_names=tuple(
            sorted(path.name for path in directories.values() if is_open(path))
        ),
    )
