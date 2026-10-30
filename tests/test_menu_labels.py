"""What the menu calls an entry, against the caption of the window it opens.

    python tests/run.py menu_labels

Firmware 7.9 declares IP ▸ Addresses as
``{name:'Addresses', title:'Address List', group:'IP', c:[...]}``: the menu
says "Addresses", the window "Address List". x3 used the title for both, so on
a 7.9 router the IP submenu offered "Address List", "Route List" and "IP Pool"
where the reference client offers Addresses, Routes and Pool. 7.21 names both
the same, which is why it only showed on older firmware.

The rule is narrow on purpose: only a container entry (no ``type``) is
labelled by its name. A typed entry keeps its title -- ``{name:'Log Entry',
title:'Log', type:'map'}`` is "Log" in the menu -- and the tabs inside a window
always keep theirs.
"""

from x3.proto import schema as sch
from x3.ui.widgets.menu_tree import _payload_from_nav, menu_label

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


root = [
    {"name": "Addresses", "title": "Address List", "group": "IP", "prio": 110,
     "c": [{"name": "Address", "title": "Address", "type": "map", "path": [20, 1]}]},
    {"name": "Routes", "title": "Route List", "group": "IP", "prio": 110,
     "c": [{"name": "Route", "title": "All Routes", "type": "map", "path": [44, 21]},
           {"name": "Rule", "title": "Rules", "type": "map", "path": [44, 22]}]},
    {"name": "Log Entry", "title": "Log", "type": "map", "path": [3, 4]},
    {"name": "Same", "title": "Same", "group": "IP",
     "c": [{"name": "Same", "type": "map", "path": [9, 9]}]},
]
nav = sch._nav_top_level(root, "roteros")
ip = next(n for n in nav if n.title == "IP")
by_name = {c.name: c for c in ip.children}

check("a container entry is labelled by its name", by_name["Addresses"].label, "Addresses")
check("...and keeps its title for the window", by_name["Addresses"].title, "Address List")
log = next(n for n in nav if n.name == "Log Entry")
check("a typed entry keeps its title as its label", log.label, "")
routes = by_name["Routes"]
check("tabs inside a window are not relabelled",
      [(c.label, c.title) for c in routes.children], [("", "All Routes"), ("", "Rules")])

payloads = [_payload_from_nav(n) for n in nav]
ip_payload = next(p for p in payloads if p["title"] == "IP")
check("the menu shows the label",
      sorted(menu_label(c) for c in ip_payload["children"]), ["Addresses", "Routes", "Same"])
check("...the window keeps the caption",
      next(c["title"] for c in ip_payload["children"] if menu_label(c) == "Addresses"),
      "Address List")
check("...and the submenu sorts on what it shows",
      [menu_label(c) for c in ip_payload["children"]], ["Addresses", "Routes", "Same"])
check("a payload without a label falls back to the title",
      menu_label({"title": "Log"}), "Log")

if failures:
    print(f"\n{len(failures)} failure(s)")
    raise SystemExit(1)
print("\nall menu-label checks passed")
