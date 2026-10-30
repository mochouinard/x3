"""Command Line… and the hover summary.

No device and no pytest required:

    .venv/bin/python test_cli_line.py

Command Line… shows a row as the router's own CLI line. The .jg names no CLI
path, so candidates come from the nav and the router says which exists; only
lines it printed under the path tried are shown. Resting on a row lists every
setting it has, not the counters it is running.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="x3-cache-")

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from x3.proto.device import _CLI_MARK, DeviceClient
from x3.ui.widgets.cli_line import cli_candidates

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)

check("IP ▸ Firewall ▸ Filter Rules is tried as /ip firewall filter first",
      cli_candidates([["IP", "Firewall", "Filter Rules"]])[0], "/ip firewall filter")
check("a plural list is tried singular first",
      cli_candidates([["IP", "DHCP Server", "Leases"]])[:2],
      ["/ip dhcp-server lease", "/ip dhcp-server leases"])
check("a folder and its own list count once",
      cli_candidates([["Interfaces", "Interface"]])[0], "/interface")


class Console(DeviceClient):
    """Answers command_line_for's console line the way 7.23.3 did."""

    def __init__(self, answers):
        super().__init__("192.0.2.1", "admin", "")
        self.answers, self.typed = answers, []

    def run_console(self, command, **kw):
        self.typed.append(command)
        out = []
        for i, part in enumerate(command.split(f':put "{_CLI_MARK}')[1:]):
            out.append(f"{_CLI_MARK}{i}")
            path = part.split('[:parse "', 1)[1].split(" export", 1)[0]
            out.extend(self.answers.get(path, []))
        return SimpleNamespace(text="\n".join(out))


rule = "/ip firewall filter add action=accept chain=input"
c = Console({"/ip firewall filter": ["# header", rule]})
check("the line the router printed is the answer",
      c.command_line_for(["/ip firewall filters", "/ip firewall filter"], 1,
                         menu_path=(20, 3)), rule)
c.command_line_for(["/ip firewall filters", "/ip firewall filter"], 1, menu_path=(20, 3))
check("...and the path that worked is tried first next time",
      c.typed[-1].index("/ip firewall filter export") < c.typed[-1].index("filters"), True)
c = Console({"/interface": ["/interface ethernet set [ find default-name=ether1 ] x=y",
                            "/interface bridge add name=br"]})
check("lines of a sub-section are not this object's", c.command_line_for(["/interface"], 1), None)
check("a path that is not a plain CLI path is never typed",
      (Console({}).command_line_for(["/x; /system reboot"], 1), ), (None, ))

# --- the hover summary --------------------------------------------------------

from x3.ui.widgets.list_pane import ListPane  # noqa: E402


class Row:
    def __init__(self):
        self.fields = [(0x1, "Name", "ether1", True), (0x2, "MTU", "1500", True),
                       (0x3, "Comment", "", True), (0x4, "<0x000004>", "7", True)]

    def object_id(self):
        return 1

    def comment(self):
        return "uplink"


pane = ListPane("Interface", path=(20, 0))
pane._on_loaded((None, ["", "Name"], [["R", "ether1"]], [Row()], None))
tip = pane._model.data(pane._model.index(0, 1), 3)    # ToolTipRole
check("resting on a row lists its settings, comment included, unnamed left out",
      tip.splitlines(), ["Name: ether1", "MTU: 1500", "Comment: uplink"])

# --- the router's word on what was set ----------------------------------------
# The .jg names a default for some fields only, so once the router's export of
# the list is in hand, a hover shows that instead: only what was changed.

from x3.ui.widgets.cli_line import line_settings  # noqa: E402

check("an export line reads as its settings, quoted values whole",
      line_settings('/ip firewall filter add action=accept chain=input comment="allow ping"'),
      [("action", "accept"), ("chain", "input"), ("comment", "allow ping")])
check("a set's [ find ... ] picks the object and is not a setting",
      line_settings("/interface ethernet set [ find default-name=ether1 ] disable-running-check=no"),
      [("disable-running-check", "no")])


class Exports(Console):
    def __init__(self):
        super().__init__({"/interface ethernet": [
            "/interface ethernet set [ find default-name=ether1 ] disable-running-check=no"]})
        self.lists = 0

    def export_lines(self, cli_path, **kw):
        self.lists += 1
        return {1: "/interface ethernet set [ find default-name=ether1 ] disable-running-check=no"}


class TypedRow(Row):
    def __init__(self):
        super().__init__()
        self.fields = self.fields + [(0x5, "Type", "Ethernet", True)]


ex = Exports()
from x3.ui.edit_mode import EditMode, EditSession  # noqa: E402

xp = ListPane("Interface", path=(20, 0),
              edit_session=EditSession(EditMode.READONLY, client=ex))
xp._on_loaded((None, ["", "Name"], [["R", "ether1"]], [TypedRow()], None))
first = xp._model.data(xp._model.index(0, 1), 3)
check("the first hover answers from the .jg while the router is asked",
      "MTU: 1500" in first, True)
