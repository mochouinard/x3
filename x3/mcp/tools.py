"""The x3 MCP tools, over whatever supplies the devices.

A tool names a device the way the connection page does -- by its note, or by
its address -- and never by password. :class:`SavedDevices` resolves that name
against the address book and opens the session itself; the model only ever
sees the result of the read.

Menus are named by their place in the nav tree, ``IP/Addresses`` or
``Interfaces/Ethernet``: the words a person would use, and the words
:func:`list_menus` hands back, rather than the handler numbers underneath.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Protocol

from x3.proto import configexport as cexp
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, DeviceError

from ..ui import connstore as cs
from ..ui.services import discover_neighbors, load_address_book, split_host_port
from . import change as chg
from . import diag
from .protocol import ToolError

#: What a fresh model is told about this server before its first call.
INSTRUCTIONS = (
    "x3 reads routers over the Winbox protocol, using the user's "
    "saved x3 address book. Start with list_devices; name a device by what it "
    "lists. list_menus shows what a device offers; read_menu reads one. "
    "ping, ping_watch, traceroute, read_log, interface_traffic, "
    "ethernet_monitor, torch and system_health run from the router "
    "itself; console_print prints what Winbox has no menu for, such as BGP "
    "advertisements, or one prefix out of a large routing table. "
    "on_devices runs one tool on several devices; snapshot_config and "
    "diff_config compare configs over time or between routers. "
    "Everything is read-only: this server cannot change a router by itself. "
    "propose_change only asks: the change opens in an x3 window where the "
    "user reads the exact commands and approves or rejects; approved, it "
    "runs under Safe Mode with a revert timer on the router, and is kept "
    "only if your checklist passes. change_status tells you how it went. "
    "Secrets come back as <hidden>."
)

#: A session left unused this long is closed before the next call reopens
#: it: a router's own idle timeout would drop it anyway, and an open session
#: shows in its active users list.
IDLE_CLOSE_S = 300.0

#: Rows a list read returns unless asked for more. The Connections table on
#: a busy edge router runs to thousands, and all of it would go into context.
DEFAULT_ROW_LIMIT = 200

#: A cdb read without secrets shows this in place of a password.
_MASKED_PASSWORD = "********"

HIDDEN = "<hidden>"


@dataclass(frozen=True)
class DeviceEntry:
    """One device a tool can name. No password: this is what gets listed."""

    name: str
    host: str
    login: str
    group: str = ""
    note: str = ""
    has_password: bool = False


class DeviceSource(Protocol):
    """Where devices come from and how a session to one is had."""

    def devices(self) -> list[DeviceEntry]: ...

    def run(self, device: str, fn: Callable[[DeviceClient], Any]) -> Any:
        """``fn`` on a connected, read-only client for ``device``."""
        ...

    def close(self) -> None: ...


class SavedDevices:
    """The address book, as the connection page merges it, plus sessions.

    Re-read on every call, so an entry saved in x3 or Winbox a minute ago is
    there without a restart. Sessions are kept between calls and closed after
    :data:`IDLE_CLOSE_S` of disuse.
    """

    def __init__(
        self,
        cdb_path: str | Path | None = None,
        *,
        connect: Callable[[cs.Connection], DeviceClient] | None = None,
    ) -> None:
        self.cdb_path = cdb_path
        self._connect = connect or _open_client
        self._clients: dict[tuple[str, str], tuple[DeviceClient, float]] = {}

    def _merged(self) -> list[cs.Connection]:
        _path, rows, _err = load_address_book(self.cdb_path, include_passwords=True)
        return [c for c in cs.load().merge(rows) if c.host]

    @staticmethod
    def _name(conn: cs.Connection, clash: bool) -> str:
        base = conn.note or conn.host
        return f"{base} ({conn.login}@{conn.host})" if clash else base

    def _named(self) -> list[tuple[str, cs.Connection]]:
        merged = self._merged()
        counts: dict[str, int] = {}
        for c in merged:
            key = (c.note or c.host).lower()
            counts[key] = counts.get(key, 0) + 1
        return [(self._name(c, counts[(c.note or c.host).lower()] > 1), c) for c in merged]

    def devices(self) -> list[DeviceEntry]:
        return [
            DeviceEntry(
                name=name, host=c.host, login=c.login, group=c.group, note=c.note,
                has_password=bool(c.password) and c.password != _MASKED_PASSWORD,
            )
            for name, c in self._named()
        ]

    def resolve(self, device: str) -> cs.Connection:
        want = (device or "").strip().lower()
        if not want:
            raise ToolError("Name a device: see list_devices.")
        named = self._named()
        for name, c in named:
            if name.lower() == want:
                return c
        hits = [
            (name, c) for name, c in named
            if want in (c.host.lower(), f"{c.login}@{c.host}".lower(), (c.note or c.host).lower())
        ]
        if len(hits) == 1:
            return hits[0][1]
        if hits:
            raise ToolError(
                f"{device!r} is more than one saved entry: "
                + ", ".join(name for name, _c in hits)
            )
        raise ToolError(f"No saved device {device!r}. See list_devices.")

    def run(self, device: str, fn: Callable[[DeviceClient], Any]) -> Any:
        conn = self.resolve(device)
        self._close_idle()
        cached = self._clients.get(conn.key)
        if cached is not None:
            try:
                out = fn(cached[0])
            except (DeviceError, OSError):
                # The router dropped a session that sat open between calls.
                # Once more on a fresh one; a second failure is a real one.
                self._drop(conn.key)
            else:
                self._clients[conn.key] = (cached[0], time.monotonic())
                return out
        try:
            client = self._connect(conn)
        except (DeviceError, OSError) as e:
            raise ToolError(f"Could not connect to {device}: {e}") from None
        self._clients[conn.key] = (client, time.monotonic())
        try:
            return fn(client)
        except DeviceError as e:
            raise ToolError(f"{device}: {e}") from None

    def _close_idle(self) -> None:
        now = time.monotonic()
        for key, (_client, used) in list(self._clients.items()):
            if now - used > IDLE_CLOSE_S:
                self._drop(key)

    def _drop(self, key: tuple[str, str]) -> None:
        client, _used = self._clients.pop(key, (None, 0.0))
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 -- already gone is fine
                pass

    def close(self) -> None:
        for key in list(self._clients):
            self._drop(key)


def _open_client(conn: cs.Connection) -> DeviceClient:
    host, port = split_host_port(conn.host)
    password = "" if conn.password == _MASKED_PASSWORD else conn.password
    client = DeviceClient(host, conn.login, password, port=port, timeout=12.0, readonly=True)
    # One console for every command this session types, not one each: each
    # console open is a login line in the router's log.
    client.keep_console = True
    client.connect()
    return client


# --- menus ------------------------------------------------------------------


@dataclass(frozen=True)
class MenuRef:
    """One openable place in a device's nav tree."""

    title: str  # "IP/Addresses"
    node: sch.NavNode

    @property
    def kind(self) -> str:
        return "list" if self.node.kind == "map" else "form"


