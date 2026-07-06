"""Data models for browser MCP server."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class BrowserState(StrEnum):
    """Browser instance states."""

    STARTING = "starting"
    READY = "ready"
    NAVIGATING = "navigating"
    ERROR = "error"
    CLOSED = "closed"


class BrowserInstance(BaseModel):
    """Represents a browser instance."""

    instance_id: str = Field(description="Unique identifier for the browser instance")
    state: BrowserState = Field(default=BrowserState.STARTING)
    current_url: str | None = Field(default=None, description="Current page URL")
    title: str | None = Field(default=None, description="Current page title")
    created_at: datetime = Field(default_factory=datetime.now)
    last_activity: datetime = Field(default_factory=datetime.now)
    headless: bool = Field(default=False)
    user_agent: str | None = None
    viewport: dict[str, int] = Field(
        default_factory=lambda: {"width": 1920, "height": 1080}
    )

    def update_activity(self):
        """Update last activity timestamp."""
        self.last_activity = datetime.now(tz=timezone.utc)


class NetworkRequest(BaseModel):
    """Represents a captured network request."""

    request_id: str = Field(description="Unique request identifier")
    instance_id: str = Field(description="Browser instance that made the request")
    url: str = Field(description="Request URL")
    method: str = Field(description="HTTP method")
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    post_data: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    resource_type: str | None = None


class NetworkResponse(BaseModel):
    """Represents a captured network response."""

    request_id: str = Field(description="Associated request ID")
    status: int = Field(description="HTTP status code")
    headers: dict[str, str] = Field(default_factory=dict)
    content_length: int | None = None
    content_type: str | None = None
    body: bytes | None = None
    timestamp: datetime = Field(default_factory=datetime.now)


class ElementInfo(BaseModel):
    """Information about a DOM element."""

    selector: str = Field(description="CSS selector or XPath")
    tag_name: str = Field(description="HTML tag name")
    text: str | None = Field(default=None, description="Element text content")
    attributes: dict[str, str] = Field(default_factory=dict)
    is_visible: bool = Field(default=True)
    is_clickable: bool = Field(default=False)
    bounding_box: dict[str, float] | None = None
    children_count: int = Field(default=0)


class PageState(BaseModel):
    """Complete state snapshot of a page."""

    instance_id: str
    url: str
    title: str
    ready_state: str = Field(description="Document ready state")
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    local_storage: dict[str, str] = Field(default_factory=dict)
    session_storage: dict[str, str] = Field(default_factory=dict)
    console_logs: list[dict[str, Any]] = Field(default_factory=list)
    viewport: dict[str, int] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


class BrowserOptions(BaseModel):
    """Options for spawning a new browser instance."""

    headless: bool = Field(default=False, description="Run browser in headless mode")
    user_agent: str | None = Field(default=None, description="Custom user agent string")
    viewport_width: int = Field(default=1920, description="Viewport width in pixels")
    viewport_height: int = Field(default=1080, description="Viewport height in pixels")
    proxy: str | None = Field(default=None, description="Proxy server URL")
    browser_args: list[str] = Field(
        default_factory=list, description="Additional browser launch arguments"
    )
    timezone_id: str | None = Field(
        default=None,
        description="IANA timezone ID applied via CDP Emulation.setTimezoneOverride",
    )
    idle_timeout_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Idle timeout override in seconds for automatic instance cleanup",
    )
    block_resources: list[str] = Field(
        default_factory=list, description="Resource types to block"
    )
    extra_headers: dict[str, str] = Field(
        default_factory=dict, description="Extra HTTP headers"
    )
    user_data_dir: str | None = Field(
        default=None, description="Path to user data directory"
    )
    sandbox: bool = Field(default=True, description="Enable browser sandbox mode")
    auto_clone: bool = Field(
        default=False,
        description=(
            "Internal: profile is a disposable auto-clone of master and is "
            "deleted when the browser closes. Set by the server from the "
            "resolved profile role, never by callers."
        ),
    )


class NavigationOptions(BaseModel):
    """Options for page navigation."""

    wait_until: str = Field(
        default="load",
        description="Wait condition: load, domcontentloaded, networkidle",
    )
    timeout: int = Field(
        default=30000,
        le=60000,
        description=(
            "Navigation timeout in ms (max 60000). Most pages load in under "
            "10s — use the default unless the page is known to be slow."
        ),
    )
    referrer: str | None = Field(default=None, description="Referrer URL")


class ScriptResult(BaseModel):
    """Result from script execution."""

    success: bool
    result: Any = None
    error: str | None = None
    execution_time: float = Field(description="Execution time in milliseconds")


class ElementAction(StrEnum):
    """Types of element actions."""

    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    HOVER = "hover"
    FOCUS = "focus"
    CLEAR = "clear"
    SCREENSHOT = "screenshot"


class HookAction(StrEnum):
    """Types of network hook actions."""

    MODIFY = "modify"
    BLOCK = "block"
    REDIRECT = "redirect"
    FULFILL = "fulfill"
    LOG = "log"


class HookStage(StrEnum):
    """Stages at which hooks can intercept."""

    REQUEST = "request"
    RESPONSE = "response"


class HookStatus(StrEnum):
    """Status of a hook."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    PAUSED = "paused"


class NetworkHook(BaseModel):
    """Represents a network hook rule."""

    hook_id: str = Field(description="Unique hook identifier")
    name: str = Field(description="Human-readable hook name")
    url_pattern: str = Field(description="URL pattern to match (supports wildcards)")
    resource_type: str | None = Field(default=None, description="Resource type filter")
    stage: HookStage = Field(description="When to intercept (request/response)")
    action: HookAction = Field(description="What to do with matched requests")
    status: HookStatus = Field(default=HookStatus.ACTIVE)
    priority: int = Field(
        default=100, description="Hook priority (lower = higher priority)"
    )

    modifications: dict[str, Any] = Field(
        default_factory=dict, description="Modifications to apply"
    )
    redirect_url: str | None = Field(default=None, description="URL to redirect to")
    custom_response: dict[str, Any] | None = Field(
        default=None, description="Custom response data"
    )

    created_at: datetime = Field(default_factory=datetime.now)
    last_triggered: datetime | None = None
    trigger_count: int = Field(
        default=0, description="Number of times this hook was triggered"
    )


class PendingRequest(BaseModel):
    """Represents a request awaiting modification."""

    request_id: str = Field(description="Fetch request ID")
    instance_id: str = Field(description="Browser instance ID")
    url: str = Field(description="Original request URL")
    method: str = Field(description="HTTP method")
    headers: dict[str, str] = Field(default_factory=dict)
    post_data: str | None = None
    resource_type: str | None = None
    stage: HookStage = Field(description="Current interception stage")

    matched_hooks: list[str] = Field(
        default_factory=list, description="IDs of hooks that matched"
    )
    modifications: dict[str, Any] = Field(
        default_factory=dict, description="Accumulated modifications"
    )
    status: str = Field(default="pending", description="Processing status")

    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: datetime | None = None


class RequestModification(BaseModel):
    """Represents modifications to apply to a request."""

    url: str | None = None
    method: str | None = None
    headers: dict[str, str] | None = None
    post_data: str | None = None
    intercept_response: bool | None = None


class ResponseModification(BaseModel):
    """Represents modifications to apply to a response."""

    status_code: int | None = None
    status_text: str | None = None
    headers: dict[str, str] | None = None
    body: str | None = None