for w in list(xp._cli_workers):
    w.wait(5000)
app.processEvents()
check("...and once it answers, a hover shows only what was set",
      xp._model.data(xp._model.index(0, 1), 3).splitlines()[-1],
      "disable-running-check: no")
xp._model.data(xp._model.index(0, 1), 3)
check("...from the cache: the list was read once", ex.lists, 1)
xp._cli_lines.clear()      # what a configuration change does (_note_content)
xp._model.data(xp._model.index(0, 1), 3)
for w in list(xp._cli_workers):
    w.wait(5000)
app.processEvents()
check("a change to the list's configuration has it read again", ex.lists, 2)

# --- in the form's order ----------------------------------------------------
# The export goes alphabetically (action, chain, comment, connection-state) and
# the wire in its own order; a firewall rule's form goes Chain, Src. Address,
# ..., Action. The hover follows the form, by the form's names, comment last.

from types import SimpleNamespace  # noqa: E402

FwField = lambda name: SimpleNamespace(  # noqa: E731
    name=name, ro=False, nonpublic=False, jg_type="string", default=None, nested=None)
fw_menu = SimpleNamespace(
    fields={1: FwField("Action"), 2: FwField("Chain"), 3: FwField("Src. Address"),
            4: FwField("Connection State")},
    form_order=[("General", 2), ("General", 3), ("Advanced", 4), ("Action", 1)],
)
check("an export's settings come in the form's order, by its names",
      ListPane._form_ordered(line_settings(
          '/ip firewall filter add action=accept chain=input comment="allow ping" '
          "connection-state=established src-address=10.0.0.0/8 zz-unknown=1"), fw_menu),
      [("Chain", "input"), ("Src. Address", "10.0.0.0/8"),
       ("Connection State", "established"), ("Action", "accept"),
       ("zz-unknown", "1"), ("Comment", "allow ping")])


class FwRow:
    fields = [(1, "Action", "accept", True), (2, "Chain", "input", True),
              (4, "Connection State", "established", True)]
    values_by_code = {1: 1, 2: b"input", 4: 2}

    def object_id(self):
        return 3

    def comment(self):
        return "allow"


fp = ListPane("Filter Rules", path=(20, 3))
fp._on_loaded((None, ["", "Chain"], [["", "input"]], [FwRow()], None))
fp._row_menu_of = lambda raw: fw_menu
check("the template summary keeps the form's order too",
      fp._row_summary(0).splitlines(),
      ["Chain: input", "Connection State: established", "Action: accept", "Comment: allow"])

# A firewall rule's name is its matchers run together, so a rule matching only
# Dst. Port 13239 was headed by a bare "13239". A name the lines already carry
# is left off; one they do not (an interface's default name) still heads them.

fp._row_label = lambda values, raw=None: "13239"
check("a name that only repeats a setting below is left off",
      fp._summary_head(None, ["Chain: input", "Dst. Port: 13239"]), "")
fp._row_label = lambda values, raw=None: "10.0.0.1:22->10.0.0.2:8080"
check("...a whole Src->Dst name too",
      fp._summary_head(None, ["Src. Address: 10.0.0.1", "Src. Port: 22",
                              "Dst. Address: 10.0.0.2", "Dst. Port: 8080"]), "")
fp._row_label = lambda values, raw=None: "ether1"
check("a name the lines do not show still heads them",
      fp._summary_head(None, ["disable-running-check: no"]), "ether1")

# --- defaults, learned once and kept ----------------------------------------
# An export leaves out what is at its default, so a field a row carries and
# its line does not name is at its default -- kept per firmware on disk, so the
# next session (and every router on that build) needs no console read.

from x3.ui import learned_defaults as ld  # noqa: E402

check("a display name becomes its CLI name",
      [ld.cli_name(n) for n in ("Loop Protect", "Dst. Address", "ARP Timeout")],
      ["loop-protect", "dst-address", "arp-timeout"])
key = ((20, 0), "ethernet")
fields = [(1, "Name"), (2, "ARP"), (3, "Loop Protect"), (4, "MTU")]
ld.learn("7.99-1", key, [
    (fields, {1: b"ether1", 2: 1, 3: 0, 4: 1500},
     "/interface ethernet set [ find default-name=ether1 ] mtu=1500"),
    (fields, {1: b"ether2", 2: 1, 3: 0, 4: 9000},
     "/interface ethernet set [ find default-name=ether2 ] mtu=9000"),
])
ld._cache.clear()        # as a new session would find it: from disk
learned = ld.known("7.99-1", key)
check("what the export leaves out is learned as the default, and kept",
      (ld.is_default(learned, 2, 1), ld.is_default(learned, 3, 0)), (True, True))
check("a value that differed between rows has no default",
      ld.is_default(learned, 1, b"ether1"), False)
check("a field the export named is not learned from",
      4 in learned, False)
check("a list never learned is not known", ld.known("7.99-1", ((20, 3), "")), None)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all cli-line tests passed")
