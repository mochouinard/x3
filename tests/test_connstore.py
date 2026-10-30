"""Offline tests for the local connection overlay on Addresses.cdb.

    .venv/bin/python test_connstore.py

No Qt, no device, no pytest. The property that matters most: an override
records only the fields we actually changed, so everything else keeps tracking
Addresses.cdb. Get that wrong and the overlay freezes a whole row at whatever
it looked like the day it was first edited.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from x3.ui import connstore as cs

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    if not cond:
        failures.append(label)
    else:
        print(f"ok  {label}")


@dataclass
class Row:
    """Stands in for x3.ui.services.AddressRow."""

    host: str
    login: str
    password: str = ""
    note: str = ""
    session: str = "<own>"
    group: str = ""


CDB = [
    Row("r1.example.net:41002", "admin", "pw1", "site-one", group="prod"),
    Row("r2.example.net:41002", "operator", "pw2", "site-two", group="prod"),
    # Same host, different login — the real shape of the 9 duplicate hosts in
    # a live address book, and why the key cannot be host alone.
    Row("dual.example.net", "admin", "pwA", "shared-note"),
    Row("dual.example.net", "operator", "pwB", "shared-note"),
]


def names(conns):
    return [(c.host, c.login) for c in conns]


# --- passthrough ----------------------------------------------------------

s = cs.Store()
merged = s.merge(CDB)
check("empty store is a passthrough", len(merged), 4)
check("every row reads as upstream", {c.origin for c in merged}, {"cdb"})
check("no flag letter on an untouched row", merged[0].flag, "")
check("fields come through", (merged[0].note, merged[0].group), ("site-one", "prod"))

# --- host alone is not an identity ---------------------------------------

check("same host, different login stay distinct",
      names(merged)[2:], [("dual.example.net", "admin"), ("dual.example.net", "operator")])
ok("...and get distinct keys", merged[2].key != merged[3].key)
check("a shared note does not merge them",
      merged[2].note == merged[3].note, True)

# --- override -------------------------------------------------------------

s = cs.Store()
s.merge(CDB)                                   # snapshot upstream
edited = dict(merged[0].as_dict(), note="renamed locally")
s.set_entry(edited, original=merged[0])
out = s.merge(CDB)
check("override applies", out[0].note, "renamed locally")
check("row is marked as locally edited", out[0].origin, "local")
check("...with a flag letter", out[0].flag, "L")
check("other rows untouched", out[1].origin, "cdb")

check("only the changed field is stored",
      s.overrides[cs.entry_key("r1.example.net:41002", "admin")],
      {"note": "renamed locally"})

# THE property: upstream edits to fields we do not override still show.
CDB2 = [Row("r1.example.net:41002", "admin", "pw1-rotated", "site-one", group="staging"),
        *CDB[1:]]
out = s.merge(CDB2)
check("a password rotated in Winbox comes through", out[0].password, "pw1-rotated")
check("...and so does a group changed in Winbox", out[0].group, "staging")
check("...while our note still wins", out[0].note, "renamed locally")

# --- editing the identity fields -----------------------------------------

s = cs.Store()
base = s.merge(CDB)
s.set_entry(dict(base[1].as_dict(), host="moved.example.net"), original=base[1])
out = s.merge(CDB)
check("host can be edited on a cdb row", out[1].host, "moved.example.net")
check("...and it stays attached to its cdb entry", out[1].origin, "local")
# Editing it a second time must not orphan the first override.
s.set_entry(dict(out[1].as_dict(), note="moved twice"), original=out[1])
check("a second edit reuses the same override", len(s.overrides), 1)
out = s.merge(CDB)
check("...and both edits survive",
      (out[1].host, out[1].note), ("moved.example.net", "moved twice"))

# --- reverting ------------------------------------------------------------

ok("revert reports it did something", s.revert(out[1]))
out = s.merge(CDB)
check("revert restores the cdb values",
      (out[1].host, out[1].note), ("r2.example.net:41002", "site-two"))
check("...and clears the flag", out[1].origin, "cdb")
ok("reverting an unedited row is a no-op", not s.revert(out[1]))

# --- hiding ---------------------------------------------------------------

s = cs.Store()
base = s.merge(CDB)
s.remove(base[1])
out = s.merge(CDB)
check("a removed cdb row disappears", len(out), 3)
check("...the right one", names(out), [
    ("r1.example.net:41002", "admin"),
    ("dual.example.net", "admin"),
    ("dual.example.net", "operator"),
])
ok("the cdb file itself is untouched — we only ever hide", len(CDB) == 4)
check("unhide brings it back", s.unhide_all(), 1)
check("...restored", len(s.merge(CDB)), 4)

# --- our own entries ------------------------------------------------------

s = cs.Store()
s.merge(CDB)
new = s.set_entry({"host": "lab.local:8291", "login": "admin",
                   "password": "labpw", "note": "bench ap", "group": "lab"})
check("a new entry is ours", new.origin, "new")
check("...flagged N", new.flag, "N")
out = s.merge(CDB)
check("it appears after the cdb rows", names(out)[-1], ("lab.local:8291", "admin"))
check("cdb rows still come first", len(out), 5)

s.set_entry({"host": "lab.local:8291", "login": "admin", "password": "changed",
             "note": "bench ap", "group": "lab"})
check("saving the same host+login again updates in place", len(s.own), 1)
check("...with the new value", s.merge(CDB)[-1].password, "changed")

s.remove(s.merge(CDB)[-1])
check("removing one of ours deletes it outright", len(s.own), 0)
check("...and it is gone from the view", len(s.merge(CDB)), 4)

# --- stale overrides ------------------------------------------------------

s = cs.Store()
base = s.merge(CDB)
s.set_entry(dict(base[0].as_dict(), note="x"), original=base[0])
s.remove(base[1])
shrunk = CDB[2:]                       # both edited entries vanish from the cdb
out = s.merge(shrunk)
check("an override with no cdb entry shows nothing", len(out), 2)
check("prune drops both stale records", s.prune(shrunk), 2)
ok("...leaving an empty store", s.is_empty())

# --- persistence ----------------------------------------------------------

s = cs.Store()
base = s.merge(CDB)
s.set_entry(dict(base[0].as_dict(), note="persisted"), original=base[0])
s.remove(base[1])
s.set_entry({"host": "lab.local", "login": "admin", "password": "p",
             "note": "n", "group": "g"})

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "connections.json"
    cs.save(s, path)
    raw = json.loads(path.read_text())
    check("file records the override readably",
          raw["overrides"][0]["match"],
          {"host": "r1.example.net:41002", "login": "admin"})
    check("...and only the changed field", raw["overrides"][0]["set"], {"note": "persisted"})
    ok("hidden entries are readable too", raw["hidden"][0]["login"] == "operator")
    if os.name != "nt":  # Windows: the profile's ACL protects it, not a mode
        check("file is not world-readable", oct(path.stat().st_mode & 0o777), "0o600")

    back = cs.load(path)
    a, b = s.merge(CDB), back.merge(CDB)
    check("round-trip preserves the merged view",
          [c.as_dict() for c in a], [c.as_dict() for c in b])
    check("...including origins", [c.origin for c in a], [c.origin for c in b])

    path.write_text("{ this is not json")
    check("a corrupt store degrades to empty, not a crash",
          cs.load(path).is_empty(), True)

check("a missing store is empty",
      cs.load(Path("/nonexistent/connections.json")).is_empty(), True)

# --- key normalisation ----------------------------------------------------

check("hostnames are case-insensitive",
      cs.entry_key("R1.Example.NET", "admin"), cs.entry_key("r1.example.net", "admin"))
ok("logins are not — the router treats them as distinct",
   cs.entry_key("h", "Admin") != cs.entry_key("h", "admin"))
check("surrounding whitespace does not make a new identity",
      cs.entry_key("  h  ", " admin "), cs.entry_key("h", "admin"))

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all connstore tests passed")
