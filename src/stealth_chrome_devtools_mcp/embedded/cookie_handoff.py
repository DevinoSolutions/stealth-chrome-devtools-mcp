"""THE one home for handing a RUNNING browser's cookie jar to a fresh one over
CDP, and for knowing which profile directories THIS backend drives (F-898).

Its sibling is ``profile_copy`` and the boundary is one measured fact. That
module copies a profile DIRECTORY and answers a file Chrome holds open by
skipping it — which on Windows is the whole SQLite cookie jar, and everywhere
is a jar mid-transaction. Measured on Chrome 153: a file copy of a RUNNING
source carries **zero** cookies, and the copier cannot say which mattered,
which is exactly why ``profile_source.seed_source`` refused a live source by
name (F-897). This module is the other door — the jar read out of the live
browser over CDP and written into the new one — so a session can be seeded
from a source the human is still using.

**What it carries, and what it does not.** Cookies. Every kind, measured:
session, persistent, ``HttpOnly``, ``Secure``, ``SameSite=None`` and
``Partitioned``/CHIPS, with every field the two CDP types share round-tripping
exactly and nothing refused, 3 of 3 runs. It carries NO ``localStorage``, no
``sessionStorage``, no IndexedDB, no Cache Storage and no service-worker
registration — ``Storage.getCookies`` is a cookie call and there is no
"enumerate every origin" CDP command to build the rest on. A JWT-in-
``localStorage`` SPA does not come across. That list is repeated in
``spawn_browser``'s ``seed_from`` docstring, in ``click_target``'s voice: name
the facts it decides and refuse the rest out loud.

**And it carries the WHOLE jar.** ``Storage.getCookies`` is browser-wide — no
``urls=`` filter, no origin argument — so seeding from a session carries every
site that session is logged into, not the one the caller had in mind. That
matches what a file copy of a CLOSED source already does (a profile's jar is
also all-or-nothing), which is why it ships as the default rather than behind a
flag; an origin allow-list is named as a later opt-in in the finding's §6 and is
deliberately not built here.

**Why the generated types and not a raw socket.** The measurement drove CDP over
a websocket of its own because ``network.Cookie.from_json`` read a field Chrome
153 had retired and killed the connection's listener — F-902, fixed since, in
``cdp_transport``: half 2 guards the delivery, half 3 supplies the retired
``sameParty``. So the jar goes through ``Connection.send`` like every other
command in this tree, which is the one door ``cdp_transport`` protects. A second
transport here would be a second answer to "how does a CDP reply reach us", one
layer below the module that exists to make that answer safe.

**``same_party`` is deliberately NOT carried, and that is F-902's fix read from
the other side.** ``_RETIRED_COOKIE_FIELDS`` SYNTHESISES ``sameParty: False``
for a Chrome that no longer sends it, so every cookie read here carries a value
Chrome did not give us; ``CookieParam.to_json`` would then write that invention
straight back. ``expires`` is the other field held out of the derived set, for
the opposite reason — it needs a translation rather than a pass-through (see
``as_param``).

**PII: no cookie NAME and no cookie VALUE may leave this module.** Not in a
return, not in an exception, not in a log line, not in a Sentry breadcrumb.
A jar is the credentials themselves, and a NAME alone identifies (``NID`` names
Google; a list of names is a list of the sites its human uses). Everything here
reports counts, the CDP METHOD and an exception TYPE —
``cdp_transport._report``'s discipline, applied to the one subsystem whose whole
payload is secrets. This is also why the caller must NOT pass a failure here to
``debug_logger.log_warning(error=...)``: that forwards ``exc_info``, and the
exception behind a failed ``Storage.setCookies`` is Chrome's answer to a command
whose parameters WERE the jar. The type and the method name the bug class; the
traceback would name the payload.

A leaf: ``nodriver`` and ``profile_seed`` (for the one path comparison), both
browsers and the manager arrive as arguments, and it decides nothing about which
session may be seeded from — that is ``profile_source``'s.
"""

import dataclasses
from collections.abc import Awaitable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypeVar

import nodriver as uc
from nodriver.core.connection import Connection

from stealth_chrome_devtools_mcp.embedded import profile_seed

#: What one CDP round trip in the hand-off answers with. A type variable rather
#: than ``Any`` so ``_step`` is transparent to its caller's types.
_T = TypeVar("_T")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nodriver import Browser
    from nodriver.cdp.network import Cookie, CookieParam

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

#: The two CDP methods this module sends, named because every message it emits
#: names the METHOD and never the payload. Both go to the BROWSER-level
#: connection: measured, ``Storage.getCookies`` on a page session never answers
#: at all, which is the first thing a plausible hand-off gets wrong because
#: every other cookie surface in this tree is tab-scoped
#: (``network_interceptor.get_cookies`` is ``Network.getCookies``, a different
#: question — the cookies THIS page would send).
READ_METHOD = "Storage.getCookies"
WRITE_METHOD = "Storage.setCookies"

