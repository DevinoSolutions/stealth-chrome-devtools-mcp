"""Canonical environment/settings home for stealth-chrome-devtools-mcp.

This module is the ONE place the whole codebase reads environment configuration
(findings F-720/F-763: env access was previously scattered across ad-hoc
``parse_bool_env`` / ``parse_float_env`` / ``_parse_nonnegative_int_env`` helpers
with divergent truthy-set parsing). It is the Python equivalent of a zod schema
for ``.env``: a typed model with coercion, strict rejection of unknown keys, and
loud failure.

Design invariants:

* It imports nothing from the rest of the package (leaf module). Embedded modules
  import it absolutely (``from stealth_chrome_devtools_mcp.settings import
  get_settings``) and must never trigger an import cycle back into ``server``
  (the runpy double-registration hazard).
* ``STEALTH_MCP_*`` keys are the application namespace: a typo'd one fails loudly.
  Legacy unprefixed names (``BROWSER_*``, ``CDP_*``, ``DEBUG``, ``PORT`` ...) and
  host/runtime detection vars (``DISPLAY``, ``USERNAME``, ``container`` ...) are
  read verbatim via ``validation_alias`` so NO env var is renamed here — except
  the one M14+A1 (X-HARD) field rename below (no back-compat alias):
  ``STEALTH_MCP_SESSION_STORAGE_CAP_GB`` becomes
  ``STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB``.
* ``extra="forbid"`` rejects unknown keys found in the ``.env`` FILE. pydantic's
  env-var source only reads known-field names, so unknown ``STEALTH_MCP_*`` env
  VARS are rejected by ``_reject_unknown_prefixed_env`` below instead.
* ``get_settings()`` is process-cached; tests clear it via
  ``get_settings.cache_clear()`` (see the autouse conftest fixture).
"""

import os
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PREFIX = "STEALTH_MCP_"


