"""The ``network-debugging`` tools. See ``tool_sections/__init__.py`` for
the contract.

plan_SERVERSPLIT slice 6. Two things distinguish it from the four sections
before it.

First, it is the only section that owns a module CONSTANT rather than just
bodies: ``_CAPTURE_OFF_NOTE``, the sentence three body-consuming tools attach
when a body is absent because capture is off. In ``server.py`` it sat wedged
between ``get_request_details`` and ``get_response_details`` — the plan's
baseline table names it as a constant embedded mid-section — and it lands at the
top of its own module here. That is a relocation inside the move, not a rewrite:
the string is byte-identical, and it is what makes this section's three
``capture_note`` tools read from one place.

Second, this is the section with the heaviest raw-``.fn`` test coupling:
``tests/test_server_network_tools.py`` reaches fifteen call sites as
``server.<tool>.fn(...)``. Every one of them is a module-attribute read that
``server.py``'s binding loop still satisfies, so none needed re-pointing — the
census was re-run for this slice rather than assumed from slices 1-5.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2 — registration is driven from ``server.py``'s binding loop, once
per execution of that module body) and the rewrite of the singleton/helper reads
to ``rt.<name>``, resolved at CALL time against the one patchable home (contract
rule 3). Docstrings and signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration.
"""

from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import _require_tab
from stealth_chrome_devtools_mcp.settings import get_settings

SECTION = "network-debugging"

# Surfaced by the body-consuming tools when a body is absent because capture is
# off, so an empty body doesn't read as a broken tool (F-605, off-by-default).
_CAPTURE_OFF_NOTE = (
    "response-body capture is off; enable via "
    "set_network_capture_filters(capture_bodies=True) or "
    "STEALTH_MCP_NETWORK_CAPTURE_BODIES=1"
)


async def list_network_requests(
    instance_id: str, filter_type: str | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    List captured network requests.

    Browser-internal traffic (chrome://, chrome-extension://, devtools://, about:)
    is excluded from capture by default — see set_network_capture_filters.

    Args:
        instance_id (str): Browser instance ID.
        filter_type (Optional[str]): Filter by CDP resource type, matched
            case-insensitively (e.g., 'document', 'image', 'script', 'xhr').

    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any]]: List of network requests, or file metadata if response too large.
    """
    requests = await rt.network_interceptor.list_requests(instance_id, filter_type)
    formatted_requests = [
        {
            "request_id": req.request_id,
            "url": req.url,
            "method": req.method,
            "resource_type": req.resource_type,
            "timestamp": req.timestamp.isoformat(),
        }
        for req in requests
    ]

    return rt.response_handler.handle_response(formatted_requests, "network_requests")


async def get_request_details(request_id: str) -> dict[str, Any] | None:
    """
    Get detailed information about a network request.

    Args:
        request_id (str): Network request ID.

    Returns:
        Optional[Dict[str, Any]]: Request details including headers, cookies, and body.
    """
    request = await rt.network_interceptor.get_request(request_id)
    if request:
        return request.dict()
    return None


async def get_response_details(request_id: str) -> dict[str, Any] | None:
    """
    Get response details for a network request.

    Response bodies are only stored when capture is enabled (off by default);
    status/headers/content-type metadata is always captured. When the body is
    absent because capture is off, a ``capture_note`` explains how to enable it.

    Args:
        request_id (str): Network request ID.

    Returns:
        Optional[Dict[str, Any]]: Response details including status, headers, and metadata.
    """
    response = await rt.network_interceptor.get_response(request_id)
    if response:
        result = response.dict()
        if response.body is None and not get_settings().network_capture_bodies:
            result["capture_note"] = _CAPTURE_OFF_NOTE
        return result
    return None


async def get_response_content(instance_id: str, request_id: str) -> str | None:
    """
    Get response body content.

    Live-refetches the body from CDP on demand, so it works even when
    response-body capture is off (the default) and independent of the stored-body
    cap. CDP evicts bodies from its own buffer quickly, so fetch soon after load.

    Args:
        instance_id (str): Browser instance ID.
        request_id (str): Network request ID.

    Returns:
        Optional[str]: Response body as text (base64 encoded for binary).
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    body = await rt._with_cdp_timeout(
        rt.network_interceptor.get_response_body(tab, request_id),
        instance_id=instance_id,
    )
    if body:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            return base64.b64encode(body).decode("utf-8")
    return None


