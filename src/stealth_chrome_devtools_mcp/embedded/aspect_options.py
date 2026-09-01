"""THE one home for "a caller passed an option this aspect no longer has" (F-851).

A clean break of the extraction OUTPUT schema was the owner's ruling (Q1) and
stands. A stale INPUT kwarg is a different thing: binding
``extraction_options={"animations": {"analyze_keyframes": True}}`` against the
new signature raised ``TypeError`` at the call site — synchronously, before any
coroutine existed, so ``asyncio.gather``'s per-aspect isolation never saw it —
and the composed clone's outer handler turned one retired string into a single
error payload for the WHOLE clone. Structure, styles, events, assets and
related_files were all lost to an error that pointed nowhere near the cause.

The repo's stated "0 external users" premise is false; there are third-party
installs on PyPI. Someone else's outdated call must degrade, not detonate.

This is a NAMED tolerance, not a widened ``except``. It drops exactly the keys
the aspect's own signature does not accept, reports each one in that aspect's
``warnings``, and lets every real error through untouched — swallowing genuine
failures here would trade a loud defect for a quiet one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

import inspect

Options = dict[str, object]
Report = dict[str, object]

# Options that really did ship and really are gone, with what replaced them.
# A weak model (or a human) reading "ignored" wants to know whether to pass
# something else instead; for all four the answer is that the behaviour became
# unconditional in schema v2. Kept honest by a test that asserts none of these
# is still a parameter of the aspect it names.
RETIRED: dict[str, dict[str, str]] = {
    "animations": {
        "include_css_animations": (
            "schema v2 always reports declared CSS animations; there is nothing "
            "to turn on"
        ),
        "include_transitions": "schema v2 always reports transitions",
        "include_transforms": "schema v2 always reports the transforms block",
        "analyze_keyframes": (
            "schema v2 always resolves keyframes; use include_subtree/"
            "include_waapi to bound what is collected"
        ),
    },
}


def _report(aspect: str, option: str) -> Report:
    """One warning naming the ignored option — in the shape the aspects already
    use for warnings, so a reader needs no second convention."""
    reason = RETIRED.get(aspect, {}).get(option)
    if reason is None:
        message = (
            f"'{option}' is not an option of the {aspect} aspect and was "
            f"ignored; the extraction completed without it"
        )
    else:
        message = (
            f"'{option}' was retired and is ignored: {reason}. The extraction "
            f"completed without it"
        )
    return {
        "code": "retired_option_ignored",
        "message": message,
        "detail": {"aspect": aspect, "option": option},
    }


def accepted(
    method: Callable[..., object], options: Options, aspect: str
) -> tuple[Options, list[Report]]:
    """Split ``options`` into what ``method`` accepts and reports for the rest.

    The aspect's own signature is the authority — no second list to keep in
    sync, so an option removed tomorrow degrades the same way with no edit here.
    """
    try:
        allowed = set(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        # An un-introspectable callable is not a reason to drop the caller's
        # options: hand them back and let the real binding error speak.
        return dict(options), []
    kept: Options = {}
    reports: list[Report] = []
    for key, value in options.items():
        if key in allowed:
            kept[key] = value
        else:
            reports.append(_report(aspect, key))
    return kept, reports


def note(result: Options, reports: list[Report]) -> None:
    """Append ``reports`` to an aspect result's ``warnings``, in place.

    Silently ignoring a retired option would be the same defect one layer down,
    so the aspect grows a ``warnings`` list if it did not have one.
    """
    if not reports:
        return
    existing = result.get("warnings")
    warnings: list[object] = [*existing] if isinstance(existing, list) else []
    warnings.extend(reports)
    result["warnings"] = warnings
