"""THE one home for "which files can hold a tool body".

plan_SERVERSPLIT §1.4 / §4.1 step 6. Four guards scan ``embedded/server.py``'s
SOURCE TEXT rather than its behaviour:

    * ``tests/test_cdp_timeout.py``  — F-164 CDP-timeout discipline
    * ``tests/test_server_call_conventions.py`` — F-202 ``handle_response`` is never awaited
    * ``tests/test_log_hygiene.py``  — the composed uvicorn run-config
    * ``tests/test_observability.py`` — the export-timeout message pin

A source-text guard does not FAIL when the code it is looking for leaves the file
it scans; it passes, over an emptier file. That is the nastiest failure mode in
the whole server-split migration, because after the fact you cannot tell a vacuous
pass from a real one. So every such guard derives its file set from HERE, and this
module carries a floor assertion: if the set ever collapses, the guards go red
instead of green.

The floor is deliberately a separate check from "the set is what SECTION_MODULES
says": the set could be correct and still be too small to mean anything, which is
exactly the state slice 0 must not silently drift into.
"""

from __future__ import annotations

from pathlib import Path

#: The smallest tool-source set that still means something: ``server.py`` +
#: ``tool_runtime.py`` + one entry per section module that has landed so far.
#: It RATCHETS UP one per slice, the mirror of the LOC cap's ratchet down —
#: waiting until slice 11 to raise it in one jump would leave ten slices during
#: which a dropped section module reads as a legitimately smaller set. Slice 1
#: (``cookies_storage``) took it to 3, slice 2 (``tabs``) to 4, slice 3
#: (``debugging``) to 5 and slice 4 (``dynamic_hooks``) to 6; slice 11 ends at 13.
MIN_TOOL_SOURCE_FILES = 6


def collect_tool_source_files(server_mod, runtime_mod, section_modules, floor):
    """The mechanism, with every input handed in so the floor can be TESTED.

    ``tool_source_files`` below is the production caller; the tests that prove
    this assertion is live rather than decorative drive this seam directly with a
    collapsed set (see ``tests/test_tool_sections_contract.py``).
    """
    files = [Path(server_mod.__file__), Path(runtime_mod.__file__)]
    files += [Path(m.__file__) for m in section_modules]
    if len(files) < floor:
        raise AssertionError(
            f"the tool-source set collapsed to {len(files)} file(s), below the "
            f"floor of {floor} — the source-scanning guards that derive their "
            "file set from here would pass VACUOUSLY over files the tool bodies "
            "have already left (plan_SERVERSPLIT §1.4 / R3)"
        )
    return files


def tool_source_files() -> list[Path]:
    """Every file that can hold a tool body: ``server.py``, ``tool_runtime.py``
    and the section modules.

    DERIVED from ``SECTION_MODULES``, so a new section module cannot escape the
    source-level guards by being forgotten here.
    """
    from stealth_chrome_devtools_mcp.embedded import server, tool_runtime
    from stealth_chrome_devtools_mcp.embedded.tool_sections import SECTION_MODULES

    return collect_tool_source_files(
        server, tool_runtime, SECTION_MODULES, MIN_TOOL_SOURCE_FILES
    )


def tool_source_text() -> str:
    """The tool-source set as one blob, for the guards that match on a string
    rather than on an AST (``tests/test_observability.py``'s message pin)."""
    return "\n".join(p.read_text(encoding="utf-8") for p in tool_source_files())
