#!/usr/bin/env python3
"""Dump the served tool surface — names, descriptions and input schemas — so a
refactor that claims to change nothing can be held to it.

plan_SERVERSPLIT §5.2. The baseline lives at ``tests/goldens/tool_surface.json``
and is a HARD golden for the duration of that plan: a diff during any slice is a
real regression, never something to regenerate. It is what catches a docstring
lost in a copy-paste, an annotation whose meaning changed when a section module
gained ``from __future__ import annotations``, or a tool that silently failed to
register.

Usage:
    python tools/dump_tool_surface.py            # to stdout
    python tools/dump_tool_surface.py --write    # (re)write the golden
    python tools/dump_tool_surface.py --check    # exit 1 on any drift
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

GOLDEN = (
    Path(__file__).resolve().parent.parent / "tests" / "goldens" / "tool_surface.json"
)


async def _surface() -> dict[str, object]:
    from stealth_chrome_devtools_mcp.embedded import server

    tools = await server.mcp.get_tools()
    # ``parameters`` is the input JSON schema FastMCP derives from the signature,
    # which is exactly what a docstring or an annotation lost in a copy-paste
    # would change. ``enabled`` catches a section gate that closed too early.
    return {
        name: {
            "description": tool.description,
            "input_schema": tool.parameters,
            "output_schema": tool.output_schema,
            "tags": sorted(tool.tags or ()),
            "enabled": tool.enabled,
        }
        for name, tool in sorted(tools.items())
    }


def _render(surface: dict[str, object]) -> str:
    return json.dumps(surface, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = _render(asyncio.run(_surface()))
    if "--write" in argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
        print(f"wrote {GOLDEN}")
        return 0
    if "--check" in argv:
        if not GOLDEN.exists():
            print(f"missing golden: {GOLDEN}")
            return 1
        if GOLDEN.read_text(encoding="utf-8") == text:
            print("tool surface IDENTICAL to the golden")
            return 0
        print("tool surface DRIFT against tests/goldens/tool_surface.json")
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
