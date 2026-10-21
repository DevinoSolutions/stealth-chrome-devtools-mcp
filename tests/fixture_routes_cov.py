"""The ``cov_*`` pages the F-873...F-881 regression lane needs.

Split out of :mod:`fixture_routes` (T-5431); that module re-exports every name
here, so callers keep importing from ``fixture_routes``.
"""

from __future__ import annotations

import base64
import time

from fixture_routes_base import _fill, _page, _send, _send_html
from fixture_routes_handlers import _query_value

# ═══════════════════════════════════════════════════════════════════════════
# ``cov_*`` — the pages the F-873…F-881 regression lane needs
# ═══════════════════════════════════════════════════════════════════════════
# One appended block, and the registration below is an explicit ``update``
# rather than an edit of ``ROUTES``/``DYNAMIC_PAGES`` above: every line here is
# new at EOF, so a branch adding its own routes merges without a conflict. The
# rule the rest of the file lives under still holds — a route with NO query
# string answers IMMEDIATELY, so the hermetic enumeration backstop can call
# every key without parking a thread.

#: Sentinels, one per page (the enumeration backstop asserts each one serves).
COV_PLAIN_SENTINEL = "fixture-cov-plain"
COV_SLOW_LOAD_SENTINEL = "fixture-cov-slow-load"
COV_SLOW_PAGE_SENTINEL = "fixture-cov-slow-page"
COV_SHELL_SENTINEL = "fixture-cov-shell"
COV_FORM_SENTINEL = "fixture-cov-form"

#: Titles, named here because the fleet node asserts ``list_instances`` reports
#: them — a page renamed in this file must fail at the import, not as a
#: mysterious title mismatch in a six-browser run.
COV_PLAIN_TITLE = "cov-plain"
COV_SHELL_TITLE = "cov-shell"
COV_FORM_TITLE = "cov-form"

#: ``/cov/form.html``'s title AFTER its own button has been clicked. The click
#: handler is the only writer, so this title exists on a page NO ``navigate``
#: call ever saw — which is F-874 §1 row 1, and the only way a listing
#: assertion can go red against a cache whose two writers are spawn and
#: navigate.
COV_CLICKED_TITLE = "cov-clicked"

#: ``/cov/slow_load.html``'s title BEFORE its ``load`` event and AFTER it. The
#: page sets the second one from a ``load`` listener and nowhere else, so a
#: navigation that answered at COMMIT can only ever read the first — which is
#: what makes F-881's node red by construction rather than by timing.
COV_TITLE_BEFORE_LOAD = "cov-before-load"
COV_TITLE_AFTER_LOAD = "cov-after-load"

#: Nothing here may park a serving thread for longer than this, whatever a
#: caller asks for. A fixture that can be told to sleep without a ceiling is a
#: way to hang the suite from inside a URL.
COV_MAX_DELAY_MS = 5_000

#: A 1x1 transparent GIF — the smallest thing that is still an ``<img>`` to
#: Blink, so ``/cov/slow_load.html``'s ``load`` waits on a real subresource.
_COV_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


def _cov_bounded_ms(query: str) -> int:
    """``?ms=`` clamped to ``[0, COV_MAX_DELAY_MS]``; ``0`` when unreadable."""
    raw = _query_value(query, "ms")
    if not raw:
        return 0
    try:
        requested = int(raw)
    except ValueError:
        return 0
    return max(0, min(requested, COV_MAX_DELAY_MS))


def cov_plain_page() -> str:
    return _page(COV_PLAIN_TITLE, COV_PLAIN_SENTINEL, "<h1 id='heading'>COV PLAIN</h1>")


def cov_slow_load_page(delay_ms: int) -> str:
    """A page that COMMITS at once and does not fire ``load`` for ``delay_ms``.

    The delay is an ``<img>``, deliberately: an image blocks ``load`` and does
    NOT block ``DOMContentLoaded``, so this page tells the three navigation
    milestones apart from one another rather than only from "not navigated".
    Both observable facts — the title and ``document.readyState`` — flip only
    at ``load``.
    """
    script = """
window.__covLoaded = false;
window.addEventListener('load', function () {
  document.title = '__AFTER__';
  window.__covLoaded = true;
});
"""
    body = (
        "<h1 id='heading'>COV SLOW LOAD</h1>"
        f"<img id='slow-asset' alt='' src='/cov/slow_asset?ms={delay_ms}'>"
        f"<script>{_fill(script, after=COV_TITLE_AFTER_LOAD)}</script>"
    )
    return _page(COV_TITLE_BEFORE_LOAD, COV_SLOW_LOAD_SENTINEL, body)


