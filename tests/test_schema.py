"""Offline tests for the .jg parser, the schema extractor and the M2 codec.

No device and no pytest required:

    .venv/bin/python test_schema.py

Optionally point it at a real plugin cache to parse every file in it:

    python tests/test_schema.py <plugin-set-dir>
"""

import glob
import os
import sys

from Crypto.Util.Padding import unpad as _aes_unpad

from x3.proto import jg
from x3.proto import message as m
from x3.proto import modern
from x3.proto import schema as sch

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    if not cond:
        failures.append(label)


# --- parser -----------------------------------------------------------------

check("bare keys + single quotes",
      jg.loads("{name:'IP Scan',type:'query'}"),
      {"name": "IP Scan", "type": "query"})
check("int, hex, negative, bool, null",
      jg.loads("{a:12,b:0x1f,c:-3,d:true,e:false,f:null}"),
      {"a": 12, "b": 31, "c": -3, "d": True, "e": False, "f": None})
check("nested arrays and objects",
      jg.loads("[{path:[ 85,1 ],c:[{id:'u1'},{id:'s2'}]}]"),
      [{"path": [85, 1], "c": [{"id": "u1"}, {"id": "s2"}]}])
check("trailing commas", jg.loads("[1,2,]"), [1, 2])
check("escaped quote in string", jg.loads(r"{s:'it\'s'}"), {"s": "it's"})
check("brace inside string is not structure", jg.loads("{s:'a{b}c'}"), {"s": "a{b}c"})
check("empty containers", jg.loads("[{},[]]"), [{}, []])

for bad in ("{a:1", "[1,2", "{a:}", "{a:1}junk"):
    try:
        jg.loads(bad)
        failures.append(f"parser accepted malformed input {bad!r}")
    except jg.JgError:
        pass

# --- prefix/wire-tag mapping ------------------------------------------------

for prefix, tag in sch.CANONICAL_TAG.items():
    ok(f"prefix {prefix!r} canonical tag {tag:#x} is a known codec tag",
       tag in vars(m).values())
    ok(f"prefix {prefix!r} accepts its own canonical tag",
       tag in sch.ACCEPTED_TAGS[prefix])
for prefix in sch.CANONICAL_TAG:
    ok(f"uppercase {prefix!r} maps to an array tag",
       (sch.CANONICAL_TAG[prefix] & 0x80) == (0x80 if prefix.isupper() else 0x00))

# --- extraction rule 1: a node with an id owns its children -----------------
# This is the /user shape: 'Name' is s1, and M10's nested union also uses id u1.
# A naive flatten overwrites field 1 with the nested u1 and loses the name.
USER = [{
    "name": "System", "group": "System", "c": [{
        "name": "User", "type": "map", "path": [13, 1], "nameval": "Name", "prefix": "user",
        "c": [
            {"name": "Name", "type": "string", "id": "s1"},
            {"name": "Allowed Address", "type": "multi", "id": "M10", "c": [
                {"type": "union", "c": [
                    {"type": "network", "id": "u1", "maskid": "u2"},
                    {"type": "network6", "id": "a3"},
                ]},
            ]},
            {"name": "Timeout", "type": "opt", "c": [
                {"type": "interval", "id": "u29", "scale": 1000, "postfix": "s"},
            ]},
            {"name": "Policy", "type": "enm", "id": "u2a",
             "values": {"type": "static", "map": ["none", "logout"]}},
            {"name": "Group", "type": "enm", "id": "u2",
             "values": {"type": "dynamic", "path": [13, 2]}},
            {"name": "Expire Password", "type": "doit", "cmd": 10},
        ],
    }],
}]

menus = []
sch._walk(USER, None, "test.jg", menus)
user = [x for x in menus if x.kind == "map"][0]

check("rule 1: field 1 keeps its own declaration", user.fields[0x01].name, "Name")
check("rule 1: field 1 stays a string", user.fields[0x01].prefix, "s")
check("rule 1: nested union is held under the array field",
      sorted(user.fields[0x10].nested), [0x01, 0x03])
ok("rule 1: nested ids do not leak to the parent", user.fields[0x01].jg_type == "string")
check("rule 2: id-less 'opt' container exposes its child", user.fields[0x29].name, "Timeout")
check("static enum unpacked", user.fields[0x2A].enum, {0: "none", 1: "logout"})
check("dynamic enum records its source handler", user.fields[0x02].enum_path, (13, 2))
check("group inherited from the ancestor node", user.group, "System")
check("cli prefix captured", user.cli_prefix, "user")
check("map reads with GETALL", user.read_command, 0xFE0004)
check("nested doit captured as its own menu",
      [x.name for x in menus if x.kind == "doit"], ["Expire Password"])