class Settings(BaseSettings):
    """Typed, validated view of every environment variable the tool reads."""

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_file=".env",
        extra="forbid",
        case_sensitive=False,
    )

    # -- Application config (STEALTH_MCP_* namespace) ------------------------
    no_auto_recovery: bool = False
    clone_output_dir: str | None = None
    browser_session_root: str | None = None
    clone_storage_cap_gb: float = 10.0
    clone_trash_retention_hours: float = 24.0
    # Trims idle named browser-session profiles over this cap (GB). M14+A1
    # (X-HARD) renamed the field from session_storage_cap_gb, so the env var is
    # now STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB (no back-compat alias).
    browser_session_storage_cap_gb: float = 20.0
    # Directory for the M3 observability file logs (default: the state-dir
    # convention, ``~/.stealth-mcp/logs`` — same idiom as ``clone_output_dir``).
    log_dir: str | None = None
    # Minimum level recorded by the M3 file logger. An unrecognized value
    # degrades to INFO at the point of use rather than failing startup —
    # logging setup must never be the reason the server won't boot.
    log_level: str = "INFO"

    # -- Response-body capture (F-605) ---------------------------------------
    # Body capture is OFF by default (metadata is always captured); when on, the
    # response-body store is byte-bounded. 0 on either byte cap = unbounded.
    network_capture_bodies: bool = False
    network_body_max_bytes: int = Field(5 * 1024 * 1024, ge=0)
    network_body_store_max_bytes: int = Field(128 * 1024 * 1024, ge=0)

    # -- Request capture bounding (A3) ---------------------------------------
    # Request metadata is always captured; the retained set is count-bounded
    # (oldest evicted FIFO) and each request's post_data is byte-bounded. This
    # mirrors the response-body caps above so a long session cannot leak memory
    # through unbounded request retention. 0 on either cap = unbounded.
    network_request_max_count: int = Field(10_000, ge=0)
    network_post_data_max_bytes: int = Field(5 * 1024 * 1024, ge=0)

    # -- Legacy unprefixed config (names preserved verbatim via alias) -------
    # 0 = idle reaping disabled (never auto-close): the correct default for a
    # persistent server. The old server.py forced BROWSER_IDLE_TIMEOUT=0 via an
    # import-time os.environ.setdefault, so 0 was already the effective runtime
    # default; encoding it here removes that shadow write and the get_settings()
    # caching hazard it created.
    close_kill_timeout: float = Field(5.0, validation_alias="CLOSE_KILL_TIMEOUT", gt=0)
    browser_idle_timeout: int = Field(0, validation_alias="BROWSER_IDLE_TIMEOUT", ge=0)
    browser_idle_reaper_interval: int = Field(
        60, validation_alias="BROWSER_IDLE_REAPER_INTERVAL", ge=1
    )
    cdp_operation_timeout_seconds: float = Field(
        30.0, validation_alias="CDP_OPERATION_TIMEOUT_SECONDS"
    )
    execute_script_timeout_seconds: float = Field(
        10.0, validation_alias="EXECUTE_SCRIPT_TIMEOUT_SECONDS"
    )
    max_user_script_bytes: int = Field(
        100_000, validation_alias="MAX_USER_SCRIPT_BYTES"
    )
    browser_state_timeout_seconds: float = Field(
        10.0, validation_alias="BROWSER_STATE_TIMEOUT_SECONDS"
    )
    stealth_browser_debug: bool = Field(False, validation_alias="STEALTH_BROWSER_DEBUG")
    debug: bool = Field(False, validation_alias="DEBUG")
    browser_master_user_data_dir: str | None = Field(
        None, validation_alias="BROWSER_MASTER_USER_DATA_DIR"
    )
    browser_profile_clone_root: str | None = Field(
        None, validation_alias="BROWSER_PROFILE_CLONE_ROOT"
    )
    browser_master_snapshot_dir: str | None = Field(
        None, validation_alias="BROWSER_MASTER_SNAPSHOT_DIR"
    )
    browser_profile_refresh_days: int = Field(
        7, validation_alias="BROWSER_PROFILE_REFRESH_DAYS"
    )
    browser_orphan_profile_max_age: int = Field(
        21600, validation_alias="BROWSER_ORPHAN_PROFILE_MAX_AGE", ge=0
    )
    stealth_chrome_profile_key: str | None = Field(
        None, validation_alias="STEALTH_CHROME_PROFILE_KEY"
    )
    browser_profile_key: str | None = Field(
        None, validation_alias="BROWSER_PROFILE_KEY"
    )
    port: int = Field(8000, validation_alias="PORT")
    xpool_safe_mode: bool = Field(False, validation_alias="XPOOL_SAFE_MODE")

    # -- Host / runtime detection (read verbatim; not operator config, but
    #    routed through Settings per the single-env-home directive 2026-07-06) -
    display: str | None = Field(None, validation_alias="DISPLAY")
    container: str | None = Field(None, validation_alias="container")
    kubernetes_service_host: str | None = Field(
        None, validation_alias="KUBERNETES_SERVICE_HOST"
    )
    user: str | None = Field(None, validation_alias="USER")
    username: str | None = Field(None, validation_alias="USERNAME")
    codex_workspace: str | None = Field(None, validation_alias="CODEX_WORKSPACE")
    claude_project_dir: str | None = Field(None, validation_alias="CLAUDE_PROJECT_DIR")
    pwd: str | None = Field(None, validation_alias="PWD")

    # -- Observability (default OFF; local single-user tool) -----------------
    sentry_dsn: str | None = Field(None, validation_alias="SENTRY_DSN")

    @classmethod
    def _known_env_names(cls) -> set[str]:
        names: set[str] = set()
        for name, field in cls.model_fields.items():
            alias = field.validation_alias
            if isinstance(alias, str):
                names.add(alias.upper())
            else:
                names.add(f"{_ENV_PREFIX}{name}".upper())
        return names

    @model_validator(mode="after")
    def _reject_unknown_prefixed_env(self) -> "Settings":
        """zod ``.strict()`` for the app namespace: a typo'd ``STEALTH_MCP_*`` env
        var fails loudly. ``extra="forbid"`` only catches unknown keys in the
        ``.env`` file, not in ``os.environ``, so this covers the env-var case."""
        known = self._known_env_names()
        unknown = sorted(
            key
            for key in os.environ  # noqa: TID251  PERMANENT(canonical env home)
            if key.upper().startswith(_ENV_PREFIX) and key.upper() not in known
        )
        if unknown:
            raise ValueError(
                f"Unknown {_ENV_PREFIX}* environment variable(s): {unknown}. "
                "Every STEALTH_MCP_* key must map to a Settings field; "
                "check for a typo."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, built once from ``.env`` + env.

    Cached: tests that mutate the environment call ``get_settings.cache_clear()``
    (see the autouse conftest fixture) so the change is re-read.
    """
    return Settings()
