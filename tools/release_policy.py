"""W12: the trust boundary (threat contract) and the ONE redaction policy.

Split out of :mod:`release_evidence` (T-5431); that module re-exports every name
here, so W15's diagnostics still import this API from ``release_evidence``.
Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ── W12: the trust boundary and the ONE redaction policy ────────────────────
#
# plan_RELEASE §2.12 puts this here rather than in a security module of its own:
# "Extend tools/release_evidence.py and the W5 contract generator — do not add a
# parallel policy file/parser." W15's diagnostics import THIS API; a second rule
# table would be a second truth about what counts as a secret.
#
# The framing matters and is load-bearing: this tests the boundary that exists.
# It does not pretend an exec-capable local automation server is a sandbox.

#: The nine dimensions plan_RELEASE §2.12 requires the threat contract to cover.
THREAT_DIMENSIONS: tuple[str, ...] = (
    "caller trust",
    "bind exposure",
    "authentication",
    "host-code execution",
    "browser-code execution",
    "filesystem reads/writes",
    "uploads",
    "downloads",
    "secrets",
)

#: The literal loopback address the HTTP transport binds by default. Asserted
#: against the real argparse default AND against the backend spawn argv, so a
#: change to either is a red test rather than a silent remote exposure.
DEFAULT_BIND_HOST = "127.0.0.1"

#: Every host-Python execution site in ``src/``: caller-supplied text reaching
#: ``exec``/``eval``/``compile`` in the SERVER process, at the server's own
#: privileges. Declared here so a NEW one is a test failure, not a discovery.
#: (module path relative to the package, symbol that receives the caller text).
HOST_EXEC_SITES: tuple[tuple[str, str], ...] = (
    # plan_SERVERSPLIT slice 9: the `exec` moved with create_python_binding's
    # body out of embedded/server.py into the cdp-functions section module. The
    # SITE is unchanged — same tool, same caller text, same host privileges;
    # only the file that holds it moved, which is exactly what this inventory
    # is here to make a reviewed decision rather than a discovery.
    ("embedded/tool_sections/cdp_functions.py", "create_python_binding"),
    ("embedded/dynamic_hook_system.py", "function_code"),
    ("embedded/dynamic_hook_system.py", "custom_condition"),
)

#: Tools whose declared purpose is to run caller-supplied code in the BROWSER.
#: JavaScript here reaches page contexts, not the host interpreter.
BROWSER_EXEC_TOOLS: frozenset[str] = frozenset(
    {
        "call_javascript_function",
        "execute_cdp_command",
        "execute_function_sequence",
        "execute_python_in_browser",
        "execute_script",
        "inject_and_execute_script",
    }
)

#: Tools that reach :data:`HOST_EXEC_SITES` — caller text executed by the host
#: interpreter with the server's privileges. This is intended behaviour under
#: the trusted-caller model; it is a TRUST REQUIREMENT, never an isolation
#: claim, and the contract says so in those words.
HOST_EXEC_TOOLS: frozenset[str] = frozenset(
    {
        "create_dynamic_hook",
        "create_python_binding",
        "create_simple_dynamic_hook",
        "validate_hook_function",
    }
)

#: Tools that write host files at a caller-chosen destination.
FILESYSTEM_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "clone_element_to_file",
        "export_debug_logs",
        "export_network_data",
        "extract_complete_element_to_file",
        "extract_element_animations_to_file",
        "extract_element_assets_to_file",
        "extract_element_events_to_file",
        "extract_element_structure_to_file",
        "extract_element_styles_to_file",
        "take_screenshot",
    }
)

#: Tools that read host files at a caller-chosen source.
FILESYSTEM_READ_TOOLS: frozenset[str] = frozenset(
    {"import_network_data", "upload_file"}
)

#: Name fragments any download tool would have to carry. The contract states the
#: ABSENCE of a download contract; this is what makes that statement checkable
#: instead of merely asserted once and left to rot.
DOWNLOAD_NAME_FRAGMENTS: tuple[str, ...] = ("download", "save_as", "fetch_to_disk")

#: The closed set of secret classes the policy classifies (plan_RELEASE §2.12).
SECRET_CLASSES: tuple[str, ...] = (
    "url-userinfo",
    "url-query-value",
    "authorization-header",
    "cookie-header",
    "environment-canary",
    "dom-form-value",
    "script-argument",
    "sensitive-path-component",
)

#: ``replace`` rewrites the matched span in place; ``drop`` removes the whole
#: mapping entry; ``preserve`` passes a value through untouched.
REDACTION_ACTIONS: frozenset[str] = frozenset({"replace", "drop", "preserve"})

#: Diagnostic fields that MUST survive redaction — a diagnostic that redacts its
#: own error code is not a safer diagnostic, it is a useless one. W15 asserts
#: both halves: every canary gone, every one of these intact.
PRESERVED_DIAGNOSTIC_FIELDS: frozenset[str] = frozenset(
    {
        "correlation_id",
        "error_code",
        "error_type",
        "next_step",
        "phase",
        "tool",
    }
)

#: The bounded replacement format. It carries the CLASS and nothing derived from
#: the secret — no length, no hash, no prefix — so the placeholder itself can
#: never become the disclosure.
REDACTION_PLACEHOLDER = "[redacted:{secret_class}]"

#: Shortest literal a caller may register as a secret. A 1-2 character "secret"
#: would redact half the diagnostic and hide the failure instead of the value.
MIN_SECRET_LENGTH = 4


@dataclass(frozen=True)
class RedactionRule:
    """One classified secret class and what the policy does with it."""

    secret_class: str
    detects: str
    action: str
    rationale: str


REDACTION_POLICY: tuple[RedactionRule, ...] = (
    RedactionRule(
        "url-userinfo",
        "the `user:password@` span of any absolute URL",
        "replace",
        "A proxy or basic-auth URL carries the credential in the host part, so "
        "the URL is unusable as-is but its scheme/host/path are what make the "
        "failure diagnosable.",
    ),
    RedactionRule(
        "url-query-value",
        "every `?k=v` / `&k=v` VALUE; keys are kept",
        "replace",
        "Tokens ride in query values. Keeping the key names preserves the shape "
        "a reader needs without preserving what the value was.",
    ),
    RedactionRule(
        "authorization-header",
        "`authorization` / `proxy-authorization` mapping entries",
        "drop",
        "There is no diagnosable content in a credential header, so the entry "
        "goes rather than being echoed as a placeholder.",
    ),
    RedactionRule(
        "cookie-header",
        "`cookie` / `set-cookie` mapping entries",
        "drop",
        "Session cookies for the user's real logged-in profiles. Same reasoning "
        "as the authorization header.",
    ),
    RedactionRule(
        "environment-canary",
        "caller-registered literal values from the process environment",
        "replace",
        "Environment values cannot be recognised structurally; the caller that "
        "knows a value is a secret registers it, and the policy removes every "
        "occurrence wherever it surfaced.",
    ),
    RedactionRule(
        "dom-form-value",
        "mapping entries holding DOM/form input values",
        "drop",
        "Whatever the user typed into the page — passwords, card numbers, "
        "message bodies. The field NAME is diagnostic; the value never is.",
    ),
    RedactionRule(
        "script-argument",
        "mapping entries holding caller-supplied script/code arguments",
        "drop",
        "`execute_script`/`create_python_binding` arguments routinely carry "
        "credentials the caller pasted into the code it asked us to run.",
    ),
    RedactionRule(
        "sensitive-path-component",
        "the host home directory and account name, in either separator form",
        "replace",
        "A path is the most common accidental identity leak in a stack trace, "
        "and the interesting part of a path is its tail, not its prefix.",
    ),
)

#: Mapping-key fragments that trigger each ``drop`` rule, lower-cased.
DROP_KEY_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "authorization-header": ("authorization",),
    "cookie-header": ("cookie",),
    "dom-form-value": ("form_value", "form_data", "input_value", "dom_value"),
    "script-argument": (
        "script",
        "python_code",
        "function_code",
        "script_args",
        "arguments",
    ),
}

_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s]+)@")
_QUERY_VALUE_RE = re.compile(r"([?&][A-Za-z0-9_.%\[\]-]+=)([^&\s\"'<>]*)")


@dataclass(frozen=True)
class ThreatRow:
    """One dimension of the trust boundary, per transport.

    ``evidence`` is the honest half: it names what actually verifies the row, or
    says in plain words that nothing does. A row that reads "documented" has NOT
    been tested, and the contract prints that distinction rather than blurring
    the two into a single reassuring table.
    """

    dimension: str
    stdio: str
    http: str
    evidence: str


THREAT_CONTRACT: tuple[ThreatRow, ...] = (
    ThreatRow(
        "caller trust",
        "TRUSTED. The client spawns the server as a child process and already "
        "has the user's privileges; there is nothing to escalate.",
        "TRUSTED. Anything that presents the user's bearer token (F-1018) is "
        "treated as the user.",
        "documented — an untrusted MCP client is OUT OF SCOPE for this "
        "release. No test simulates a hostile caller, because the design "
        "grants a caller everything a hostile one would want.",
    ),
    ThreatRow(
        "bind exposure",
        "No socket. stdin/stdout of the child process only.",
        f"Loopback: the `--host` default is the literal `{DEFAULT_BIND_HOST}`, "
        "and the backend is spawned with that literal host. Remote exposure "
        "requires the user to pass `--host 0.0.0.0` themselves.",
        "TESTED — `tests/test_security_boundary.py::"
        "test_http_bind_defaults_to_literal_loopback` and `::"
        "test_backend_spawn_argv_pins_the_loopback_host` read the real "
        "argparse default and the real spawn argv; `::"
        "test_no_environment_knob_can_change_the_bind_host` proves no "
        "`STEALTH_MCP_*` setting reaches the host.",
    ),
    ThreatRow(
        "authentication",
        "NONE, and none is meaningful: the caller owns the process.",
        "A per-user bearer token (F-1018). The backend keeps one in "
        "`<state dir>/backend.token` (POSIX mode `0600`; on Windows a DACL "
        "granting only the current user) and answers `401` to any request "
        "without `Authorization: Bearer <token>`, after F-1013's Host (`421`) "
        "and Origin (`403`) checks; every first-party client sends it. It is "
        "one shared secret per user, not a channel binding: it keeps other OS "
        "users and local processes without the file out, and does nothing "
        "about the same user's own processes, which can read the file and the "
        "browser profiles. A backend that cannot secure the file does not "
        "serve. `--host 0.0.0.0` stays the operator's decision.",
        "documented — pinned outside this section, in the unit lane, by "
        "`tests/test_http_host_origin_guard.py` (no token 401, wrong token 401, "
        "right token 200 over a real socket; mode `0600` or exactly one ACE on "
        "Windows; every first-party client sends it) and end to end by "
        "`tests/test_backend_bearer_token_e2e.py` (an integration node). The "
        "gate runs no live HTTP acceptance test, so HTTP stays DESCRIBED, "
        "never qualified.",
    ),
    ThreatRow(
        "host-code execution",
        "YES, by design. `create_python_binding`, the two hook-creation tools "
        "and `validate_hook_function` run caller-supplied Python through "
        "`exec`/`eval` IN THE SERVER PROCESS, at the user's privileges.",
        "Identical, reachable by anything that presents the user's bearer token.",
        "TESTED as an INVENTORY, not as isolation — `::"
        "test_host_python_execution_sites_are_exactly_the_declared_set` "
        "AST-scans `src/` so a new host-exec site cannot appear unannounced. "
        "No test claims the execution is contained, because it is not.",
    ),
    ThreatRow(
        "browser-code execution",
        "YES, by design: six tools run caller JavaScript (or Python "
        "transpiled to JavaScript) in page contexts, and `execute_cdp_command` "
        "reaches the raw CDP surface.",
        "Identical.",
        "documented — the browser-side containment question needs real Chrome "
        "and is NOT answered here. No isolation property is claimed.",
    ),
    ThreatRow(
        "filesystem reads/writes",
        "UNRESTRICTED within the user's own permissions. Ten tools write to a "
        "caller-chosen destination and two read from a caller-chosen source; "
        "absolute paths and `..` traversal are accepted, not rejected.",
        "Identical.",
        "PARTIALLY TESTED — `::TestFilesystemDestinationMatrix` pins the exact "
        "resolved destination and overwrite behaviour of the three hermetic "
        "paths (`export_network_data`, `import_network_data`, "
        "`export_debug_logs`) across relative, absolute, `..`, mixed-separator "
        "and symlink/junction-escape forms. The seven browser-backed "
        "`*_to_file` tools are NOT covered.",
    ),
    ThreatRow(
        "uploads",
        "`upload_file` hands host files to a page's file input by absolute "
        "path. The page then receives their bytes.",
        "Identical.",
        "documented — needs real Chrome; NOT tested here. Exact-bytes and "
        "exact-name verification remain unproven.",
    ),
    ThreatRow(
        "downloads",
        "NO download tool exists. There is no destination contract, no "
        "completion signal, and no path guarantee for a browser-initiated "
        "download.",
        "Identical.",
        "TESTED — `::test_no_download_tool_is_served` keeps the ABSENCE true "
        "by checking the live registry, so a future download tool cannot land "
        "while the contract still promises there is none.",
    ),
    ThreatRow(
        "secrets",
        "The server holds no credential of its own, but it drives the user's "
        "real logged-in browser profiles and can read anything they can.",
        "Identical, behind only the user's bearer token and the loopback bind.",
        "TESTED for the DIAGNOSTIC path only — `::TestRedactionPolicy` proves "
        "every one of the eight secret classes is absent from "
        "policy-processed output while error type/code/correlation survive. "
        "It says nothing about what a tool RESULT may contain: results are "
        "returned to the trusted caller verbatim, by design.",
    ),
)


def threat_rows() -> tuple[ThreatRow, ...]:
    """The threat contract — the contract generator's only source for it."""
    return THREAT_CONTRACT