def device_nav(client: DeviceClient) -> list[sch.NavNode]:
    """The nav tree this device would show, as the main window filters it."""
    nav = list(client.nav_tree())
    plugins = getattr(client, "device_plugins", None)
    if plugins:
        nav = sch.filter_nav_by_plugins(nav, plugins)
    board = client.info.board
    if board:
        try:
            caps = client.system_caps()
        except DeviceError:
            caps = None
        nav = sch.filter_nav_by_pred(
            nav, board=board, arch=client.info.architecture or None, syscaps=caps,
        )
    return nav


def flatten_menus(nav: list[sch.NavNode]) -> list[MenuRef]:
    """Every openable node, titled by its path through the tree.

    A node's toolbar actions (IP ▸ DNS ▸ Settings) are listed under it, since
    for reading they are menus like any other.
    """
    out: list[MenuRef] = []
    seen: set[str] = set()

    def add(title: str, node: sch.NavNode) -> None:
        if node.openable and not node.hidden and title.lower() not in seen:
            seen.add(title.lower())
            out.append(MenuRef(title, node))

    def walk(nodes: list[sch.NavNode], prefix: str) -> None:
        for n in nodes:
            title = f"{prefix}/{n.title}" if prefix else n.title
            add(title, n)
            for a in n.actions or []:
                add(f"{title}/{a.title}", a)
            walk(n.children or [], title)

    walk(nav, "")
    return out


def find_menu(menus: list[MenuRef], want: str) -> MenuRef:
    """``want`` by exact title, else by a unique ending, else by a unique part.

    "IP ▸ Addresses" and "IP > Addresses" read as "IP/Addresses", the way a
    path is copied off the menu bar. With no match the closest titles are
    offered, so a plural too many costs no extra round trip.
    """
    w = re.sub(r"\s*[>▸]\s*", "/", want.strip()).strip("/").lower()
    if not w:
        raise ToolError("Name a menu: see list_menus.")
    for m in menus:
        if m.title.lower() == w:
            return m
    for test in (
        lambda t: t.endswith("/" + w),
        lambda t: w in t,
    ):
        hits = [m for m in menus if test(m.title.lower())]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ToolError(
                f"{want!r} matches {len(hits)} menus: "
                + ", ".join(m.title for m in hits[:20])
                + (" ..." if len(hits) > 20 else "")
            )
    titles = {m.title.lower(): m.title for m in menus}
    close = difflib.get_close_matches(w, list(titles), n=3, cutoff=0.6)
    if close:
        raise ToolError(
            f"No menu {want!r} on this device. Did you mean "
            + " or ".join(repr(titles[c]) for c in close) + "? See list_menus."
        )
    raise ToolError(f"No menu {want!r} on this device. See list_menus.")


def _secret_codes(client: DeviceClient, menu: sch.Menu | None) -> tuple[set[int], set[str]]:
    """Codes and names to mask in this menu's rows.

    The same rules the session transcript masks by: the schema's secret
    leaves, every secret any subtype of the family declares (an interface
    row's IPsec Secret lives on the overlay, not the base), and the name
    fallback for fields the schema does not cover.
    """
    codes: set[int] = set()
    names: set[str] = set()
    if menu is None:
        return codes, names
    for code, f in (menu.fields or {}).items():
        leaf = sch.secret_leaf(f)
        if leaf is not None:
            codes.update((code, leaf.code))
            names.add((f.name or "").lower())
    generic = getattr(menu, "generic", None)
    if generic and client.schema is not None:
        try:
            codes.update(client.schema.family_secret_codes(generic))
        except Exception:  # noqa: BLE001 -- over-masking is the fallback
            pass
    names.discard("")
    return codes, names


def _is_secret(name: str, code: int | None, codes: set[int], names: set[str]) -> bool:
    return (
        (code is not None and code in codes)
        or name.lower() in names
        or DeviceClient._looks_secret(name)
    )


def _cell(text: str) -> str:
    return " ".join(str(text).split())


# --- the tools --------------------------------------------------------------


