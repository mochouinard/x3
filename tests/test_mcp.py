"""Offline tests for the x3 MCP server (x3/mcp):

    .venv/bin/python test_mcp.py

No router and no Qt. Pins the things that would be expensive to get wrong in
front of a model:

  * the wire: one JSON-RPC message per line, no reply to a notification, and
    a tool that prints to stdout does not corrupt the stream;
  * a tool's failure is a result the model reads (isError), not a protocol
    error, and the session outlives a tool that raises;
  * passwords: list_devices says whether one is saved, never what it is; a
    secret column or field reads <hidden>; the export is redacted;
  * the export section cannot carry a second console command;
  * devices are named the way the connection page names them, a clash is
    told apart by login@host, and a dropped session is reopened once.
"""

import io
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="x3-mcp-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")

from x3.proto import schema as sch  # noqa: E402
from x3.proto.device import DeviceError  # noqa: E402
from x3.ui import connstore as cs  # noqa: E402
from x3.mcp import tools as T  # noqa: E402
from x3.mcp.protocol import Server, ToolError, serve  # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"ok  {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}\n     got:  {got!r}\n     want: {want!r}")


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}" + (f"\n     {detail}" if detail else ""))


def wire(server, *msgs):
    """Replies, decoded, to ``msgs`` sent as lines."""
    out = io.StringIO()
    serve(server, io.StringIO("".join(
        (m if isinstance(m, str) else json.dumps(m)) + "\n" for m in msgs
    )), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


# --- protocol ----------------------------------------------------------------


def loud(_args):
    print("chatter a library left in")
    return "fine"


def broken(_args):
    raise RuntimeError("boom")


def refused(_args):
    raise ToolError("try another menu")


srv = Server([
    ({"name": "loud", "inputSchema": {"type": "object"}}, loud),
    ({"name": "broken", "inputSchema": {"type": "object"}}, broken),
    ({"name": "refused", "inputSchema": {"type": "object"}}, refused),
], version="1.2", instructions="read only")

replies = wire(
    srv,
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "loud"}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "broken"}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "refused"}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}},
    {"jsonrpc": "2.0", "id": 7, "method": "resources/list"},
    "{not json",
    {"jsonrpc": "2.0", "id": 8, "method": "ping"},
)
by_id = {r.get("id"): r for r in replies}
check("one reply per request, none for the notification",
      [r.get("id") for r in replies], [1, 2, 3, 4, 5, 6, 7, None, 8])
init = by_id[1]["result"]
check("a version the client asks for and we speak is the one agreed",
      init["protocolVersion"], "2025-03-26")
check("and the server says it has tools, its name, and how to use it",
      (init["capabilities"], init["serverInfo"], init["instructions"]),
      ({"tools": {"listChanged": False}}, {"name": "x3", "version": "1.2"}, "read only"))
check("an unknown version gets the newest we speak",
      srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "1999-01-01"}})["result"]["protocolVersion"],
      "2025-06-18")
check("tools/list lists them", [t["name"] for t in by_id[2]["result"]["tools"]],
      ["loud", "broken", "refused"])
check("a tool printing to stdout lands on stderr, not in the reply stream",
      by_id[3]["result"], {"content": [{"type": "text", "text": "fine"}], "isError": False})
check("a tool that raises is a result the model reads, and the session goes on",
      (by_id[4]["result"]["isError"], by_id[4]["result"]["content"][0]["text"]),
      (True, "RuntimeError: boom"))
check("a ToolError reads as itself", by_id[5]["result"]["content"][0]["text"],
      "try another menu")
check("an unknown tool is a protocol error", by_id[6]["error"]["code"], -32602)
check("so is an unknown method", by_id[7]["error"]["code"], -32601)
check("and a line that is not JSON", by_id[None]["error"]["code"], -32700)
ok("stdout is put back after serving", sys.stdout is not sys.stderr)


# --- the address book ---------------------------------------------------------

store = cs.Store()
store.own.extend([
    {"host": "10.0.0.1", "login": "admin", "password": "s3cret-one", "note": "Core",
     "group": "lab"},
    {"host": "10.0.0.2:8299", "login": "admin", "password": "", "note": "", "group": ""},
    {"host": "10.0.0.3", "login": "admin", "password": "x", "note": "Edge", "group": "a"},
    {"host": "10.0.0.4", "login": "ro", "password": "********", "note": "Edge", "group": "b"},
])
cs.save(store)