def redaction_rows() -> tuple[RedactionRule, ...]:
    """The redaction policy — one table, imported, never re-declared."""
    return REDACTION_POLICY


def placeholder(secret_class: str) -> str:
    """The bounded replacement for one secret class."""
    if secret_class not in SECRET_CLASSES:
        raise ValueError(f"unknown secret class {secret_class!r}")
    return REDACTION_PLACEHOLDER.format(secret_class=secret_class)


def host_secret_values() -> tuple[tuple[str, str], ...]:
    """The host identity strings the policy always removes.

    Both separator forms, because a Windows diagnostic mixes them freely and a
    policy that only knows one of them leaks through the other.
    """
    import contextlib
    import getpass

    values: list[str] = []
    home = str(Path.home())
    values.extend([home, home.replace("\\", "/")])
    with contextlib.suppress(Exception):  # PERMANENT(no account name is not a failure)
        values.append(getpass.getuser())
    seen: list[tuple[str, str]] = []
    for value in values:
        if len(value) >= MIN_SECRET_LENGTH and value not in {v for _, v in seen}:
            seen.append(("sensitive-path-component", value))
    return tuple(seen)


def _normalise_secrets(
    secrets: object, *, include_host_paths: bool
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for entry in secrets or ():
        secret_class, value = entry  # type: ignore[misc]
        if secret_class not in SECRET_CLASSES:
            raise ValueError(f"unknown secret class {secret_class!r}")
        if len(value) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"refusing to register a {len(value)}-character secret: a literal "
                f"shorter than {MIN_SECRET_LENGTH} characters would redact the "
                f"diagnostic instead of the value"
            )
        pairs.append((secret_class, value))
    if include_host_paths:
        pairs.extend(host_secret_values())
    # Longest first: a secret that contains another must be replaced whole.
    return tuple(sorted(pairs, key=lambda pair: len(pair[1]), reverse=True))