class ToolBox:
    """The tool set, bound to one device source."""

    def __init__(self, source: DeviceSource, *,
                 launch_change: Callable[[str], None] | None = None) -> None:
        self.source = source
        self._menus: dict[str, list[MenuRef]] = {}
        #: Opens the x3 change window for a job id. Without one (no window to
        #: approve in), the change tools are not offered at all.
        self._launch_change = launch_change

    def definitions(self) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], str]]]:
        defs = self._read_definitions()
        if self._launch_change is not None and hasattr(self.source, "resolve"):
            defs += self._change_definitions()
        return defs

    def _change_definitions(self) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], str]]]:
        device = {"type": "string", "description": "A name from list_devices."}
        check = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["ping", "rows", "export"],
                         "description": "ping: from the router. rows: how many rows "
                                        "a console_print would return. export: how "
                                        "many lines of the config export match a "
                                        "regular expression; use it to prove a line "
                                        "exists or is gone without depending on a "
                                        "menu's field names."},
                "match": {"type": "string",
                          "description": "export: a regular expression (Python "
                                         "syntax, any case) tried on each export "
                                         "line, e.g. 'chain=srcnat.*dst-address="
                                         "203\\.0\\.113\\.0/24'."},
                "section": {"type": "string",
                            "description": "export: only lines under this menu, "
                                           "e.g. '/ip firewall nat'."},
                "name": {"type": "string", "description": "What this check proves, in a few words."},
                "within_s": {"type": "integer", "minimum": 0, "maximum": chg.MAX_WITHIN_S,
                             "description": "Keep trying this long before it counts "
                                            "as failed, e.g. 120 for a BGP session "
                                            "that has to come back. Default 0: one try."},
                "address": {"type": "string", "description": "ping: what to ping from the router."},
                "count": {"type": "integer", "minimum": 1, "maximum": 50},
                "max_loss_pct": {"type": "integer", "minimum": 0, "maximum": 99,
                                 "description": "ping: most loss that still passes. Default 0."},
                "src_address": {"type": "string"},
                "routing_table": {"type": "string"},
                "interface": {"type": "string"},
                "menu": {"type": "string",
                         "description": "rows: a console_print menu, e.g. "
                                        "'/routing bgp session'."},
                "where": {"type": "array", "items": {"type": "string"},
                          "description": "rows: console_print conditions, e.g. "
                                         "['name=upstream-1', 'established']."},
                "min": {"type": "integer", "minimum": 0,
                        "description": "rows/export: fewest matches that pass. "
                                       "Default 1 when max is not given."},
                "max": {"type": "integer", "minimum": 0,
                        "description": "rows/export: most matches that pass; 0 "
                                       "proves something is gone."},
            },
            "required": ["type"],
        }
        return [
            ({
                "name": "propose_change",
                "description": "Ask the user to approve a configuration change "
                               "on one device. This does not change anything: it "
                               "opens an x3 window showing the user your summary, "
                               "the exact commands and the checklist. If they "
                               "approve, x3 saves the config, takes Safe Mode, "
                               "sets a revert timer on the router, types the "
                               "commands, then runs the checklist: all pass and "
                               "the change is kept, anything else and it is "
                               "reverted. Returns a change id at once; follow it "
                               "with change_status. Commands are full-path "
                               f"{'/'.join(sorted(chg.WRITE_VERBS))} lines, one "
                               "change each, no scripting; a '?' is written \\3F. "
                               "Users, files, packages, certificates, scripts and "
                               "the scheduler are not changed this way. Write the "
                               "checklist to prove the change worked AND that "
                               "nothing else broke (the upstream still pings, the "
                               "BGP sessions are still established). The "
                               "checklist is also run once before the change: a "
                               "check meant to pass only afterwards should FAIL "
                               "there, and one that already passes proves "
                               "nothing. A revert does not rest on Safe Mode's "
                               "undo alone, which leaves some changes in place "
                               "(a disabled firewall rule; on some versions an "
                               "added route): x3 removes what an add made and "
                               "restores what an enable or disable changed, and "
                               "reports any line still different.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "summary": {"type": "string",
                                "description": "What the change is for, for the "
                                               "person approving it. Plain words."},
                    "commands": {"type": "array", "items": {"type": "string"},
                                 "minItems": 1, "maxItems": chg.MAX_COMMANDS,
                                 "description": "Typed in this order, e.g. "
                                                "'/routing filter rule set [find "
                                                "chain=bgp-out] disabled=yes'."},
                    "checks": {"type": "array", "items": check,
                               "minItems": 1, "maxItems": chg.MAX_CHECKS},
                    "settle_s": {"type": "integer", "minimum": 0, "maximum": chg.MAX_SETTLE_S,
                                 "description": "Wait this long after the commands "
                                                "before the checklist. Default 5."},
                }, "required": ["device", "summary", "commands", "checks"]},
                "annotations": {"readOnlyHint": False, "destructiveHint": True,
                                "idempotentHint": False, "openWorldHint": True},
            }, self.propose_change),
            ({
                "name": "change_status",
                "description": "Where a proposed change stands: waiting for the "
                               "user, rejected (with their note), running, kept, "
                               "reverted, or not confirmed; with each command's "
                               "own result, the checklist before and after, what "
                               "changed in the export, and after a revert "
                               "anything still different from before.",
                "inputSchema": {"type": "object", "properties": {
                    "id": {"type": "string", "description": "From propose_change."},
                    "wait_s": {"type": "integer", "minimum": 0, "maximum": 55,
                               "description": "Wait up to this long for the next "
                                              "step instead of answering at once."},
                }, "required": ["id"]},
                "annotations": {"readOnlyHint": True},
            }, self.change_status),
        ]

    def _read_definitions(self) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], str]]]:
        ro = {"readOnlyHint": True, "openWorldHint": True}
        device = {"type": "string", "description": "A name from list_devices."}
        return [
            ({
                "name": "list_devices",
                "description": "The saved x3 address book: one line per device "
                               "with its name, address, login and group. "
                               "Passwords are never shown.",
                "inputSchema": {"type": "object", "properties": {
                    "filter": {"type": "string",
                               "description": "Only entries containing this text."},
                }},
                "annotations": {"readOnlyHint": True},
            }, self.list_devices),
            ({
                "name": "list_neighbors",
                "description": "Routers answering neighbour discovery "
                               "(MNDP) on this machine's LAN right now.",
                "inputSchema": {"type": "object", "properties": {
                    "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 10,
                                  "description": "How long to listen. Default 3."},
                }},
                "annotations": ro,
            }, self.list_neighbors),
            ({
                "name": "device_info",
                "description": "Identity, model, firmware version, CPU, memory "
                               "and uptime of one device.",
                "inputSchema": {"type": "object", "properties": {"device": device},
                                "required": ["device"]},
                "annotations": ro,
            }, self.device_info),
            ({
                "name": "list_menus",
                "description": "The menus this device offers, as paths to pass "
                               "to read_menu, each marked list or form.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "filter": {"type": "string",
                               "description": "Only menus whose path contains this."},
                }, "required": ["device"]},
                "annotations": ro,
            }, self.list_menus),
            ({
                "name": "read_menu",
                "description": "Read one menu. A list comes back as a table of "
                               "the columns the schema shows by default (flags "
                               "first: X disabled, D dynamic, R running ...), "
                               "with the comment last; 'columns' picks others, "
                               "all_fields=true gives every field of every row. "
                               "A form comes back as its fields. Secrets read "
                               "<hidden>.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "menu": {"type": "string",
                             "description": "A path from list_menus, e.g. "
                                            "'IP/Addresses'. A unique ending "
                                            "such as 'Addresses' will do."},
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "Column names to show instead "
                                               "of the default set. A wrong "
                                               "name lists the ones there are."},
                    "all_fields": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1,
                              "description": f"Most rows. Default {DEFAULT_ROW_LIMIT}."},
                }, "required": ["device", "menu"]},
                "annotations": ro,
            }, self.read_menu),
            ({
                "name": "export_config",
                "description": "The device's configuration as '/export terse' "
                               "prints it, one full command per line, without "
                               "secrets. Read off the console; nothing is "
                               "written to the device.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "section": {"type": "string",
                                "description": "Only the lines under this "
                                               "menu, e.g. '/ip firewall' or "
                                               "'/ip/firewall/filter'."},
                    "contains": {"type": "string",
                                 "description": "Only lines with this text "
                                                "(any case)."},
                    "match": {"type": "string",
                              "description": "Only lines matching this "
                                             "regular expression (Python "
                                             "syntax, any case)."},
                    "exclude": {"type": "string",
                                "description": "Leave out lines matching this "
                                               "regular expression, e.g. "
                                               "'bogon' to skip a long "
                                               "bogon list."},
                }, "required": ["device"]},
                "annotations": ro,
            }, self.export_config),
            ({
                "name": "ping",
                "description": "Ping from the router: what the router can reach, "
                               "not what this machine can.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "address": {"type": "string", "description": "IP address or host name."},
                    "count": {"type": "integer", "minimum": 1, "maximum": 20,
                              "description": "Probes. Default 4."},
                    "size": {"type": "integer", "minimum": 28, "maximum": 10000,
                             "description": "Packet size in bytes, for MTU checks."},
                    "interface": {"type": "string",
                                  "description": "Send out of this interface."},
                    "src_address": {"type": "string"},
                    "routing_table": {"type": "string",
                                      "description": "VRF or routing table."},
                }, "required": ["device", "address"]},
                "annotations": ro,
            }, self.ping),
            ({
                "name": "traceroute",
                "description": "Traceroute from the router, one row per hop "
                               "with loss and latency.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "address": {"type": "string", "description": "IP address or host name."},
                    "count": {"type": "integer", "minimum": 1, "maximum": 5,
                              "description": "Probes per hop. Default 1."},
                    "max_hops": {"type": "integer", "minimum": 1, "maximum": 30,
                                 "description": "Default 15."},
                    "src_address": {"type": "string"},
                    "routing_table": {"type": "string",
                                      "description": "VRF or routing table."},
                }, "required": ["device", "address"]},
                "annotations": ro,
            }, self.traceroute),
            ({
                "name": "read_log",
                "description": "The newest lines of the router's log (/log "
                               "print), optionally narrowed by topic or text.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "topics": {"type": "string",
                               "description": "Comma-separated, any of them, "
                                              "e.g. 'dhcp,error'."},
                    "contains": {"type": "string",
                                 "description": "Only lines with this text."},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 1000,
                              "description": f"Most lines, newest kept. Default {DEFAULT_LOG_LINES}."},
                }, "required": ["device"]},
                "annotations": ro,
            }, self.read_log),
            ({
                "name": "interface_traffic",
                "description": "Live rates on one interface right now (bits "
                               "and packets per second, drops, errors), from "
                               "/interface monitor-traffic.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "interface": {"type": "string", "description": "Interface name."},
                }, "required": ["device", "interface"]},
                "annotations": ro,
            }, self.interface_traffic),
            ({
                "name": "console_print",
                "description": "A read-only 'print' at the router's console, "
                               "for what Winbox has no menu for or what needs "
                               "a filter on the router side: BGP "
                               "advertisements per peer, one prefix out of a "
                               "full routing table, OSPF LSAs, the ARP or "
                               "bridge host table. Rows come back one per "
                               "line as 'print terse' writes them "
                               "(key=value, flags first: A active, X "
                               "disabled ...). BGP advertisements are listed "
                               "only for connections with "
                               "output.keep-sent-attributes=yes. Only these "
                               "menus: "
                               + ", ".join(f"{m} ({d})" for m, d in diag.PRINT_MENUS.items())
                               + ".",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "menu": {"type": "string",
                             "description": "One of the menus above, e.g. "
                                            "'/routing bgp advertisements'. "
                                            "A unique ending such as 'bgp "
                                            "advertisements' will do."},
                    "where": {"type": "array", "items": {"type": "string"},
                              "description": "Conditions, all of which must "
                                             "hold: 'peer=upstream-1', "
                                             "'dst-address=203.0.113.0/24', "
                                             "'dst-address in 10.0.0.0/8', "
                                             "'10.1.2.3 in dst-address' (the "
                                             "routes covering an address), "
                                             "'interface~^vrrp' (a regular "
                                             "expression), 'active', "
                                             "'!disabled'. Values are typed "
                                             "quoted, which is the form every "
                                             "menu matches."},
                    "detail": {"type": "boolean",
                               "description": "'print detail' instead of "
                                              "terse: every field, wrapped "
                                              "over several lines."},
                    "count_only": {"type": "boolean",
                                   "description": "Only how many rows match."},
                    "stats": {"type": "boolean",
                              "description": "'print stats': the counters "
                                             "(rx/tx errors and drops on "
                                             "/interface, bytes and packets "
                                             "on firewall rules), as a table."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000,
                              "description": f"Most lines. Default {DEFAULT_ROW_LIMIT}."},
                }, "required": ["device", "menu"]},
                "annotations": ro,
            }, self.console_print),
            ({
                "name": "ethernet_monitor",
                "description": "The physical link of one ethernet or SFP port "
                               "(/interface ethernet monitor once): link "
                               "status, negotiated rate and duplex, and for "
                               "an SFP module its RX/TX power, temperature "
                               "and vendor.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "interface": {"type": "string", "description": "Ethernet or SFP port name."},
                }, "required": ["device", "interface"]},
                "annotations": ro,
            }, self.ethernet_monitor),
            ({
                "name": "torch",
                "description": "Who is using an interface right now (/tool "
                               "torch for a few seconds): flows by protocol, "
                               "source, destination and port, with their "
                               "rates. Costs the router some CPU while it "
                               "runs.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "interface": {"type": "string", "description": "Interface name."},
                    "seconds": {"type": "integer", "minimum": 1, "maximum": 10,
                                "description": "How long to watch. Default 3."},
                    "src_address": {"type": "string",
                                    "description": "Only this source address or prefix."},
                    "dst_address": {"type": "string",
                                    "description": "Only this destination address or prefix."},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535,
                             "description": "Only flows with this port."},
                }, "required": ["device", "interface"]},
                "annotations": ro,
            }, self.torch),
            ({
                "name": "system_health",
                "description": "Board sensors (/system health print): "
                               "temperatures, voltages, fans and power "
                               "supplies, on boards that have them.",
                "inputSchema": {"type": "object", "properties": {"device": device},
                                "required": ["device"]},
                "annotations": ro,
            }, self.system_health),
            ({
                "name": "ping_watch",
                "description": "Ping from the router for a stretch of time at "
                               "a short interval and return a loss timeline: "
                               "when replies stopped, for how long, and the "
                               "round-trip times around it. For measuring a "
                               "failover or a flapping link in seconds lost.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "address": {"type": "string", "description": "IP address or host name."},
                    "seconds": {"type": "integer", "minimum": 1, "maximum": 300,
                                "description": "How long to ping. Default 30."},
                    "interval_ms": {"type": "integer", "minimum": 20, "maximum": 1000,
                                    "description": "Between probes. Default 200."},
                    "interface": {"type": "string",
                                  "description": "Send out of this interface."},
                    "src_address": {"type": "string"},
                    "routing_table": {"type": "string",
                                      "description": "VRF or routing table."},
                }, "required": ["device", "address"]},
                "annotations": ro,
            }, self.ping_watch),
            ({
                "name": "on_devices",
                "description": "Run one read tool on several devices and get "
                               "the answers together, one section per device, "
                               "e.g. BGP sessions on four routers. A device "
                               "that fails says why in its section; the "
                               "others still answer.",
                "inputSchema": {"type": "object", "properties": {
                    "devices": {"type": "array", "items": {"type": "string"},
                                "minItems": 1, "maxItems": MAX_FANOUT,
                                "description": "Names from list_devices."},
                    "tool": {"type": "string",
                             "description": "Any tool here that takes a "
                                            "device, e.g. 'console_print'."},
                    "arguments": {"type": "object",
                                  "description": "That tool's arguments, "
                                                 "without 'device'."},
                }, "required": ["devices", "tool"]},
                "annotations": ro,
            }, self.on_devices),
            ({
                "name": "snapshot_config",
                "description": "Save the device's export (without secrets) on "
                               "this machine, next to the snapshots x3 takes "
                               "itself, for diff_config to compare later. "
                               "Nothing is written to the router.",
                "inputSchema": {"type": "object", "properties": {
                    "device": device,
                    "label": {"type": "string",
                              "description": "A word for the file name, e.g. "
                                             "'before-drain'."},
                }, "required": ["device"]},
                "annotations": {"readOnlyHint": False, "destructiveHint": False,
                                "openWorldHint": True},
            }, self.snapshot_config),
            ({
                "name": "list_snapshots",
                "description": "The saved exports of one device, newest "
                               "first, as names diff_config takes.",
                "inputSchema": {"type": "object", "properties": {"device": device},
                                "required": ["device"]},
                "annotations": ro,
            }, self.list_snapshots),
            ({
                "name": "diff_config",
                "description": "Compare two configurations as a unified diff "
                               "of their export lines. Each side is a device "
                               "name (its config right now), or 'name@last' / "
                               "'name@<snapshot>' for a saved one (see "
                               "list_snapshots). With only 'a' given, the "
                               "newest snapshot of it is compared with now. "
                               "Two devices compare one router with another.",
                "inputSchema": {"type": "object", "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                    "section": {"type": "string",
                                "description": "Only this menu, e.g. '/routing filter'."},
                }, "required": ["a"]},
                "annotations": ro,
            }, self.diff_config),
        ]

    # --- handlers -----------------------------------------------------------

    def list_devices(self, args: dict[str, Any]) -> str:
        want = str(args.get("filter") or "").lower()
        rows = [
            d for d in self.source.devices()
            if not want or want in " ".join((d.name, d.host, d.login, d.group, d.note)).lower()
        ]
        if not rows:
            return "No saved devices" + (f" matching {want!r}." if want else ".")
        lines = ["name\taddress\tlogin\tgroup\tpassword saved"]
        for d in rows:
            lines.append("\t".join((
                _cell(d.name), d.host, d.login, _cell(d.group),
                "yes" if d.has_password else "no",
            )))
        return "\n".join(lines)

    def list_neighbors(self, args: dict[str, Any]) -> str:
        timeout = min(10.0, max(0.5, float(args.get("timeout_s") or 3.0)))
        try:
            rows = discover_neighbors(timeout=timeout)
        except PermissionError as e:
            raise ToolError(f"Cannot listen for neighbours: {e}") from None
        if not rows:
            return "No neighbours answered."
        lines = ["identity\taddress\tmac\tboard\tversion\tuptime\tinterface"]
        seen: set[str] = set()
        for n in sorted(rows, key=lambda n: n.identity.lower()):
            if n.mac in seen:
                continue
            seen.add(n.mac)
            lines.append("\t".join(_cell(v) for v in (
                n.identity, n.address, n.mac, n.board, n.version, n.uptime, n.interface,
            )))
        return "\n".join(lines)

    def device_info(self, args: dict[str, Any]) -> str:
        def read(client: DeviceClient) -> str:
            try:
                info = client.refresh_resources()
            except DeviceError:
                info = client.info
            fields = (
                ("identity", info.identity), ("board", info.board),
                ("version", info.version), ("architecture", info.architecture),
                ("platform", info.platform), ("cpu load", info.cpu_load),
                ("free memory", info.free_memory),
                ("total memory", info.total_memory), ("uptime", info.uptime),
            )
            return "\n".join(f"{k}: {v}" for k, v in fields if v)

        return self.source.run(_device(args), read)

    def _menus_for(self, device: str, client: DeviceClient) -> list[MenuRef]:
        # Per device and per process: the nav is the schema's, and a device
        # changes firmware rarely enough that a restart is the fix when it does.
        if device not in self._menus:
            self._menus[device] = flatten_menus(device_nav(client))
        return self._menus[device]

    def list_menus(self, args: dict[str, Any]) -> str:
        device = _device(args)
        want = str(args.get("filter") or "").lower()

        def read(client: DeviceClient) -> str:
            menus = [m for m in self._menus_for(device, client) if want in m.title.lower()]
            if not menus:
                return "No menus" + (f" matching {want!r}." if want else ".")
            return "\n".join(f"{m.title}\t{m.kind}" for m in menus)

        return self.source.run(device, read)

    def read_menu(self, args: dict[str, Any]) -> str:
        device = _device(args)
        limit = max(1, int(args.get("limit") or DEFAULT_ROW_LIMIT))
        all_fields = bool(args.get("all_fields"))

        def read(client: DeviceClient) -> str:
            ref = find_menu(self._menus_for(device, client), str(args.get("menu") or ""))
            node = ref.node
            path = tuple(node.path or ())
            if node.kind == "item":
                return _read_form(client, ref, path)
            menu, cols, rows, objs, _up = client.table_rows(
                path, type_value=node.type_value, family=node.generic, name=node.name,
            )
            codes, names = _secret_codes(client, menu)
            head = f"# {ref.title}: {len(objs)} row(s)"
            if len(objs) > limit:
                head += f", showing the first {limit}"
            if client.last_truncated():
                head += f". {client.last_truncated()}"
            shown = objs[:limit]
            if all_fields:
                out = [head]
                for i, (obj, row) in enumerate(zip(shown, rows)):
                    out.append(f"--- row {i}")
                    said: set[str] = set()
                    for code, name, value, _ok in obj.fields:
                        # A code the schema has no name for is noise to a
                        # reader that cannot look it up.
                        if name.startswith("<0x"):
                            continue
                        value = HIDDEN if _is_secret(name, code, codes, names) else _cell(value)
                        out.append(f"{name}: {value}")
                        said.add(name)
                    # A pair column's members have no names of their own
                    # (Connections' Orig./Repl. Bytes is q20 and q24), so the
                    # loop above skips them; the list's cell is the reading.
                    for col, cell in zip(cols, row):
                        if col and cell and col not in said:
                            hidden = _is_secret(col, None, codes, names)
                            out.append(f"{col}: {HIDDEN if hidden else _cell(cell)}")
                            said.add(col)
                    if obj.comment():
                        out.append(f"comment: {_cell(obj.comment())}")
                return "\n".join(out)
            keep = _pick_columns(menu, cols, args.get("columns"))
            notes = [_cell(o.comment()) for o in shown]
            if any((cols[i] or "").lower() in ("comment", "description") for i in keep):
                notes = [""] * len(shown)
            out = [head, "\t".join(
                [cols[i] or "flags" for i in keep] + (["comment"] if any(notes) else [])
            )]
            for row, note in zip(rows, notes):
                cells = [
                    HIDDEN if row[i] and _is_secret(cols[i], None, codes, names) else _cell(row[i])
                    for i in keep
                ]
                out.append("\t".join(cells + ([note] if any(notes) else [])))
            return "\n".join(out)

        return self.source.run(device, read)

    def export_config(self, args: dict[str, Any]) -> str:
        # The section is cut out of a whole export here, never typed at the
        # console as "<section> export": the console runs what it is given,
        # and no check on a model's string is as safe as not sending one.
        # Terse puts each line's full menu path at its start, so the cut is
        # a prefix match.
        words = str(args.get("section") or "").replace("/", " ").split()
        section = "/" + " ".join(words).lower() if words else ""
        # Matched here on the text that came back, never typed at the
        # console, so any pattern is safe to take.
        keep = _line_filter(args)

        def read(client: DeviceClient) -> str:
            got = client.export_config(show_sensitive=False)
            if got.console_error:
                raise ToolError(f"The console refused {got.command!r}: {got.console_error}")
            lines = cexp.redact(got.text).splitlines()
            if section:
                lines = _export_section(lines, section)
                if len(lines) == len(_export_header(lines)):
                    if not got.complete:
                        # Said plainly: a slow router whose export stalled
                        # read as one that has no such section at all.
                        raise ToolError(
                            f"The export stopped before the end, so {section} may "
                            "simply not have been reached. Try again."
                        )
                    raise ToolError(f"Nothing under {section} in this device's export.")
            if keep is not None:
                head = _export_header(lines)
                # A long value (a script's source) goes on over indented
                # lines; those belong to the command above and stay with it.
                commands: list[list[str]] = []
                for ln in lines[len(head):]:
                    if commands and ln[:1].isspace():
                        commands[-1].append(ln)
                    else:
                        commands.append([ln])
                hits = [c for c in commands if keep("\n".join(c))]
                lines = head + [f"# {len(hits)} matching command(s)"] + [ln for c in hits for ln in c]
            if not got.complete:
                lines.append("# (capture stopped before the end: may be truncated)")
            return "\n".join(lines)

        return self.source.run(_device(args), read)


    # --- diagnostics, typed at the router's console -----------------------------

    def _console(self, device: str, command: str, timeout: float) -> str:
        """Run one command built by :mod:`.diag` and return what it printed,
        headed by the command itself so the reader sees exactly what ran."""

        def read(client: DeviceClient) -> str:
            got = client.run_console(command, timeout=timeout)
            if got.console_error:
                raise ToolError(f"The router refused {command!r}: {got.console_error}")
            lines = [ln.rstrip() for ln in cexp.redact(got.text).splitlines()]
            if not got.complete:
                lines.append(f"# (stopped after {timeout:.0f}s: output may be partial)")
            return "\n".join([f"# {command}", *lines])

        return self.source.run(device, read)

    def _src_and_table(self, args: dict[str, Any]) -> dict[str, str | None]:
        return {
            "src_address": diag.address(args["src_address"], "src_address")
            if args.get("src_address") else None,
            "routing_table": diag.routing_table(args["routing_table"])
            if args.get("routing_table") else None,
        }

    def ping(self, args: dict[str, Any]) -> str:
        device = _device(args)
        dest = diag.address(args.get("address"))
        count = diag.number(args.get("count"), "count", 1, 20, 4)
        size = diag.number(args.get("size"), "size", 28, 10000, 0) or None
        extra = self._src_and_table(args)
        iface_arg = args.get("interface")

        def build(client: DeviceClient) -> str:
            iface = diag.interface(iface_arg, _iface_names(client)) if iface_arg else None
            return diag.ping(dest, count=count, size=size, interface=iface, **extra)

        command = self.source.run(device, build)
        return self._console(device, command, timeout=15 + 2 * count)

    def traceroute(self, args: dict[str, Any]) -> str:
        device = _device(args)
        dest = diag.address(args.get("address"))
        count = diag.number(args.get("count"), "count", 1, 5, 1)
        hops = diag.number(args.get("max_hops"), "max_hops", 1, 30, 15)
        command = diag.traceroute(dest, count=count, max_hops=hops, **self._src_and_table(args))
        # A silent hop costs about a second per probe; leave room for every
        # hop to be one, up to what a tool call can sensibly wait.
        return self._console(device, command, timeout=min(150, 15 + 1.5 * hops * count))

    def read_log(self, args: dict[str, Any]) -> str:
        device = _device(args)
        topics = {t.strip().lower() for t in str(args.get("topics") or "").split(",") if t.strip()}
        needle = str(args.get("contains") or "").lower()
        most = diag.number(args.get("lines"), "lines", 1, 1000, DEFAULT_LOG_LINES)
        text = self._console(device, diag.LOG_COMMAND, timeout=30)
        head, *lines = text.splitlines()
        kept = [
            ln for ln in lines
            if ln.strip()
            and (not topics or topics & _log_topics(ln))
            and (not needle or needle in ln.lower())
        ]
        if not kept:
            return f"{head}\nNo log lines" + (" match." if topics or needle else ".")
        note = [f"# last {most} of {len(kept)} lines"] if len(kept) > most else []
        return "\n".join([head, *note, *(ln.strip() for ln in kept[-most:])])

    def interface_traffic(self, args: dict[str, Any]) -> str:
        device = _device(args)
        command = self.source.run(device, lambda client: diag.monitor_traffic(
            diag.interface(args.get("interface"), _iface_names(client))
        ))
        return self._console(device, command, timeout=20)

    def console_print(self, args: dict[str, Any]) -> str:
        device = _device(args)
        menu = diag.print_menu(args.get("menu"))
        where = args.get("where") or []
        if isinstance(where, str):
            where = [where]
        command = diag.print_command(
            menu, [diag.condition(c) for c in where],
            detail=bool(args.get("detail")), count_only=bool(args.get("count_only")),
            stats=bool(args.get("stats")),
        )
        most = diag.number(args.get("limit"), "limit", 1, 5000, DEFAULT_ROW_LIMIT)
        # A filtered print over a full table walks every route on the
        # router, which takes a while on a small CPU.
        text = self._console(device, command, timeout=90)
        head, *lines = text.splitlines()
        lines = [ln for ln in lines if ln.strip()]
        if not lines:
            out = [head, "No rows" + (" match." if where else ".")]
            if where:
                out.append("# The router says the same for a field name it does not "
                           "have: check the names with detail=true and no where.")
            if menu == "/routing bgp advertisements":
                out.append("# This list is kept only for connections with "
                           "output.keep-sent-attributes=yes; without it the router "
                           "records nothing here, whatever it sends.")
            return "\n".join(out)
        if where and bool(args.get("count_only")) and [ln.strip() for ln in lines] == ["0"]:
            # The same silence as an empty print: 0 is also what a field the
            # menu does not have comes to.
            return "\n".join([head, "0", "# 0 is also what the router answers for a field "
                              "name this menu does not have: check the names with "
                              "detail=true and no where."])
        if len(lines) > most:
            return "\n".join([head, f"# first {most} of {len(lines)} lines", *lines[:most]])
        return "\n".join([head, *lines])

    def ethernet_monitor(self, args: dict[str, Any]) -> str:
        device = _device(args)
        command = self.source.run(device, lambda client: diag.ethernet_monitor(
            diag.interface(args.get("interface"), _iface_names(client))
        ))
        return self._console(device, command, timeout=20)

    def torch(self, args: dict[str, Any]) -> str:
        device = _device(args)
        seconds = diag.number(args.get("seconds"), "seconds", 1, 10, 3)
        port = diag.number(args.get("port"), "port", 1, 65535, 0) or None
        src, dst = args.get("src_address"), args.get("dst_address")
        command = self.source.run(device, lambda client: diag.torch(
            diag.interface(args.get("interface"), _iface_names(client)),
            seconds=seconds, src=str(src) if src else None,
            dst=str(dst) if dst else None, port=port,
        ))
        head, *lines = self._console(device, command, timeout=seconds + 20).splitlines()
        rows = [ln for ln in diag.last_screen(lines) if ln.strip()]
        if not rows:
            return f"{head}\nNo traffic seen in {seconds}s."
        return "\n".join([head, *rows])

    def system_health(self, args: dict[str, Any]) -> str:
        return self._console(_device(args), diag.HEALTH_COMMAND, timeout=20)

    def ping_watch(self, args: dict[str, Any]) -> str:
        device = _device(args)
        dest = diag.address(args.get("address"))
        seconds = diag.number(args.get("seconds"), "seconds", 1, 300, 30)
        interval = diag.number(args.get("interval_ms"), "interval_ms", 20, 1000, 200)
        count = max(1, seconds * 1000 // interval)
        extra = self._src_and_table(args)
        iface_arg = args.get("interface")

        def build(client: DeviceClient) -> str:
            iface = diag.interface(iface_arg, _iface_names(client)) if iface_arg else None
            return diag.ping(dest, count=count, interface=iface, interval_ms=interval, **extra)

        command = self.source.run(device, build)
        text = self._console(device, command, timeout=seconds + 30)
        head, *lines = text.splitlines()
        return "\n".join([head, *diag.ping_timeline(lines, interval)])

    def on_devices(self, args: dict[str, Any]) -> str:
        names = args.get("devices") or []
        if isinstance(names, str):
            names = [names]
        names = [str(n) for n in names if str(n).strip()]
        if not names:
            raise ToolError("devices is required: names from list_devices.")
        if len(names) > MAX_FANOUT:
            raise ToolError(f"At most {MAX_FANOUT} devices in one call.")
        tool = str(args.get("tool") or "")
        handlers = {d["name"]: (d, fn) for d, fn in self.definitions()}
        if tool not in handlers or "device" not in handlers[tool][0]["inputSchema"]["properties"]:
            raise ToolError(
                f"on_devices runs a tool that takes a device, not {tool!r}. One of: "
                + ", ".join(n for n, (d, _f) in handlers.items()
                            if "device" in d["inputSchema"]["properties"])
            )
        fn = handlers[tool][1]
        inner = dict(args.get("arguments") or {})
        out = []
        for name in names:
            try:
                got = fn({**inner, "device": name})
            except ToolError as e:
                got = f"error: {e}"
            out.append(f"=== {name}\n{got}")
        return "\n\n".join(out)

    # --- changes: proposed here, approved and run in the x3 change window ----------

    def propose_change(self, args: dict[str, Any]) -> str:
        device = _device(args)
        conn = self.source.resolve(device)
        if chg.policy_for(conn.host, conn.login) == chg.NEVER:
            raise ToolError(
                f"The user has closed {device} to proposed changes. Do not propose "
                "again for this router; tell them what you would have changed."
            )
        summary = " ".join(str(args.get("summary") or "").split())
        if not summary:
            raise ToolError("summary is required: what the change is for, in plain words.")
        commands = args.get("commands") or []
        if isinstance(commands, str):
            commands = [commands]
        if not 1 <= len(commands) <= chg.MAX_COMMANDS:
            raise ToolError(f"commands: between 1 and {chg.MAX_COMMANDS} lines.")
        checks = args.get("checks") or []
        if not isinstance(checks, list) or not 1 <= len(checks) <= chg.MAX_CHECKS:
            raise ToolError(
                "checks: at least one. A change is kept only if its checklist "
                "passes, so say what must still work afterwards."
            )
        job = chg.new_job(
            device=device, host=conn.host, login=conn.login, summary=summary[:600],
            commands=[chg.check_command(c) for c in commands],
            checks=[chg.check_check(c) for c in checks],
            settle_s=diag.number(args.get("settle_s"), "settle_s", 0, chg.MAX_SETTLE_S, 5),
        )
        busy = chg.active_for(conn.host, conn.login)
        if busy is not None:
            raise ToolError(
                f"Change {busy['id']} for {device} is still {busy['status']}. One at "
                "a time per router: see change_status."
            )
        chg.save(job)
        try:
            self._launch_change(job["id"])
        except (OSError, ToolError) as e:
            job["status"] = chg.NOT_APPLIED
            job["result"] = {"status": chg.NOT_APPLIED,
                             "why": f"The x3 change window could not be opened: {e}"}
            chg.save(job)
            raise ToolError(f"The x3 change window could not be opened: {e}") from None
        return (
            f"Change {job['id']} for {device} is now in front of the user in an x3 "
            "window. Nothing has been sent to the router. Call change_status with "
            f"id {job['id']} (wait_s up to 55) to learn their decision and the "
            f"result. If approved, the run takes up to {chg.deadline_s(job)}s."
        )

    def change_status(self, args: dict[str, Any]) -> str:
        job_id = str(args.get("id") or "").strip()
        wait = diag.number(args.get("wait_s"), "wait_s", 0, 55, 0)
        job = chg.load(job_id)
        seen = (job.get("status"), len(job.get("events") or []))
        end = time.monotonic() + wait
        while (job.get("status") not in chg.FINAL and not chg.abandoned(job)
               and time.monotonic() < end):
            time.sleep(0.5)
            job = chg.load(job_id)
            if (job.get("status"), len(job.get("events") or [])) != seen \
                    and job.get("status") != chg.RUNNING:
                break
        return chg.report(job)

    def _snapshot_dirs(self, device: str) -> tuple[str, list[Path]]:
        """Where this device's snapshots live, found by its address: the
        directory name is identity_host, and the identity may have changed
        since an older one was taken."""
        host = self.source.run(device, lambda c: (
            c.host if int(c.port or 8291) == 8291 else f"{c.host}:{c.port}"
        ))
        root = _export_root()
        slug = _slug(host)
        dirs = sorted(p for p in root.glob(f"*_{slug}") if p.is_dir()) if root.is_dir() else []
        return host, dirs

    def _snapshots(self, device: str) -> list[Path]:
        _host, dirs = self._snapshot_dirs(device)
        files = [f for d in dirs for f in d.glob("*.rsc")]
        return sorted(files, key=lambda f: f.name, reverse=True)

    def snapshot_config(self, args: dict[str, Any]) -> str:
        device = _device(args)
        label = str(args.get("label") or "mcp")
        from ..ui import autoexport

        def take(client: DeviceClient) -> Any:
            got = client.export_config(show_sensitive=False)
            if not got.complete:
                raise ToolError("The export stopped before the end; nothing was saved.")
            host = client.host if int(client.port or 8291) == 8291 else f"{client.host}:{client.port}"
            who = SimpleNamespace(identity=client.info.identity or "", host=host)
            return autoexport.store(got, session=who, reason=label)

        rec = self.source.run(device, take)
        if rec.unchanged:
            return ("Unchanged since the last snapshot, so nothing new was "
                    f"written. Newest: {self._snapshots(device)[0].stem}"
                    if self._snapshots(device) else "Unchanged since the last snapshot.")
        return f"Saved {rec.path.stem} ({rec.path})"

    def list_snapshots(self, args: dict[str, Any]) -> str:
        files = self._snapshots(_device(args))
        if not files:
            return "No snapshots of this device yet. snapshot_config takes one."
        return "\n".join(f.stem for f in files[:100])

    def _config_lines(self, spec: str, section: str) -> tuple[str, list[str]]:
        name, _, snap = spec.partition("@")
        name, snap = name.strip(), snap.strip()
        if not name:
            raise ToolError("Name a device, optionally with @last or @<snapshot>.")
        if snap:
            files = self._snapshots(name)
            if not files:
                raise ToolError(f"No snapshots of {name} yet.")
            pick = files[0] if snap.lower() in ("last", "latest") else next(
                (f for f in files if f.stem.startswith(snap) or snap in f.stem), None)
            if pick is None:
                raise ToolError(f"No snapshot {snap!r} of {name}. See list_snapshots.")
            text = pick.read_text(encoding="utf-8", errors="replace")
            label = f"{name}@{pick.stem}"
        else:
            got = self.source.run(name, lambda c: c.export_config(show_sensitive=False))
            if not got.complete:
                raise ToolError(f"The export of {name} stopped before the end. Try again.")
            text, label = got.text, f"{name} (now)"
        lines = cexp.redact(text).splitlines()
        if section:
            lines = _export_section(lines, section)
        # The header carries the time of the export and nothing else to compare.
        lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
        return label, lines

    def diff_config(self, args: dict[str, Any]) -> str:
        a = str(args.get("a") or "").strip()
        b = str(args.get("b") or "").strip()
        if not a:
            raise ToolError("a is required: a device, or device@last.")
        if not b:
            if "@" in a:
                raise ToolError("With a snapshot as a, name b too.")
            a, b = f"{a}@last", a
        words = str(args.get("section") or "").replace("/", " ").split()
        section = "/" + " ".join(words).lower() if words else ""
        la, ta = self._config_lines(a, section)
        lb, tb = self._config_lines(b, section)
        diff = list(difflib.unified_diff(ta, tb, la, lb, n=1, lineterm=""))
        if not diff:
            return f"No difference between {la} and {lb}" + (f" under {section}." if section else ".")
        if len(diff) > 1000:
            diff = diff[:1000] + [f"# ... {len(diff) - 1000} more diff lines; narrow with section"]
        return "\n".join(diff)


#: Devices on_devices takes in one call.
MAX_FANOUT = 12


def _export_root() -> Path:
    from ..ui import autoexport
    return autoexport.export_root()


def _slug(text: str) -> str:
    from ..ui import autoexport
    return autoexport._slug(text)


def _line_filter(args: dict[str, Any]) -> Callable[[str], bool] | None:
    contains = str(args.get("contains") or "").lower()
    pats = []
    for key in ("match", "exclude"):
        text = str(args.get(key) or "")
        if text:
            try:
                pats.append(re.compile(text, re.I))
            except re.error as e:
                raise ToolError(f"{key} is not a regular expression: {e}") from None
        else:
            pats.append(None)
    match, exclude = pats
    if not (contains or match or exclude):
        return None
    return lambda ln: ((not contains or contains in ln.lower())
                       and (match is None or bool(match.search(ln)))
                       and (exclude is None or not exclude.search(ln)))


#: Log lines read_log returns unless asked for more.
DEFAULT_LOG_LINES = 50

_LOG_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}|[a-z]{3}/\d{2}(?:/\d{4})?|\d{2}:\d{2}:\d{2}", re.I)


