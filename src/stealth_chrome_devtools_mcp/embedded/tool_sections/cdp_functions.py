"""The ``cdp-functions`` tools. See ``tool_sections/__init__.py`` for the
contract.

plan_SERVERSPLIT slice 9, and the one section a runtime GATE can switch off.
``server.py`` disables ``cdp-functions`` in two places — at module scope when
``settings.xpool_safe_mode`` is on, and in the ``__main__`` block for
``--xpool-safe`` / ``--disable-cdp-functions`` — because these thirteen tools
are the ones that trigger ``Runtime.enable``. Both paths run ``mcp.remove_tool``
through ``apply_disabled_sections``, i.e. they act on tools that are ALREADY
registered, which is why the plan places the binding loop well before the
module-scope gate (plan_SERVERSPLIT R6). A loop that ran after it would register
these thirteen into a gate that had already closed, and the served surface would
silently carry them again. The slice's own verification is therefore not a unit
test but a real backend subprocess: control serves 94, ``--xpool-safe`` serves
81, ``--disable-cdp-functions`` serves 81 — exactly thirteen removed by each.

Two bodies return through ``response_handler.handle_response``, which is
SYNCHRONOUS (F-202); ``discover_object_methods`` carries the comment that says
so, and ``tests/test_server_call_conventions.py`` re-derives its AST scan from
``tests/source_scan.py``, so the guard follows the bodies here.
``execute_function_sequence`` keeps its function-local ``FunctionCall`` import
verbatim, and ``create_python_binding`` keeps the ``exec`` that is the whole
point of a python binding.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2 — registration is driven from ``server.py``'s binding loop, once
per execution of that module body) and the rewrite of the singleton/helper reads
to ``rt.<name>``, resolved at CALL time against the one patchable home (contract
rule 3). Docstrings and signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration.
"""

from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError, _require_tab

SECTION = "cdp-functions"


async def list_cdp_commands() -> list[str]:
    """
    List all available CDP Runtime commands for function execution.

    Returns:
        List[str]: List of available CDP command names.
    """
    return await rt.cdp_function_executor.list_cdp_commands()


