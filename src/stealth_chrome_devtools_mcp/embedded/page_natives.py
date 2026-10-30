"""THE one home for "ask a page node something through the browser's own code".

(T-10284, T-10299.)

Every page-side script of the click and listing paths asks a node for something
(``isConnected``, its client rects, its root, ``click()``). Run in the page's own
JavaScript world, every such question is answered by the PAGE: it owns every
prototype and every global the script touches. A ``<form>`` is
``[LegacyOverrideBuiltIns]`` (a named control shadows any member of it); a page's
top-level ``class Node {}`` replaces the ``Node`` global; ``EventTarget.prototype``
can gain an ``isConnected`` that wins over the intact ``Node.prototype`` one;
``Object.prototype.get = ...`` is inherited by every property descriptor;
``getComputedStyle`` can be replaced by a function that throws ``true``. Each of
those was a successful, wrong answer, and each round of review found the next.

So :func:`run` does not run in the page's world. It runs the script in an
ISOLATED WORLD of the node's frame (``Page.createIsolatedWorld``): the same DOM,
its own JavaScript globals and prototypes, which the page's scripts cannot reach.
In that world ``Node``, ``Object``, ``getComputedStyle`` and every prototype are
the browser's originals, so nothing in a page can steer an answer, and no script
here carries a guard against a page. What is left is the one thing a world does
not change: a ``<form>`` still answers ``form.isConnected`` with its named
control, because that is the DOM object's own behaviour in every world. So a
script asks the interface (:data:`NATIVE_JS`, ``native(Node, 'isConnected')``),
never the node, and that is correct because the interface is pristine here.

**Which scripts run here.** Every script that only ASKS (the connectivity token,
the visibility read, the aim read) and the two that act on the click path -- the
synthetic ``el.click()`` (the pristine ``HTMLElement.prototype.click`` runs, so a
custom element's own ``click()`` override is not called, and the page's listeners
still hear the click: DOM events cross worlds) and the input probe (a capturing
listener on the frame's ``window``, which receives the page's real events and is
invisible to the page). The user-gesture flag is the caller's: ``False`` for a
read (MEASURED: a read with ``userGesture`` true gives the page transient user
activation after ONE connectivity read), ``True`` for the click path, as 2.1.53
sent it. Nothing stays in the page's world on the click path; text entry and
selection (``text_entry``, ``control_state``) are other modules' business and are
unchanged.

**One world per frame, asked for every time.** ``Page.createIsolatedWorld`` for
one frame and one name is idempotent -- it answers the live context, and the
context a navigation replaced -- so there is no cached context id to go stale, and
the choice between "invalidate on ``executionContextDestroyed``" and "retry on
``Cannot find context``" does not arise.

**Which frame holds the node, and the order the frames are tried in.** Nothing
cheap names it: ``DOM.describeNode`` carries no frame id for an element or its
document (MEASURED, headless Chrome 156, on an in-process cross-origin frame, a
``data:`` frame and a ``srcdoc`` frame), and the object a ``DOM.resolveNode``
gives is a stranger's until a script looks at ``ownerDocument``. So the node's
frame is found by asking, and every frame answers ONE of two things:

* *here*: the script ran (every script is wrapped, :data:`WRAPPER_JS`, so that it
  answers :data:`FOREIGN` unless the node's ``ownerDocument`` is the document of
  the world it was resolved in);
* *not here*: the script answered :data:`FOREIGN`; or ``DOM.resolveNode`` in this
  frame's world gave NO object (MEASURED: an isolated world answers a node of an
  in-process frame of ANOTHER origin, or a ``data:`` frame, with a null object --
  no ``objectId`` -- while a same-origin ``srcdoc`` frame's nodes resolve in the
  main frame's world and are told apart by the script); or Chrome said the node is
  not in this frame's document or the frame itself has gone (``_NOT_HERE_MARKERS``,
  e.g. a sibling frame navigating between the two commands).

"Not here" for a frame is never an answer about the node: the search goes on to
the next frame. The node has left only when Chrome says so about the node itself
(``No node with given id found``) or when every frame said "not here"
(:class:`FrameGone`). The order: the frame that last answered for this tab first
(a page's nodes cluster: a listing of an iframe's rows, a form in a widget), then
the page's frames from the main one down, depth first. The common read is one
attempt and sends no ``Page.getFrameTree`` once a frame is remembered; a node
elsewhere costs the attempts before its frame, and the one that answers is
remembered. Never ``-32602 Either objectId or ... must be specified``: a call
without an object is never sent, so that reply could only be our own bug and is
a ``ReadError``.

**A failed read is a failed read.** ``exceptionDetails`` of the reply is a throw,
and :func:`run` raises ``ToolError`` for it whatever ``result`` holds (a thrown
``true`` arrives as a perfectly good ``true`` value). A transport failure is the
same: Chrome saying the node or its context is gone (:func:`departed`) is the
departure ``detached_element`` classifies, every other failure is a
``ToolError``. The resolved remote object lives in an object group of the
call. A read that FINISHED releases it at once, waiting at most
:data:`RELEASE_WAIT_S`. A read that FAILED or was CANCELLED releases nothing and
raises at once, unmodified (a ``CancelledError`` stays one): it records its group
on the tab (:data:`_LEFTOVERS`) and the NEXT read on that tab releases the
recorded groups first, together, within one :data:`RELEASE_WAIT_S` (a release that
fails or times out is logged and dropped, never retried and never the read's own
result). No task is spawned: a read abandoned by its caller at ``INPUT_BUDGET_S``
because its page is blocked (a ``confirm()`` in a click listener, a busy loop)
is not held behind that page, and nothing outlives the event loop that a
shutdown cancelled it in. A group left by a failed read lives until the tab's
next read, its navigation or its close; Chrome frees a document's groups on
navigation (MEASURED, headless Chrome 156, for a same-origin and a ``data:``
navigation: ``Runtime.callFunctionOn`` on an ``objectId`` taken in a group before
``Page.navigate`` is callable until it and answers ``Cannot find context with
specified id`` afterwards); a closed tab takes its contexts with it (not measured
separately). The list is capped at :data:`MAX_LEFTOVERS` per tab, the oldest
dropped with a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from typing import TYPE_CHECKING

from nodriver import cdp
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from nodriver import Element, Tab

#: The one isolated world every read of the package uses (per frame).
WORLD_NAME = "stealth-mcp-reads"

#: What a wrapped script answers when the node belongs to ANOTHER frame's
#: document: :func:`run` then tries the next frame. Ours, never a page's.
FOREIGN = "stealth.read:foreign"

# Chrome's answers when a node a caller HOLDS is no longer in the document
# (T-10034, STEALTH-...-ER and -EP), or the document or context it was resolved
# in has been replaced between two commands of one read (a navigation, T-10284).
# nodriver addresses a held ``Element`` by its backend node id through
# ``DOM.resolveNode`` (``apply``, ``click``, ``get_position``, ``update`` all
# start there): a node the page removed answers the first, a node the page
# detached but still references answers the second, and the third is what a
# ``Runtime.callFunctionOn`` on an object of a context that navigated away says.
# Deliberately NOT in ``element_resolution``'s stale-node markers: those say a
# RESOLUTION raced and re-running it is always right, these say the element a
# caller is holding is gone, and whether re-running the caller's work is safe is
# the caller's question (a click that may have been dispatched must not be).
#
# ``-32602 Either objectId or executionContextId or uniqueContextId must be
# specified`` is NOT here: it is what a call without an
# ``objectId`` gets, which a null ``DOM.resolveNode`` reply produced when the node
# lives in a frame of another origin. That is a frame saying "not here", handled
# before a call is built; the reply itself could only be our own bug.
_DEPARTED_MARKERS = (
    "No node with given id found",
    "Node with given id does not belong to the document",
    "Cannot find context with specified id",
)

# What Chrome says to ONE frame's attempt that is about the frame, not the node:
# the frame is gone (``createIsolatedWorld``), or the node is not in the document
# of the world it was just asked of / that world was replaced before the
# resolution (``resolveNode``, e.g. a sibling frame navigating between the two
# commands). The search goes on to the next frame. For those two commands ONLY:
# the same words from ``callFunctionOn`` are the node's own context leaving.
_NOT_HERE_MARKERS = (
    "No frame for given id found",
    "Node with given id does not belong to the document",
    "Cannot find context with specified id",
)

#: How long a read waits for the release of its object group once it is over.
RELEASE_WAIT_S = 1.0

#: How many object groups of failed reads a tab remembers for its next read to
#: release; past it the oldest is dropped (with a warning).
MAX_LEFTOVERS = 32

#: The attribute of a ``Tab`` that holds the object groups its failed or
#: cancelled reads left (a list: a ``Tab`` is unhashable, as with the frame hint).
_LEFTOVERS = "_stealth_reads_leftovers"

#: What a frame's attempt answers when the node is not in that frame.
_NOT_HERE = object()

#: The attribute of a ``Tab`` that remembers the frame that last answered for it.
_FRAME_HINT = "_stealth_reads_frame"

_groups = itertools.count(1)


class ReadError(ToolError):
    """A read of a held node failed: the script threw, or a command failed.

    A ``ToolError`` for every caller that reports it; a distinct type for the one
    that must tell it from a ``ToolError`` of its own making (``dom_handler``'s
    click, which says "the input may have reached the page" when one comes out
    after the first input).
    """


class FrameGone(ProtocolException):
    """No frame of the page holds the node: its frame, and so it, has left."""


def departed(exc: BaseException | None) -> bool:
    """Whether *exc* is Chrome saying the held node (or its context) is gone."""
    if isinstance(exc, FrameGone):
        return True
    return isinstance(exc, ProtocolException) and any(
        marker in str(exc) for marker in _DEPARTED_MARKERS
    )


#: ``native(iface, name)``: the accessor's getter for an accessor, the function
#: for a method, of ``iface.prototype`` -- ``native(Node, 'isConnected')``. Called
#: as ``.call(node)``. Only meaningful in the isolated world, where ``iface`` and
#: ``Object.getOwnPropertyDescriptor`` are the browser's own.
NATIVE_JS = """const native = (iface, name) => {
        const own = Object.getOwnPropertyDescriptor(iface.prototype, name);
        return own.get !== undefined ? own.get : own.value;
    };"""

#: What :func:`run` wraps every script in: ``native`` defined once for the script,
#: and the :data:`FOREIGN` answer for a node of another frame's document.
WRAPPER_JS = """(elem) => {
    __NATIVE__
    if (native(Node, 'ownerDocument').call(elem) !== document) return "__FOREIGN__";
    return (__SCRIPT__)(elem);
}""".replace("__NATIVE__", NATIVE_JS).replace("__FOREIGN__", FOREIGN)

#: ``getRootNode`` and ``shadow(root, 'host' | 'mode')``, which answers ``null``
#: for any root that is not a shadow root: what a script needs to climb out of
#: shadow roots. A shadow root is recognised by ``nodeType`` 11 and the
#: ``ShadowRoot`` accessor's own brand check (a plain ``DocumentFragment`` throws
#: there), NOT by ``instanceof ShadowRoot``: an isolated world is one wrapper
#: world shared by the page's frames, so the wrapper of a node first read through
#: one frame's context keeps THAT frame's prototypes when it is read through
#: another's (MEASURED: ``root instanceof ShadowRoot`` was false for a closed root
#: inside an iframe), while a brand check looks at the object, not its prototype.
ROOTS_JS = """const getRootNode = native(Node, 'getRootNode');
    const nodeType = native(Node, 'nodeType');
    const shadow = (root, name) => {
        if (nodeType.call(root) !== 11) return null;
        try { return native(ShadowRoot, name).call(root); } catch (e) { return null; }
    };"""

#: The NATIVE accessor or method for every member the aim read takes off a node,
#: each looked up ONCE at the top of the script that uses it.
NATIVES_JS = (
    """__ROOTS__
    const ownerDocument = native(Node, 'ownerDocument');
    const nodeName = native(Node, 'nodeName');
    const contains = native(Node, 'contains');
    const tagName = native(Element, 'tagName');
    const idOf = native(Element, 'id');
    const classListOf = native(Element, 'classList');
    const getClientRects = native(Element, 'getClientRects');
    const matches = native(Element, 'matches');
    const closest = native(Element, 'closest');
    const elementFromPoint = native(Document, 'elementFromPoint');"""
).replace("__ROOTS__", ROOTS_JS)


def _frame_ids(tree: cdp.page.FrameTree) -> list[cdp.page.FrameId]:
    """The frame ids of *tree*, the main frame first, then depth first."""
    ids = [tree.frame.id_]
    for child in tree.child_frames or []:
        ids.extend(_frame_ids(child))
    return ids


async def _release_bounded(tab: Tab, group: str, seconds: float) -> None:
    """Release *group*, waiting at most *seconds*; it never raises and never masks.

    A release that fails must not replace the read's own result or error (a
    navigation already freed the group), and one that does not answer -- the page
    is blocked behind a dialog or a busy loop -- must not hold the caller: it is
    logged and abandoned.
    """
    try:
        await asyncio.wait_for(
            tab.send(cdp.runtime.release_object_group(group)), seconds
        )
    except TimeoutError:
        debug_logger.log_warning(
            "page_natives",
            "_release_bounded",
            f"object group {group} was not released within {seconds:g} s",
        )
    except Exception as exc:  # noqa: BLE001  PERMANENT(a failed release must not replace the read's own result or error; a navigation already freed the group)
        debug_logger.log_warning(
            "page_natives",
            "_release_bounded",
            f"could not release object group {group}: {type(exc).__name__}",
        )


def _leave(tab: Tab, group: str) -> None:
    """Record *group* on *tab* for its next read to release (a failed read)."""
    left = getattr(tab, _LEFTOVERS, None)
    if left is None:
        left = []
        try:
            setattr(tab, _LEFTOVERS, left)
        except AttributeError:
            return  # a tab that takes no attributes remembers nothing
    left.append(group)
    if len(left) > MAX_LEFTOVERS:
        dropped = left.pop(0)
        debug_logger.log_warning(
            "page_natives",
            "_leave",
            f"object group {dropped} dropped unreleased: {MAX_LEFTOVERS} are waiting",
        )


async def _release_leftovers(tab: Tab) -> None:
    """Release the groups earlier failed reads of *tab* left, in one bounded wait.

    They are taken off the tab first (a concurrent read does not release them
    twice) and released together, so the wait is :data:`RELEASE_WAIT_S` however
    many there are. A read cancelled during that wait puts back every group whose
    release did not finish, so the next read still releases it (releasing a
    group twice is harmless; forgetting one keeps it until a navigation).
    """
    left = getattr(tab, _LEFTOVERS, None)
    if not left:
        return
    groups = list(left)
    left.clear()
    finished: set[str] = set()

    async def release(group: str) -> None:
        await _release_bounded(tab, group, RELEASE_WAIT_S)
        finished.add(group)

    try:
        await asyncio.gather(*(release(g) for g in groups))
    except asyncio.CancelledError:
        for group in groups:
            if group not in finished:
                _leave(tab, group)
        raise


def _says_not_here(exc: BaseException) -> bool:
    return isinstance(exc, ProtocolException) and any(
        marker in str(exc) for marker in _NOT_HERE_MARKERS
    )


async def _in_frame(
    element: Element, frame_id: cdp.page.FrameId, script: str, user_gesture: bool
) -> object:
    """One attempt: the wrapped *script* on *element*, in the world of *frame_id*.

    Answers the script's value, or :data:`_NOT_HERE` when the node is not in this
    frame (see the module docstring); ``exceptionDetails`` is raised, never
    returned.
    """
    tab = element.tab
    try:
        world = await tab.send(
            cdp.page.create_isolated_world(
                frame_id, world_name=WORLD_NAME, grant_univeral_access=False
            )
        )
    except ProtocolException as exc:
        if _says_not_here(exc):
            return _NOT_HERE
        raise
    group = f"{WORLD_NAME}:{next(_groups)}"
    try:
        try:
            remote = await tab.send(
                cdp.dom.resolve_node(
                    backend_node_id=element.backend_node_id,
                    object_group=group,
                    execution_context_id=world,
                )
            )
        except ProtocolException as exc:
            if _says_not_here(exc):
                return _NOT_HERE
            raise
        if remote.object_id is None:
            # The world answers a node of a frame it cannot reach with a null
            # object, and registers nothing in the group.
            return _NOT_HERE
        result, thrown = await tab.send(
            cdp.runtime.call_function_on(
                script,
                object_id=remote.object_id,
                arguments=[cdp.runtime.CallArgument(object_id=remote.object_id)],
                return_by_value=True,
                user_gesture=user_gesture,
            )
        )
    except BaseException:
        _leave(tab, group)
        raise
    await _release_bounded(tab, group, RELEASE_WAIT_S)
    if thrown is not None:
        raise ReadError(
            "the page-side read threw: "
            f"{(thrown.exception and thrown.exception.description) or thrown.text}"
        )
    value = result.value if result is not None else None
    return _NOT_HERE if value == FOREIGN else value


async def _search(element: Element, script: str, user_gesture: bool) -> object:
    """The frame search: the remembered frame, then the page's frames in order."""
    tab = element.tab
    remembered = getattr(tab, _FRAME_HINT, None)
    tried: list[cdp.page.FrameId] = []
    steps = ([remembered] if remembered is not None else []) + [None]
    for step in steps:
        if step is None:
            tree = await tab.send(cdp.page.get_frame_tree())
            frames = [f for f in _frame_ids(tree) if f not in tried]
        else:
            frames = [step]
        for frame_id in frames:
            tried.append(frame_id)
            value = await _in_frame(element, frame_id, script, user_gesture)
            if value is not _NOT_HERE:
                # a tab that takes no attributes remembers nothing
                with contextlib.suppress(AttributeError):
                    setattr(tab, _FRAME_HINT, frame_id)
                return value
    raise FrameGone("no frame of the page holds the node")