def _log_topics(line: str) -> set[str]:
    """The topics of one ``/log print`` line: the first word after its date
    and time, ``system,info,account`` -> {system, info, account}."""
    for word in line.split():
        if not _LOG_STAMP.fullmatch(word):
            return {t for t in word.lower().split(",") if t}
    return set()


def _iface_names(client: DeviceClient) -> list[str]:
    return [name for _id, name, _type in client.interface_choices() if name]


def _pick_columns(menu: sch.Menu | None, cols: list[str], asked: Any) -> list[int]:
    """Indexes into ``cols`` to show: the ones asked for, else the default set.

    ``cols`` is :meth:`DeviceClient.table_rows`' header -- flags, the rule
    number on an ordered list, then every column the menu has with the .jg's
    default-visible ones first. The full set is what a header menu offers a
    person; for a model it is a wall of counters nobody asked about.
    """
    if asked:
        if not isinstance(asked, list):
            raise ToolError("columns must be a list of column names.")
        picked, missing = [], []
        for a in asked:
            i = _column_index(cols, str(a))
            if i is None:
                missing.append(str(a))
            else:
                picked.append(i)
        if missing:
            raise ToolError(
                f"No column {', '.join(map(repr, missing))}. This list has: "
                + ", ".join(c for c in cols if c)
            )
        return [0] + picked
    lead = 2 if len(cols) > 1 and cols[1] == sch.INDEX_LABEL else 1
    if menu is None:
        return list(range(len(cols)))
    n = len(sch.default_columns(menu)) or len(sch.preferred_columns(menu))
    # A rate is the difference between two reads, and this is one: the list
    # pane fills those cells from its previous sample, here they are empty.
    rates = {
        lead + j for j, col in enumerate(sch.list_columns(menu)) if col.kind == "rate"
    }
    return [i for i in range(min(len(cols), lead + n)) if i not in rates]


