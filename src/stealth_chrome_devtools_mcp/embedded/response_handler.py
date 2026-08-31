"""Response handler for managing large responses and automatic file-based fallbacks."""

import contextlib
import dataclasses
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from stealth_chrome_devtools_mcp.embedded.singleton import STATE_DIR
from stealth_chrome_devtools_mcp.settings import get_settings


def default_clone_output_dir() -> Path:
    """Per-user directory for clone / large-response artifacts.

    Must never resolve inside the installed package: on a real (non-editable)
    install that path lives in ``site-packages`` — often read-only (system
    Python, containers, ``pip install --user``), where the first screenshot or
    large-response spill would raise ``PermissionError``; and where it *is*
    writable, artifacts silently accumulate in the install. This mirrors the
    project's existing state-dir convention (``~/.stealth-mcp``, overridable via
    a ``STEALTH_MCP_*`` env var). Pure: computes the path, never creates it.
    """
    configured = get_settings().clone_output_dir
    if configured and configured.strip():
        return Path(configured).expanduser()
    return STATE_DIR / "element_clones"


#: Estimated-token ceiling for an INLINE tool result; above it the payload is
#: spilled to a file and replaced by a descriptor (F-837).
#:
#: Derivation from the 2026-08-30 live measurements, not a round number:
#: a 59,734-char response (≈14.9k estimated tokens under the ``len // 4`` rule
#: below) was returned inline and then REJECTED by the MCP client with "result
#: exceeds maximum allowed tokens", while 138.91 KB and 282.83 KB diverted
#: correctly. So the old 20,000 ceiling sat ABOVE the client's practical limit
#: and left a dead band — too big to deliver, too small to divert.
#:
#: The client ceiling in play is 25,000 tokens (Claude Code's default MCP
#: tool-output cap). The rejection proves the ``len // 4`` estimate is
#: optimistic for the markup-heavy payloads this handler carries: 59,734 chars
#: exceeded 25,000 real tokens, i.e. under ~2.4 chars/token, not 4. Taking 2.0
#: chars/token as the worst case, real ~= 2x estimated; budgeting 20,000 real
#: tokens (80% of the ceiling, leaving room for the MCP envelope) gives
#: 20,000 / 2 = 10,000 estimated tokens ~= 40,000 chars. The regression size
#: sits 1.49x above that, and the two already-diverting sizes stay diverted.
INLINE_TOKEN_CEILING = 10_000

#: Depth guard shared by :func:`json_safe` and :func:`surrogate_safe` — a
#: self-referential or pathologically nested object degrades (to ``str``, or to
#: itself unrepaired) rather than blowing the stack.
_MAX_CONVERSION_DEPTH = 24

#: "this object has no JSON form of its own" — distinct from a real ``None``.
_NO_FORM = object()

#: Any code point in the UTF-16 surrogate range that appears inside a Python
#: ``str`` is by construction UNPAIRED (F-823): CPython stores a valid astral
#: character — an emoji, a G-clef, a CJK-ext ideograph — as ONE code point
#: above U+FFFF, never as a pair, and ``json`` recombines a well-formed
#: ``😀`` escape pair on the way IN. So a match here is always a
#: broken half, and never a false positive on the emoji this pattern exists to
#: leave intact.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

#: U+FFFD REPLACEMENT CHARACTER — Unicode's own "a character was here and it
#: did not survive" marker, one per broken half. Not ``?`` (what
#: ``errors="replace"`` yields when *encoding*), which is indistinguishable
#: from a question mark the page really contained.
_REPLACEMENT_CHAR = "�"


def _repair_text(text: str) -> str:
    """One lone surrogate becomes one U+FFFD; every other character untouched.

    Returns the SAME object when there is nothing to repair, which is what lets
    :func:`surrogate_safe` keep a clean payload's identity.
    """
    if _LONE_SURROGATE.search(text) is None:
        return text
    return _LONE_SURROGATE.sub(_REPLACEMENT_CHAR, text)


