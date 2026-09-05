"""The ``dynamic-hooks`` tools. See ``tool_sections/__init__.py`` for the contract.

plan_SERVERSPLIT slice 4, and the only section that carries **synchronous** tool
bodies — the last five below are plain ``def``, not ``async def``. That is the
point of the slice: ``tool_registry``'s ``_surrogate_safe_returns`` branches on
``inspect.iscoroutinefunction`` and wraps a sync tool in a sync wrapper, and the
binding loop applies that wrapper from ``server.py`` to a function whose
``__globals__`` is now THIS module. ``tests/test_tool_dispatch.py`` pins that the
registered ``get_hook_documentation`` is still not a coroutine function after the
move, and the 94-minus-5 async split stays exactly where it was.

Zero shared dependencies beyond ``rt.dynamic_hook_ai``: no tab, no browser
manager, no CDP timeout. That is why this section goes before the two heavier
ones — the sync branch is proven in isolation.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2 — registration is driven from ``server.py``'s binding loop, once
per execution of that module body) and the rewrite of the singleton read to
``rt.<name>``, resolved at CALL time against the one patchable home (contract
rule 3). Docstrings and signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration.
"""

from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt

SECTION = "dynamic-hooks"


async def create_dynamic_hook(
    name: str,
    requirements: dict[str, Any],
    function_code: str,
    instance_ids: list[str] | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    """
    Create a new dynamic hook with AI-generated Python function.

    This is the new powerful hook system that allows AI to write custom Python functions
    that process network requests in real-time with no pending state.

    Args:
        name (str): Human-readable hook name
        requirements (Dict[str, Any]): Matching criteria (url_pattern, method, resource_type, custom_condition)
        function_code (str): Python function code that processes requests (must define process_request(request))
        instance_ids (Optional[List[str]]): Browser instances to apply hook to (all if None)
        priority (int): Hook priority (lower = higher priority)

    Returns:
        Dict[str, Any]: Hook creation result with hook_id

    Example function_code:
        ```python
        def process_request(request):
            if "example.com" in request["url"]:
                return HookAction(action="redirect", url="https://httpbin.org/get")
            return HookAction(action="continue")
        ```
    """
    return await rt.dynamic_hook_ai.create_dynamic_hook(
        name=name,
        requirements=requirements,
        function_code=function_code,
        instance_ids=instance_ids,
        priority=priority,
    )


async def create_simple_dynamic_hook(
    name: str,
    url_pattern: str,
    action: str,
    target_url: str | None = None,
    custom_headers: dict[str, str] | None = None,
    instance_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a simple dynamic hook using predefined templates (easier for AI).

    Args:
        name (str): Hook name
        url_pattern (str): URL pattern to match
        action (str): Action type - 'block', 'redirect', 'add_headers', or 'log'
        target_url (Optional[str]): Target URL for redirect action
        custom_headers (Optional[Dict[str, str]]): Headers to add for add_headers action
        instance_ids (Optional[List[str]]): Browser instances to apply hook to

    Returns:
        Dict[str, Any]: Hook creation result
    """
    return await rt.dynamic_hook_ai.create_simple_hook(
        name=name,
        url_pattern=url_pattern,
        action=action,
        target_url=target_url,
        custom_headers=custom_headers,
        instance_ids=instance_ids,
    )


async def list_dynamic_hooks(instance_id: str | None = None) -> dict[str, Any]:
    """
    List all dynamic hooks.

    Args:
        instance_id (Optional[str]): Optional filter by browser instance

    Returns:
        Dict[str, Any]: List of hooks with details and statistics
    """
    return await rt.dynamic_hook_ai.list_dynamic_hooks(instance_id=instance_id)


async def get_dynamic_hook_details(hook_id: str) -> dict[str, Any]:
    """
    Get detailed information about a specific dynamic hook.

    Args:
        hook_id (str): Hook identifier

    Returns:
        Dict[str, Any]: Detailed hook information including function code
    """
    return await rt.dynamic_hook_ai.get_hook_details(hook_id=hook_id)


async def remove_dynamic_hook(hook_id: str) -> dict[str, Any]:
    """
    Remove a dynamic hook.

    Args:
        hook_id (str): Hook identifier to remove

    Returns:
        Dict[str, Any]: Removal status
    """
    return await rt.dynamic_hook_ai.remove_dynamic_hook(hook_id=hook_id)


def get_hook_documentation() -> dict[str, Any]:
    """
    Get comprehensive documentation for creating hook functions (AI learning).

    Returns:
        Dict[str, Any]: Documentation of request object structure and HookAction types
    """
    return rt.dynamic_hook_ai.get_request_documentation()


def get_hook_examples() -> dict[str, Any]:
    """
    Get example hook functions for AI learning.

    Returns:
        Dict[str, Any]: Collection of example hook functions with explanations
    """
    return rt.dynamic_hook_ai.get_hook_examples()


def get_hook_requirements_documentation() -> dict[str, Any]:
    """
    Get documentation on hook requirements and matching criteria.

    Returns:
        Dict[str, Any]: Requirements documentation and best practices
    """
    return rt.dynamic_hook_ai.get_requirements_documentation()


def get_hook_common_patterns() -> dict[str, Any]:
    """
    Get common hook patterns and use cases.

    Returns:
        Dict[str, Any]: Common patterns like ad blocking, API proxying, etc.
    """
    return rt.dynamic_hook_ai.get_common_patterns()


def validate_hook_function(function_code: str) -> dict[str, Any]:
    """
    Validate hook function code for common issues before creating.

    Args:
        function_code (str): Python function code to validate

    Returns:
        Dict[str, Any]: Validation results with issues and warnings
    """
    return rt.dynamic_hook_ai.validate_hook_function(function_code=function_code)


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["dynamic-hooks"]``.
#: The five synchronous bodies come last, exactly as they did in ``server.py``.
TOOLS = (
    create_dynamic_hook,
    create_simple_dynamic_hook,
    list_dynamic_hooks,
    get_dynamic_hook_details,
    remove_dynamic_hook,
    get_hook_documentation,
    get_hook_examples,
    get_hook_requirements_documentation,
    get_hook_common_patterns,
    validate_hook_function,
)
