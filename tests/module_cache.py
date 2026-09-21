"""THE one home for taking a module out of ``sys.modules`` and putting it back.

A module's identity lives in **two** places, not one: the ``sys.modules``
mapping, and the attribute its PARENT package carries. ``import a.b`` writes
both -- CPython binds ``b`` onto ``a`` as the last step of loading it -- and the
import system keeps them agreeing. A test that writes only the mapping leaves
them disagreeing, and both directions of that disagreement have now been
measured on this branch:

* **Pop the PARENT, leave the children cached.** The next ``import a`` builds a
  NEW module object while ``import a.b`` is a ``sys.modules`` HIT that never
  re-binds ``b`` onto it -- not even an explicit ``importlib.import_module``
  repairs it. The package is then permanently un-walkable by attribute.
* **Pop a CHILD, re-import it, restore only ``sys.modules[name]``.** The
  re-import rebound the parent's attribute to the NEW object, so putting the OLD
  one back in the mapping leaves the parent naming a module the cache does not.

Neither is local to the test that does it. pytest resolves a dotted
``monkeypatch.setattr("a.b.c.d", ...)`` target by ``__import__`` plus a
``getattr`` walk (``_pytest/monkeypatch.py``), so a file that leaves the pair
disagreeing hands every LATER file in the lane a parent that names the wrong
object -- green alone, red under the full alphabetical order. That is exactly
how ``tests/test_python_exec_timeout.py`` came to fail with ``module
'stealth_chrome_devtools_mcp' has no attribute 'embedded'``, and how
``embedded.file_based_element_cloner`` came to be orphaned by
``tests/test_element_cloner_output_dir.py``'s isolation fixture.

Both hazards are one rule -- **move the PAIR, never one half** -- and this is
where the rule lives. Two files need it; a second spelling is precisely how the
two would come to disagree about it.

Deliberately NOT a general import sandbox: it touches the names it is given and
nothing else, it does no importing of its own, and it knows nothing about this
package. ``tests/operator_fence.py`` is the other module-level test helper and
answers a different question (where a run is allowed to write).
"""

from __future__ import annotations

import contextlib
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType


def bind(name: str, module: ModuleType | None) -> None:
    """Make ``sys.modules[name]`` and the parent package's attribute agree.

    ``module`` is the object that should be at ``name`` afterwards, or ``None``
    for "nothing should be" -- in which case the parent's attribute is dropped
    too, so a removal is as complete as a restore.

    A parent that is not itself cached is left alone: there is no object to
    write the attribute onto, and re-importing one here would be this module
    doing an import of its own.
    """
    parent_name, _, leaf = name.rpartition(".")
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module

    parent = sys.modules.get(parent_name) if parent_name else None
    if parent is None:
        return
    if module is None:
        with contextlib.suppress(AttributeError):
            delattr(parent, leaf)
    else:
        setattr(parent, leaf, module)


def cached_under(prefix: str) -> dict[str, ModuleType]:
    """Every cached module that is ``prefix`` or lives under it.

    A plain ``startswith(prefix)`` would also match a sibling top-level package
    whose name merely begins with these letters, so the dot is required.
    """
    return {
        name: module
        for name, module in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }


@contextlib.contextmanager
def preserved(*names: str) -> Iterator[None]:
    """Put these names back as they are now -- both halves -- on the way out."""
    saved = {name: sys.modules.get(name) for name in names}
    try:
        yield
    finally:
        for name in sorted(saved):
            bind(name, saved[name])


@contextlib.contextmanager
def absent(*names: str) -> Iterator[None]:
    """Remove these names for the block, then restore them.

    The removal goes through :func:`bind` as well, so the body sees a parent
    that does not name the module either -- which is what makes a fresh import
    inside the block actually fresh.
    """
    with preserved(*names):
        for name in names:
            bind(name, None)
        yield


@contextlib.contextmanager
def pristine_package(prefix: str) -> Iterator[None]:
    """Restore a whole package subtree, whatever the body imported or popped.

    Names the body ADDED are removed rather than left behind, because the point
    of the call sites is that a node's pops and re-imports do not outlive it.
    Restoration is in sorted order so a parent is back before its children are
    written onto it (``"a"`` sorts before ``"a.b"``).
    """
    saved = cached_under(prefix)
    try:
        yield
    finally:
        for name in sorted(cached_under(prefix), reverse=True):
            if name not in saved:
                bind(name, None)
        for name in sorted(saved):
            bind(name, saved[name])