check("doit keeps its command number",
      [x.command for x in menus if x.kind == "doit"], [10])

# --- extraction rule 3: one path can host several menus ---------------------

SHARED = [{
    "name": "Iface", "type": "map", "path": [20, 0],
    "c": [{"name": "Name", "type": "string", "id": "s1"}],
}, {
    "name": "Iface Queue", "type": "map", "path": [20, 0],
    "c": [{"name": "Rate", "type": "number", "id": "u1"}],
}]
menus2 = []
sch._walk(SHARED, None, "test.jg", menus2)
s2 = sch.Schema(menus2)
check("rule 3: both menus retained at the shared path", len(s2.by_path[(20, 0)]), 2)
check("rule 3: field 1 differs per menu",
      (menus2[0].fields[1].prefix, menus2[1].fields[1].prefix), ("s", "u"))
check("record() prefers the larger field set", s2.record((20, 0)).name, "Iface")

# --- an address with its port elsewhere, and a tray read from elsewhere -----
# Connections, up to 7.19 at least: each address is an ipaddrandport whose port
# sits under `portid`, and the window's tray is read from Connection Tracking
# (20,17), its Max Entries on u14 -- the code the reply port arrives in. The
# address read as a bare number (16777343) and the port as "Max Entries".

CONN = [{
    "name": "Connection", "type": "map", "path": [20, 32],
    "c": [
        {"name": "Src. Address", "type": "ipaddrandport", "id": "u7", "portid": "u9"},
        {"name": "Reply Dst. Address", "type": "ipaddrandport", "id": "uc",
         "portid": "ue"},
        {"type": "statusbar", "path": [20, 17],
         "c": [{"name": "Max Entries", "type": "number", "id": "ue"}]},
    ],
}]
menus3 = []
sch._walk(CONN, None, "test.jg", menus3)
s3 = sch.Schema(menus3)
conn = menus3[0]


def conn_row(msg):
    return {name: val for _c, name, val, _ok in s3.decode((20, 32), msg, menu=conn)}


got = conn_row(m.Message().add_u32(7, 16777343).add_u32(9, 40123)
               .add_u32(0xC, 0x0F02000A).add_u32(0xE, 53))
check("an ipaddrandport reads address:port", got.get("Src. Address"), "127.0.0.1:40123")
check("...the reply side too", got.get("Reply Dst. Address"), "10.0.2.15:53")
check("...and the address alone when there is no port (ICMP)",
      conn_row(m.Message().add_u32(7, 16777343)).get("Src. Address"), "127.0.0.1")
ok("a tray read from another handler names no field of the row",
   "Max Entries" not in got)
check("...and is not drawn from the row either", conn.statusbar, [])

# --- rendering --------------------------------------------------------------

check("enum renders as a name", user.fields[0x2A].render(1), "logout")
check("enum falls back to the number when unmapped", user.fields[0x2A].render(9), "9")
check("scale + postfix applied", user.fields[0x29].render(600000), "600s")
ipf = sch.Field(code=1, prefix="u", name="ip", jg_type="ipaddr")
# The router puts IPv4 on the wire little-endian: the first dotted octet is the low byte.
check("ipaddr u32 renders dotted-quad", ipf.render(0x0158A8C0), "192.168.88.1")
netf = sch.Field(code=1, prefix="u", name="net", jg_type="network")
check("network renders like ipaddr", netf.render(0x0058A8C0), "192.168.88.0")
macf = sch.Field(code=1, prefix="r", name="mac", jg_type="macaddr")
check("macaddr raw renders colon-separated",
      macf.render(b"\x48\xa9\x8a\x12\x34\x56"), "48:A9:8A:12:34:56")

# --- tolerance for how the router actually encodes ---------------------------------

ok("integers accepted at any width", all(
    sch.Field(code=1, prefix="u", name="n", jg_type="number").accepts(t)
    for t in (m.T_U32, 0x09, m.T_U64)))
ok("declared array accepted as a collapsed scalar",
   sch.Field(code=1, prefix="U", name="n", jg_type="multinumber").accepts(m.T_U32))

# --- codec: zero-length nested message --------------------------------------
# The router sends field 0x33 on the File handler (72,*) as an empty message.

check("empty payload decodes to an empty message", len(list(m.Message.from_bytes(b""))), 0)
frame = m.Message().add_u32(0x01, 7)
frame._entries.append((0x33, m.T_MESSAGE, m.Message()))
round_tripped = m.Message.from_bytes(frame.to_bytes())
check("empty nested message survives a round trip",
      [c for c, _t, _v in round_tripped], [0x01, 0x33])
check("empty nested message decodes as an empty Message",
      len(list(round_tripped.find(0x33)[1])), 0)
