"""settings.json is read once per version of itself, not once per question.

No device and no pytest required:

    .venv/bin/python test_settings_cache.py

Every toolbar icon asks which icon source is configured, so opening one
17-tab list window used to stat and JSON-parse the settings file about 140
times. It is cached now, which buys three obligations: an edit from outside
this process must still be seen, a caller must not be able to corrupt the
cache by editing what it was handed, and the cache must follow
``$XDG_CONFIG_HOME`` rather than pinning whatever it saw first (the test
suite moves it, and so can a user).
"""

import json
import os
import sys
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from x3.ui import settings

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


def write_raw(data: dict) -> None:
    """Write the file behind settings.py's back, as another process would."""
    path = settings.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


# --- round trip -------------------------------------------------------------

settings.save_settings({"icon_source": "builtin", "titlebar_tabs": True})
check("what was saved is what is read back",
      settings.load_settings(),
      {"icon_source": "builtin", "titlebar_tabs": True})
check("repeated reads agree", settings.load_settings(), settings.load_settings())
check("a typed accessor sees it", settings.load_titlebar_tabs(), True)

# --- the cache does not outlive the file it came from -----------------------

write_raw({"icon_source": "stock", "titlebar_tabs": False})
check("an edit from outside this process is picked up",
      settings.load_settings().get("icon_source"), "stock")

os.remove(settings.settings_path())
check("a deleted file reads as empty, not as the last cache",
      settings.load_settings(), {})
check("...and a typed accessor falls back to its default",
      settings.load_titlebar_tabs(), False)

# A save through the module is visible immediately, without a stat race.
settings.save_settings({"live_updates": False})
check("a save is visible at once", settings.load_live_updates(), False)

# --- callers get a copy, not the cache --------------------------------------

settings.save_settings({"start_mode": "live"})
borrowed = settings.load_settings()
borrowed["start_mode"] = "readonly"
borrowed["injected"] = True
check("editing the returned dict does not reach the cache",
      settings.load_settings(), {"start_mode": "live"})
check("...and the read-modify-write callers still work",
      (settings.save_settings(borrowed), settings.load_settings())[1],
      {"start_mode": "readonly", "injected": True})

# --- the config dir follows the environment ---------------------------------

first = settings.config_dir()
moved = tempfile.mkdtemp(prefix="x3-test-moved-")
os.environ["XDG_CONFIG_HOME"] = moved
ok("moving XDG_CONFIG_HOME moves the config dir",
   settings.config_dir() != first, f"{settings.config_dir()} == {first}")
ok("...and the settings path with it",
   str(settings.settings_path()).startswith(moved), str(settings.settings_path()))
check("the new location starts empty", settings.load_settings(), {})
settings.save_settings({"submenu_order": "jg"})
check("...and is what writes now go to", settings.load_submenu_order(), "jg")

if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall settings-cache tests passed")