async def execute_cdp_command(
    instance_id: str, command: str, params: dict[str, Any] = None
) -> dict[str, Any]:
    """
    Execute a raw CDP command by name — the low-level escape hatch beneath the
    exec-family tools. Prefer `execute_script` for ordinary page JS; use this
    only for one specific CDP method (e.g. 'evaluate', 'Page.reload').

    Args:
        instance_id (str): Browser instance ID.
        command (str): CDP command, either domain-qualified ('Page.reload',
                'Emulation.setDeviceMetricsOverride') or a bare Runtime method
                ('evaluate'). Wire casing and snake_case both resolve.
        params (Dict[str, Any], optional): Command parameters. Wire casing and
                snake_case both resolve, per parameter ('returnByValue' and
                'return_by_value' are the same argument), so the CDP docs' own
                spelling works.

    Returns:
        Dict[str, Any]: Command execution result.

    Example:
        # Both the command NAME and its PARAM names are casing-flexible.
        params = {"expression": "document.title", "return_by_value": True}
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.execute_cdp_command(tab, command, params or {}),
        instance_id=instance_id,
    )


async def get_execution_contexts(instance_id: str) -> list[dict[str, Any]]:
    """
    Get all available JavaScript execution contexts.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        List[Dict[str, Any]]: List of execution contexts with their details.
    """
    tab = await rt.browser_manager.get_tab(instance_id)
    if not tab:
        return []
    contexts = await rt._with_cdp_timeout(
        rt.cdp_function_executor.get_execution_contexts(tab), instance_id=instance_id
    )
    return [
        {
            "id": ctx.id,
            "name": ctx.name,
            "origin": ctx.origin,
            "unique_id": ctx.unique_id,
            "aux_data": ctx.aux_data,
        }
        for ctx in contexts
    ]


async def discover_global_functions(
    instance_id: str, context_id: str = None
) -> list[dict[str, Any]]:
    """
    Discover all global JavaScript functions available in the page.

    Args:
        instance_id (str): Browser instance ID.
        context_id (str, optional): Optional execution context ID.

    Returns:
        List[Dict[str, Any]]: List of discovered functions with their details.
    """
    tab = await rt.browser_manager.get_tab(instance_id)
    if not tab:
        return []
    functions = await rt._with_cdp_timeout(
        rt.cdp_function_executor.discover_global_functions(tab, context_id),
        instance_id=instance_id,
    )
    result = [
        {
            "name": func.name,
            "path": func.path,
            "signature": func.signature,
            "description": func.description,
        }
        for func in functions
    ]

    file_response = rt.response_handler.handle_response(
        result,
        fallback_filename_prefix="global_functions",
        metadata={
            "context_id": context_id,
            "function_count": len(result),
            "url": getattr(tab, "url", "unknown"),
        },
    )

    if isinstance(file_response, dict) and "file_path" in file_response:
        return [
            {
                "name": "LARGE_RESPONSE_SAVED_TO_FILE",
                "path": "file_storage",
                "signature": "automatic_file_fallback",
                "description": f"Response too large ({file_response['estimated_tokens']} tokens), saved to: {file_response['filename']}",
            }
        ]

    return file_response


async def discover_object_methods(
    instance_id: str, object_path: str
) -> list[dict[str, Any]]:
    """
    Discover methods of a specific JavaScript object.

    Args:
        instance_id (str): Browser instance ID.
        object_path (str): Path to the object (e.g., 'document', 'window.localStorage').

    Returns:
        List[Dict[str, Any]]: List of discovered methods.
    """
    tab = await rt.browser_manager.get_tab(instance_id)
    if not tab:
        return []
    methods = await rt._with_cdp_timeout(
        rt.cdp_function_executor.discover_object_methods(tab, object_path),
        instance_id=instance_id,
    )
    methods_data = [
        {
            "name": method.name,
            "path": method.path,
            "signature": method.signature,
            "description": method.description,
        }
        for method in methods
    ]

    # handle_response is synchronous — awaiting its dict return raises TypeError.
    return rt.response_handler.handle_response(
        methods_data, f"object_methods_{object_path.replace('.', '_')}"
    )


async def call_javascript_function(
    instance_id: str, function_path: str, args: list[Any] = None
) -> dict[str, Any]:
    """
    Invoke an already-defined JavaScript function by its dotted path — does not
    run arbitrary source.

    Use `execute_script` / `inject_and_execute_script` to run source; use this
    when the function already exists on the page (e.g. 'document.querySelector');
    use `execute_function_sequence` to chain several such calls in one round trip.

    Args:
        instance_id (str): Browser instance ID.
        function_path (str): Full path to the function (e.g., 'document.getElementById').
        args (List[Any], optional): List of arguments to pass to the function.

    Returns:
        Dict[str, Any]: Function call result.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.call_discovered_function(
            tab, function_path, args or []
        ),
        instance_id=instance_id,
    )


async def inspect_function_signature(
    instance_id: str, function_path: str
) -> dict[str, Any]:
    """
    Inspect a JavaScript function's signature and details.

    Args:
        instance_id (str): Browser instance ID.
        function_path (str): Full path to the function.

    Returns:
        Dict[str, Any]: Function signature and details.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.inspect_function_signature(tab, function_path),
        instance_id=instance_id,
    )


async def inject_and_execute_script(
    instance_id: str, script_code: str, context_id: str = None
) -> dict[str, Any]:
    """
    Run custom JavaScript source, optionally inside a specific execution context
    (isolated world / iframe) via `context_id`.

    Like `execute_script`, but lets you choose the execution context; prefer
    plain `execute_script` when the default page context is what you want.

    Args:
        instance_id (str): Browser instance ID.
        script_code (str): JavaScript code to execute.
        context_id (str, optional): Optional execution context ID.

    Returns:
        Dict[str, Any]: Script execution result.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.inject_and_execute_script(
            tab, script_code, context_id
        ),
        instance_id=instance_id,
    )