try:
    m.Message.from_bytes(b"XX\x00")
    failures.append("a non-empty frame with bad magic was accepted")
except m.CodecError:
    pass

# --- AES frame padding must match the router byte-for-byte --------------------
# A frame padded with plain PKCS#7 gains a whole extra 16-byte block whenever
# the M2+HMAC length is already block-aligned. The router accepts the long form,
# so x3ctl talking to a device never noticed; real Winbox rejects it with
# "Decrypt failed". That made the MITM proxy die on whichever file happened to
# end on a poisoned chunk size -- roteros.jg.gz on firmware 7.23.1, every session.

_pad_mismatch = []
_unaligned = []
for _n in range(3, 2100):
    _m2 = b"M2" + b"x" * (_n - 2)
    _mac = b"m" * 20
    _plain = modern.ros_pad(_m2, _mac)
    _L = len(_m2) + len(_mac)
    if len(_plain) != _L + 1 + ((0xF - _L) % 16):
        _pad_mismatch.append(_n)
    if len(_plain) % 16:
        _unaligned.append(_n)
ok("padded length matches the router's rule at every size", not _pad_mismatch)
ok("padded length is always a 16-byte multiple", not _unaligned)

# The specific frame that killed every proxy session: the final 15,713-byte
# chunk of a 933,217-byte plugin file.
_eof_reply = (m.Message()
              .add_u32_array(m.SYS_TO, [0, 1])
              .add_u32_array(m.SYS_FROM, [2, 2])
              .set_request_id(36)
              .add_string_array(0xFF001C, [b"msg-proxy-7.23.1"])
              .add_bool(0x000004, True)
              .add_u32(0xFF0003, 2)
              .add_raw(0x000003, b"\x00" * 15713)).to_bytes()
check("the poisoned eof frame is the aligned case", (len(_eof_reply) + 20) % 16, 15)
check("it pads to exactly one byte, not a whole block",
      len(modern.ros_pad(_eof_reply, b"m" * 20)) - (len(_eof_reply) + 20), 1)

# ros_pad must stay the exact inverse of the receive path in _recv_encrypted.
for _n in (3, 100, 15807 - 20 + 1, 2048):
    _m2 = b"M2" + b"y" * (_n - 2)
    _mac = b"z" * 20
    _plain = modern.ros_pad(_m2, _mac)
    if _plain[-1] != 0:
        _plain = _aes_unpad(_plain, 16)
    _plain = _plain[:-1]
    if (_plain[:-20], _plain[-20:]) != (_m2, _mac):
        failures.append(f"ros_pad does not invert the receive path at n={_n}")
ok("ros_pad inverts the receive path", True)

# --- plugin mirroring -------------------------------------------------------
# Plugin definitions are served gzipped under '<name>.gz'; icon sheets are not.
# Requesting a bare '<name>.jg' fails with "cannot open source file".

check("plugin definitions get .gz appended", sch.wire_name("roteros.jg"), "roteros.jg.gz")
check("icon sheets are fetched bare", sch.wire_name("icons.png"), "icons.png")
# The index's `unique` field is not the wire name: a decrypted Winbox 4 session
# shows a real device answering `roteros.jg.gz`, the index name plus .gz.
check("unique is not the wire name", sch.wire_name("roteros.jg"), "roteros.jg.gz")

import gzip
import tempfile

INDEX = [
    {"name": "secure.jg", "crc": 2101426697, "size": 22428},
    {"name": "icons.png", "crc": 1110366916, "size": 25219},
]
payloads = {
    "secure.jg.gz": gzip.compress(b"[{name:'IPsec',type:'map',path:[85,1]}]"),
    "icons.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
}
asked = []


def fake_fetch(name):
    asked.append(name)
    if name not in payloads:
        raise RuntimeError("cannot open source file")
    return payloads[name]