opened: list[str] = []


class FakeClient:
    def __init__(self, host):
        self.host = host
        self.fail_next = False
        self.closed = False

    def close(self):
        self.closed = True


def fake_connect(conn):
    opened.append(conn.host)
    return FakeClient(conn.host)


src = T.SavedDevices(os.path.join(_TMP, "missing.cdb"), connect=fake_connect)
box = T.ToolBox(src)

listing = box.list_devices({})
check("named by note, else address; a clash told apart by login@host",
      [line.split("\t")[0] for line in listing.splitlines()[1:]],
      ["Core", "10.0.0.2:8299", "Edge (admin@10.0.0.3)", "Edge (ro@10.0.0.4)"])
check("password saved: yes only for a real one, not empty, not the cdb mask",
      [line.split("\t")[-1] for line in listing.splitlines()[1:]],
      ["yes", "no", "yes", "no"])
ok("and no password text anywhere in the listing",
   "s3cret-one" not in listing and "********" not in listing)
check("filter narrows it", len(box.list_devices({"filter": "lab"}).splitlines()), 2)

check("resolve by name, case-insensitively", src.resolve("core").host, "10.0.0.1")
check("by address", src.resolve("10.0.0.3").login, "admin")
check("by login@host", src.resolve("ro@10.0.0.4").host, "10.0.0.4")
try:
    src.resolve("Edge")
    ok("the bare clashing name is refused", False)
except ToolError as e:
    ok("the bare clashing name is refused, naming both", "admin@10.0.0.3" in str(e)
       and "ro@10.0.0.4" in str(e), str(e))
try:
    src.resolve("nowhere")
    ok("an unknown device is refused", False)
except ToolError as e:
    ok("an unknown device is refused and points at list_devices", "list_devices" in str(e))

check("a call opens a session", src.run("Core", lambda c: c.host), "10.0.0.1")
check("the next reuses it", (src.run("Core", lambda c: c.host), opened), ("10.0.0.1", ["10.0.0.1"]))


first = src._clients[cs.entry_key("10.0.0.1", "admin")][0]


def flaky(c):
    if c is first:
        raise DeviceError("connection reset")
    return "read"


check("a session the router dropped is reopened once and the call retried",
      (src.run("Core", flaky), len(opened)), ("read", 2))
ok("and the dead one closed", first.closed)
src._clients[cs.entry_key("10.0.0.1", "admin")] = (FakeClient("10.0.0.1"), -1e9)
src.run("Core", lambda c: None)
check("a session idle past IDLE_CLOSE_S is replaced before use", len(opened), 3)
src.close()
check("close() closes them all", src._clients, {})


# --- menus -------------------------------------------------------------------

N = sch.NavNode
nav = [
    N(name="ip", title="IP", kind="folder", children=[
        N(name="addr", title="Addresses", kind="map", path=(20, 1)),
        N(name="dns", title="DNS", kind="folder", children=[
            N(name="static", title="Static", kind="map", path=(20, 2)),
        ], actions=[N(name="dnsset", title="Settings", kind="item", path=(20, 3))]),
        N(name="hide", title="Hidden", kind="map", path=(20, 4), hidden=True),
    ]),
    N(name="hs", title="Hotspot", kind="folder", children=[
        N(name="hsa", title="Addresses", kind="map", path=(30, 1)),
    ]),
]
menus = T.flatten_menus(nav)
check("every openable node, by its path, actions under their node, hidden left out",
      [(m.title, m.kind) for m in menus],
      [("IP/Addresses", "list"), ("IP/DNS/Settings", "form"),
       ("IP/DNS/Static", "list"), ("Hotspot/Addresses", "list")])
check("exact title", T.find_menu(menus, "ip/dns/static").title, "IP/DNS/Static")
check("a unique ending", T.find_menu(menus, "Settings").title, "IP/DNS/Settings")
check("a unique part", T.find_menu(menus, "stat").title, "IP/DNS/Static")
try:
    T.find_menu(menus, "Addresses")
    ok("an ambiguous name is refused", False)
except ToolError as e:
    ok("an ambiguous name is refused, listing the candidates",
       "IP/Addresses" in str(e) and "Hotspot/Addresses" in str(e), str(e))
