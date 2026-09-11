"""Static call-convention guards for embedded/server.py.

``ResponseHandler.handle_response`` is synchronous and returns a plain
dict/list. ``await``-ing its return value raises ``TypeError: object dict
can't be used in 'await' expression`` at runtime — which silently broke the
``extract_element_assets``, ``extract_related_files`` and
``discover_object_methods`` tools: the CDP work completed, then the return
line threw on every call.

A live-browser test can't cover all 96 tools cheaply, but the convention is
statically checkable: parse server.py and assert no ``await`` ever wraps a
``handle_response`` call. This fails on any reintroduction, regardless of
which tool it lands in.
"""

import ast

from source_scan import tool_source_files


def _tool_trees() -> dict[str, ast.AST]:
    """Every file that can hold a tool body, not just ``server.py``.

    plan_SERVERSPLIT §1.4: as bodies move into ``tool_sections/`` a guard hard-
    wired to ``server.py`` would not go red — it would pass over an emptier
    file. ``tests/source_scan.py`` derives the set and carries the floor
    assertion that makes a collapsed set red rather than green.
    """
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in tool_source_files()
    }


class TestHandleResponseCallConvention:
    def test_handle_response_is_never_awaited(self):
        offenders = [
            f"{name}:{node.value.lineno}"
            for name, tree in _tool_trees().items()
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "handle_response"
        ]
        assert offenders == [], (
            f"handle_response is synchronous — awaiting its dict return raises "
            f"TypeError and breaks the tool. Offending site(s): {offenders}"
        )

    def test_handle_response_is_actually_called(self):
        # Guard against this suite going vacuous if the helper gets renamed OR
        # if every call site moves to a file the scan does not cover: the
        # convention test only means something while call sites exist.
        calls = [
            node
            for tree in _tool_trees().values()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "handle_response"
        ]
        assert len(calls) >= 5, (
            "expected handle_response call sites across the tool-source set"
        )