with tempfile.TemporaryDirectory() as tmp:
    got, cached, failed = sch.download_plugin_set(INDEX, 3411267040, tmp, fake_fetch)
    check("mirrored both files", (got, cached, failed), (2, 0, 0))
    check("asked for the gz form of the plugin", asked, ["secure.jg.gz", "icons.png"])
    check("plugin stored decompressed under its index name",
          open(os.path.join(tmp, "secure.jg"), "rb").read()[:2], b"[{")
    check("icon stored verbatim",
          open(os.path.join(tmp, "icons.png"), "rb").read()[:4], b"\x89PNG")
    check("per-file .crc marker holds that file's own crc, as Winbox writes it",
          open(os.path.join(tmp, "secure.crc")).read().strip(), "2101426697")

    # A second pass must trust the markers and re-fetch nothing.
    asked.clear()
    got, cached, failed = sch.download_plugin_set(INDEX, 3411267040, tmp, fake_fetch)
    check("second pass is fully cached", (got, cached, failed), (0, 2, 0))
    check("second pass fetched nothing", asked, [])

    # A changed CRC means a new build of that file: re-fetch it.
    asked.clear()
    moved = [dict(INDEX[0], crc=1), INDEX[1]]
    got, cached, failed = sch.download_plugin_set(moved, 3411267040, tmp, fake_fetch)
    check("changed crc re-fetches just that file", (got, cached, failed), (1, 1, 0))
    check("re-fetch used the gz name", asked, ["secure.jg.gz"])

    # The mirrored directory must be loadable as a schema.
    loaded = sch.Schema.load(tmp)
    check("mirrored cache parses as a schema", [x.name for x in loaded.menus], ["IPsec"])

    # A fetch failure is contained, not fatal.
    got, cached, failed = sch.download_plugin_set(
        [{"name": "missing.jg", "crc": 5}], 1, tmp, fake_fetch)
    check("a failed file is reported, not raised", (got, cached, failed), (0, 0, 1))

# --- navigation order (needs the real plugin set; it is gitignored) ---------

_SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "7.21.1-151146055")