async def search_network_requests(
    instance_id: str,
    url_pattern: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    response_contains: str | None = None,
    payload_contains: str | None = None,
    resource_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Search network requests; ``url_pattern`` filters the URL (substring, not glob).

    Args:
        instance_id (str): Browser instance ID.
        url_pattern (Optional[str]): URL filter — case-insensitive substring.
        method (Optional[str]): HTTP method, matched whole, case-insensitively.
        status_code (Optional[int]): Exact response status code.
        response_contains (Optional[str]): Case-insensitive substring of the body.
        payload_contains (Optional[str]): Case-insensitive substring of the payload.
        resource_type (Optional[str]): Case-insensitive substring of the CDP type.
        limit (int): Max results per page.
        offset (int): Starting index for pagination.

    Returns:
        Dict[str, Any]: Paginated results with metadata. Includes a ``capture_note``
        when body capture is off (response_contains matching is unavailable then).
    """
    result = await rt.network_interceptor.search_requests(
        instance_id,
        url_pattern,
        method,
        status_code,
        response_contains,
        payload_contains,
        resource_type,
        limit,
        offset,
    )
    filters = await rt.network_interceptor.get_capture_filters(instance_id)
    if not filters.get("capture_bodies"):
        result["capture_note"] = _CAPTURE_OFF_NOTE
    return result


async def export_network_data(instance_id: str, filepath: str) -> dict[str, Any]:
    """
    Export network data to JSON file.

    Args:
        instance_id (str): Browser instance ID.
        filepath (str): Path to save JSON file.

    Returns:
        Dict[str, Any]: ``{"success": bool}``, plus a ``capture_note`` when body
        capture is off (exported responses have no bodies until it is enabled).
    """
    success = await rt.network_interceptor.export_to_json(instance_id, filepath)
    result: dict[str, Any] = {"success": success}
    filters = await rt.network_interceptor.get_capture_filters(instance_id)
    if not filters.get("capture_bodies"):
        result["capture_note"] = _CAPTURE_OFF_NOTE
    return result


async def import_network_data(instance_id: str, filepath: str) -> bool:
    """
    Import network data from JSON file.

    Args:
        instance_id (str): Browser instance ID.
        filepath (str): Path to JSON file.

    Returns:
        bool: True if successful.
    """
    return await rt.network_interceptor.import_from_json(instance_id, filepath)


async def set_network_capture_filters(
    instance_id: str,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    capture_bodies: bool | None = None,
    capture_internal_urls: bool | None = None,
) -> bool:
    """
    Set resource type filters for network capture to reduce memory usage.

    Args:
        instance_id (str): Browser instance ID.
        include_types (Optional[List[str]]): Only capture these types (e.g., ['XHR', 'Fetch', 'Document']).
        exclude_types (Optional[List[str]]): Exclude these types (e.g., ['Image', 'Stylesheet', 'Font', 'Script']).
        capture_bodies (Optional[bool]): Enable/disable response-body capture for this
            instance (default off; overrides STEALTH_MCP_NETWORK_CAPTURE_BODIES). Each
            argument is merged — passing only capture_bodies keeps include/exclude.
        capture_internal_urls (Optional[bool]): Capture Chrome's own traffic —
            chrome://, chrome-extension://, chrome-error://, devtools://, about: —
            which is EXCLUDED by default so the page's requests are not drowned out
            (overrides STEALTH_MCP_NETWORK_CAPTURE_INTERNAL_URLS).

    Type matching is case-insensitive, so 'document' and 'Document' behave alike.
    Common resource types: Document, Stylesheet, Image, Media, Font, Script, XHR, Fetch, WebSocket, Manifest, Other

    Returns:
        bool: True if successful.
    """
    await rt.network_interceptor.set_capture_filters(
        instance_id,
        include_types,
        exclude_types,
        capture_bodies,
        capture_internal_urls,
    )
    return True


async def get_network_capture_filters(instance_id: str) -> dict[str, Any]:
    """
    Get current network capture filters plus resolved body-capture state.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, Any]: 'include'/'exclude' lists, the resolved 'capture_bodies'
        and 'capture_internal_urls' flags, and body-store usage
        ('body_store_bytes', 'body_store_max_bytes', 'body_max_bytes').
    """
    return await rt.network_interceptor.get_capture_filters(instance_id)


async def modify_headers(instance_id: str, headers: dict[str, str]) -> bool:
    """
    Modify request headers for future requests.

    Args:
        instance_id (str): Browser instance ID.
        headers (Dict[str, str]): Headers to add/modify.

    Returns:
        bool: True if modified successfully.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.network_interceptor.modify_headers(tab, headers), instance_id=instance_id
    )


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in
#: ``SECTION_TOOLS["network-debugging"]``.
TOOLS = (
    list_network_requests,
    get_request_details,
    get_response_details,
    get_response_content,
    search_network_requests,
    export_network_data,
    import_network_data,
    set_network_capture_filters,
    get_network_capture_filters,
    modify_headers,
)
