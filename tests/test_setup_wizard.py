"""``type:setup`` -- the DHCP and Hotspot setup wizards.

No device and no pytest required:

    .venv/bin/python test_setup_wizard.py

Two menus are declared this way and neither is one you browse to: Winbox offers
**DHCP Setup** on the DHCP Server window and **Hotspot Setup** on the Hotspot
one, as a button beside Add. Each node carries the handler it writes to --
(23,0) and (63) -- and a flat list of children where every ``tab`` starts a
page and *names its prompt*::

    {name:'Select interface to run DHCP server on',type:'tab'},
    {name:'DHCP Server Interface',type:'enm',id:'ub',values:{dynamic (20,0)}},
    {name:'Select network for DHCP addresses',type:'tab'},
    {name:'DHCP Address Space',type:'network',id:'u15',maskid:'u16'},
    …

So the pages, their prompts, their fields and their vocabularies are all in the
schema. Two things had to be got right beyond reading them:

* **Which window offers it.** Not the nav: the wizard's own handler is the
  window's, so a window offers every wizard whose path is a prefix of one of
  its panes' (which puts Hotspot Setup, path (63), on the (63,1) servers list).

* **The array fields.** ``Addresses to Give Out`` is a ``multinetwork`` and
  ``DNS Servers`` a ``multiipaddr``; their editors already know how to put an
  array on the wire, and a text box would have sent a comma-separated string.
  So the wizard borrows the property form's editors rather than growing its
  own.

Finishing is one ADD on that handler -- the router creates the pool, the
network and the DNS entries itself -- and it goes through the session like any
other write, so queue mode reviews it and read-only mode refuses it.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit

from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.scalar_array import ScalarArrayEditor
from x3.ui.widgets.setup_wizard import SetupWizard

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"FAIL {label}: {detail}")


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

schema = Schema.load(SCHEMA_DIR)
dhcp = schema.dialogs.get("DHCP Setup")
hotspot = schema.dialogs.get("Hotspot Setup")

IFACE_CODE, SPACE_CODE, GATEWAY_CODE = 0xB, 0x15, 0x1F
RELAY_CODE, POOL_CODE, SEND_DNS_CODE = 0x29, 0x33, 0x3E
DNS_CODE, LEASE_CODE = 0x3D, 0x47


# --- what the schema says ---------------------------------------------------

ok("both wizards are parsed", dhcp is not None and hotspot is not None)
check("...as their own kind, not as menus", (dhcp.kind, hotspot.kind),
      ("setup", "setup"))
check("...carrying the handler they write to", (dhcp.path, hotspot.path),
      ((23, 0), (63,)))
ok("...and out of the path index, being no menu you browse to",
   schema.record((63,)) is None or schema.record((63,)).kind != "setup")

check("a page per prompt, in the .jg's order", dhcp.tab_names()[:3],
      ["Select interface to run DHCP server on",
       "Select network for DHCP addresses",
       "Select gateway for given network"])
check("...eight of them for DHCP", len(dhcp.tab_names()), 8)
check("...and nine for Hotspot", len(hotspot.tab_names()), 9)
check("the interface page's field comes with the device's own list",
      dhcp.fields[IFACE_CODE].enum_path, (20, 0))
check("...and the address space knows where its mask goes",
      dhcp.fields[SPACE_CODE].maskid is not None, True)

# Which window offers which.
check("DHCP Setup belongs to the DHCP server window",
      [mn.title for mn in schema.wizards_for((23, 0))], ["DHCP Setup"])
check("...and not to the leases window", schema.wizards_for((23, 1)), [])
check("Hotspot Setup reaches its servers list by prefix",
      [mn.title for mn in schema.wizards_for((63, 1))], ["Hotspot Setup"])
check("an unrelated list offers none", schema.wizards_for((20, 0)), [])


# --- the wizard -------------------------------------------------------------

class StubClient:
    schema = schema

    def __init__(self):
        self.added: list[tuple] = []

    def enum_choices(self, fld):
        if fld.enum_path == (20, 0):
            return [(1, "ether1"), (2, "ether2")]
        return sorted((fld.enum or {}).items())

    def add_obj(self, path, values):
        self.added.append((tuple(path), dict(values)))
        return 42, None

    def menu_for(self, path, type_value=None, **kw):
        return schema.record(tuple(path))


app = QApplication.instance() or QApplication(sys.argv)

client = StubClient()
session = EditSession(EditMode.LIVE, client=client)
wiz = SetupWizard(dhcp, client=client, edit_session=session)
app.processEvents()

check("the dialog is titled as the .jg titles it", wiz.windowTitle(),
      "DHCP Setup")
check("one page per prompt", wiz._stack.count(), 8)
check("...opening on the first", wiz._prompt.text(),
      "Select interface to run DHCP server on")
check("...and saying where you are", wiz._step.text(), "Step 1 of 8")
ok("Back is not offered on the first page", not wiz._back.isEnabled())
check("...and the last page finishes instead of continuing",
      wiz._next.text(), "Next")

editors = {
    fld.code: editor for page in wiz._editors for fld, editor in page
}
ok("the interface page is a dropdown of the device's interfaces",
   isinstance(editors[IFACE_CODE], QComboBox)
   and editors[IFACE_CODE].count() == 2,
   f"{type(editors[IFACE_CODE]).__name__}")
ok("Send DNS is a checkbox", isinstance(editors[SEND_DNS_CODE], QCheckBox))
ok("the address space is a box you type into",
   isinstance(editors[SPACE_CODE], QLineEdit))
ok("...while the pool is an array editor, which knows its wire shape",
   isinstance(editors[POOL_CODE], ScalarArrayEditor),
   f"{type(editors[POOL_CODE]).__name__}")
ok("...and so are the DNS servers",
   isinstance(editors[DNS_CODE], ScalarArrayEditor))
check("the lease time starts on the .jg's default",
      editors[LEASE_CODE].value(), 1800)

# A page blocks only on what the .jg actually asks for: `min:1` on the pool
# means at least one range, and nothing marks the address space required at
# all. (The interface page is a dropdown, so it is answered from the start --
# the same as Winbox, which pre-picks the first interface.)
POOL_PAGE = 4
ok("the pool page will not advance while it is empty",
   wiz._page_problem(POOL_PAGE) is not None,
   f"{wiz._page_problem(POOL_PAGE)}")
editors[POOL_CODE]._add_row("192.168.88.10-192.168.88.100")
ok("...and does once it has one range", wiz._page_problem(POOL_PAGE) is None)
ok("a page the .jg asks nothing of is not blocked either",
   wiz._page_problem(1) is None)
ok("the interface page is answered from the start, being a dropdown",
   wiz._page_problem(0) is None)
editors[IFACE_CODE].setCurrentIndex(1)
ok("the relay page is optional, so it advances empty",
   wiz._page_problem(3) is None)

# Fill it in and finish.
editors[SPACE_CODE].setText("192.168.88.0/24")
editors[GATEWAY_CODE].setText("192.168.88.1")
editors[SEND_DNS_CODE].setChecked(True)
editors[DNS_CODE]._add_row("1.1.1.1")

change = wiz.change()
check("finishing is one add on the wizard's own handler",
      (change.action, change.path), ("add", (23, 0)))
by_code = {e["code"]: e["new"] for e in change.edits}
check("...carrying the interface that was picked", by_code[IFACE_CODE], 2)
check("...the gateway as typed", by_code[GATEWAY_CODE], "192.168.88.1")
ok("...Send DNS as a boolean", by_code[SEND_DNS_CODE] is True)
ok("...and the arrays as the (tag, values) pair the write path passes through",
   isinstance(by_code[DNS_CODE], tuple) and isinstance(by_code[DNS_CODE][1], list),
   f"{by_code[DNS_CODE]}")
ok("an empty optional field is left out rather than sent blank",
   RELAY_CODE not in by_code, f"{sorted(by_code)}")

summary = session.apply(change, require_live=False)
check("the session sends it as an add", len(client.added), 1)
check("...to the handler the .jg names", client.added[0][0], (23, 0))
ok("...with every answer in it", len(client.added[0][1]) >= 5,
   f"{client.added[0][1]}")
ok("...and reports what happened", "42" in summary, summary)
wiz.close()

# Queue mode holds it for review instead of sending it, like every other write.
queued: list = []
queue_session = EditSession(EditMode.QUEUE, client=client)
queue_session.enqueue = queued.append
wiz2 = SetupWizard(hotspot, client=client, edit_session=queue_session)
app.processEvents()
check("the Hotspot wizard has its own pages", wiz2._stack.count(), 9)
before = len(client.added)
summary2 = wiz2.commit(wiz2.change())
check("nothing reached the device", len(client.added), before)
check("...it went to the pending queue instead", len(queued), 1)
check("...as an add on the Hotspot handler",
      (queued[0].action, queued[0].path), ("add", (63,)))
ok("...and the wizard says so", "queued" in (summary2 or "").lower(),
   f"{summary2}")
wiz2.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall setup-wizard tests passed")