if os.path.isdir(_SCHEMA_DIR):
    real = sch.Schema.load(_SCHEMA_DIR)
    titles = [n.title for n in real.nav]
    # Winbox sorts the left menu on the .jg `prio` key. The interesting part is
    # that WireGuard (100) lands between Interfaces (99) and Bridge (101).
    check("nav follows .jg prio", titles, [
        "Quick Set", "WiFi", "Wireless", "Interfaces", "WireGuard", "Bridge",
        "PPP", "Switch", "Mesh", "IP", "IPv6", "MPLS", "Routing", "System",
        "Queues", "Files", "Log", "RADIUS", "Tools", "Terminal", "Dot1X",
        "LCD", "Marks", "Partition", "Make Supout.rif",
    ])
    by_title = {n.title: n for n in real.nav}
    check("prio read from the .jg",
          [by_title[t].prio for t in ("Quick Set", "Interfaces", "WireGuard", "Partition")],
          [10, 99, 100, 999])
    # Synthesized group folders have no prio of their own; they inherit the
    # smallest prio among their members. Getting this wrong sank IP/IPv6.
    check("group folders inherit a prio",
          [by_title[t].prio for t in ("IP", "IPv6", "MPLS", "Routing", "System")],
          [110, 110, 110, 111, 120])
    # Merging folders across plugins must fill gaps, not let the first file win:
    # dhcp.jg names the IP/IPv6 groups without a groupIcon.
    check("group icons survive a cross-plugin merge",
          [by_title[t].icon for t in ("IP", "IPv6")], [6, 7])

    # Interface types are path-less `inherit:'iface'` overlays; they read the
    # generic's handler (20,0) and are told apart by typevalue.
    iface_kids = {k.title: k for k in by_title["Interfaces"].children}
    ok("interface types reach the nav",
       all(t in iface_kids for t in ("Ethernet", "VLAN", "VXLAN", "Bonding", "VRRP")))
    check("overlays resolve to the generic path",
          {iface_kids[t].path for t in ("Ethernet", "VLAN", "Bonding")}, {(20, 0)})
    check("overlays keep their typevalue",
          [iface_kids[t].type_value for t in ("Ethernet", "VLAN", "Bonding")], [1, 30, 40])
    check("sharing a path does not collapse the nodes",
          len({k.node_id for k in by_title["Interfaces"].children}),
          len(by_title["Interfaces"].children))
    ok("nonpublic overlays stay out", not any(
        t in iface_kids for t in ("PWR", "Loopback", "VRF")))
    ok("no nav node carries the raw 0xffffffff typevalue", not any(
        k.type_value == 0xFFFFFFFF for n in real.nav for k in n.children))

    # --- nameless maps are vocabularies, not menus --------------------------
    # 24 maps in 7.21 declare no `name` and no `title`, `nameval:'Name'` and a
    # single Name column at 0xfe0010. Each is the target of a
    # `values:{type:'dynamic',path:[...]}` dropdown: (20,2) is a filter rule's
    # Chain and Jump Target, (20,34) the Address List boxes across firewall,
    # DHCP and Hotspot, (138,32) WiFi Security's Multi Passphrase Group,
    # (13,3) the policy list a user group is built from. Treating them as
    # menus put a tab labelled '?' on six windows.
    def _walk_nav(nodes):
        for n in nodes:
            yield n
            yield from _walk_nav(n.children)

    ok("no nav entry is nameless", not [
        n.path for n in _walk_nav(real.nav) if not n.title or n.title == "?"])

    def _tab_strip(group, folder):
        kids = next(k for k in by_title[group].children if k.title == folder)
        return [k.title for k in kids.children]

    check("Firewall's tab strip is Winbox's, with no '?' between the rules",
          _tab_strip("IP", "Firewall"),
          ["Filter Rules", "NAT", "Mangle", "Raw", "Service Ports",
           "Connections", "Tracking", "Address Lists", "Layer7 Protocols"])
    check("...and Logging keeps only its two lists",
          _tab_strip("System", "Logging"), ["Rules", "Actions"])
    # Out of the nav, still in the schema: the dropdowns read them by path.
    ok("the vocabularies are still records", all(
        real.record(p) is not None and real.record(p).nameval
        for p in ((20, 2), (20, 34), (138, 32), (13, 3), (3, 3))))

    # --- overlay family bases -----------------------------------------------
    # `typevalue:0xffffffff` is not a type, it is the base of a family: Winbox
    # shows it as a real tab listing every type under it. Dropping it made
    # Wireless open on W60G (empty on any router without a 60GHz radio) and
    # PPP open on PPTP Server instead of Interface.
    wireless = {k.title: k for k in by_title["Wireless"].children[0].children}
    check("the family base leads the Wireless window",
          by_title["Wireless"].children[0].children[0].title, "WiFi Interfaces")
    check("a family base carries the family, not a type",
          (wireless["WiFi Interfaces"].generic,
           wireless["WiFi Interfaces"].type_value,
           wireless["WiFi Interfaces"].path),
          ("wlan", None, (20, 0)))
    check("the family's types are what its list shows",
          sorted(real.family_type_values("wlan")), [35, 36, 38])
    check("PPP's family base too",
          [k.title for k in by_title["PPP"].children][:1], ["Interface"])
    ok("PPP's base does not collide with Interfaces ▸ Interface",
       by_title["PPP"].children[0].node_id != iface_kids["Interface"].node_id
       if "Interface" in iface_kids else True)
    # A generic declared on an overlay reads the handler it inherits, so
    # `inherit:'wlan'` menus resolve to (20,0) like the rest of the family.
    check("a generic declared on an overlay resolves transitively",
          real.generic_paths.get("wlan"), (20, 0))
    ok("Nstreme Dual reaches the nav through it", "Nstreme Dual" in wireless)

    # --- overlay chains -----------------------------------------------------
    # A wireless interface is type 35 for every Atheros card; `hwtype` then
    # picks the concrete one. Resolving only the first level left wlan1 with
    # the bare Interface form and no Wireless tab at all.
    iface_base = real.record((20, 0))
    hwtype_code = next(
        c for c, f in real.overlay_by_generic["wlan"].fields.items()
        if f.name == "hwtype"
    )
    check("one level still resolves",
          [o.title for o in real.overlay_chain(iface_base, 1)], ["Ethernet"])
    check("the chain walks the family then the hardware slot",
          [o.title for o in real.overlay_chain(iface_base, 35, {hwtype_code: 12})],
          ["WiFi Interfaces", "Wireless", "Wireless (Hardware)", "Wireless (IPQ4019)"])
    check("an unknown hwtype still gets the family default",
          [o.title for o in real.overlay_chain(iface_base, 35, {hwtype_code: 999})],
          ["WiFi Interfaces", "Wireless", "Wireless (Hardware)"])
    check("a type under a different family resolves through it",
          [o.title for o in real.overlay_chain(iface_base, 19)],
          ["Interface", "PPP Server"])
    ok("a type in no family gets no overlay",
       real.overlay_chain(iface_base, 0xDEAD) == [])
    # Every tab Winbox v3 draws on a wireless interface, including the six a
    # *named* `type:deck` opens (Data Rates, Advanced, HT MCS, Tx Power,
    # Current Tx Power, Advanced Status). An anonymous deck is an in-place
    # field swap and keeps the tab it sits in; a named one is a tab.
    check("the merged form keeps Winbox's tab order",
          real.record_for((20, 0), 35, {hwtype_code: 12}).tab_names(),
          ["General", "Wireless", "Data Rates", "Advanced", "HT", "HT MCS",
           "WDS", "Nstreme", "NV2", "Tx Power", "Current Tx Power",
           "Advanced Status", "Compression Status", "Status", "Traffic"])
    check("a family list's columns come from the family overlay",
          real.record_for_family((20, 0), "wlan").tab_names(),
          ["General", "Wireless", "Status", "Traffic"])

    # --- menu-level pred guards ---------------------------------------------
    # W60G is declared `board ~ .*60ad.*`: Winbox does not show the tab at all
    # on a router without a 60GHz radio.
    def wireless_tabs(board, arch):
        tree = sch.filter_nav_by_pred(real.nav, board=board, arch=arch)
        folder = next(n for n in tree if n.title == "Wireless")
        return [k.title for k in folder.children[0].children]

    ok("a board guard hides its menu",
       "W60G" not in wireless_tabs("EDGE10-LAB", "arm"))
    ok("...and shows it on a board that matches",
       "W60G" in wireless_tabs("wAP 60adG", "arm"))
    ok("an unguarded sibling is untouched",
       "W60G Station" in wireless_tabs("EDGE10-LAB", "arm"))
    ok("no board means no filtering",
       "W60G" in [k.title for k in by_title["Wireless"].children[0].children])
    check("unreadable pred terms count as true",
          sch.menu_pred_holds({"type": "syscap", "value": 2}, board="EDGE10"), True)
    check("an arch guard is evaluated",
          [sch.menu_pred_holds({"type": "not", "pred": {"type": "arch",
                                                        "value": "smips"}},
                               board="EDGE10", arch=a)
           for a in ("arm", "smips")],
          [True, False])
    # ipv6.jg wraps its tunnels in containers that hold nothing but overlays;
    # those must fold into Interfaces rather than become top-level entries.
    ok("overlay-only containers are re-homed",
       "gre6" not in titles and "GRE6 Tunnel" in iface_kids)
    check("generic paths are indexed", real.generic_paths.get("iface"), (20, 0))
    # prefix / nonpublic used to be dropped on the overlay branch of _walk.
    overlay = real.overlays["iface"]
    check("overlay passthrough", (overlay[30].cli_prefix, overlay[63].nonpublic),
          ("vlan", True))

    # Command menus (Ping, Torch, ...) reach the nav when a wrapper holds
    # exactly one of them; the per-window command buttons stay out.
    tools = {k.title: k for k in by_title["Tools"].children}
    ok("Tools reaches its commands", all(
        t in tools for t in ("Ping", "Traceroute", "Torch", "Bandwidth Test",
                             "IP Scan", "WoL", "Speed Test", "Profile")))
    check("a command node carries the child's kind and path",
          (tools["Ping"].kind, tools["Ping"].path), ("query", (22,)))
    ok("multi-child wrappers stay folders",
       tools["Packet Sniffer"].kind == "folder" and tools["RoMON"].kind == "folder")
    system = {k.title: k for k in by_title["System"].children}
    ok("System reaches Reboot / Shutdown / Password",
       all(system[t].kind == "doit" for t in ("Reboot", "Shutdown", "Password")))

    # A query has two field namespaces and they overlap: Ping declares u3 as
    # both "Packet Size" (request) and "Reply Size" (result). Merging them, as
    # _walk used to, silently loses one.
    ping = real.command_menu((22,), "query", "Ping")
    req = {f.name for f in ping.request_fields.values() if f.name}
    res = {f.name for f in ping.fields.values() if f.name}
    ok("the request parameter survives", "Packet Size" in req)
    ok("the result column survives", "Reply Size" in res)
    ok("the two namespaces are distinct", "Reply Size" not in req)
    check("command ids are read",
          (ping.start_command, ping.cancel_command, ping.auto_limit, ping.auto_scroll),
          (0xFE000F, 0xFE0011, 1000, True))
    torch = real.command_menu((45, 5), "query", "Torch")
    check("handler-local command ids too",
          (torch.start_command, torch.cancel_command), (1, 2))
    # query_columns keeps declaration order and does not cap at 8.
    check("Ping result columns in .jg order",
          [f.name for f in sch.query_columns(ping)],
          ["Seq #", "Status", "Time", "Reply Size", "TTL", "Min", "Avg", "Max"])
    ok("Torch keeps all 18 columns", len(sch.query_columns(torch)) == 18)
    ok("Ping's min/avg/max tray is parsed", len(ping.statusbar) >= 3)

    # Request encoding, checked against the bytes real Winbox put on the wire
    # (a session capture, decoded in the query-tools README section). The
    # trap: `addr` is a nested message, `ipaddr` is a bare u32, and only the
    # semantic .jg type says which.
    addr_tag, addr_val = sch.encode_form_value(ping.request_fields[0x16], "8.8.8.8")
    check("an addr wraps the v4 address in a nested message",
          (addr_tag, [(c, v) for c, _t, v in addr_val]),
          (m.T_MESSAGE, [(sch.ADDR_INNER_V4, 134744072)]))
    check("an ipaddr stays a bare u32",
          sch.encode_form_value(ping.request_fields[0x09], "10.0.0.1"),
          (m.T_U32, 0x0100000A))
    check("an unset address is 0, not an error",
          sch.encode_form_value(ping.request_fields[0x09], ""), (m.T_U32, 0))
    check("numbers keep their tag",
          sch.encode_form_value(ping.request_fields[0x03], "56"), (m.T_U32, 56))
    check("bools use the tag, not a payload",
          sch.encode_form_value(ping.request_fields[0x0B], False),
          (m.T_BOOL_FALSE, False))
    check("ipv4 round-trips", sch.format_ipv4(sch.parse_ipv4("192.168.87.1")),
          "192.168.87.1")
    # Round-tripping is not enough: the pair was self-consistent and still
    # byte-reversed against the wire. The router uses both orders, so each is
    # pinned to the number that was actually observed.
    check("a record menu's ipaddr puts the first octet in the low byte",
          sch.parse_ipv4("192.168.88.1"), 0x0158A8C0)
    check("...which is what Winbox sent when adding 192.168.234.1",
          sch.parse_ipv4("192.168.234.1"), 32155840)
    check("...and how a netmask encodes",
          sch.parse_ipv4("255.255.255.0"), 16777215)
    check("a nested addr is network order instead",
          sch.parse_ipv4_net("192.168.87.1"), 0xC0A85701)
    check("net order round-trips too",
          sch.format_ipv4_net(sch.parse_ipv4_net("8.8.4.4")), "8.8.4.4")
    check("the two orders really do disagree",
          sch.format_ipv4(sch.parse_ipv4_net("192.168.87.1")), "1.87.168.192")
    # Ping To is the nested one; a bare ipaddr field is not.
    _tag, _inner = sch.encode_form_value(ping.request_fields[0x16], "8.8.4.4")
    check("Ping To encodes in network order",
          (_tag, [(c, v) for c, _t, v in _inner]),
          (m.T_MESSAGE, [(sch.ADDR_INNER_V4, 0x08080404)]))