check("a path copied off the menu bar reads as one",
      [T.find_menu(menus, w).title for w in ("IP ▸ DNS ▸ Static", "IP > Addresses")],
      ["IP/DNS/Static", "IP/Addresses"])
try:
    T.find_menu(menus, "IP/DNS/Statics")
    said = ""
except ToolError as e:
    said = str(e)
ok("a near miss is offered the menu it nearly named",
   "Did you mean 'IP/DNS/Static'" in said, said)

# --- columns: by the list's name or the console's ------------------------------
# A model writes the command line's property names; the list has its own.

COLS = ["", "Identity", "IP Address", "MAC Address", "Board Name", "Src. Address",
        "Dst. Address"]
check("columns by the console's names",
      T._pick_columns(None, COLS, ["address", "mac-address", "board", "dst-address"]),
      [0, 2, 3, 4, 6])
check("...and by the list's own, in any case", T._pick_columns(None, COLS, ["ip address"]),
      [0, 2])
try:
    T._pick_columns(None, ["", "Src. Address", "Dst. Address"], ["addr"])
    said = ""
except ToolError as e:
    said = str(e)
ok("a name that fits no column, or two, is refused with the list",
   "No column 'addr'" in said and "Src. Address" in said, said)


# --- reads: secrets ------------------------------------------------------------


class Row:
    def __init__(self, fields, note=""):
        self.fields = fields
        self.form_fields = []
        self._note = note

    def comment(self):
        return self._note


class ReadClient:
    schema = None

    def nav_tree(self):
        return [N(name="ppp", title="PPP", kind="folder", children=[
            N(name="sec", title="Secrets", kind="map", path=(27, 2)),
            N(name="prof", title="Profile", kind="item", path=(27, 3)),
        ])]

    info = type("I", (), {"board": "", "architecture": ""})()
    device_plugins: set = set()

    def table_rows(self, path, **_kw):
        rows = [Row([(1, "Name", "alice", True), (2, "Password", "hunter2", True),
                     (3, "<0x000003>", "7", True)], note="vpn user")]
        # Rx/Tx is a pair column: its members have no names of their own, so
        # only the list's cell carries it.
        return (None, ["", "Name", "Password", "Rx/Tx"],
                [["X", "alice", "hunter2", "1471 B / 3097 B"]], rows, None)

    def last_truncated(self):
        return None

    def menu_for(self, path, **_kw):
        return None

    def get(self, path, **_kw):
        return Row([(1, "Name", "default", True), (9, "IPsec Secret", "psk!", True)])

    complete = True

    def export_config(self, *, command=None, show_sensitive=True):
        self.export_args = (command, show_sensitive)
        from x3.proto.configexport import ConfigExport

        return ConfigExport(
            text="# 2026-10-28 by RouterOS 7.23.3\n"
                 "/interface wireless\n"
                 "# managed by CAPsMAN\n"
                 "set [ find default-name=wlan1 ] ssid=lab\n"
                 "/ip address add address=10.0.0.1/24 interface=ether1\n"
                 "/ip firewall filter add action=accept chain=input\n"
                 "/ip firewall nat add action=masquerade chain=srcnat\n"
                 "/ip firewall filtered-out add\n"
                 "/ppp secret add name=alice password=hunter2 service=l2tp\n",
            command=command or "/export terse",
            complete=self.complete,
        )


rc = ReadClient()


class OneDevice:
    def devices(self):
        return []

    def run(self, device, fn):
        return fn(rc)

    def close(self):
        pass


rb = T.ToolBox(OneDevice())
table = rb.read_menu({"device": "lab", "menu": "Secrets"})
ok("a list's secret column reads <hidden>", "hunter2" not in table
   and "<hidden>" in table, table)
ok("the comment comes along as the last column",
   table.splitlines()[1].endswith("comment") and table.splitlines()[2].endswith("vpn user"),
   table)
detail = rb.read_menu({"device": "lab", "menu": "Secrets", "all_fields": True})
ok("every-field view hides it too, and drops codes nobody can name",
   "hunter2" not in detail and "Password: <hidden>" in detail and "<0x" not in detail,
   detail)
ok("...and keeps a pair column the fields cannot name",
   "Rx/Tx: 1471 B / 3097 B" in detail and detail.count("Name: alice") == 1, detail)
form = rb.read_menu({"device": "lab", "menu": "Profile"})
ok("a form's secret field reads <hidden>", "psk!" not in form
   and "IPsec Secret: <hidden>" in form, form)