#: The one step of a hand-off that is NOT a CDP round trip, named in the same
#: breath as the two that are — see :func:`_translated` (review N1).
TRANSLATE_STEP = "CookieParam translation"

#: The word recorded for a seed taken this way, and the value
#: ``spawn_diagnostics.profile_selection.seeded_via`` reports on success.
VIA_CDP = "cdp-cookies"

#: …and what it reports when the hand-off failed: the session EXISTS and works,
#: it is simply missing the cookies, so the honest word is the mechanism that
#: did run.
VIA_COPY = "copy"

#: Held out of the DERIVED carried set, each for its own reason.
#: ``same_party`` is F-902's synthesised field (see the module docstring) and
#: forwarding it would write an invention back to Chrome. ``expires`` needs a
#: translation rather than a pass-through and ``as_param`` makes it.
_NOT_CARRIED = frozenset({"same_party", "expires"})


def _carried_fields() -> tuple[str, ...]:
    """Every ``Network.Cookie`` field with a ``Network.CookieParam``
    counterpart, read from nodriver's OWN generated dataclasses so nothing here
    is typed by hand and a CDP bump is visible rather than silent.

    What the derivation drops without being told to: ``size``, ``session`` and
    ``partition_key_opaque``, which are read-only report fields with no
    ``CookieParam`` counterpart, and ``url``, which is ``CookieParam``'s alone
    — a jar read already carries ``domain``/``path``, and inventing a ``url``
    for a cookie that arrived without one would change which host it belongs to.
    """
    param_names = {
        field.name for field in dataclasses.fields(uc.cdp.network.CookieParam)
    }
    return tuple(
        field.name
        for field in dataclasses.fields(uc.cdp.network.Cookie)
        if field.name in param_names and field.name not in _NOT_CARRIED
    )


#: Measured on Chrome 153 as the wire names ``name value domain path secure
#: httpOnly sameSite priority sourceScheme sourcePort partitionKey`` — all
#: eleven round-trip exactly, ``partitionKey`` with both sub-fields intact.
CARRIED_FIELDS = _carried_fields()


def as_param(cookie: "Cookie") -> "CookieParam":
    """One jar entry as the ``CookieParam`` that puts it back.

    A verbatim pass-through over :data:`CARRIED_FIELDS`, plus ``expires`` ONLY
    when it is a real time. CDP reports a SESSION cookie's expiry as ``-1``,
    which is a marker and not a date: forwarding it is a claim about 1969, and
    omitting it is what makes the target treat the cookie as a session cookie
    too (measured — the target then reports ``session: true``). That is the one
    translation in the hand-off and it is the one shape a file copy can never
    carry at all, because a session cookie is never written to the jar on disk.
    """
    param = uc.cdp.network.CookieParam(
        **{name: getattr(cookie, name) for name in CARRIED_FIELDS}
    )
    if not is_session(cookie):
        param.expires = uc.cdp.network.TimeSinceEpoch(cookie.expires)
    return param


def is_session(cookie: "Cookie") -> bool:
    """A cookie with no REAL expiry — CDP's ``-1`` marker, or none sent at all.

    One home for that test because two sites make it and they must agree: the
    translation omits the field, and the record counts how many were like that.
    """
    expires = getattr(cookie, "expires", None)
    return expires is None or expires <= 0


def params(cookies: Iterable["Cookie"]) -> list["CookieParam"]:
    """The whole jar as the batch ``Storage.setCookies`` takes."""
    return [as_param(cookie) for cookie in cookies]


def _connection(browser: "Browser") -> Connection:
    """The BROWSER-level CDP connection, or a refusal naming neither.

    ``getattr`` rather than an attribute read because nodriver sets
    ``Browser.connection`` to ``None`` in ``__init__`` and fills it during
    ``start``; a browser that never got there has nothing to ask.

    A ``HandoffError`` and not a bare ``RuntimeError`` (review S2): ``failure``
    repeats the text of this class and of nothing else, so a ``RuntimeError``
    here was reported as ``RuntimeError from Storage.getCookies`` — telling an
    operator a CDP call failed when the truth is that there was no connection
    to make one on. The text is ours and carries no payload, which is exactly
    the condition under which repeating it is allowed.
    """
    connection = getattr(browser, "connection", None)
    if connection is None:
        raise HandoffError("browser has no CDP connection")
    return connection


async def read_jar(browser: "Browser") -> list["Cookie"]:
    """Every cookie in the BROWSER — not in a page, not for a url."""
    return await _connection(browser).send(uc.cdp.storage.get_cookies()) or []