# --- type:'age' is a stamp on the uptime clock, not a duration -------------
# Verified on a live router (uptime 16681094s): poll twice and the value does
# not tick, so the duration only exists relative to /system/resource uptime.
_cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "7.21.1-151146055")
leases = None
if os.path.isdir(_cache):
    leases = sch.Schema.load(_cache).record((23, 1))
else:
    print("skipped age-field tests: no 7.21.1-151146055 plugin cache")
if leases is not None:
    ok("Leases is a collection with age fields",
       leases.is_collection and leases.has_age_field)
    # 0x7b is declared twice -- s7b Client ID, u7b Age -- so the age scan has
    # to look through tag variants, not just the base declaration.
    check("age codes include the variant-only Age id",
          sorted(leases.age_codes()), [0x67, 0x77, 0x7B])

    last_seen = leases.fields[0x77]
    check("no uptime, no duration: the raw stamp shows through",
          last_seen.render(16680494), "16680494")
    check("a past stamp reads as elapsed time",
          last_seen.render(16680494, uptime=16681094), "00:10:00")
    check("a future stamp (Expires After) reads as time remaining",
          leases.fields[0x67].render(16681694, uptime=16681094), "00:10:00")
    check("'never' still wins over the clock",
          last_seen.render(0xFFFFFFFF, uptime=16681094), "never")
    check("'sometime' still wins over the clock",
          last_seen.render(0xFFFFFFFE, uptime=16681094), "sometime")
    # Leases restored from disk carry a previous boot's clock, which arrives as
    # a negative u32. Subtracting gave a 48000-day gap before this guard.
    check("a previous-boot stamp says nothing rather than 48213d",
          last_seen.render(4217407898, uptime=16681094), "")
    check("format_age_stamp is direction-agnostic",
          sch.format_age_stamp(100, 700), sch.format_age_stamp(700, 100))

