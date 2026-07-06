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
from pathlib import Path

import server


def _server_tree() -> ast.AST:
    return ast.parse(Path(server.__file__).read_text(encoding="utf-8"))


class TestHandleResponseCallConvention:
    def test_handle_response_is_never_awaited(self):
        offenders = [
            node.value.lineno
            for node in ast.walk(_server_tree())
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "handle_response"
        ]
        assert offenders == [], (
            f"handle_response is synchronous — awaiting its dict return raises "
            f"TypeError and breaks the tool. Offending server.py lines: {offenders}"
        )

    def test_handle_response_is_actually_called(self):
        # Guard against this suite going vacuous if the helper gets renamed:
        # the convention test only means something while call sites exist.
        calls = [
            node
            for node in ast.walk(_server_tree())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "handle_response"
        ]
        assert len(calls) >= 5, "expected handle_response call sites in server.py"
