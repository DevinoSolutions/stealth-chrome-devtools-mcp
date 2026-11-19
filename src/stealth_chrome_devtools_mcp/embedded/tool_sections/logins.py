"""The ``logins`` tools. See ``tool_sections/__init__.py`` for the contract.

The login vault's tool surface (T-19366): ``list_logins`` says which stored logins
exist and ``login`` signs one in. The vault, the sealed tab and the page-driving
live in ``vault_store``, ``login_seal`` and ``login_flow``; this module is only
the surface, and the one thing it adds is turning a locked vault into the sentence
that tells the agent what a person has to do.

Nothing here can return a secret: ``list_logins`` returns ``vault_store``'s public
rows, and ``login`` returns an outcome.
"""

from stealth_chrome_devtools_mcp.embedded import (
    login_flow,
    tab_binding,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

SECTION = "logins"

LOCKED = "the login vault is locked: a person must run `stealthy login unlock`"


async def list_logins() -> dict[str, object]:
    """
    List the logins a person stored in the login vault.

    A person adds logins with `stealthy login add` and unlocks the vault with
    `stealthy login unlock`; nothing here can add, change or reveal one. A
    password is never returned, and a username only for a login whose owner chose
    to show it. A login that was rejected once is `frozen` until a person edits it.

    Returns:
        Dict[str, Any]: unlocked (True) and logins, a list of label, kind
            ("password" or "provider"), site, via (provider logins: the label of
            the login that account signs in with), frozen (null, or when it was
            frozen) and username (only when shown).
    """
    try:
        logins = vault_store.public_entries()
    except vault_store.VaultLocked:
        raise ToolError(LOCKED) from None
    return {"unlocked": True, "logins": logins}


async def login(instance_id: str, label: str) -> dict[str, str | int]:
    """
    Sign a stored login in to a browser instance's profile.

    The backend does it in a tab of its own that no tool can read or touch while
    it runs: it opens the login page, checks the page's host is one this login
    may be used on, types the username and password, and closes the tab. The
    password never reaches you, a log or a network record; you get an outcome.
    The session is saved in the profile, so reload your own tab afterwards.
    Use list_logins for the labels. A login that fails once is frozen so it is
    not retried into an account lockout; a person unfreezes it.

    While it runs, the browser is yours only in part. Every browser-internal page
    (chrome://, chrome-extension://, devtools:// and the like) is hidden from
    list_tabs and refused by every tool, and navigate and new_tab refuse to load
    one; about:blank and the new-tab page still work. go_back and go_forward are
    refused. Any DevTools window open in that browser is CLOSED, and the result
    says how many (closed_devtools_windows). A site that keeps showing its login
    form after the sign-in reads needs_user: only a form that comes back in a new
    page, or a new error message, counts as a refused password.

    Args:
        instance_id (str): Browser instance ID.
        label (str): The login's label, from list_logins.

    Returns:
        Dict[str, str | int]: outcome, label, detail and
            closed_devtools_windows (how many open DevTools windows the login
            closed in this browser). outcome is one of:
            logged_in (signed in), already_logged_in (the page needed no
            sign-in), needs_totp (a one-time code is asked for, which this tool
            cannot give), needs_user (a step it cannot take: a passkey, a device
            prompt, a puzzle, an unknown form), rejected (the password was
            refused and the login is now frozen), use_provider (a "sign in with"
            login: the provider account is signed in, now click the site's own
            provider button).
    """
    try:
        return await login_flow.run(
            rt.browser_manager,
            instance_id,
            label,
            tab_binding.caller() or "in-process",
        )
    except vault_store.VaultLocked:
        raise ToolError(LOCKED) from None
    except ToolError:
        raise
    except Exception as error:  # noqa: BLE001  PERMANENT(an unknown failure must reach the caller as its class only)
        # Whatever else went wrong, the caller learns THAT and not WHAT: the text
        # of an unexpected exception is the one place a secret could ride out in
        # a tool result, so only its class is returned. The detail goes to the
        # log, where the registry masks it (a traceback included).
        rt.debug_logger.log_error("logins", "login", error)
        raise ToolError(
            f"the login failed unexpectedly ({type(error).__name__}); "
            "the backend log has the detail"
        ) from None


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["logins"]``.
TOOLS = (list_logins, login)
