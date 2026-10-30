"""THE one home for "read a member of a page node through the browser's own code".

(T-10284.)

Every page-side script of the click and listing paths asks a node for something
(``isConnected``, its client rects, its root, ``click()``). Asked as
``elem.<name>`` the page answers: an HTML ``<form>`` is
``[LegacyOverrideBuiltIns]``, so a named control shadows ANY member, with no
page script involved. Asked as ``Object.getOwnPropertyDescriptor(Node.prototype,
name)`` the PAGE still answers, through its own global: ``Node`` is a writable,
configurable global, so an ordinary top-level ``class Node {}`` /
``function Node() {}`` / ``var Node = ...`` (a linked-list demo) turns such a
read into a ``TypeError``. That is what 2.1.53's click scripts did
(``click_element``: ``AIM_JS``, ``ARM_PROBE_JS``, ``SYNTHETIC_JS`` read
``Node.prototype``, ``Element.prototype``, ``HTMLElement.prototype`` and
``ShadowRoot``); the scripts that list, describe and wait read no interface.
Since this change those also ask connectivity, and a version that read
``Node.prototype`` would have made ``query_elements`` and ``get_element_state``
fail on such a page too, which is why no script here names a DOM interface.

:data:`NATIVE_JS` finds the member on the NODE's own prototype chain, starting
one step up (a form's named controls live on the form object, which the walk
never reads) and taking the FARTHEST owner of the name, short of the chain's last
prototype (``Object.prototype``). The farthest owner is the interface's own
member: a page's class, a ``setPrototypeOf`` or an accessor it adds on a
prototype nearer the node can only sit NEARER, and so can pollution of
``Object.prototype``, which is not read. On an unmodified chain every name these
scripts read has exactly one owner, so nothing changes there. The boundary until
T-10299 (an isolated world) is a page that replaces the ORIGINAL accessor on
the farthest prototype, or the realm's own ``Object.getOwnPropertyDescriptor`` /
``Object.getPrototypeOf`` / ``Function.prototype.call``: any read in the page's
world sees that, and so does a page that cuts a node off from the DOM
interfaces altogether (``setPrototypeOf(el, {get isConnected() {...}})``: a
chain that no longer reaches ``Node.prototype`` is read as it is, MEASURED to
answer ``yes`` for a removed node). A page class or an accessor added nearer the
node, on a chain that still reaches the interfaces, is not that boundary.

Three fragments, each splicing the one before it, so ``native`` is defined once
per script scope:

* :data:`NATIVE_JS` -- ``native(obj, name)``, the walk. The accessor's getter for
  an accessor, the function for a method, ``undefined`` for a name nothing on
  the chain defines.
* :data:`ROOTS_JS` -- what a script needs to climb out of shadow roots:
  ``getRootNode`` and ``shadow(root, 'host' | 'mode')``, which answers ``null``
  for any root that is not a shadow root. It does not name ``ShadowRoot``
  either (``r instanceof ShadowRoot`` reads the same global).
* :data:`NATIVES_JS` -- every member :data:`click_target.AIM_JS` reads.

The fragments are spliced into a script of the form ``(elem) => { ... }``: they
read from ``elem``. :func:`read` is the one way such a read-only script is sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nodriver import cdp

if TYPE_CHECKING:
    from nodriver import Element


async def read(element: Element, js_function: str) -> object:
    """THE one send of a READ-ONLY script: ``Element.apply`` without a user gesture.

    ``Element.apply`` is ``DOM.resolveNode`` then ``Runtime.callFunctionOn`` with
    ``userGesture=True``, which MEASURED (headless Chrome, T-10284) gives the page
    transient user activation: ``navigator.userActivation.hasBeenActive`` is true
    after one connectivity read of one node. A listing or a state read must not
    do that to every element it touches, so the connectivity, visibility and aim
    reads are sent here instead: the same two commands, ``userGesture`` false,
    and ``Element.apply``'s own reading of the reply (the value, or what the
    script threw as a plain value, ``None`` for a thrown Error object). The
    script that clicks keeps ``Element.apply``.
    """
    tab = element.tab
    remote = await tab.send(
        cdp.dom.resolve_node(backend_node_id=element.backend_node_id)
    )
    value, thrown = await tab.send(
        cdp.runtime.call_function_on(
            js_function,
            object_id=remote.object_id,
            arguments=[cdp.runtime.CallArgument(object_id=remote.object_id)],
            return_by_value=True,
            user_gesture=False,
        )
    )
    return value.value if value else thrown or None


#: ``native(obj, name)``. Walks the WHOLE chain from ``Object.getPrototypeOf(obj)``
#: (never ``obj`` itself: that is where a form's named controls are) and answers
#: the FARTHEST own property of that name, leaving out the chain's last
#: prototype (the one whose own prototype is ``null``, ``Object.prototype``).
#: The farthest owner is the interface's own member, because a page's class, a
#: ``setPrototypeOf`` or an accessor it adds to a prototype nearer the node
#: can only sit NEARER than the built-in one; and on an unmodified chain every
#: name these scripts read has exactly one owner, so nothing changes there. The
#: caller applies the result with ``.call(obj)``; one that is ``undefined``
#: fails there, inside whatever ``try`` the caller already has.
NATIVE_JS = """const native = (obj, name) => {
        let found;
        for (let proto = Object.getPrototypeOf(obj); proto !== null;
             proto = Object.getPrototypeOf(proto)) {
            if (Object.getPrototypeOf(proto) === null) break;
            const own = Object.getOwnPropertyDescriptor(proto, name);
            if (own !== undefined) found = own.get !== undefined ? own.get : own.value;
        }
        return found;
    };"""

#: ``shadow(root, name)`` is ``host`` / ``mode`` of a shadow root and ``null`` for
#: a ``Document`` or a plain fragment (``nodeType`` 11 with no ``host`` on its
#: chain), so ``shadow(r, 'host') !== null`` is "r is a shadow root".
ROOTS_JS = """__NATIVE__
    const nodeType = native(elem, 'nodeType');
    const getRootNode = native(elem, 'getRootNode');
    const shadow = (root, name) => {
        const read = nodeType.call(root) === 11 ? native(root, name) : undefined;
        return read === undefined ? null : read.call(root);
    };""".replace("__NATIVE__", NATIVE_JS)

#: The NATIVE accessor or method for every member the click path reads off a
#: node, each read ONCE at the top of the script that uses it (Codex round 5).
#: Never ``elem.<name>`` (see the module docstring), and never through an
#: interface's global.
NATIVES_JS = (
    """__ROOTS__
    const ownerDocument = native(elem, 'ownerDocument');
    const nodeName = native(elem, 'nodeName');
    const contains = native(elem, 'contains');
    const tagName = native(elem, 'tagName');
    const idOf = native(elem, 'id');
    const classListOf = native(elem, 'classList');
    const getClientRects = native(elem, 'getClientRects');
    const matches = native(elem, 'matches');
    const closest = native(elem, 'closest');
    const doc = ownerDocument.call(elem);
    const elementFromPoint = native(doc, 'elementFromPoint');"""
).replace("__ROOTS__", ROOTS_JS)
