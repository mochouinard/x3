"""A parsed schema is kept on disk and reused until the .jg files change.

No device and no pytest required:

    .venv/bin/python test_schema_cache.py

Parsing a 7.21 plugin set is ~1.2 MB of JavaScript object literal and about
half a second of Python — the largest single cost in a connect, paid again on
every connect for an answer that only changes when the firmware does. What is
pinned here is that reusing it is *safe*: a warm load must be
indistinguishable from a cold one, a changed .jg must not be served from a
stale file, and anything unreadable must fall back to parsing rather than
raise.

The trap this exists to catch: ``Schema`` carries an ``id()``-keyed index of
its overlays. ``id()`` does not survive a round trip, and a silently broken
one does not fail — it collapses every overlay chain onto a single cache
entry, so a VLAN would quietly render an Ethernet's form.
"""

import os
import shutil
import sys
import tempfile

_CACHE = tempfile.mkdtemp(prefix="x3ctl-cache-test-")
os.environ["XDG_CACHE_HOME"] = _CACHE

from x3.proto import schema as sch

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
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())


SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "7.21.1-151146055")
if not os.path.isdir(SRC):
    print(f"skipped: no plugin cache at {SRC}")
    raise SystemExit(0)

WORK = os.path.join(tempfile.mkdtemp(prefix="x3-jg-test-"), "7.21.1-151146055")
shutil.copytree(SRC, WORK)

IFACE = (20, 0)


def fingerprint(s: sch.Schema) -> tuple:
    """Everything a caller could tell two schemas apart by."""
    hwtype = next(c for c, f in s.overlay_by_generic["wlan"].fields.items()
                  if f.name == "hwtype")
    return (
        len(s.menus),
        s.field_count,
        len(s.overlay_list),
        [n.title for n in s.nav],
        sorted(s.family_type_values("wlan")),
        s.generic_paths.get("wlan"),
        [m.title for m in s.overlay_chain(s.record(IFACE), 35, {hwtype: 12})],
        s.record_for(IFACE, 35, {hwtype: 12}).tab_names(),
        s.record_for(IFACE, 1).tab_names(),
    )


def cache_files() -> list[str]:
    d = sch.schema_cache_dir()
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


# --- cold, then warm --------------------------------------------------------

ok("nothing is cached yet", not cache_files(), str(cache_files()))
cold = sch.Schema.load(WORK)
ok("a cold load writes a cache file", len(cache_files()) == 1, str(cache_files()))

warm = sch.Schema.load(WORK)
check("a warm load is indistinguishable from a cold one",
      fingerprint(warm), fingerprint(cold))
ok("...and it really came from the cache, not a reparse",
   sch.load_schema_cache(WORK) is not None)

# The id()-keyed overlay index is rebuilt, not carried.
check("every overlay is back in the identity index",
      len(warm._overlay_key), len(warm.overlay_list))
check("...with distinct slots",
      len(set(warm._overlay_key.values())), len(warm.overlay_list))
ok("the merge cache starts empty on a fresh load",
   not sch.Schema.load(WORK)._merged_cache)
# Two different chains must not share a merged menu.
eth = warm.record_for(IFACE, 1)
vlan = warm.record_for(IFACE, 30)
ok("different types still merge to different menus",
   eth.tab_names() != vlan.tab_names(),
   f"{eth.tab_names()} == {vlan.tab_names()}")

# --- invalidation -----------------------------------------------------------

target = os.path.join(WORK, "wlan6.jg")
os.utime(target, (0, 0))
ok("touching a .jg invalidates the cache", sch.load_schema_cache(WORK) is None)
after_touch = sch.Schema.load(WORK)
check("...and the rebuild is still correct",
      fingerprint(after_touch), fingerprint(cold))
ok("...and it is cached again", sch.load_schema_cache(WORK) is not None)

with open(target, "ab") as fh:
    fh.write(b"\n")
ok("a changed size invalidates it too", sch.load_schema_cache(WORK) is None)
shutil.copy(os.path.join(SRC, "wlan6.jg"), target)
sch.Schema.load(WORK)

# --- a cache file that cannot be trusted is a miss, never an error ----------

path = os.path.join(sch.schema_cache_dir(), cache_files()[0])
with open(path, "wb") as fh:
    fh.write(b"not a pickle at all")
ok("a corrupt cache file reads as a miss", sch.load_schema_cache(WORK) is None)
check("...and loading still works", fingerprint(sch.Schema.load(WORK)),
      fingerprint(cold))

with open(path, "wb") as fh:
    fh.write(b"")
ok("an empty cache file reads as a miss", sch.load_schema_cache(WORK) is None)
sch.Schema.load(WORK)

# A directory with no .jg files has no identity to key on.
empty = tempfile.mkdtemp(prefix="x3ctl-empty-")
ok("an empty directory is never cached", sch.load_schema_cache(empty) is None)
ok("...and store declines it", sch.store_schema_cache(empty, cold) is False)

# --- opting out -------------------------------------------------------------

check("cache=False does not consult the cache",
      fingerprint(sch.Schema.parse(WORK)), fingerprint(cold))
os.environ["X3_NO_SCHEMA_CACHE"] = "1"
try:
    ok("the env switch disables reads", sch.load_schema_cache(WORK) is None)
    ok("...and writes", sch.store_schema_cache(WORK, cold) is False)
    check("...while loading still works",
          fingerprint(sch.Schema.load(WORK)), fingerprint(cold))
finally:
    del os.environ["X3_NO_SCHEMA_CACHE"]

shutil.rmtree(_CACHE, ignore_errors=True)
shutil.rmtree(os.path.dirname(WORK), ignore_errors=True)
shutil.rmtree(empty, ignore_errors=True)

if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall schema-cache tests passed")