def surrogate_safe(data: Any, depth: int = 0) -> Any:
    """THE one repair for lone surrogates in a tool payload (F-823).

    Real pages produce them: a UTF-16-indexed ``slice``/``substring``, a
    truncating extractor, or a mis-decoded byte run hands back HALF an emoji.
    UTF-8 has no encoding for an unpaired surrogate, so FastMCP's
    ``pydantic_core.to_json(data, fallback=str)`` — the last step before the
    payload leaves this process — raised ``UnicodeEncodeError: 'utf-8' codec
    can't encode character '\\ud83d' ... surrogates not allowed`` and the whole
    tool call failed, though the CDP work had already succeeded (Sentry
    STEALTH-CHROME-DEVTOOLS-MCP-4M, on ``execute_script``).

    Deliberately narrower than :func:`json_safe`: this is the ENCODING guard,
    so it rewrites **strings only** and returns every other leaf — including
    objects it does not understand — by identity. That is what makes it safe to
    run on the return of all 94 tools from ``tool_registry``, where a type
    conversion would silently change payload shapes that FastMCP's serializer
    handles natively (``datetime``, ``bytes``, ``UUID``). ``json_safe`` calls
    it too, so the large-response spill file gets the same one policy.

    Total by construction, never raises: every branch is an ``isinstance``
    check over already-materialized data, the recursion is depth-guarded, and
    the only string operation is a regex substitution.
    """
    if isinstance(data, str):
        return _repair_text(data)
    if depth >= _MAX_CONVERSION_DEPTH:
        return data
    if isinstance(data, dict):
        repaired: dict[Any, Any] = {}
        changed = False
        for key, value in data.items():
            new_key = _repair_text(key) if isinstance(key, str) else key
            new_value = surrogate_safe(value, depth + 1)
            changed = changed or new_key is not key or new_value is not value
            repaired[new_key] = new_value
        return repaired if changed else data
    if isinstance(data, (list, tuple, set, frozenset)):
        # A repaired sequence comes back as a ``list`` (same JSON array either
        # way); an untouched one comes back as the very object passed in, so a
        # tuple/set payload only changes type where a repair actually happened.
        items: list[Any] = []
        changed = False
        for value in data:
            new_value = surrogate_safe(value, depth + 1)
            changed = changed or new_value is not value
            items.append(new_value)
        return items if changed else data
    return data


def _own_json_form(value: Any) -> Any:
    """The object's own JSON representation, or :data:`_NO_FORM`.

    nodriver's CDP types are dataclasses carrying their own wire encoder:
    ``to_json`` yields exactly the record Chrome sent, so a converted
    ``ExceptionDetails`` still shows its real ``text`` / ``exception`` /
    ``className``. The suppressions are deliberate and terminal, not silent
    failures: every path here has the ``str(value)`` fallback below it, so a
    refusing encoder costs fidelity, never the tool call it is part of.
    """
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        with contextlib.suppress(Exception):
            return to_json()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        with contextlib.suppress(Exception):
            return dataclasses.asdict(value)
    return _NO_FORM