picked = rb.read_menu({"device": "lab", "menu": "Secrets", "columns": ["name"]})
check("columns picks by name, flags always first",
      picked.splitlines()[1:], ["flags\tName\tcomment", "X\talice\tvpn user"])

exported = rb.export_config({"device": "lab"})
check("export asks for the plain, non-sensitive form, whatever the section",
      rc.export_args, (None, False))
ok("and redacts what older firmware prints anyway", "hunter2" not in exported
   and "password=" in exported, exported)
check("a section is cut from the whole, header kept, by whole words",
      rb.export_config({"device": "lab", "section": "ip/firewall/filter"}).splitlines(),
      ["# 2026-10-28 by RouterOS 7.23.3",
       "/ip firewall filter add action=accept chain=input"])
check("a parent section takes all its children",
      len(rb.export_config({"device": "lab", "section": "/IP  Firewall"}).splitlines()), 4)
# A menu the router annotates comes as a bare path line with its notes and
# `set` lines under it, which carry no path of their own.
check("a bare path line keeps the lines under it",
      rb.export_config({"device": "lab", "section": "/interface wireless"}).splitlines()[1:],
      ["/interface wireless", "# managed by CAPsMAN", "set [ find default-name=wlan1 ] ssid=lab"])
check("...and they stay out of every other section",
      rb.export_config({"device": "lab", "section": "/ip address"}).splitlines()[1:],
      ["/ip address add address=10.0.0.1/24 interface=ether1"])
# A capture that stalled before the end has not shown that a section is absent:
# a slow router's export read as having no NAT at all.
rc.complete = False
try:
    rb.export_config({"device": "lab", "section": "/system"})
except ToolError as e:
    said = str(e)
else:
    said = ""
ok("a cut-short export says so, not 'nothing under'", "stopped before the end" in said, said)
rc.complete = True
for bad in ("/ip; /system reboot", "/ip address\n/system reset-configuration"):
    rc.export_args = None
    try:
        rb.export_config({"device": "lab", "section": bad})
    except ToolError:
        pass
    check(f"section {bad!r} never reaches the console", rc.export_args, (None, False))


# --- diagnostics: what may be typed at the console -----------------------------

from x3.mcp import diag  # noqa: E402

check("an IPv4 address goes through as itself", diag.address(" 8.8.8.8 "), "8.8.8.8")
check("IPv6 comes out in its canonical form",
      diag.address("2001:DB8:0:0::1"), "2001:db8::1")
check("a host name is allowed", diag.address("Dns.Google"), "dns.google")
for bad in ("1.1.1.1; /system reboot", "1.1.1.1 count=999", "[/system reboot]",
            "$x", "fe80::1%ether1", "a\nb", "1.2.3", "", "-c"):
    try:
        diag.address(bad)
        ok(f"address {bad!r} is refused", False)
    except ToolError:
        ok(f"address {bad!r} is refused", True)
check("numbers default when absent", diag.number(None, "count", 1, 20, 4), 4)
for bad in (0, 21, "4; /quit", 2.5j):
    try:
        diag.number(bad, "count", 1, 20, 4)
        ok(f"count {bad!r} is refused", False)
    except ToolError:
        ok(f"count {bad!r} is refused", True)
known = ["ether1", "bridge-lan", "evil$x", "WAN (fibre)"]
check("an interface is matched case-insensitively and typed as the device spells it",
      diag.interface("ETHER1", known), '"ether1"')
check("spaces and brackets in a real name are fine, quoted",
      diag.interface("wan (fibre)", known), '"WAN (fibre)"')
for bad in ("eth9", "evil$x", 'ether1" ; /system reboot ; "'):
    try:
        diag.interface(bad, known)
        ok(f"interface {bad!r} is refused", False)
    except ToolError:
        ok(f"interface {bad!r} is refused", True)
try:
    diag.routing_table("main; /quit")
    ok("a routing table with a command in it is refused", False)
except ToolError:
    ok("a routing table with a command in it is refused", True)
check("ping's command shape",
      diag.ping("8.8.8.8", count=3, size=1400, interface='"ether1"',
                src_address="10.0.0.1", routing_table="main"),
      '/ping address=8.8.8.8 count=3 size=1400 interface="ether1" '
      'src-address=10.0.0.1 vrf=main')