def _words(text: str) -> tuple[str, ...]:
    return tuple(re.sub(r"[-_./]+", " ", text.lower()).split())


def _column_index(cols: list[str], asked: str) -> int | None:
    """Where ``asked`` is in ``cols``: by the list's own name, or by the console's.

    A model writes what the router's command line calls a property --
    ``mac-address``, ``board`` -- where the list says "MAC Address" and "Board
    Name". The command line also drops an "IP" in front and a "Name" behind:
    ``address`` is the IP Address column. Only a single match counts; a name
    that could be either of two columns is no answer.
    """
    want = _words(asked)
    if not want:
        return None
    for i, c in enumerate(cols):
        if c and _words(c) == want:
            return i
    loose = []
    for i, c in enumerate(cols):
        w = _words(c)
        forms = {w}
        if len(w) > 1 and w[0] == "ip":
            forms.add(w[1:])
        if len(w) > 1 and w[-1] == "name":
            forms.add(w[:-1])
        if len(w) > 2 and w[0] == "ip" and w[-1] == "name":
            forms.add(w[1:-1])
        if want in forms:
            loose.append(i)
    return loose[0] if len(loose) == 1 else None


def _read_form(client: DeviceClient, ref: MenuRef, path: tuple[int, ...]) -> str:
    menu = client.menu_for(path, name=ref.node.name)
    row = client.get(path, menu=menu)
    codes, names = _secret_codes(client, menu)
    out = [f"# {ref.title}"]
    if row.form_fields:
        tab = None
        for t, code, name, value, _ok in row.form_fields:
            if t != tab:
                tab = t
                out.append(f"[{t}]")
            out.append(f"{name}: {HIDDEN if _is_secret(name, code, codes, names) else _cell(value)}")
    else:
        for code, name, value, _ok in row.fields:
            out.append(f"{name}: {HIDDEN if _is_secret(name, code, codes, names) else _cell(value)}")
    return "\n".join(out)


def _export_header(lines: list[str]) -> list[str]:
    """The ``#`` lines an export opens with: when, which firmware, which box."""
    head = []
    for ln in lines:
        if not ln.startswith("#"):
            break
        head.append(ln)
    return head


def _export_section(lines: list[str], section: str) -> list[str]:
    """``section``'s lines of a terse export, under the export's own header.

    Terse puts each line's full menu path at its start, so the cut is mostly a
    prefix match. Not always: a menu the router annotates comes as a bare path
    line, then its ``#`` notes and ``set`` lines with no path of their own --
    CAPsMAN-managed wireless reads ``/interface wireless``, ``# managed by
    CAPsMAN``, ``set [ find default-name=wlan1 ] ...``. Those lines belong to
    the path line above them.
    """
    head = _export_header(lines)
    body, inside = [], False
    for ln in lines[len(head):]:
        if ln.startswith("/"):
            inside = ln == section or ln.startswith(section + " ")
        if inside:
            body.append(ln)
    return head + body


def _device(args: dict[str, Any]) -> str:
    device = str(args.get("device") or "").strip()
    if not device:
        raise ToolError("Name a device: see list_devices.")
    return device