# --- type overlays key on the field the schema names, not a guessed code ----
# IP → Routes and IPv6 → Routes are both handler (44,21), told apart by the
# nav's typevalue (2 / 3). Matching that against hardcoded interface codes
# found nothing on a route, so the list filtered all 55 rows away and showed
# an empty window.
if os.path.isdir(_cache):
    _s = sch.Schema.load(_cache)
    route = _s.record((44, 21))
    iface = _s.record((20, 0))
    check("Route declares its discriminator", route.typeon, "rtype")
    check("Interface declares its own", iface.typeon, "type")
    check("the route type field resolves to rtype's code",
          sch.type_field_code(route), 0x81)
    check("a menu with no typeon resolves to nothing",
          sch.type_field_code(leases), None)

    ipv4_route = {0x81: 2, 0x01: 12345}
    check("the declared field wins", sch.wire_type_value(ipv4_route, route), 2)
    # Without the menu there is nothing to key on but the interface codes, and
    # a route's 0x01 is a real field with a real number in it -- so answering
    # from a guess would be confidently wrong, not merely unhelpful.
    check("guessing is off once the schema has spoken",
          sch.wire_type_value({0x01: 12345}, route), None)
    check("menus without typeon keep the interface fallback",
          sch.wire_type_value({0x10001: 6}), 6)


