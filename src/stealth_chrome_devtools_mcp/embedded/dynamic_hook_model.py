"""
The dynamic hook data model: ``RequestInfo``, ``HookAction`` and ``DynamicHook``.

Split from ``dynamic_hook_system`` (T-9135) so that module, which owns interception
and the paused-request handlers, stays under its file budget.

**This is host Python execution, not a sandbox (T-6206, F-L).** A hook's
``function_code`` is ``exec``'d and a ``custom_condition`` is ``eval``'d in the
backend process, with that process's privileges. The trimmed ``__builtins__``
dicts below only keep the usual hook vocabulary at hand; they are NOT a security
boundary and must not be read as one — the injected ``fnmatch`` module alone
reaches ``os`` (``fnmatch.os``), and ``str.__class__.__base__.__subclasses__()``
works in the eval namespace. Whoever can create a hook can run arbitrary Python
on the host. Do not build a sandbox here by adding names to or removing names
from those dicts; a real boundary would be a separate process.
"""

import fnmatch
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import CallerError

ERROR_TEXT_LIMIT = 500


def describe_error(error: BaseException) -> str:
    """``Type: message`` of an exception out of the CALLER's hook code, at most
    :data:`ERROR_TEXT_LIMIT` characters: the one text for the hook's ``last_error``
    and for the WARNING about it (the traceback is attached to the log separately)."""
    return f"{type(error).__name__}: {error}"[:ERROR_TEXT_LIMIT]


@dataclass
class RequestInfo:
    """Request information passed to hook functions."""

    request_id: str
    instance_id: str
    url: str
    method: str
    headers: dict[str, str]
    post_data: str | None = None
    resource_type: str | None = None
    stage: str = "request"  # "request" or "response"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for AI function processing."""
        return asdict(self)


@dataclass
class HookAction:
    """Action returned by hook functions."""

    action: str  # "continue", "block", "redirect", "fulfill", "modify"
    url: str | None = None  # For redirect/modify
    method: str | None = None  # For modify
    headers: dict[str, str] | None = None  # For modify/fulfill
    body: str | None = None  # For fulfill
    status_code: int | None = None  # For fulfill
    post_data: str | None = None  # For modify


class DynamicHook:
    """A dynamic hook with AI-generated function."""

    def __init__(
        self,
        hook_id: str,
        name: str,
        requirements: dict[str, Any],
        function_code: str,
        priority: int = 100,
    ):
        self.hook_id = hook_id
        self.name = name
        self.requirements = requirements
        self.function_code = function_code
        self.priority = priority  # Lower number = higher priority
        self.created_at = datetime.now(tz=timezone.utc)
        self.trigger_count = 0
        self.last_triggered: datetime | None = None
        # The caller's own code failing (T-9135): theirs to read back, not our defect.
        self.error_count = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.status = "active"
        self.request_stage = requirements.get(
            "stage", "request"
        )  # 'request' or 'response'

        self._compiled_function = self._compile_function()

    def record_error(self, error: BaseException) -> None:
        """Remember that the CALLER's code (the function, or its ``custom_condition``)
        raised, so ``list_hooks`` / ``get_hook_details`` can show it to them."""
        self.error_count += 1
        self.last_error = describe_error(error)
        self.last_error_at = datetime.now(tz=timezone.utc)

    def error_record(self) -> dict[str, Any]:
        """What the caller reads back about their code failing (T-9135)."""
        return {
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat()
            if self.last_error_at
            else None,
        }

    def _compile_function(self) -> Callable:
        """Compile the AI-generated function."""
        try:
            # Not a sandbox: see the module docstring.
            namespace = {
                "HookAction": HookAction,
                "datetime": datetime,
                "fnmatch": fnmatch,
                "__builtins__": {
                    "len": len,
                    "str": str,
                    "int": int,
                    "float": float,
                    "bool": bool,
                    "dict": dict,
                    "list": list,
                    "tuple": tuple,
                    "print": lambda *args: debug_logger.log_info(
                        "hook_function", self.name, " ".join(map(str, args))
                    ),
                },
            }

            exec(self.function_code, namespace)  # noqa: S102  PERMANENT(exec hook code by design)

            if "process_request" not in namespace:
                raise ValueError("Function must define 'process_request(request)'")  # noqa: TRY301  plan_M4ph1

            return namespace["process_request"]

        except Exception as e:
            # The caller's code does not load: refuse the hook (T-9135) instead of
            # keeping one that silently continues every request.
            raise CallerError(
                f"Hook '{self.name}' was not created: its function code does not "
                f"load ({describe_error(e)}). Fix the code and create it again."
            ) from e

    def matches(self, request: RequestInfo) -> bool:  # noqa: PLR0911  DEBT(F-165)
        """Check if this hook matches the request."""
        try:
            # Check URL pattern
            if "url_pattern" in self.requirements and not fnmatch.fnmatch(
                request.url, self.requirements["url_pattern"]
            ):
                return False

            # Check method
            if (
                "method" in self.requirements
                and request.method.upper() != self.requirements["method"].upper()
            ):
                return False

            # Check resource type
            if (
                "resource_type" in self.requirements
                and str(request.resource_type).lower()
                != str(self.requirements["resource_type"]).lower()
            ):
                return False

            # Check stage
            if (
                "stage" in self.requirements
                and request.stage != self.requirements["stage"]
            ):
                return False

            # Check custom conditions (if any)
            if "custom_condition" in self.requirements:
                condition_code = self.requirements["custom_condition"]
                # Host ``eval``, not a sandbox: see the module docstring.
                namespace = {
                    "request": request,
                    "__builtins__": {"len": len, "str": str},
                }
                try:
                    result = eval(condition_code, namespace)  # noqa: S307  PERMANENT(eval hook matching by design)
                    if not result:
                        return False
                except Exception as cond_err:
                    self.record_error(cond_err)
                    debug_logger.log_warning(
                        "dynamic_hook",
                        "matches",
                        f"Custom condition eval failed for hook {self.name}: "
                        f"{describe_error(cond_err)}",
                        error=cond_err,
                    )
                    return False

            return True

        except Exception as e:
            debug_logger.log_error(
                "dynamic_hook", "matches", f"Error matching hook {self.name}: {e}"
            )
            return False