def redact_text(
    text: str, *, secrets: object = (), include_host_paths: bool = True
) -> str:
    """Apply the canonical policy to one string.

    Literal secrets are removed case-insensitively and longest-first, then the
    structural URL rules run. Both orders are safe: a literal already replaced
    by its placeholder is still matched (and re-replaced) by the query rule, so
    no ordering produces a leak — only a different placeholder.
    """
    result = text
    for secret_class, value in _normalise_secrets(
        secrets, include_host_paths=include_host_paths
    ):
        result = re.sub(
            re.escape(value), placeholder(secret_class), result, flags=re.IGNORECASE
        )
    result = _USERINFO_RE.sub(
        lambda m: f"{m.group(1)}{placeholder('url-userinfo')}@", result
    )
    return _QUERY_VALUE_RE.sub(
        lambda m: f"{m.group(1)}{placeholder('url-query-value')}", result
    )


def _drop_class_for_key(key: str) -> str | None:
    lowered = key.lower()
    for secret_class, fragments in DROP_KEY_FRAGMENTS.items():
        if any(fragment in lowered for fragment in fragments):
            return secret_class
    return None


def redact(
    value: object, *, secrets: object = (), include_host_paths: bool = True
) -> object:
    """THE redactor. Every later diagnostic surface calls this, not its own copy.

    Mappings lose their ``drop``-classified entries entirely, keep
    :data:`PRESERVED_DIAGNOSTIC_FIELDS` byte-for-byte, and recurse everywhere
    else. Strings go through :func:`redact_text`. Numbers and booleans pass.
    """
    if isinstance(value, str):
        return redact_text(
            value, secrets=secrets, include_host_paths=include_host_paths
        )
    if isinstance(value, dict):
        out: dict[object, object] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in PRESERVED_DIAGNOSTIC_FIELDS:
                out[key] = item
                continue
            if _drop_class_for_key(name) is not None:
                continue
            out[key] = redact(
                item, secrets=secrets, include_host_paths=include_host_paths
            )
        return out
    if isinstance(value, (list, tuple)):
        return [
            redact(item, secrets=secrets, include_host_paths=include_host_paths)
            for item in value
        ]
    return value