def cov_app_shell_page() -> str:
    """F-878's app-shell shape: the document cannot scroll, a nested div can."""
    style = (
        "html,body{margin:0;height:100%;overflow:hidden}"
        "#shell{height:100%;overflow:auto}"
        "#filler{height:6000px}"
    )
    body = (
        "<div id='shell'><h1 id='heading'>COV SHELL</h1>"
        "<div id='filler'></div><div id='shell-bottom'>COV-SHELL-BOTTOM</div></div>"
    )
    return _page(
        COV_SHELL_TITLE, COV_SHELL_SENTINEL, body, head=f"<style>{style}</style>"
    )


def cov_form_page() -> str:
    """One form, one field, no submit button — plus the control whose value is
    legitimately NOT what was typed.

    ``#masked`` rewrites every input event into ``dd-dd`` groups, so ``1234``
    reads back as ``12-34``: a tool that verified "the field CONTAINS what I
    typed" would report a failure the page had accepted.
    """
    script = """
window.__covForm = { submits: 0, selected: [], clicks: [] };
document.getElementById('cov-form').addEventListener('submit', function (event) {
  event.preventDefault();
  window.__covForm.submits += 1;
});
document.getElementById('masked').addEventListener('input', function (event) {
  var digits = event.target.value.replace(/[^0-9]/g, '').slice(0, 4);
  var groups = digits.match(/.{1,2}/g) || [];
  event.target.value = groups.join('-');
});
document.getElementById('cov-select').addEventListener('change', function (event) {
  window.__covForm.selected.push(event.target.value);
});
document.getElementById('cov-button').addEventListener('click', function () {
  window.__covForm.clicks.push('cov-button');
  document.getElementById('cov-clicked').textContent = 'CLICKED';
  document.title = '__CLICKED_TITLE__';
});
"""
    body = (
        "<h1 id='heading'>COV FORM</h1>"
        "<form id='cov-form'><input type='text' id='cov-field' name='q'></form>"
        "<input type='text' id='masked' value=''>"
        "<select id='cov-select'>"
        "<option value='a'>Alpha</option>"
        "<option value='b' label='Beta label'>Beta</option>"
        "<option value='c'>Gamma</option>"
        "</select>"
        "<button type='button' id='cov-button'>Press</button>"
        "<span id='cov-clicked'>NOT-CLICKED</span>"
        f"<script>{_fill(script, clicked_title=COV_CLICKED_TITLE)}</script>"
    )
    return _page(COV_FORM_TITLE, COV_FORM_SENTINEL, body)


def _r_cov_plain(handler, query: str) -> None:
    _send_html(handler, cov_plain_page())


def _r_cov_slow_asset(handler, query: str) -> None:
    """The subresource ``/cov/slow_load.html`` waits on: sleep, then a pixel.

    A peer that gave up first is EXPECTED here and is swallowed, exactly as the
    W10 fault routes swallow it: a node that closed its instance while this
    thread was still sleeping is the normal end of the ``domcontentloaded``
    case, and letting the stdlib handler print that traceback would make every
    green run look like it had an error in it.
    """
    time.sleep(_cov_bounded_ms(query) / 1000.0)
    try:
        _send(handler, 200, [("Content-Type", "image/gif")], _COV_PIXEL)
    except OSError:  # peer gave up first — the expected end of the DCL node
        handler.close_connection = True


def _r_cov_slow_load(handler, query: str) -> None:
    _send_html(handler, cov_slow_load_page(_cov_bounded_ms(query)))


def _r_cov_slow_page(handler, query: str) -> None:
    """Withhold the WHOLE response for ``?ms=``, then serve a plain page.

    The delay lives in this thread and nowhere near the renderer, which is the
    only reason it can measure whether two requests OVERLAP: a delay written in
    page JavaScript would be serialised by Blink's own main thread and would
    answer a different question.
    """
    time.sleep(_cov_bounded_ms(query) / 1000.0)
    try:
        _send_html(
            handler,
            _page(
                "cov-slow-page",
                COV_SLOW_PAGE_SENTINEL,
                "<h1 id='heading'>COV SLOW</h1>",
            ),
        )
    except OSError:  # peer gave up first, same policy as the fault routes
        handler.close_connection = True


def _r_cov_app_shell(handler, query: str) -> None:
    _send_html(handler, cov_app_shell_page())


def _r_cov_form(handler, query: str) -> None:
    _send_html(handler, cov_form_page())