async def write_jar(browser: "Browser", cookies: Sequence["CookieParam"]) -> None:
    """Put a whole jar into the BROWSER, in ONE call.

    ``Storage.setCookies`` gives no per-cookie verdict — one answer for the
    batch — so there is nothing to read back here and the caller counts what
    landed instead. Measured: nothing was refused, 3 of 3 runs, and every
    cookie offered alone afterwards was already there.
    """
    await _connection(browser).send(uc.cdp.storage.set_cookies(cookies=list(cookies)))


class HandoffError(RuntimeError):
    """A hand-off that did not complete, carrying a SHAPE-ONLY reason.

    The one exception type this module raises, and the only thing whose text is
    ever repeated anywhere (:func:`failure`). Everything else is reported by its
    TYPE alone, because an exception raised beneath us is Chrome's answer to a
    command whose parameters were the jar, and no message from there may be
    quoted back. Deliberately a ``RuntimeError`` and not a ``ToolError``: a
    failed hand-off is REPORTED, never raised at a caller, so it must not join
    the class whose whole meaning is "a tool said no".
    """


def _failed(method: str, error: BaseException) -> HandoffError:
    """Name the CDP method that failed and the TYPE that failed it.

    Built from the type NAME only, so the instance behind it is not referenced
    and cannot be reached from the raised error — ``cdp_transport``'s rule for
    the same payload, which is why the caller raises this OUTSIDE its ``except``
    as well (a chained ``__context__`` is a second door onto the same string).
    """
    return HandoffError(f"{type(error).__name__} from {method}")


async def _step(method: str, awaitable: Awaitable[_T]) -> _T:
    """One CDP round trip, with a failure that names WHICH one.

    The method is read off the step that actually failed and never set ahead of
    time by the caller: a hand-off is three round trips, and a caller stamping
    a method before the call attributed a failed READ to ``Storage.setCookies``
    (caught by ``tests/test_cookie_handoff.py``) — a diagnostic pointing at the
    wrong half of the mechanism is worse than none.

    A ``HandoffError`` from INSIDE the step is re-raised untouched (review S2).
    It is the one exception class this module writes, so its text is already
    shape-only AND already more specific than anything that could be wrapped
    around it — ``browser has no CDP connection`` says what happened, while
    ``HandoffError from Storage.getCookies`` says a CDP call failed, which is
    not what happened at all. A bare ``raise`` re-raises the SAME object, so it
    chains to nothing and the PII argument below is untouched.
    """
    failure_to_raise: HandoffError | None = None
    try:
        return await awaitable
    except HandoffError:
        raise
    except Exception as error:  # noqa: BLE001  PERMANENT(F-898): every CDP failure becomes one shape-only report
        failure_to_raise = _failed(method, error)
        del error
    raise failure_to_raise from None


def _translated(jar: Sequence["Cookie"]) -> list["CookieParam"]:
    """:func:`params`, under ``_step``'s naming discipline and without an await.

    The translation is the one step of a hand-off that is not a round trip, and
    it was the one step with no name on its failure (review N1): a cookie whose
    field nodriver's ``CookieParam`` will not take raises HERE, between the two
    CDP calls, and the caller reported it as a bare type — the same diagnostic
    gap ``_step`` exists to close, one statement away from it. It reuses
    ``_failed``, so there is ONE phrasing of "which step, which type" and not a
    second one, and raises OUTSIDE the ``except`` for ``_failed``'s own reason:
    a chained ``__context__`` is a second door onto whatever the failing cookie
    put in the original message.

    A sync sibling rather than a third argument to ``_step``, which takes an
    awaitable: wrapping this in a coroutine to reach that function would add a
    round trip's shape to something that makes none.
    """
    failure_to_raise: HandoffError | None = None
    try:
        return params(jar)
    except Exception as error:  # noqa: BLE001  PERMANENT(F-898): every failure becomes one shape-only report
        failure_to_raise = _failed(TRANSLATE_STEP, error)
        del error
    raise failure_to_raise from None


class Handoff(NamedTuple):
    """What the hand-off moved, in counts and nothing else.

    ``jar_after`` is the TARGET's whole jar afterwards and is deliberately not
    described as "how many of ours landed": the target is a real browser and
    Chrome's own startup fetches put cookies in a seconds-old profile (measured
    — a Google ``NID`` appeared in one run of three). Reporting the number we
    can actually see keeps the record true of what it counted.
    """

    read: int
    sent: int
    jar_after: int
    session_cookies: int
    partitioned: int

    def record(self) -> dict[str, object]:
        """The diagnostics fields, shape only — see the module docstring."""
        return {
            "seeded_via": VIA_CDP,
            "cookies_carried": self.sent,
            "cookies_read": self.read,
            "cookies_session": self.session_cookies,
            "cookies_partitioned": self.partitioned,
            "cookies_in_target": self.jar_after,
        }


