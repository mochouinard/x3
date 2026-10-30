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
from typing import Any, Callable, Protocol

from x3.proto import configexport as cexp
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, DeviceError

from ..ui import connstore as cs
from ..ui.services import discover_neighbors, load_address_book, split_host_port
from . import diag
from .protocol import ToolError

#: What a fresh model is told about this server before its first call.
INSTRUCTIONS = (
    "x3 reads routers over the Winbox protocol, using the user's "
    "saved x3 address book. Start with list_devices; name a device by what it "
    "lists. list_menus shows what a device offers; read_menu reads one. "
    "ping, traceroute, read_log and interface_traffic run from the router "
    "itself. Everything is read-only: this server cannot change a router. "
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

    def __init__(self, source: DeviceSource) -> None:
        self.source = source
        self._menus: dict[str, list[MenuRef]] = {}

    def definitions(self) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], str]]]:
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
