"""One module per ``SECTION_TOOLS`` section. The contract, stated once:

1. A section module exports exactly two names: ``SECTION`` (the section string) and
   ``TOOLS`` (a tuple of the section's tool functions, in surface order).
2. A section module NEVER applies ``@section_tool`` or any ``mcp.*`` decorator, and
   never imports ``mcp``, ``registry`` or ``server``. Registration is DRIVEN from
   ``server.py``'s module body, once per execution of it — see server.py's binding
   loop and DESIGN §8. A decorator here would register into the FIRST execution's
   ``mcp`` only, leaving the runpy ``__main__`` load with a zero-tool app: the
   mirror image of the 282 == 3 x 94 accumulation this repo has already paid for,
   and strictly nastier, because the 94-count tripwire cannot see it.
3. Every singleton and knob is resolved as ``rt.<name>`` at call time. A
   ``from ...tool_runtime import browser_manager`` binding would create a second
   patchable home and silently defeat ``tests/conftest.py``'s ``patched_server``.
4. Tools raise ``tool_errors.ToolError`` on failure (DESIGN §9). Unchanged by the move.

Rules 2 and 3 are enforced by AST in ``tests/test_tool_sections_contract.py``;
``tests/test_tool_module_reload.py`` proves the property rule 2 exists to protect.

plan_SERVERSPLIT slice 0 (the mechanism slice) created this subpackage EMPTY;
slices 1-11 add one module each, smallest and most isolated first, and the
remaining bodies stay in ``server.py`` until their slice. The name is
``tool_sections`` and not ``tools``
on purpose: ``embedded/__init__.py`` puts ``embedded/`` on ``sys.path`` and the
repo root already has a ``tools/`` directory, so a bare ``import tools`` would be
ambiguous between two directories and namespace-package semantics can merge them
silently.
"""

from stealth_chrome_devtools_mcp.embedded.tool_sections import cookies_storage

#: THE one enumeration of the section modules. ``server.py``'s binding loop walks
#: this, and the source-scanning guards derive their file set from it (see
#: ``tests/source_scan.py``). Adding a section module means adding it here — and
#: the 94-count tripwire fires if you forget.
#:
#: Held in the canonical section order the migration finishes in. Note the
#: binding loop runs before the bodies still decorated in ``server.py``, so
#: mid-migration ``SECTION_TOOLS`` lists the MOVED sections first; nothing reads
#: that ordering (``release_evidence`` and the contract generator both sort, and
#: ``--list-sections`` only totals), and it settles once every section has moved.
SECTION_MODULES = (cookies_storage,)
