"""The sentinels and frame/navigation constants the fake tab doubles share.

Split out of :mod:`fakes` (T-5431); that module re-exports every name here, so
tests keep writing ``from fakes import ...``.
"""

from __future__ import annotations

#: "this double was not told to answer anything unusual" — distinct from every
#: value a test might legitimately want it to answer with, ``None`` included.
_UNSET = object()

#: The two frames :class:`FakeTab` models. A real subframe's lifecycle events
#: carry the SUBFRAME's id (measured, F-882), which is what makes a wait's frame
#: filter observable at all.
MAIN_FRAME = "F-main"
IFRAME = "F-child"
#: What a superseding loader's id carries, so the fake can tell which document
#: the tab is showing without a second bookkeeping field.
SUPERSEDED_MARK = "-sup"
#: The one ``errorText`` under which OUR loader never commits (a download, or a
#: page that navigated itself away while we were still pending).
ABORTED_NAVIGATION = "net::ERR_ABORTED"
#: What ``location.href`` reads on the page Chrome commits for any OTHER
#: ``errorText`` (a refused connection, a host that does not resolve): its own
#: error page, under our loader, whatever URL was asked for (F-802's evidence).
CHROME_ERROR_PAGE = "chrome-error://chromewebdata/"
#: ``supersede_after`` for the abort that IS a supersession: no document of ours
#: ever commits, and the one that aborted us takes its place.
ABORTED_SUPERSESSION = "aborted"
#: What Chrome answers a command whose document was replaced while the command
#: was in flight (F-882e). Copied verbatim from the CI failures it reproduces —
#: release-gate runs 35316298288 attempt 1 and 35460514255 attempt 1, both
#: ``integration (macOS/ARM64)``, both raised out of the post-navigation read as
#: ``ProtocolException: Inspected target navigated or closed [code: -32000]``.
#: Deliberately NOT imported from ``navigation_milestone``: a fake that reads the
#: product's own constants would compare the product to itself, and what a pin
#: on this needs to measure is whether it recognises CHROME's wording.
TARGET_SWAPPED_ERROR = {
    "code": -32000,
    "message": "Inspected target navigated or closed",
}