async def create_persistent_function(
    instance_id: str, function_name: str, function_code: str
) -> dict[str, Any]:
    """
    Create a persistent JavaScript function that survives page reloads.

    Args:
        instance_id (str): Browser instance ID.
        function_name (str): Name for the function.
        function_code (str): JavaScript function code.

    Returns:
        Dict[str, Any]: Function creation result.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.create_persistent_function(
            tab, function_name, function_code, instance_id
        ),
        instance_id=instance_id,
    )


async def execute_function_sequence(
    instance_id: str, function_calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Invoke several already-defined JavaScript functions in order, in a single
    round trip.

    Each entry is one `call_javascript_function`-style call; use this to batch
    them instead of issuing repeated `call_javascript_function` calls.

    Args:
        instance_id (str): Browser instance ID.
        function_calls (List[Dict[str, Any]]): List of function calls, each with 'function_path', 'args', and optional 'context_id'.

    Returns:
        List[Dict[str, Any]]: List of function call results.
    """
    from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import FunctionCall

    tab = await _require_tab(rt.browser_manager, instance_id)
    calls = []
    for call_data in function_calls:
        calls.append(
            FunctionCall(
                function_path=call_data["function_path"],
                args=call_data.get("args", []),
                context_id=call_data.get("context_id"),
            )
        )
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.execute_function_sequence(tab, calls),
        instance_id=instance_id,
    )


async def create_python_binding(
    instance_id: str, binding_name: str, python_code: str
) -> dict[str, Any]:
    """
    Create a binding that allows JavaScript to call Python functions.

    Args:
        instance_id (str): Browser instance ID.
        binding_name (str): Name for the binding.
        python_code (str): Python function code (as string).

    Returns:
        Dict[str, Any]: Binding creation result.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    try:
        exec_globals = {}
        exec(python_code, exec_globals)
        python_function = None
        for name, obj in exec_globals.items():
            if callable(obj) and not name.startswith("_"):
                python_function = obj
                break
        if not python_function:
            raise ToolError("No function found in Python code")
        return await rt._with_cdp_timeout(
            rt.cdp_function_executor.create_python_binding(
                tab, binding_name, python_function
            ),
            instance_id=instance_id,
        )
    except Exception as e:
        raise ToolError(f"Failed to create Python function: {e!s}")


async def execute_python_in_browser(
    instance_id: str, python_code: str
) -> dict[str, Any]:
    """
    Author browser logic in Python — it is transpiled to JavaScript and then
    executed in the page.

    Use `execute_script` to write JavaScript directly; use this when you would
    rather write Python.

    Args:
        instance_id (str): Browser instance ID.
        python_code (str): Python code to translate and execute.

    Returns:
        Dict[str, Any]: Execution result.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.execute_python_in_browser(tab, python_code),
        instance_id=instance_id,
    )


async def get_function_executor_info(instance_id: str = None) -> dict[str, Any]:
    """
    Get information about the CDP function executor state.

    Args:
        instance_id (str, optional): Optional browser instance ID for specific info.

    Returns:
        Dict[str, Any]: Function executor state and capabilities.
    """
    return await rt._with_cdp_timeout(
        rt.cdp_function_executor.get_function_executor_info(instance_id),
        instance_id=instance_id,
    )


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["cdp-functions"]``
#: — the thirteen the xpool-safe gate removes as a unit.
TOOLS = (
    list_cdp_commands,
    execute_cdp_command,
    get_execution_contexts,
    discover_global_functions,
    discover_object_methods,
    call_javascript_function,
    inspect_function_signature,
    inject_and_execute_script,
    create_persistent_function,
    execute_function_sequence,
    create_python_binding,
    execute_python_in_browser,
    get_function_executor_info,
)
