"""Regression guard for M10a (plan_M3.md step 8): no `except Exception` in
embedded/ may swallow silently.

"Silently" is defined exactly per plan_M3 SS5.4's classifier: an
``ExceptHandler`` whose type is precisely ``Exception`` (not a narrower
tuple/name - those already carry intentional specificity and are out of
scope) is truly-silent when ALL three hold:
  1. no ``raise`` anywhere in its body,
  2. no call anywhere in its body whose dotted name contains one of the
     recognized logging markers (see LOG_MARKERS below),
  3. if the handler binds a name (``except Exception as e``), that name is
     never referenced in the body.

DELTA FROM SS5.4: the original classifier's marker list did not include
``log_debug`` - it didn't exist yet. M10a-0 added ``DebugLogger.log_debug``
(team-lead ruling, 2026-07-06) as a deliberately ring-less bridge for the
droppable-by-default DEBUG sites this same sweep fixes (plan_M3 SS7 risk 3).
Without this addition, every M10a-7 site fixed with ``log_debug(...)`` would
read as still-silent to this guard and it could never pass. ``log_debug`` is
therefore a first-class recognized marker here, on par with
log_error/log_warning/log_info.

ALLOWLIST starts EMPTY (plan_M3 SS3 step 8): it is the documented seam for
any future deliberate silence, not a place to grandfather today's findings.
All 17 sites identified by the SS5.4 classifier at PR-A's tip were fixed in
M10a-7a/7b/7c/7d before this guard was written; the guard is expected to
already be green (a re-run of the same classifier, not just the acceptance
check for it).
"""

import ast
import sys
from pathlib import Path

EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)
if str(EMBEDDED_DIR) not in sys.path:
    sys.path.insert(0, str(EMBEDDED_DIR))

LOG_MARKERS = (
    "log_error",
    "log_warning",
    "log_info",
    "log_debug",  # M10a-0 delta from plan_M3 SS5.4 - see module docstring.
    "logger",
    "logging",
    "print",
    "_emit",
    "traceback",
    "warn",
)

# Seam for future deliberate silences. Format: "relative/path.py:lineno".
# Empty by design (plan_M3 SS3 step 8) - do not pre-populate with today's
# findings; they were all fixed, not allowlisted.
ALLOWLIST: frozenset[str] = frozenset()


def _dotted_name(func: ast.expr) -> str:
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _calls_in(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            yield sub


def _has_log_call(handler_body: list[ast.stmt]) -> bool:
    for stmt in handler_body:
        for call in _calls_in(stmt):
            dotted = _dotted_name(call.func)
            if any(marker in dotted for marker in LOG_MARKERS):
                return True
    return False


def _has_raise(handler_body: list[ast.stmt]) -> bool:
    for stmt in handler_body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise):
                return True
    return False


def _bound_name_referenced(handler: ast.ExceptHandler) -> bool:
    if handler.name is None:
        return False
    for stmt in handler.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name) and sub.id == handler.name:
                return True
    return False


def _is_exact_exception_type(handler: ast.ExceptHandler) -> bool:
    return isinstance(handler.type, ast.Name) and handler.type.id == "Exception"


def _find_silent_excepts() -> list[str]:
    """Sweep every embedded/*.py file, return "relative/path.py:lineno" for
    each truly-silent `except Exception` handler not covered by ALLOWLIST."""
    offenders: list[str] = []
    for py_file in sorted(EMBEDDED_DIR.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                continue  # bare `except:` - a separate, unrelated concern.
            if not _is_exact_exception_type(node):
                continue  # narrower/tuple types already carry intentional specificity.

            silent = (
                not _has_raise(node.body)
                and not _has_log_call(node.body)
                and not _bound_name_referenced(node)
            )
            if silent:
                site = f"{py_file.name}:{node.lineno}"
                if site not in ALLOWLIST:
                    offenders.append(site)

    return offenders


class TestNoSilentExcepts:
    def test_no_truly_silent_except_exception_in_embedded(self):
        offenders = _find_silent_excepts()
        assert offenders == [], (
            "Truly-silent `except Exception` handlers found in embedded/ "
            "(no raise, no recognized log call, unused bound name): "
            f"{offenders}. Either add a log call (log_error/log_warning/"
            "log_info/log_debug) explaining why the exception is swallowed, "
            "or - if the silence is genuinely deliberate - add the exact "
            '"file.py:lineno" site to ALLOWLIST with a comment explaining why.'
        )

    def test_guard_is_not_vacuous(self):
        # Confirms the sweep actually visits real content, not an empty/
        # misconfigured directory - matches test_server_call_conventions.py's
        # companion-assertion convention.
        total_except_exception = 0
        for py_file in sorted(EMBEDDED_DIR.glob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and _is_exact_exception_type(
                    node
                ):
                    total_except_exception += 1
        assert total_except_exception >= 50, (
            "expected many `except Exception` handlers across embedded/ - "
            "got suspiciously few; is EMBEDDED_DIR resolving correctly?"
        )