# --- picking a plugin cache -------------------------------------------------

# One firmware version can be cached many times over: one directory per package set,
# plus one per build of our own router, which serves a few menus under a real
# Firmware version. Picking by name sorted '7.21.1-922503154' (a 5 KB stub)
# above '7.21.1-151146055' (the real 918 KB set), so a 7.21.1 device got a tree
# with no Routing > GMP in it -- no error anywhere, just a menu that was gone.

import tempfile as _tempfile

with _tempfile.TemporaryDirectory() as _base:
    def _cache(name, size, crc=None):
        d = os.path.join(_base, name)
        os.makedirs(d)
        with open(os.path.join(d, "roteros.jg"), "w") as fh:
            fh.write("[]" + " " * size)
        if crc is not None:
            with open(os.path.join(d, "roteros.crc"), "w") as fh:
                fh.write(str(crc))
        return d

    stub = _cache("7.21.1-922503154", 5_000, 922503154)
    real = _cache("7.21.1-151146055", 918_000, 151146055)
    other = _cache("7.21.1-151146050", 918_000, 151146050)

    check("build id comes from roteros.crc", sch.build_id_of_dir(real), 151146055)
    check("build id falls back to the directory name",
          sch.build_id_of_dir(os.path.join(_base, "7.21.1-922503154")), 922503154)
    check("no build id: the fullest set wins, not the last name",
          sch.find_version_dir("7.21.1", _base), real)
    check("a build id the device gave us wins outright",
          sch.find_version_dir("7.21.1", _base, build_id=151146050), other)
    check("a stub is still reachable when it is what the device serves",
          sch.find_version_dir("7.21.1", _base, build_id=922503154), stub)
    check("an unknown build id falls back rather than failing",
          sch.find_version_dir("7.21.1", _base, build_id=1), real)
    # resolve_schema_dir reads the real Winbox cache before anything handed to
    # it, so point both bases at the temporary one for the two calls below.
    _saved = (sch.default_cache_dir, sch.user_cache_dir)
    sch.default_cache_dir = lambda: _base
    sch.user_cache_dir = lambda: _base
    try:
        check("resolve_schema_dir agrees with the prefix search",
              sch.resolve_schema_dir(version="7.21.1"), real)
        check("resolve_schema_dir prefers the exact build",
              sch.resolve_schema_dir(version="7.21.1", build_id=922503154), stub)
    finally:
        sch.default_cache_dir, sch.user_cache_dir = _saved
    ok("a version with no cache at all resolves to something else or nothing",
       sch.find_version_dir("9.99", _base) is None)


# --- optional: parse a real cache ------------------------------------------

if len(sys.argv) > 1:
    files = sorted(glob.glob(os.path.join(sys.argv[1], "*", "*.jg")))
    bad = 0
    for f in files:
        try:
            jg.load(f)
        except Exception as e:
            bad += 1
            failures.append(f"parse failed for {f}: {type(e).__name__}: {e}")
    versions = {os.path.basename(os.path.dirname(f)) for f in files}
    print(f"corpus: parsed {len(files) - bad}/{len(files)} .jg files "
          f"across {len(versions)} versions")


# --- the two halves of _walk have to read the same .jg keys ----------------
# _walk builds a Menu twice: once for a menu with a path, once for a path-less
# type overlay. Every time the two lists drift, the overlay silently loses
# whatever the newer branch reads -- and a field left off a dataclass call does
# not keep the base's value, it takes the default. It has cost `prefix` and
# `generic` once (see the comment in the overlay branch), then autorefresh_ms,
# which put PPP interface counters on the 5s age fallback instead of the 1Hz
# the Interface menu declares, and `nonpublic`, which the path-backed branch
# was not reading at all. So pin the invariant rather than the instances.

import ast
import inspect

walk_src = inspect.getsource(sch._walk)
walk_tree = ast.parse(walk_src.lstrip())
menu_calls = [
    n for n in ast.walk(walk_tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    and n.func.id == "Menu"
]
check("_walk builds a Menu for a path and for an overlay", len(menu_calls), 2)
by_path, overlay = ({k.arg for k in c.keywords} for c in menu_calls)

#: Only these may differ, and each because it means nothing on the other side.
ONLY_OVERLAY = {"inherit", "typevalue"}
missing_from_overlay = by_path - overlay - ONLY_OVERLAY
missing_from_by_path = overlay - by_path - ONLY_OVERLAY
check("the overlay branch reads every key the path-backed one does",
      sorted(missing_from_overlay), [])
check("...and the path-backed branch reads every key the overlay one does",
      sorted(missing_from_by_path), [])

if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all schema/codec tests passed")