check("traceroute's", diag.traceroute("1.1.1.1", count=1, max_hops=15),
      "/tool traceroute address=1.1.1.1 count=1 max-hops=15")
check("and the log's has no where-clause to build",
      diag.LOG_COMMAND, "/log print without-paging")
check("log topics are the first word after the stamp",
      [sorted(T._log_topics(x)) for x in (
          "2026-10-28 06:39:47 system,info,account user admin logged in",
          "oct/28 06:39:47 dhcp,info client1 got an address",
          "06:39:47 wireless,info aa:bb connected",
      )],
      [["account", "info", "system"], ["dhcp", "info"], ["info", "wireless"]])


class ConsoleClient:
    """Answers run_console with canned output and remembers what was typed."""

    def __init__(self, text, complete=True, error=""):
        self.typed: list[str] = []
        self.text, self.complete, self.error = text, complete, error

    def interface_choices(self):
        return [(1, "ether1", "Ethernet"), (2, "bridge-lan", "Bridge")]

    def run_console(self, command, *, timeout):
        from x3.proto.configexport import ConfigExport

        self.typed.append(command)
        self.timeout = timeout
        return ConfigExport(text=self.text, complete=self.complete,
                            console_error=self.error, command=command)


def on(client):
    class Src:
        def devices(self):
            return []

        def run(self, device, fn):
            return fn(client)

        def close(self):
            pass

    return T.ToolBox(Src())


cc = ConsoleClient("SEQ HOST    \n  0 8.8.8.8 56 ok   \n  sent=1 received=1")
out = on(cc).ping({"device": "x", "address": "8.8.8.8", "count": 1, "interface": "Ether1"})
check("ping types one built command", cc.typed, ['/ping address=8.8.8.8 count=1 interface="ether1"'])
check("and the result opens with it, padding trimmed",
      out.splitlines(), ['# /ping address=8.8.8.8 count=1 interface="ether1"',
                         "SEQ HOST", "  0 8.8.8.8 56 ok", "  sent=1 received=1"])
cc = ConsoleClient("")
try:
    on(cc).ping({"device": "x", "address": "8.8.8.8; /system reboot"})
except ToolError:
    pass
check("a refused argument types nothing at all", cc.typed, [])
cc = ConsoleClient("partial", complete=False)
ok("a command cut off by its timeout says so",
   "may be partial" in on(cc).traceroute({"device": "x", "address": "1.1.1.1"}))
ok("and traceroute's timeout covers every hop going silent",
   cc.timeout >= 15 * 1 * 1.5)
cc = ConsoleClient("", error="bad command name traceroute")
try:
    on(cc).traceroute({"device": "x", "address": "1.1.1.1"})
    ok("a console refusal is a tool error", False)
except ToolError as e:
    ok("a console refusal is a tool error", "bad command name" in str(e))

log = "\n".join([
    " 2026-10-28 04:42:56 dhcp,info client1 on ether1 got IP address 10.0.2.15",
    " 2026-10-28 05:00:00 system,error,critical login failure for user x",
    " 2026-10-28 05:01:00 system,info,account user admin logged in",
    " 2026-10-28 05:02:00 ppp,info secret password=hunter2 added",
    " 2026-10-28 05:03:00 dhcp,warning offering lease without success",
])
cc = ConsoleClient(log)
check("read_log narrows by any listed topic",
      on(cc).read_log({"device": "x", "topics": "error, warning"}).splitlines()[1:],
      ["2026-10-28 05:00:00 system,error,critical login failure for user x",
       "2026-10-28 05:03:00 dhcp,warning offering lease without success"])
check("by text, keeping the newest, and says how many it dropped",
      on(cc).read_log({"device": "x", "contains": "DHCP", "lines": 1}).splitlines()[1:],
      ["# last 1 of 2 lines",
       "2026-10-28 05:03:00 dhcp,warning offering lease without success"])
ok("console output is redacted like the export",
   "hunter2" not in on(cc).read_log({"device": "x"}))
check("no match is said, not an empty answer",
      on(cc).read_log({"device": "x", "topics": "ospf"}).splitlines()[-1], "No log lines match.")
cc = ConsoleClient("name: ether1")
on(cc).interface_traffic({"device": "x", "interface": "BRIDGE-LAN"})
check("interface_traffic types the device's own spelling, quoted, once",
      cc.typed, ['/interface monitor-traffic "bridge-lan" once'])


print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all mcp checks passed")
