"""Where x3 keeps its files on each platform, and how it finds network
interfaces where Linux's kernel interfaces are missing.

    python tests/run.py portability

Linux must keep exactly the folders it always had (XDG), Windows must put
settings in the roaming profile and the bulky rest in the local one, and an
``XDG_*_HOME`` variable must win everywhere -- the test runner depends on that
to keep every test out of the real home directory. MAC connections and MNDP
read interfaces from /sys and ioctl on Linux; elsewhere they go through psutil,
checked here against a fake one.
"""

import os
import socket
import sys
import types
from pathlib import Path

from x3 import paths
from x3.proto import macwinbox, mndp

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


saved_env = dict(os.environ)
saved_platform = sys.platform
home = Path.home()
for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
    os.environ.pop(k, None)

try:
    sys.platform = "linux"
    check("Linux keeps its XDG folders",
          [paths.config_dir(), paths.data_dir(), paths.cache_dir(), paths.state_dir()],
          [home / ".config/x3", home / ".local/share/x3", home / ".cache/x3",
           home / ".local/state/x3"])

    sys.platform = "win32"
    os.environ["APPDATA"] = r"C:\Users\ann\AppData\Roaming"
    os.environ["LOCALAPPDATA"] = r"C:\Users\ann\AppData\Local"
    check("Windows: settings roam with the profile",
          paths.config_dir(), Path(r"C:\Users\ann\AppData\Roaming") / "x3")
    check("...and logs, snapshots, caches stay on the machine",
          [paths.data_dir(), paths.cache_dir(), paths.state_dir()],
          [Path(r"C:\Users\ann\AppData\Local") / "x3" / k for k in ("data", "cache", "state")])

    sys.platform = "darwin"
    check("macOS uses ~/Library",
          [paths.config_dir(), paths.cache_dir()],
          [home / "Library/Application Support/x3", home / "Library/Caches/x3"])

    os.environ["XDG_CONFIG_HOME"] = "/tmp/elsewhere"
    for sys.platform in ("linux", "win32", "darwin"):
        check(f"XDG_CONFIG_HOME wins on {sys.platform}",
              paths.config_dir(), Path("/tmp/elsewhere/x3"))
finally:
    sys.platform = saved_platform
    os.environ.clear()
    os.environ.update(saved_env)

# --- interfaces through psutil ------------------------------------------------
Addr = types.SimpleNamespace
fake = types.ModuleType("psutil")
fake.AF_LINK = -1
fake.net_if_addrs = lambda: {
    "Ethernet": [Addr(family=-1, address="52-54-00-12-34-56", netmask=None, broadcast=None),
                 Addr(family=socket.AF_INET, address="192.168.88.20",
                      netmask="255.255.255.0", broadcast=None)],
    "Wi-Fi": [Addr(family=-1, address="aa-bb-cc-dd-ee-ff", netmask=None, broadcast=None),
              Addr(family=socket.AF_INET, address="10.1.2.3",
                   netmask="255.255.0.0", broadcast="10.1.255.255")],
    "Loopback Pseudo-Interface 1": [
        Addr(family=socket.AF_INET, address="127.0.0.1", netmask="255.0.0.0", broadcast=None)],
    "vEthernet (no IP)": [
        Addr(family=-1, address="00-15-5d-00-00-01", netmask=None, broadcast=None)],
}
sys.modules["psutil"] = fake
ifaces = macwinbox._psutil_ifaces()
check("only interfaces with a MAC and a non-loopback IPv4",
      [i.name for i in ifaces], ["Ethernet", "Wi-Fi"])
check("Windows-style MACs (dashes) are read",
      ifaces[0].mac, bytes.fromhex("525400123456"))
check("a missing broadcast is worked out from the netmask",
      ifaces[0].broadcast, "192.168.88.255")
check("...and a reported one is kept", ifaces[1].broadcast, "10.1.255.255")

real_dir = Path.is_dir
Path.is_dir = lambda self: False if str(self) == "/sys/class/net" else real_dir(self)
try:
    check("list_ifaces falls back to psutil without /sys/class/net",
          [i.name for i in macwinbox.list_ifaces()], ["Ethernet", "Wi-Fi"])
finally:
    Path.is_dir = real_dir
# None, not del: where the real psutil is installed (Windows, macOS) a deleted
# entry would simply be imported again.
sys.modules["psutil"] = None
check("without psutil the list is just empty", macwinbox._psutil_ifaces(), [])

# MNDP's per-interface broadcasts use the same list when fcntl is missing.
real_fcntl = sys.modules.get("fcntl")
sys.modules["fcntl"] = None
real_list = macwinbox.list_ifaces
macwinbox.list_ifaces = lambda: ifaces
try:
    check("MNDP broadcasts on each interface without ioctl",
          mndp._iface_broadcasts(), ["10.1.255.255", "192.168.88.255"])
finally:
    macwinbox.list_ifaces = real_list
    if real_fcntl is None:
        del sys.modules["fcntl"]
    else:
        sys.modules["fcntl"] = real_fcntl

# The protocol library must import without the POSIX terminal modules.
import importlib  # noqa: E402

saved = {k: sys.modules.get(k) for k in ("termios", "tty", "x3.proto.terminal")}
sys.modules["termios"] = None
sys.modules["tty"] = None
sys.modules.pop("x3.proto.terminal", None)
try:
    importlib.import_module("x3.proto.terminal")
    check("the terminal channel imports without termios", True, True)
except ImportError as e:
    check("the terminal channel imports without termios", str(e), "")
finally:
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

if failures:
    print(f"\n{len(failures)} failure(s)")
    raise SystemExit(1)
print("\nall portability checks passed")