async def hand_off(source: "Browser", target: "Browser") -> Handoff:
    """Read the running SOURCE's jar and write it into the TARGET.

    Three round trips and no deadline of its own: the caller's
    ``rt._with_cdp_timeout`` is the bound, on ``script_evaluation``'s rule that
    a second deadline is a second answer to one question. The third trip is the
    read-back, which is what makes the record a count of something observed
    rather than of something sent.
    """
    jar = await _step(READ_METHOD, read_jar(source))
    outgoing = _translated(jar)
    await _step(WRITE_METHOD, write_jar(target, outgoing))
    landed = await _step(READ_METHOD, read_jar(target))
    return Handoff(
        read=len(jar),
        sent=len(outgoing),
        jar_after=len(landed),
        session_cookies=sum(1 for cookie in jar if is_session(cookie)),
        partitioned=sum(
            1 for cookie in jar if getattr(cookie, "partition_key", None) is not None
        ),
    )


def failure(error: BaseException) -> str:
    """THE one phrasing of a failed hand-off, and the one PII boundary it has.

    A :class:`HandoffError` is the only exception whose TEXT is repeated,
    because this module is the only thing that writes it and everything it
    writes is shape. Anything else is reported by its TYPE alone — the text of
    a ``Storage.setCookies`` failure is Chrome's answer to a command whose
    parameters were the jar, and that string reaches the tool's answer, the
    durable log and Sentry at once. ``cdp_transport``'s rule for the same
    payload, which is where a cookie-carrying reply's own re-raise was found
    interpolating the whole jar.
    """
    if isinstance(error, HandoffError):
        return str(error)
    return type(error).__name__


class Driven:
    """Which profile directories THIS backend is currently driving a browser on.

    A SNAPSHOT and not a live view, taken once per spawn so the ``seed_from``
    pre-flight and the resolver ask the same question of the same answer and
    cannot disagree — the discipline
    ``require_allowed_user_data_dir`` already has for the directory a request
    MEANS. What it can go stale about resolves the safe way: a source that
    closed between the snapshot and the hand-off costs the new session its
    cookies and says so, while a source that OPENED cannot make a refusal into
    a permission.

    ``holds`` is the predicate ``profile_source.seed_source`` asks; ``instance``
    is what the hand-off needs afterwards. Both compare through
    ``profile_seed.same_dir``, the tree's one path comparison, because a caller
    may name a session and a spawn may have recorded an absolute path for it.
    """

    def __init__(self, profiles: Sequence[tuple[Path, str]] = ()) -> None:
        self._profiles = tuple(profiles)

    def instance(self, profile: Path) -> str | None:
        """The instance id of a browser of ours on that directory, else None."""
        for driven, instance_id in self._profiles:
            if profile_seed.same_dir(driven, profile):
                return instance_id
        return None

    def holds(self, profile: Path) -> bool:
        return self.instance(profile) is not None


#: Fail-closed default for every caller that does not supply a witness. Reading
#: a browser's cookie jar is never something an omitted argument may authorise,
#: so "nobody asked" resolves to 2.1.12's refusal.
NOTHING_DRIVEN = Driven()


async def driven_profiles(manager: "BrowserManager") -> Driven:
    """Snapshot the directories this backend drives a browser on.

    Over ``list_instances``, the manager's one public reading of what exists —
    which also discards an instance whose browser process has died, so a Chrome
    that is gone can never be offered as a hand-off source.

    **The directory is the entry's ``options``, and NOT ``BrowserInstance``**,
    which has no such field: it carries ``instance_id``, ``state``, ``headless``,
    ``viewport``, ``user_agent``, the two ``last_navigated_*`` caches and the two
    timestamps, and nothing about where the profile is. Reaching for
    ``instance.user_data_dir`` therefore finds nothing on every real instance —
    silently, since ``getattr`` answers None — and the whole hand-off never
    fires. It was written that way in the first draft and the hermetic pins
    PASSED, because the double offered the attribute the product does not have;
    ``ty`` is what caught it. Both writers of ``_instances`` put the same
    ``BrowserOptions`` under ``"options"`` (``browser_manager._launch_browser``
    and ``browser_reattach``'s adoption), so one reading covers a spawn and an
    adopted browser alike. An entry that cannot say which directory it drives is
    not a candidate and is skipped rather than guessed at.
    """
    profiles: list[tuple[Path, str]] = []
    for instance in await manager.list_instances():
        entry = await manager.get_instance(instance.instance_id)
        directory = getattr((entry or {}).get("options"), "user_data_dir", None)
        if directory:
            profiles.append((Path(directory), instance.instance_id))
    return Driven(profiles)