async def run(
    element: Element, js_function: str, *, user_gesture: bool = False
) -> object:
    """THE one send of a script that asks (or acts on) a held node: its value.

    *js_function* is ``(elem) => { ... }``; it runs in the isolated world of the
    node's frame (see the module docstring), wrapped by :data:`WRAPPER_JS`.
    ``user_gesture`` is ``Runtime.callFunctionOn``'s flag: ``False`` for a read,
    ``True`` for the click path.

    Raises ``ReadError`` (a ``ToolError``) for a script that threw (whatever it
    threw: a thrown ``true`` has a value, and the value is not an answer) and for
    any failure of the commands that is not Chrome saying the node is gone.
    Chrome's own departure replies (:func:`departed`) propagate as raised, for
    ``element_resolution.detached_element`` to classify; so does
    :class:`FrameGone`. The groups an earlier failed read of the tab left are
    released first (:func:`_release_leftovers`).
    """
    script = WRAPPER_JS.replace("__SCRIPT__", js_function)
    await _release_leftovers(element.tab)
    try:
        return await _search(element, script, user_gesture)
    except ReadError:
        raise
    except Exception as exc:
        if departed(exc):
            raise
        raise ReadError(
            f"could not read the node: {type(exc).__name__}: {exc}"
        ) from exc
