"""THE one home for "read a member of a page node through the browser's own code".

(T-10284.)

Every click-path script asks a node for something (``isConnected``, its client
rects, its root, ``click()``). Asked as ``elem.<name>`` the page answers: an
HTML ``<form>`` is ``[LegacyOverrideBuiltIns]``, so a named control shadows ANY
member, with no page script involved. Asked as
``Object.getOwnPropertyDescriptor(Node.prototype, name)`` the PAGE still
answers, through its own global: ``Node`` is a writable, configurable global,
so an ordinary top-level ``class Node {}`` / ``function Node() {}`` /
``var Node = ...`` (a linked-list demo) turns every such read into a
``TypeError`` -- on measured Chrome every element then answered the
connectivity read's ``threw`` token, and ``query_elements``,
``get_element_state`` and ``click_element`` failed on the page.

So no script here names a DOM interface. :data:`NATIVE_JS` finds the member by
walking the NODE's own prototype chain, starting one step up: a form's named
controls live on the form object itself, which the walk never reads, and a page
that merely declares a global with an interface's name changes nothing about
the chain a node already has. A page that REPLACES the prototypes' members
themselves is the accepted boundary until T-10299 (an isolated world): the
walk finds the replacement, as any read in the page's world would.

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

Both fragments are spliced into a script of the form ``(elem) => { ... }``: they
read from ``elem``.
"""

#: ``native(obj, name)``. Starts at ``Object.getPrototypeOf(obj)``, never at
#: ``obj`` itself (that is where a form's named controls are), and stops at the
#: first prototype with an OWN property of that name. The caller applies the
#: result with ``.call(obj)``; one that is ``undefined`` fails there, inside
#: whatever ``try`` the caller already has.
NATIVE_JS = """const native = (obj, name) => {
        for (let proto = Object.getPrototypeOf(obj); proto !== null;
             proto = Object.getPrototypeOf(proto)) {
            const own = Object.getOwnPropertyDescriptor(proto, name);
            if (own !== undefined) return own.get !== undefined ? own.get : own.value;
        }
        return undefined;
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