def _convert(value: Any, depth: int = 0) -> Any:
    """Recursively rewrite ``value`` into plain JSON data. Never raises."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth >= _MAX_CONVERSION_DEPTH:
        return str(value)
    if isinstance(value, dict):
        return {str(k): _convert(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_convert(v, depth + 1) for v in value]
    if isinstance(value, Enum):
        return _convert(value.value, depth + 1)
    own = _own_json_form(value)
    return str(value) if own is _NO_FORM else _convert(own, depth + 1)


def json_safe(data: Any) -> Any:
    """THE one home for making a tool payload JSON-serializable (F-822).

    Returns ``data`` **unchanged** when it is already pure JSON data — the
    common case, so the identity contract callers rely on still holds — and
    otherwise a converted copy in which every foreign object has become plain
    data.

    Why a tool payload contains foreign objects at all: nodriver's
    ``Tab.evaluate`` *returns* ``cdp.runtime.ExceptionDetails`` in the value's
    place when the evaluated JS throws (and its bare ``RemoteObject`` when the
    value is falsy) instead of raising, so
    ``dom_handler.get_page_content``'s three unguarded ``evaluate`` calls could
    put a CDP dataclass under ``text`` / ``url`` / ``title``. That crashed the
    size estimate below with ``TypeError: Object of type ExceptionDetails is
    not JSON serializable`` — and would have crashed the MCP encoder next.

    Distinct from ``tool_errors._require_js_value``, which *raises* on that same
    record: that guard is the error convention for the user-facing eval escape
    hatch (F-795), where a throwing script IS the failure. This one is the
    transport guard for payloads whose foreign object is incidental — a page
    whose ``document.body.innerText`` threw still has real HTML to return.

    Serializable is not yet *encodable*, so this also runs the payload through
    :func:`surrogate_safe` (F-823) — the same one string policy the tool-return
    boundary applies — which is what makes the spill file below writable.
    """
    try:
        json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        data = _convert(data)
    return surrogate_safe(data)


class ResponseHandler:
    """Handle large responses by automatically falling back to file-based storage."""

    def __init__(
        self, max_tokens: int = INLINE_TOKEN_CEILING, clone_dir: str | None = None
    ):
        """
        Initialize the response handler.

        Args:
            max_tokens: Maximum tokens before falling back to file storage
            clone_dir: Directory to store large response files
        """
        self.max_tokens = max_tokens
        if clone_dir is None:
            self.clone_dir = default_clone_output_dir()
        else:
            self.clone_dir = Path(clone_dir)
        self.clone_dir.mkdir(parents=True, exist_ok=True)

    def estimate_tokens(self, data: Any) -> int:
        """
        Estimate token count for data (rough approximation).

        Args:
            data: The data to estimate tokens for

        Returns:
            Estimated token count
        """
        if isinstance(data, (dict, list)):
            # Convert to JSON string and estimate ~4 chars per token.
            # ``default=str`` so an un-encodable member costs an approximate
            # size, never a raised TypeError (F-822): estimating a payload's
            # size must not be able to fail the tool that produced it.
            json_str = json.dumps(data, ensure_ascii=False, default=str)
            return len(json_str) // 4
        if isinstance(data, str):
            return len(data) // 4
        return len(str(data)) // 4

    def handle_response(
        self,
        data: Any,
        fallback_filename_prefix: str = "large_response",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Handle response data, automatically falling back to file storage if too large.

        Args:
            data: The response data
            fallback_filename_prefix: Prefix for filename if file storage is needed
            metadata: Additional metadata to include in file response

        Returns:
            Either the original data or file storage info if data was too large
        """
        # One conversion at the boundary covers BOTH exits: nothing this
        # handler returns, inline or spilled, can carry a raw CDP object.
        data = json_safe(data)
        estimated_tokens = self.estimate_tokens(data)

        if estimated_tokens <= self.max_tokens:
            # Data is small enough, return as-is
            return data

        # Data is too large, save to file
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        filename = f"{fallback_filename_prefix}_{timestamp}_{unique_id}.json"
        file_path = self.clone_dir / filename

        # ``data`` came through ``json_safe`` above; the caller's ``metadata``
        # did not, and it reaches the same utf-8 encoder — a page title
        # carrying half an emoji would crash the write and leave a truncated
        # spill file (F-823). Same helper, applied once to the one part that
        # had not seen it yet (and reused for the descriptor below), rather
        # than re-serializing the by-definition-oversized payload a second time.
        safe_metadata = json_safe(metadata or {})

        # Prepare file content with metadata
        file_content = {
            "metadata": {
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "estimated_tokens": estimated_tokens,
                "auto_saved_due_to_size": True,
                **safe_metadata,
            },
            "data": data,
        }

        # Save to file
        with Path(file_path).open("w", encoding="utf-8") as f:
            # ``default=str`` guards the caller-supplied ``metadata`` too: a
            # half-written spill file would be worse than an approximate one.
            json.dump(file_content, f, indent=2, ensure_ascii=False, default=str)

        # Return file info instead of data
        file_size_kb = file_path.stat().st_size / 1024

        return {
            "file_path": str(file_path),
            "filename": filename,
            "file_size_kb": round(file_size_kb, 2),
            "estimated_tokens": estimated_tokens,
            "reason": "Response too large, automatically saved to file",
            "metadata": safe_metadata,
        }


# Global instance
response_handler = ResponseHandler()