def _validate_threat_contract() -> list[str]:
    problems: list[str] = []
    dimensions = [row.dimension for row in THREAT_CONTRACT]
    missing = [name for name in THREAT_DIMENSIONS if name not in dimensions]
    if missing:
        problems.append(f"threat contract is missing dimension(s) {missing}")
    if len(set(dimensions)) != len(dimensions):
        problems.append("threat contract has a duplicate dimension")
    for row in THREAT_CONTRACT:
        problems.extend(
            f"threat row {row.dimension!r} has an empty {field}"
            for field, text in (("stdio", row.stdio), ("http", row.http))
            if not text.strip()
        )
        if not row.evidence.strip():
            problems.append(f"threat row {row.dimension!r} declares no evidence state")
    return problems


def _validate_redaction_policy() -> list[str]:
    problems: list[str] = []
    classes = [rule.secret_class for rule in REDACTION_POLICY]
    if sorted(classes) != sorted(SECRET_CLASSES):
        problems.append("redaction policy does not cover SECRET_CLASSES exactly")
    if len(set(classes)) != len(classes):
        problems.append("redaction policy has a duplicate secret class")
    for rule in REDACTION_POLICY:
        if rule.action not in REDACTION_ACTIONS:
            problems.append(f"rule {rule.secret_class!r} has action {rule.action!r}")
        if rule.action == "drop" and rule.secret_class not in DROP_KEY_FRAGMENTS:
            problems.append(f"drop rule {rule.secret_class!r} has no key fragments")
        if rule.action == "replace" and rule.secret_class in DROP_KEY_FRAGMENTS:
            problems.append(f"replace rule {rule.secret_class!r} also drops keys")
    return problems


def validate_policy() -> list[str]:
    """Structural problems with the threat contract or the redaction policy."""
    return _validate_threat_contract() + _validate_redaction_policy()
