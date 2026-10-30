"""Menu/field schema for the router, derived from Winbox .jg plugin files.

A .jg file is the Winbox UI definition (see jg.py). Read structurally it is
also a complete, machine-readable map of the router management surface: for every
menu it gives the handler path, the wire field id of every attribute, that
field's type, its human name, and its enum/range/unit metadata. For firmware 7.23.1
that is ~480 handler paths and ~7800 fields -- the same information you would
otherwise recover one probe at a time.

Field ids carry their wire type in the first character, which maps 1:1 onto the
tags in message.py. Uppercase is the array form of the lowercase scalar::

    u number/enm/interval/ipaddr/fixedpoint   T_U32     U -> T_U32_ARRAY
    b bool/flag                               T_BOOL
    s string/secret/script/password           T_STRING  S -> T_STRING_ARRAY
    q bignumber/bigbytes/bigbitrate           T_U64     Q -> T_U64_ARRAY
    r macaddr/raw/macnetwork                  T_RAW     R -> T_RAW_ARRAY
    a ip6addr/network6                        T_IP6     A -> T_IP6_ARRAY
    m addr/message                            T_MESSAGE M -> T_MESSAGE_ARRAY

Note ``ipaddr`` is a plain u32 on the wire and ``macaddr`` a 6-byte raw, so the
semantic .jg type is what tells you how to render a value -- the wire tag alone
cannot.

Three structural rules matter and are easy to get wrong. All three were
confirmed against a live 7.23.1 device:

1.  A node that has an ``id`` owns its ``c:`` children as a *nested* namespace
    (the element layout of an array/union), not as sibling fields. Flattening
    them corrupts the parent: ``/user`` has ``{name:'Name',id:'s1'}`` and also
    ``{name:'Allowed Address',id:'M10',c:[...{id:'u1'}...]}``, and a naive walk
    overwrites field 1 with the nested ``u1``.
2.  Container nodes without an ``id`` (``opt``, ``tab``, ``cond``, ``union``,
    ``group``) do hold sibling fields in ``c:`` and must be descended into.
3.  A handler path is *not* a unique key. 116 of 481 paths on 7.23.1 host more
    than one menu -- typically a ``map`` record plus several ``doit`` dialogs
    whose field ids overlap but mean different things. Keep them separate.

The router is also looser about wire types than the schema is: integers arrive at
whatever width fits (u8/u32/u64 interchangeably) and a declared array may
arrive as a bare scalar when it holds a single element. ACCEPTED_TAGS encodes
that tolerance so validation does not report noise.
"""

from __future__ import annotations

import glob
import os
import pickle
import re
from dataclasses import dataclass, field as dc_field, replace
from typing import Any, Iterator

from . import jg
from . import message as m

# Menu node types that own a field namespace.
MENU_TYPES = {"map", "item", "query", "action", "doit"}

# Nodes that group sibling fields rather than describing one.
CONTAINER_TYPES = {"opt", "tab", "cond", "union", "group", "set", "separator", "enable", "comment"}

# Containers that hold the human label for an otherwise unnamed inner field:
# `{name:'Timeout',type:'opt',c:[{type:'interval',id:'u29'}]}` is how the UI
# labels an optional field, and the id-bearing child carries no name of its own.
# Menu containers (map/item/doit/query) are excluded -- their name names the
# menu, not a field.
NAME_FROM_CONTAINER = {"opt", "union", "tuple", "table", "multituple"}

#: `.jg` node types that are a *rate*: no id of their own, a ``value:`` naming
#: the field whose movement they report. See the branch in :func:`_collect`.
RATE_TYPES = {"changerate", "bigchangerate", "bigbitchangerate"}

# Field-id prefix -> canonical wire tag used when encoding.
CANONICAL_TAG = {
    "u": m.T_U32,
    "b": m.T_BOOL_TRUE,
    "s": m.T_STRING,
    "q": m.T_U64,
    "r": m.T_RAW,
    "a": m.T_IP6,
    "m": m.T_MESSAGE,
    "U": m.T_U32_ARRAY,
    "S": m.T_STRING_ARRAY,
    "Q": m.T_U64_ARRAY,
    "R": m.T_RAW_ARRAY,
    "A": m.T_IP6_ARRAY,
    "M": m.T_MESSAGE_ARRAY,
}

# The array half of CANONICAL_TAG: an id whose prefix is upper case holds a
# list, and this is the tag that list goes out under.
ARRAY_TAGS = {p: t for p, t in CANONICAL_TAG.items() if p.isupper()}

_INTS = {m.T_U32, 0x09, m.T_U64}
_BOOLS = {m.T_BOOL_FALSE, m.T_BOOL_TRUE}
_STRS = {m.T_STRING, 0x21}
_RAWS = {m.T_RAW, 0x31}
_MSGS = {m.T_MESSAGE, 0x29}

# The tags that natively *are* each prefix's type. Used to disambiguate a field
# id declared more than once in one menu, before falling back to the tolerant
# ACCEPTED_TAGS below.
STRICT_TAGS = {
    "u": {m.T_U32, 0x09},
    "b": set(_BOOLS),
    "s": set(_STRS),
    "q": {m.T_U64},
    "r": set(_RAWS),
    "a": {m.T_IP6},
    "m": set(_MSGS),
    "U": {m.T_U32_ARRAY},
    "Q": {m.T_U64_ARRAY},
    "S": {m.T_STRING_ARRAY, 0xA1},
    "R": {m.T_RAW_ARRAY, 0xB1},
    "A": {m.T_IP6_ARRAY},
    "M": {m.T_MESSAGE_ARRAY, 0xA9},
}

# Tags we tolerate on receive for each prefix. Wider than CANONICAL_TAG because
# The router re-widths integers freely and collapses single-element arrays to scalars.
ACCEPTED_TAGS = {
    "u": _INTS | _BOOLS,
    "q": _INTS,
    "b": _BOOLS | _INTS,
    "s": _STRS,
    "r": _RAWS,
    "a": {m.T_IP6} | _RAWS,
    "m": _MSGS,
    "U": {m.T_U32_ARRAY} | _INTS,
    "Q": {m.T_U64_ARRAY, m.T_U32_ARRAY} | _INTS,
    "S": {m.T_STRING_ARRAY, 0xA1} | _STRS,
    "R": {m.T_RAW_ARRAY, 0xB1} | _RAWS,
    "A": {m.T_IP6_ARRAY} | _RAWS,
    "M": {m.T_MESSAGE_ARRAY, 0xA9} | _MSGS,
}


@dataclass
class Field:
    """One attribute of a menu, with its wire id and presentation metadata."""

    code: int
    prefix: str
    name: str
    jg_type: str
    ro: bool = False
    optional: bool = False
    nonpublic: bool = False
    scale: int | None = None
    postfix: str | None = None
    enum: dict[int, str] | None = None
    enum_path: tuple[int, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    default: Any = None
    # For `network`/`network6`, the field id holding the prefix length or mask.
    maskid: int | None = None
    # For `ipaddrandport`, the field id holding the port. Connections declares
    # its addresses this way up to 7.19 at least: Src. Address is u7 with its
    # port in u9, a code nothing else names.
    portid: int | None = None
    # `range:1` network: the maskid holds the range's HIGH address, not a
    # netmask (firewall address lists, NAT "To Addresses"). Off = maskid is a
    # netmask / prefix length.
    is_range: bool = False
    # For a `numberrange`/`integerrange`: the wire code holding the high end of
    # the range (the field's own id is the low end). On a `multinumberrange`
    # (`.jg` ``id2``) it is the whole *array* of high ends, one per range.
    # None = not a range, or a range list carrying its pairs in one array.
    highid: int | None = None
    # `.jg` ``radix``: the base a number is shown/entered in. 16 = hex (bridge
    # Priority, firewall TOS); the "hex" postfix that rides with it is a radix
    # marker, not a unit, so it is not appended.
    radix: int = 10
    # `.jg` ``zeroinvalid``: a zero value means "unset" and is shown blank
    # (a DNS Static A record with no address, an empty IP field), not 0.0.0.0.
    zeroinvalid: bool = False
    # A multi field's element type, from its idless child: a stats counter is
    # ``{type:'multibignumber', c:[{type:'bigbytes'}]}`` and each element is a
    # byte count, not a bare integer. None = elements render as the field does.
    element_type: str | None = None
    #: Bounds declared on that element child. The field's own ``min``/``max``
    #: bound the array's *length*, so a cell's range has to be kept apart.
    element_min: Any = None
    element_max: Any = None
    #: Memoised single-element view of this field, built on first render.
    _element_view: "Field | None" = dc_field(
        default=None, repr=False, compare=False
    )
    # `.jg` ``hideonempty``: the row is not drawn at all unless the object
    # carries a value for it. Every use of it in 7.21 is an ethernet/switch
    # counter -- a port's stats tab lists the counters that port keeps, and
    # Winbox leaves out the ones the chip does not report.
    hide_on_empty: bool = False
    # `req:1`: WinBox blocks OK/Apply until this field is filled. The device
    # does not enforce it (a gateway-less route is accepted, then vanishes), so
    # it is a local validation only.
    required: bool = False
    # For a bare `not`-wrapped value (firewall TOS Value, Discovery Interface
    # List): the wire code of the negate flag. The field itself is the inner
    # value; a "!" toggle on the form writes this bool. None = not negatable.
    negate_code: int | None = None
    nested: dict[int, "Field"] = dc_field(default_factory=dict)
    # Other wire types the same menu declares for this id. A menu may declare
    # one field id several times -- as union alternatives, or once per UI mode
    # gated on a `cond` -- and the device may send any of those forms.
    alt_prefixes: set[str] = dc_field(default_factory=set)
    # The other declarations in full, keyed by prefix, so for_tag() can return
    # the one the wire actually matches instead of guessing.
    variants: dict[str, "Field"] = dc_field(default_factory=dict)
    # Sibling variants of a `single` union collapsed onto this (the primary)
    # field: Allow Address is an IPv4 network OR an IPv6 network; an address
    # list entry is an IPv4 network OR a DNS name. Each has its own wire code;
    # the UI shows one row that switches between them (see UnionEditor).
    union_alts: list["Field"] = dc_field(default_factory=list)
    # Winbox list "flags" column letter (e.g. R=running, S=slave, X=disabled).
    hint: str | None = None
    # dateandtime: value is often 1/scale-second units; relative:1 marks
    # link-up/down style fields that may be age-like on the wire.
    relative: bool = False
    #: `.jg` add-time defaults that are not a plain value: ``todaydef`` (a
    #: date), ``nowdef`` (a time), ``randomdef`` (a MAC), ``defuser`` (the
    #: logged-in user). One of "today" | "now" | "random" | "user".
    default_kind: str | None = None
    #: `.jg` ``deflen`` / ``defmask``: the default for this field's companion
    #: length/mask code -- an IPv6 address gets a /64, an IPv4 network a
    #: 255.255.255.0. See :attr:`maskid`.
    default_mask: Any = None
    #: `.jg` ``deflow`` / ``defhigh``: a range's default ends (the low goes to
    #: this field's own code, the high to :attr:`highid`).
    default_low: Any = None
    default_high: Any = None
    #: `.jg` ``sortbyvalue`` / ``sorted``: how a dropdown is ordered. The .jg
    #: asks for one explicitly where it wants one, so the default is the order
    #: the values arrived in -- the device's own for a live list.
    sort_by_value: bool = False
    sort_alpha: bool = False
    #: `.jg` ``cols`` / ``rows``: the editor's size in characters and lines.
    cols: int | None = None
    rows: int | None = None
    #: `.jg` ``small``: draw a set's checkboxes compactly (firewall ▸ Days).
    small: bool = False
    #: `.jg` ``lastcol``: parsed, not acted on. "Put this column last" is the
    #: obvious reading and it is wrong: Interface declares it on Tx / Rx, which
    #: Winbox shows in the middle of the default set (see
    #: test_default_columns.py). Perhaps "last of its group", perhaps "the
    #: column that stretches". Left alone until something settles it.
    last_column: bool = False
    #: `.jg` ``trimzeros``: drop a fixedpoint's trailing zeros. Without it the
    #: scale's own precision is kept, so 1.50 stays 1.50.
    trim_zeros: bool = False
    #: `.jg` ``inlinepostfix``: a table keeps this unit in the cell ("0.0%")
    #: instead of moving it into the column header as it does otherwise.
    inline_postfix: bool = False
    #: `.jg` ``precise``: show the exact byte count, not a rounded unit.
    precise: bool = False
    #: `.jg` ``nofilter``: keep this field out of the list's text filter.
    no_filter: bool = False
    #: `.jg` ``inv``: the UI shows the *opposite* of the wire. 29 fields are
    #: declared that way -- a wireless interface's "Multicast Buffering" is the
    #: negation of the bit the device keeps, and a bridge host's flag letter is
    #: shown when its bit is clear. Reading the bit straight showed every one
    #: of them backwards.
    invert: bool = False
    #: `.jg` ``optval``: the wire value that *means* "not set" for an optional
    #: field. A Shared Users of 0 is "unlimited", not zero users, and clearing
    #: the field sends that 0 rather than leaving the field out.
    unset_value: Any = None
    #: `.jg` ``private``: a secret the device never sends back (Quick Set's
    #: WiFi Password). Whatever arrives is not the password, so it is not shown
    #: and not sent unless something was typed.
    private: bool = False
    #: `.jg` ``hyperlink``: the menu this value belongs to, so the row can
    #: open it. The value is an object id there, which is also what names it.
    link_path: tuple[int, ...] | None = None
    #: `.jg` ``showdef`` (on the ``opt`` wrapper): while this field is unset,
    #: show the value the device would use, greyed, rather than an empty box.
    #: The value comes from the object's defaults message -- see
    #: :attr:`Menu.defaults_code`.
    show_default: bool = False
    #: `.jg` ``offset``: the wire number is shifted by this much before it is
    #: shown. An LTE RSSI arrives as 0..40 and means -95..-55 dBm, so the
    #: unshifted number is not just badly formatted, it is the wrong value.
    offset: int | None = None
    #: `.jg` ``radio``: a small enum Winbox draws as radio buttons rather than
    #: a dropdown (Rate default/configured, Use IPv6 no/yes/required/default).
    radio: bool = False
    #: `.jg` ``compact``: a radio group Winbox draws as an ordinary labelled
    #: row. Without it the group is a *section*: the field name sits in a rule
    #: above the buttons (wireless Rate, PPP Use IPv6). See RadioSection.
    compact: bool = False
    #: `.jg` ``columns``: how many members of a ``set`` Winbox puts on one row
    #: before wrapping -- 4 for the wireless rates, 8 for Allow From Ports.
    #: Only ``set`` declares it (39 of them in 7.21). None = one row, no wrap.
    set_columns: int | None = None
    #: `.jg` ``numbar`` colours: (level, colour, label) worst-first. The first
    #: entry whose level is at or above the value names and colours it --
    #: "good", yellow. The last level is a sentinel maximum.
    bar_colors: tuple[tuple[int, str, str], ...] | None = None
    #: `.jg` ``enumfilter``: (id, pred) per enum entry. The map in
    #: :attr:`enum` is the whole vocabulary -- needed to *render* whatever the
    #: device sends -- while these say which entries this object may be *set*
    #: to. Wireless Band and Channel Width are filtered on the radio's own
    #: `wphymodes` bits, so an unfiltered dropdown offers 5GHz on a 2.4GHz
    #: card. None = every entry is offerable. See :func:`enum_allowed_ids`.
    enum_filters: tuple[tuple[int, Any], ...] | None = None
    #: `.jg` ``pred`` on a ``values:{type:'dynamic'}`` source: which *rows* of
    #: that menu this field may be set to. The path names the whole table and
    #: the pred narrows it -- a bridge port's Bridge reads (20,0), every
    #: interface, filtered to ``type == 12``, so the dropdown offers the
    #: bridges rather than the interface list. 217 dynamic sources in 7.21
    #: carry one (140 fields once the type overlays are merged):
    #: "an interface with the VLAN capability", "a CA certificate", "an IPv4
    #: address". None = the whole table is offerable.
    #: See :func:`enum_source_allows`, which decides one row against it.
    enum_pred: Any = None
    #: `.jg` ``slotenum``: the two slots (fields on this same object) holding
    #: the entry labels and their wire values -- wireless Tx/Rx Chains, where
    #: the chain count and names come from the card. (names_slot, values_slot).
    enum_slots: tuple[str, str] | None = None
    #: `.jg` ``offsetenum``: name of the slot whose value shifts every entry's
    #: wire value (the wireless frequency offset).
    enum_offset_slot: str | None = None
    #: `.jg` ``queryenum``: a device query behind the dropdown -- the wireless
    #: frequency list, which the router computes from band/width/country.
    #: {"path": (…), "cmd": int, "values_id": code, "labels_id": code,
    #:  "params": [(name, code), …]}. Kept so the UI can run it.
    enum_query: dict[str, Any] | None = None
    # `.jg` ``width``: the pixel width Winbox gives this field's list column --
    # and, by its presence, the statement that the field *is* one of the list's
    # default columns. A field without it exists but starts hidden, offered by
    # the header's column menu. See :func:`default_columns`.
    column_width: int | None = None

    @property
    def wire_tag(self) -> int:
        return CANONICAL_TAG[self.prefix]

    @property
    def is_array(self) -> bool:
        return self.prefix.isupper()

    def accepts(self, tag: int) -> bool:
        if tag in ACCEPTED_TAGS.get(self.prefix, set()):
            return True
        return any(tag in ACCEPTED_TAGS.get(p, set()) for p in self.alt_prefixes)

    def element_view(self) -> "Field":
        """This field as its own element type, for rendering one array item.

        ``multibignumber`` says how the value is carried, not what it means:
        the element child says that, and it is what decides whether 2521802342
        reads as itself or as 2404.8 MiB.
        """
        if not self.element_type or self.element_type == self.jg_type:
            return self
        if self._element_view is None:
            # A positional vocabulary (gridmultinumber) names the array's
            # slots, so it must not follow the element view and rename its
            # value; a real element enum (multinumber of enms) does.
            positional = self.jg_type in ("gridmultinumber", "numbertable")
            self._element_view = replace(
                self, jg_type=self.element_type, element_type=None,
                _element_view=None,
                enum=None if positional else self.enum,
                enum_path=None if positional else self.enum_path,
                minimum=self.element_min if self.element_min is not None
                else (None if positional else self.minimum),
                maximum=self.element_max if self.element_max is not None
                else (None if positional else self.maximum),
            )
        return self._element_view

    def for_tag(self, tag: int) -> "Field":
        """The declaration matching this wire tag, when the id has several.

        Prefers a declaration whose own type the tag natively is, before the
        permissive ACCEPTED_TAGS fallback -- otherwise `u`'s tolerance for
        scalars would swallow a `U` array's tag and mislabel the value.
        """
        if not self.variants:
            return self
        candidates = [self, *self.variants.values()]
        for fld in candidates:
            if tag in STRICT_TAGS.get(fld.prefix, ()):
                return fld
        for fld in candidates:
            if tag in ACCEPTED_TAGS.get(fld.prefix, ()):
                return fld
        return self

    def _number(self, value: int) -> str:
        """One number the way this field spells it -- ``0x1f`` on `radix:16`."""
        return f"0x{value:x}" if self.radix == 16 else str(value)

    def render(
        self,
        value: Any,
        *,
        lookup: dict[int, str] | None = None,
        uptime: float | None = None,
    ) -> str:
        """Format a decoded wire value the way the UI would show it.

        ``lookup`` resolves dynamic enums (e.g. Interface id → name from
        another menu). Static ``self.enum`` is tried first.

        ``uptime`` is the device's uptime in seconds, needed to turn a
        ``type:'age'`` stamp into the duration Winbox displays -- see
        :func:`format_age_stamp`.
        """
        if isinstance(value, list):
            # A range list is a flat run of (low, high) pairs, so element by
            # element it reads "100, 100" where Winbox shows "100". Unless it
            # declares `id2`: then this array is the lows alone and the highs
            # are a sibling code, which only the row-level _render can reach.
            if self.jg_type in RANGE_LIST_TYPES and self.highid is None:
                ranges = format_range_list(value, self._number)
                if ranges is not None:
                    return ranges
            # Prefer scalar enum when the same field id is also multi (the router
            # often reuses ids). A single-element list is common for multi.
            if self.enum is not None and len(value) == 1:
                return self.render(value[0], lookup=lookup, uptime=uptime)
            elem = self.element_view()
            if self.is_array or self.jg_type.startswith("multi") or len(value) != 1:
                return ", ".join(
                    elem.render(v, lookup=lookup, uptime=uptime) for v in value
                )
            return elem.render(value[0], lookup=lookup, uptime=uptime)

        # An addr record keeps everything above 0xFE0000, which the summary
        # below skips as plumbing -- why Dst. Address and Gateway were blank.
        # `lookup` is Interface's id -> name map here: see _render.
        if isinstance(value, m.Message) and self.jg_type == "addr":
            return format_addr(value, lookup)

        # Nested M2 (multi / message) — show a short summary, not repr().
        if isinstance(value, m.Message):
            by_code: dict[int, Any] = {}
            for code, _tag, val in value:
                if code < 0xFE0000:
                    by_code.setdefault(code, val)
            # An address entry is an (address, mask) pair, and the mask sits
            # under the address field's own `maskid` rather than as a nested
            # field in its own right. Emitting it as a separate value is what
            # printed "192.168.0.0, 65535" where Winbox shows 192.168.0.0/16.
            mask_codes = {f.maskid for f in self.nested.values() if f.maskid}
            parts = []
            for code, val in by_code.items():
                if code in mask_codes:
                    continue
                nested = self.nested.get(code)
                if nested is None:
                    parts.append(str(val))
                    continue
                text = nested.render(val, lookup=lookup, uptime=uptime)
                if nested.maskid is not None and nested.maskid in by_code:
                    text = _with_prefix(text, by_code[nested.maskid])
                parts.append(text)
            return ", ".join(parts) if parts else ""

        iv: int | None = None
        try:
            if isinstance(value, (bytes, bytearray)) and len(value) <= 8:
                iv = int.from_bytes(value, "little")
            elif isinstance(value, bool):
                iv = int(value)
            elif isinstance(value, int):
                iv = value
            elif isinstance(value, str) and value.lstrip("-").isdigit():
                iv = int(value)
        except (TypeError, ValueError):
            iv = None

        # A secret the device does not send back holds nothing worth showing.
        if self.private:
            return ""

        # `optval`: this exact value is how the field says "not set".
        if (
            self.unset_value is not None
            and iv is not None
            and iv == _as_int(self.unset_value)
        ):
            return ""

        # A `set` is a bitmask over its named members, not a scalar enum: the
        # member at enum key k is bit 1<<k (see SetEditor, confirmed against
        # The router). Reading it as a scalar named PPP Authentication 0x1 --
        # pap alone -- after the *second* member, and printed the wireless
        # Tx Chains mask as "3" instead of "chain0,chain1".
        if self.jg_type in ("set", "multibits") and self.enum and iv is not None:
            if iv == 0:
                return ""
            members = [
                name for key, name in sorted(self.enum.items())
                if iv & (1 << int(key))
            ]
            # No member matched: the value is not this vocabulary's, so show
            # the number rather than falling through and naming a member the
            # mask does not contain.
            return ",".join(members) if members else str(iv)

        # Static enums (e.g. Add Default Route: 0=no, 1=yes, 2=special classless).
        if self.enum is not None and iv is not None:
            if iv in self.enum:
                return self.enum[iv]
            # Tolerate accidental sign-extension noise on tiny enums
            if (iv & 0xFF) in self.enum and iv > 0xFF:
                return self.enum[iv & 0xFF]

        # Dynamic enum (Interface → name)
        if lookup is not None and iv is not None and iv in lookup:
            return lookup[iv]

        # All ones is the router's "none" for an enum or a reference to
        # another table: a disabled route's Contribution arrives as 4294967295,
        # and Winbox shows nothing there.
        if iv == 0xFFFFFFFF and (
            self.enum is not None or self.enum_path is not None or lookup is not None
        ):
            return ""

        if self.jg_type in ("bool", "flag"):
            truth = value if isinstance(value, bool) else (
                bool(iv) if iv is not None else None
            )
            if truth is not None:
                if self.invert:
                    truth = not truth
                return "true" if truth else "false"

        # `offset` / `numbar`: a shifted number, and the "no signal yet"
        # sentinel these fields use (SINR declares def:0x80000000).
        if iv is not None and (self.offset is not None or self.jg_type == "numbar"):
            if iv >= 0x80000000:
                return ""
            if self.offset:
                iv -= self.offset
                value = iv

        # `integer` is signed, carried as a u32: a Noise Floor Threshold of
        # -110 arrives as 4294967186, and a wireless Tx Power of -5 dBm as
        # 4294967291. The schema stores its own min/max the same way (the .jg
        # writes `min:4294967266` for -30), so the conversion belongs here
        # rather than at each use.
        if iv is not None and self.jg_type == "integer" and iv >= 0x80000000:
            iv -= 0x1_0000_0000
            value = iv

        # Unset optional numbers: The router uses high sentinels (0xFFFFFFFF, etc.)
        if iv is not None and self.jg_type in (
            "number", "bignumber", "decimal", "bigdecimal", "numberrange", "age", "interval"
        ):
            if iv >= 0xFFFFFF00 or iv < 0:
                return ""

        # `zeroinvalid`: a zero here means "unset" (DNS Static Address, a blank
        # IP field), which WinBox shows empty rather than as 0.0.0.0 / 0.
        if self.zeroinvalid and (
            iv == 0 or (isinstance(value, (bytes, bytearray)) and not any(value))
        ):
            return ""

        # A type:'age' field is a stamp on the device's uptime clock, not a
        # duration, so it only means something next to that clock.
        if self.jg_type == "age" and iv is not None and uptime is not None:
            # Leases kept on disk come back stamped in a *previous* boot's
            # clock, which arrives as a negative u32 (>= 2^31). There is no
            # way to place those on this boot's clock, so say nothing rather
            # than report the 48000-day gap the subtraction would give.
            if iv >= 0x80000000:
                return ""
            return format_age_stamp(iv, uptime)

        if (
            self.jg_type in ("ipaddr", "network", "network6", "ipaddrandport")
            and iv is not None
        ):
            # The router puts a record menu's IPv4 on the wire with the first dotted
            # octet in the low byte: 0x0158A8C0 is 192.168.88.1. An
            # ipaddrandport's port is joined on in _render_top.
            return format_ipv4(iv)
        if (
            self.jg_type in ("addr", "network6", "ip6addr")
            and isinstance(value, (bytes, bytearray))
            and len(value) == 16
        ):
            import ipaddress

            # `ip6addr` was missing here and every one rendered as 32 hex
            # digits. Traceroute's Host is one, and carries an IPv4 hop
            # IPv4-mapped -- with the IPv4 low byte first, like a record
            # menu's: a trace to the router's own 192.168.219.1 ends in one
            # hop whose last four bytes read 1.219.168.192.
            v6 = ipaddress.IPv6Address(bytes(value))
            if v6.ipv4_mapped is not None:
                return str(ipaddress.IPv4Address(bytes(reversed(v6.packed[12:]))))
            return str(v6)
        if self.jg_type in ("macaddr", "macnetwork") and isinstance(value, (bytes, bytearray)):
            return ":".join(f"{b:02X}" for b in value)
        if self.jg_type == "dateandtime" and iv is not None and iv:
            return _render_dateandtime(iv, scale=self.scale, relative=self.relative)
        # System ▸ Clock, read live off a 7.23 device in America/Toronto:
        # Date 1785888000 (Unix midnight), Time 50751 (seconds since midnight,
        # 14:05:51), GMT Offset 4294952896 (-14400 = -4h). So a clockdate is a
        # timestamp, a clocktime is a time of day, and a timezone is a signed
        # number of seconds -- three different things behind three .jg types
        # that all arrive as a bare u32.
        if self.jg_type == "clockdate" and iv is not None:
            return format_clock_date(iv)
        if self.jg_type == "clocktime" and iv is not None:
            return format_clock_time(iv)
        if self.jg_type == "timezone" and iv is not None:
            return format_gmt_offset(iv)
        # Winbox shows a bare interval as hh:mm:ss. When the template declares
        # its own postfix ("600s") that presentation wins — the .jg is telling
        # us the unit to display.
        if self.jg_type in ("kbytes", "kbyte") and iv is not None:
            return format_kbytes(iv)
        if self.jg_type == "interval" and iv is not None and not self.postfix:
            secs = iv / self.scale if self.scale else iv
            return format_interval(secs)
        if self.scale and iv is not None:
            if (
                self.jg_type == "fixedpoint"
                and not self.trim_zeros
                and self.scale > 1
            ):
                # A fixedpoint keeps the precision its scale implies -- 1.50,
                # not 1.5 -- and `trimzeros:1` is how the .jg asks for the
                # zeros to go. Only fixedpoint: a scaled *interval* with a
                # postfix reads "600s", which is why the flag exists on one
                # type and not the other.
                digits = max(0, len(str(int(self.scale))) - 1)
                text = f"{iv / self.scale:.{digits}f}"
            else:
                text = f"{iv / self.scale:g}"
            return f"{text}{self.postfix}" if self.postfix else text
        if isinstance(value, (bytes, bytearray)):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return value.hex()
        if iv is not None and self.jg_type == "netmask":
            return render_netmask(iv)
        if iv is not None and self.jg_type in ("bigbitrate", "bitrate"):
            # Same reading, two widths: `bigbitrate` is the u64 an interface
            # counter needs, `bitrate` the u32 on a Simple Queue's Avg. Rate
            # and Tools ▸ Ping Speed. Only the first was ever wired, so the
            # nineteen `bitrate` fields showed the raw bits per second.
            return _render_bitrate(iv)
        if iv is not None and self.jg_type == "bigbytes":
            # `precise:1` wants the count itself, not "2.4 MiB".
            return f"{iv:,}" if self.precise else format_bytes(iv)
        if iv is not None and self.jg_type in ("number", "bignumber", "decimal"):
            if self.radix == 16:
                # Shown in hex (bridge Priority, TOS); the "hex" postfix is the
                # radix marker, not a unit to append.
                return f"0x{iv:x}"
            text = str(iv)
            return f"{text}{self.postfix}" if self.postfix else text
        text = str(value)
        return f"{text}{self.postfix}" if self.postfix and iv is not None else text


#: Bits every full read and every mutation carries in the 0xFE000C mask,
#: before a menu's own ``refreshfilter`` is OR'd in. See :attr:`Menu.write_mask`.
STD_MASK_BASE = 5


@dataclass
class Menu:
    """A single menu, dialog or action: one handler path plus a field namespace."""

    path: tuple[int, ...]
    name: str
    title: str
    kind: str
    group: str | None = None
    cli_prefix: str | None = None
    nameval: Any = None
    #: `.jg` ``typeon``: the *name* of the field that says which type overlay an
    #: object belongs to. Interface says 'type', Route says 'rtype' -- the wire
    #: code behind either is unguessable, so the name is what has to be stored.
    typeon: Any = None
    #: `.jg` ``refreshfilter``: the extra 0xFE000C bits this menu's reads and
    #: writes carry on top of :data:`STD_MASK_BASE`. See :attr:`write_mask`.
    refresh_filter: int | None = None
    command: int | None = None
    write_perm: int | None = None
    source: str = ""
    fields: dict[int, Field] = dc_field(default_factory=dict)
    # Property-form layout: (tab_title, field_code) in .jg declaration order.
    # Tabs are siblings in the .jg (`type:'tab'`), not parents — we track the
    # active tab while scanning so DHCP/Advanced/Status survive flattening.
    form_order: list[tuple[str, int]] = dc_field(default_factory=list)
    # Richer layout preserving Winbox chrome the flat form_order loses:
    # tuples (Tx/Rx Rate), separators, graphbox. Each entry is a dict:
    #   {"kind":"field","tab":str,"code":int}
    #   {"kind":"tuple","tab":str,"title":str,"sep":str,"codes":[int,...],"separate":bool}
    #   {"kind":"separator","tab":str}
    #   {"kind":"graphbox","tab":str,"graphs":[{"name":str,"curves":[str,...]}, ...]}
    form_layout: list[dict[str, Any]] = dc_field(default_factory=list)
    # Every declaration in .jg order: (tab_title, code, wire prefix).
    # form_order keeps one entry per code, which is not enough: a menu may
    # declare the same id several times as genuinely different fields, told
    # apart only by wire type and shown on different tabs. Ethernet declares
    # 0x469 as `u469` "PoE Out Status" (PoE tab) and as `Q469` "Tx Bytes"
    # (Overall Stats). decode_form routes each value by the tag it arrives
    # with, so it needs the tab per declaration, not per code.
    form_decls: list[tuple[str, int, str]] = dc_field(default_factory=list)
    # The list's default columns, in .jg declaration order -- every node that
    # carries a ``width``. Each entry is a dict:
    #   {"kind":"field","code":int,"prefix":str,"name":str,"width":int}
    #   {"kind":"tuple","codes":[int,...],"sep":str,"name":str,"width":int}
    #   {"kind":"objtype","name":str,"width":int}
    #   {"kind":"alias","name":str,"width":int}   (resolved by name at use)
    # See :func:`default_columns`, which turns these into Column objects.
    column_decls: list[dict[str, Any]] = dc_field(default_factory=list)
    # `.jg` ``ordered:1`` on the map node: position decides the outcome, so the
    # list shows the rule number and allows reordering. 61 menus declare it --
    # every firewall chain, but also Simple Queues, DNS Static, IPsec Policies,
    # Bridge Ports and Web Proxy Access.
    ordered: bool = False
    # Named visibility conditions from the .jg (type:cond), e.g. sfp / poe.
    conditions: dict[str, Any] = dc_field(default_factory=dict)
    # tab title -> condition name when the tab itself has on:.
    tab_conditions: dict[str, str] = dc_field(default_factory=dict)
    # Winbox ``type:statusbar`` entries — window tray/status bar only, never
    # form rows. Parsed from every menu that declares them (DHCP Client, RADIUS,
    # Queue, Wireless, …). Each item is a dict:
    #   field:  {"kind":"field","code":int,"name":str|None,"withouttitle":bool}
    #   tuple:  {"kind":"tuple","codes":[int,...],"sep":str,"postfix":str|None,
    #            "name":str|None,"withouttitle":bool}
    statusbar: list[dict[str, Any]] = dc_field(default_factory=list)
    # Optional statusbar source (path/autorefresh/cmd) from the .jg node.
    statusbar_meta: dict[str, Any] = dc_field(default_factory=dict)
    # True when the .jg contains ``type:enable`` (Disabled flag 0xfe000a chrome).
    has_enable: bool = False
    #: ``type:'numflag'`` nodes: a number whose *value* picks a flags-column
    #: letter. Route's Contribution (u22) 4 is A(ctive), its type (u112) 2 is
    #: C(onnect) and 10 d(hcp) -- what makes a v3 route read "DAd". Each is
    #: ``{"code": int, "letters": {int: str}, "band": bool}``; with ``band``
    #: the keys are bit positions rather than values.
    numflags: list[dict[str, Any]] = dc_field(default_factory=list)
    # True when the .jg contains ``type:comment`` (Comment button on the form).
    has_comment: bool = False
    # True when the .jg contains ``type:preset``.
    has_preset: bool = False
    #: `.jg` ``def:'mfe0024'`` on the menu itself: the wire code of a nested
    #: message the device sends alongside an object, holding the value each
    #: field would take if it were left unset. It is what a ``showdef`` field
    #: shows greyed -- a WiFi interface inheriting SSID and Country from its
    #: configuration profile. Three menus declare one in 7.21: WiFi, WiFi
    #: Configuration (both 0xFE0024) and CAP Interface (0x7008).
    defaults_code: int | None = None
    #: Winbox ``type:filter``: the dropdowns above a list that narrow it.
    #: Firewall filters on Chain (plus `static` / `dynamic`), Address Lists on
    #: List, Routes6 on Routing Table -- 24 lists declare one. Each item:
    #:   {"filteron": field name, "path": (menu the values come from) | None,
    #:    "conds": [{"name": str, "on": cond name}, …]}
    list_filters: list[dict[str, Any]] = dc_field(default_factory=list)
    # Winbox ``type:contextmenu``: what a right-click on one of this menu's
    # rows offers. Each entry names another menu to open, the fields to carry
    # into it, and whether to run it straight away -- Neighbors ▸ right-click ▸
    # Tools ▸ Ping opens Ping with Ping To already filled in and running.
    # Each item:
    #   {"kind":"contextmenu","name":str,"group":str|None,"open":str|None,
    #    "tab":str|None,"autostart":bool,"on":str|None,
    #    "link":[{"src":str|None,"dst":str,"val":Any,"conv":str|None}]}
    context_actions: list[dict[str, Any]] = dc_field(default_factory=list)
    # Winbox ``type:toggle``: the Simple Mode ⇄ Advanced Mode button in the
    # form's button column (a wireless interface, a PPP Client). Its ``id`` is
    # a dialog-local widget id, not a wire field -- but every ``on:'adv'``
    # condition reads it *by name*, so the form has to supply the value as a
    # pseudo-slot (see :func:`condition_is_true`'s ``ui_slots``).
    #   {"name": "umode", "modes": ["Simple Mode", "Advanced Mode"]}
    mode_toggle: dict[str, Any] | None = None
    # Side-button actions from the template (declaration order), e.g. DHCP
    # Client ``Release`` / ``Renew`` (``type:doit``). Each item:
    #   {"kind":"doit","name":str,"cmd":int,"on":str|None,"global":bool}
    form_actions: list[dict[str, Any]] = dc_field(default_factory=list)
    # Winbox type overlays: base Interface is ``generic='iface'``; Ethernet is
    # ``inherit='iface', typevalue=1`` and merges into the property form.
    generic: str | None = None
    inherit: str | None = None
    typevalue: int | None = None
    #: Title of the deepest type overlay folded into this menu -- the name a
    #: ``type:objtype`` row shows ("Ethernet", "Bridge", "VLAN"). None on a
    #: menu that is not a merged one; see :func:`_merge_overlay_into`.
    type_title: str | None = None
    # ``nonpublic:1`` on the .jg node: not shown in the UI (PWR, Loopback, VRF).
    nonpublic: bool = False
    #: `.jg` ``ro:1`` on the menu node itself: the whole menu is device output,
    #: not settings -- System ▸ Resources, About, License, Log, Neighbors,
    #: Firewall ▸ Connections, Active Users. 104 nodes declare it. Two things
    #: follow: every field under it is read-only (the flag is threaded in as
    #: ``inherited_ro``, the same path a ``ro`` container already took), and
    #: the property form drops Cancel and Apply, because there is nothing on
    #: it to write or to revert. See :meth:`Menu.is_collection` for the other
    #: half of the button stack.
    read_only: bool = False
    #: `.jg` ``nonaddable``: rows here are not created by hand (an interface, a
    #: bridge host, a relayed binding), so Winbox offers no Add. 61 maps.
    addable: bool = True
    #: `.jg` ``nonmovable``: an ordered list whose order is not the user's.
    movable: bool = True
    #: `.jg` ``removable``: 42 maps say so explicitly, and 380 that plainly do
    #: allow Remove say nothing -- so this is recorded and not yet acted on
    #: rather than guessed at. See test_list_flags.py.
    removable_declared: bool = False
    #: `.jg` ``treeon``: this list is a tree, and the named field holds each
    #: row's parent -- a VLAN's `Interface`, a bridge port's `Parent`, a
    #: package's `bundle`, a file's `container`. The value is matched against
    #: the other rows' own names (see :func:`name_field_code`, which follows
    #: ``nameval``), so an address list nests on List and a queue on Name.
    tree_on: str | None = None
    #: `.jg` ``priority`` on a tab: where it goes in the strip. Status declares
    #: 0xFFFFFFFE and Traffic 0xFFFFFFFF, i.e. "last, and after Status".
    tab_priority: dict[str, int] = dc_field(default_factory=dict)
    #: `.jg` ``iconon`` + ``icons``: the field whose value picks a row's icon,
    #: and the value → sprite index map. Firewall lists use it for the Action.
    icon_on: str | None = None
    icon_map: dict[int, int] = dc_field(default_factory=dict)
    #: `.jg` ``highlight``: rows for which this condition holds are marked
    #: (Interface ▸ `notrunning`).
    highlight_on: str | None = None
    #: `.jg` ``maxobjs`` / ``maxobjsmsg``: stop after this many rows, and say
    #: so ("There are too many routes to show them all.").
    max_objects: int | None = None
    max_objects_message: str | None = None
    #: `.jg` ``fulltitle``: the window's title where it differs from the menu's
    #: own ("DNS Static" for the entry the tree calls "Static"). 24 menus.
    full_title: str | None = None
    # --- query / action menus (Ping, Torch, Traceroute, Bandwidth Test) -----
    # A query has two field namespaces: the parameters you fill in (``request``)
    # and the result columns it streams back (``c``). They collide -- Ping
    # declares u3 as both "Packet Size" (request) and "Reply Size" (result) --
    # so they are kept apart, unlike map/item menus which have only one.
    request_fields: dict[int, Field] = dc_field(default_factory=dict)
    request_order: list[tuple[str, int]] = dc_field(default_factory=list)
    request_layout: list[dict[str, Any]] = dc_field(default_factory=list)
    request_decls: list[tuple[str, int, str]] = dc_field(default_factory=list)
    #: Named ``gridcell`` sections of the request, in order. Non-empty means
    #: the request's "tabs" are columns drawn side by side (Traceroute).
    request_gridcells: list[str] = dc_field(default_factory=list)
    start_command: int | None = None    # .jg startcmd
    cancel_command: int | None = None   # .jg cancelcmd
    #: .jg ``autoclose``: a one-shot whose window closes when it is done
    #: (Download Backup, Save Config To). Not a run anyone repeats side by side.
    auto_close: bool = False
    poll_command: int | None = None     # .jg pollcmd
    autorefresh_ms: int | None = None   # .jg autorefresh
    auto_limit: int | None = None       # .jg autolimit (Ping caps at 1000 rows)
    auto_scroll: bool = False           # .jg autoscroll
    uptime_field: str | None = None     # .jg uptime

    @property
    def is_collection(self) -> bool:
        return self.kind == "map"

    @property
    def can_add(self) -> bool:
        """Whether this list offers Winbox's Add.

        Two keys say no, and between them they cover every list Winbox draws
        without a ``+``: ``nonaddable`` (IP ▸ Services -- eight fixed rows you
        configure but never create) and ``ro:1`` (Neighbors, Log, Active
        Users -- the device's own output).
        """
        return self.is_collection and self.addable and not self.read_only

    @property
    def can_remove(self) -> bool:
        """Whether this list offers Winbox's Remove.

        This is what ``removable`` is *for*, and why it looked meaningless on
        its own: 380 lists that plainly allow Remove declare nothing, so the
        flag cannot mean "Remove is allowed" everywhere. It is load-bearing on
        exactly the lists you cannot add to, where it is the only thing
        separating the two kinds:

        * **Add and Remove alike are off** -- IP ▸ Services and Service Ports,
          Neighbors, Log, Packages, Switch ▸ Ports, System ▸ Health. A fixed
          set of rows, or a reading.
        * **Remove without Add** -- Active Users (drop a session), Firewall ▸
          Connections (kill one), System ▸ Ports, Hotspot ▸ Hosts, Tools ▸
          SMS Inbox. Each declares ``removable:1``, and Winbox shows the
          ``-`` on every one of them.

        The exception is a ``generic`` base: Interface and Files are
        ``nonaddable`` because a row is created from a type overlay (Add ▸
        VLAN) or by upload, not because the row is the device's to keep --
        Winbox removes a VLAN from the Interface list and a file from Files.
        """
        if not self.is_collection:
            return False
        if self.can_add:
            # An ordinary list. Nothing to check: this is the 267 that neither
            # declare `removable` nor need to.
            return True
        return bool(self.removable_declared or self.generic)

    @property
    def write_mask(self) -> int:
        """The 0xFE000C value Winbox sends on a full read and on every mutation.

        Derived, not guessed: across all 32 handlers of the 2026-08-06
        add/remove capture the value is always ``STD_MASK_BASE | refreshfilter``.
        Menus declaring no ``refreshfilter`` send a bare 5; Firewall Rule
        declares 2 and sends 7; Interface declares 0x4000A and sends 0x4000F.
        (A bare ``refreshfilter`` also appears on the wire — that is the
        periodic auto-refresh read, a different request.)
        """
        return STD_MASK_BASE | int(self.refresh_filter or 0)

    @property
    def has_age_field(self) -> bool:
        """True when any field is a ``type:'age'`` stamp needing the uptime clock."""
        return bool(self.age_codes())

    def age_codes(self) -> set[int]:
        """Wire codes of this menu's ``type:'age'`` fields.

        Checks tag variants too: DHCP Lease declares id 0x7b twice — ``s7b`` is
        Client ID and ``u7b`` is Age — and only the variant is the age one.
        """
        out = set()
        for code, fld in self.fields.items():
            if fld.jg_type == "age" or any(
                v.jg_type == "age" for v in (fld.variants or {}).values()
            ):
                out.add(code)
        return out

    def statusbar_decls(self) -> set[tuple[int, str]]:
        """(code, wire prefix) of the tray cells -- see :func:`statusbar_owns`."""
        return _statusbar_decls(self.statusbar)

    def statusbar_codes(self) -> set[int]:
        """All wire codes that belong only on the window status bar."""
        return _statusbar_codes(self.statusbar)

    def form_chrome_codes(self) -> set[int]:
        """Codes that must never appear as property-form rows.

        Includes ``type:statusbar`` fields, object ``.id``, and (when the
        template has ``type:enable``) the standard Disabled flag.
        """
        out = self.statusbar_codes()
        out.add(STD_OBJ_ID_CODE)
        if self.has_enable:
            out.add(FLAG_DISABLED_CODE)
        return out

    @property
    def read_command(self) -> int:
        return 0xFE0004 if self.is_collection else 0xFE000D

    def label(self) -> str:
        p = ",".join(str(x) for x in self.path)
        return f"({p}) {self.kind} {self.name!r}"

    def field_for(self, code: int, prefix: str | None = None) -> "Field | None":
        """The declaration of ``code`` with this wire type.

        Fields are keyed by code alone; a second declaration of the same id
        with a different wire type is kept as a variant. A form row knows
        which one it is (its layout entry carries the prefix), so it must be
        able to ask for that one rather than whichever came first.
        """
        return _field_variant(self.fields, code, prefix)

    def decl_tabs(self) -> dict[tuple[int, str], str]:
        """(code, prefix) -> tab, for routing a decoded value to its tab."""
        out: dict[tuple[int, str], str] = {}
        for tab, code, prefix in self.form_decls:
            out.setdefault((code, prefix), tab)
        return out

    def tab_names(self) -> list[str]:
        """Ordered unique tab titles for this menu's property form.

        Declaration order, except where a tab declares a ``priority``: Status
        carries 0xFFFFFFFE and Traffic 0xFFFFFFFF, which is the .jg saying
        "these two go last, in that order", however early they were declared.
        """
        seen: list[str] = []
        for tab, _code in self.form_order:
            if tab not in seen:
                seen.append(tab)
        if self.tab_priority:
            order = {t: i for i, t in enumerate(seen)}
            # A tab with no priority keeps the place it was declared in; one
            # with a priority sorts after all of them, by that number. Both
            # values in 7.21 are near 2^32, which is the .jg spelling "last".
            seen.sort(key=lambda t: (
                (1, self.tab_priority[t]) if t in self.tab_priority
                else (0, order[t])
            ))
        return seen or ["General"]

    def form_fields(self) -> list[tuple[str, Field]]:
        """(tab, field) pairs in UI order for building a property dialog."""
        out: list[tuple[str, Field]] = []
        seen: set[int] = set()
        for tab, code in self.form_order:
            fld = self.fields.get(code)
            if fld is None or code in seen:
                continue
            seen.add(code)
            out.append((tab, fld))
        for code, fld in self.fields.items():
            if code not in seen:
                out.append(("General", fld))
                seen.add(code)
        return out

    def flag_fields(self) -> list[Field]:
        """Fields that contribute letters to the Winbox flags column.

        Order matches .jg declaration (typically X / R / S / …). Includes the
        standard Disabled flag (0xfe000a → 'X') only when the template has
        ``type:enable`` or already declares that flag.
        """
        flags = [
            f for f in self.fields.values()
            if f.jg_type == "flag" and f.hint
        ]
        # Preserve declaration order via form_order when possible
        order = {code: i for i, (_t, code) in enumerate(self.form_order)}
        flags.sort(key=lambda f: order.get(f.code, 10_000 + f.code))
        if self.has_enable and not any(f.hint == FLAG_DISABLED_HINT for f in flags):
            # type:enable uses the standard Disabled flag on the wire even
            # when .jg only has a bare enable chrome node (no id).
            flags.insert(0, Field(
                code=FLAG_DISABLED_CODE,
                prefix="b",
                name="Disabled",
                jg_type="flag",
                hint=FLAG_DISABLED_HINT,
            ))
        return flags

    def __iter__(self) -> Iterator[Field]:
        return iter(self.fields.values())


def format_flag_letters(
    menu: "Menu | None",
    values_by_code: dict[int, Any],
) -> str:
    """Build the Winbox left flags string (e.g. ``'XRS'``, ``'R'``).

    A letter is included when its flag field is present and truthy on the wire.

    The universal flags (X/I/D) come first, in Winbox's legend order, then the
    menu's own letters. They are handled here rather than through
    ``flag_fields`` because the .jg does not declare them -- yet the device
    sends them on every row, and a dynamic entry that reads as static is worse
    than no flags column at all.
    """
    def _on(code: int) -> bool:
        v = values_by_code.get(code)
        return v is True or v == 1

    letters = [hint for code, hint in UNIVERSAL_FLAGS if _on(code)]
    if menu is None:
        return "".join(letters)

    for nf in menu.numflags:
        v = values_by_code.get(nf["code"])
        if isinstance(v, bool) or not isinstance(v, int):
            continue
        for key, letter in nf["letters"].items():
            if (v >> key) & 1 if nf["band"] else v == key:
                letters.append(letter)

    for fld in menu.flag_fields():
        if fld.code in _UNIVERSAL_CODES:
            continue  # already placed, in legend order
        # `inv:1` shows the letter when the bit is *clear*: a bridge host is
        # marked local when its "not local" bit is off.
        shown = _on(fld.code)
        if fld.invert:
            shown = fld.code in values_by_code and not shown
        if shown and fld.hint:
            letters.append(fld.hint)
        # Some replies omit false flags entirely; nothing to add.
    return "".join(letters)


def mask_prefix_len(mask: Any) -> int | None:
    """Prefix length for a contiguous netmask, whatever byte order it is in.

    Counting set bits sidesteps the ordering question: The router sends 255.255.0.0
    as 0x0000FFFF, and a contiguous mask has the same number of ones either
    way round. A non-contiguous value is not a prefix and returns None.
    """
    try:
        iv = int(mask)
    except (TypeError, ValueError):
        return None
    if iv < 0 or iv > 0xFFFFFFFF:
        return None
    bits = bin(iv).count("1")
    # Contiguous in one order or the other; anything else is not a prefix.
    contiguous = {
        (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF if bits else 0,
        (1 << bits) - 1,
    }
    return bits if iv in contiguous else None


def _with_prefix(addr: str, mask: Any) -> str:
    """``192.168.0.0`` + 0x0000FFFF → ``192.168.0.0/16``.

    A full mask is left bare: Winbox shows a single host as just its address.
    """
    if not addr:
        return addr
    bits = mask_prefix_len(mask)
    if bits is None or bits >= 32:
        return addr
    return f"{addr}/{bits}"


def is_disabled_flags(flags: str) -> bool:
    return FLAG_DISABLED_HINT in (flags or "")


def is_dynamic_flags(flags: str) -> bool:
    """Dynamic rows are device-managed: Winbox offers Copy, never Edit."""
    return FLAG_DYNAMIC_HINT in (flags or "")


def _enum_map(values: Any) -> tuple[dict[int, str] | None, tuple[int, ...] | None]:
    """Unpack a .jg `values:` block into a literal map and/or a handler ref."""
    if not isinstance(values, dict):
        return None, None
    vtype = values.get("type")
    if vtype == "static":
        raw = values.get("map")
        if isinstance(raw, list):
            return {i: str(v) for i, v in enumerate(raw)}, None
        if isinstance(raw, dict):
            out = {}
            for k, v in raw.items():
                try:
                    out[int(k)] = str(v)
                except (TypeError, ValueError):
                    continue
            return out, None
        return None, None
    if vtype == "dynamic":
        p = values.get("path")
        if isinstance(p, list) and all(isinstance(i, int) for i in p):
            return None, tuple(p)
        return None, None
    if vtype == "defenum":
        inner, ref = _enum_map(values.get("values"))
        out = {int(values.get("defid", 0)): str(values.get("defname", "none"))}
        if inner:
            out.update(inner)
        return out, ref
    if vtype in ("enumfilter", "offsetenum", "remapenum"):
        # Each wraps another source and narrows or shifts it:
        #   enumfilter  keeps the entries whose pred holds (Band per radio)
        #   offsetenum  shifts every wire value by a slot (frequency offset)
        #   remapenum   reads the pred slots off *another* object (a WDS
        #               link's master radio) -- the vocabulary is the same
        # The whole vocabulary is what renders a value, so the map comes back
        # intact here; the narrowing rides on the Field (see enum_filters).
        return _enum_map(values.get("values"))
    if vtype == "queryenum":
        # The dropdown is a device query, not a table (wireless Frequency).
        return None, None
    if vtype == "slotenum":
        # Labels and values are fields on this object, resolved per row.
        return None, None
    if vtype == "pair":
        # A choice from a dynamic list AND fixed extra entries (Group Authority
        # = an interface OR none / self). Return the static map and the dynamic
        # path together; enum_choices merges them.
        merged: dict[int, str] = {}
        path: tuple[int, ...] | None = None
        for child in _as_node_list(values.get("c")):
            m2, p2 = _enum_map(child)
            if m2:
                merged.update(m2)
            if p2 and path is None:
                path = p2
        return (merged or None), path
    return None, None


def _bar_colors(node: dict[str, Any]) -> tuple[tuple[int, str, str], ...] | None:
    """A ``numbar``'s colour bands: ``colors:[{level,color,label}, …]``."""
    out: list[tuple[int, str, str]] = []
    for band in _as_node_list(node.get("colors")):
        if not isinstance(band, dict):
            continue
        try:
            level = int(band.get("level"))
        except (TypeError, ValueError):
            continue
        out.append((level, str(band.get("color") or ""), str(band.get("label") or "")))
    out.sort()
    return tuple(out) or None


def _enum_filters(values: Any) -> tuple[tuple[int, Any], ...] | None:
    """The `(id, pred)` list of an ``enumfilter``, wherever it is nested.

    ``remapenum`` and ``offsetenum`` wrap one, so the search descends.
    """
    if not isinstance(values, dict):
        return None
    if values.get("type") == "enumfilter":
        out: list[tuple[int, Any]] = []
        for f in _as_node_list(values.get("filters")):
            if not isinstance(f, dict):
                continue
            try:
                fid = int(f.get("id"))
            except (TypeError, ValueError):
                continue
            out.append((fid, f.get("pred")))
        return tuple(out) or None
    return _enum_filters(values.get("values"))


def _enum_slots(values: Any) -> tuple[str, str] | None:
    """The (labels, values) slot pair of a ``slotenum``."""
    if not isinstance(values, dict) or values.get("type") != "slotenum":
        return None
    names, vals = values.get("names"), values.get("values")
    if isinstance(names, str) and isinstance(vals, str) and names and vals:
        return (names, vals)
    return None


def _enum_offset_slot(values: Any) -> str | None:
    """The slot an ``offsetenum`` shifts its entries by."""
    if not isinstance(values, dict):
        return None
    if values.get("type") == "offsetenum":
        slot = values.get("slot")
        return str(slot) if isinstance(slot, str) and slot else None
    return _enum_offset_slot(values.get("values"))


def _enum_source_pred(values: Any) -> Any:
    """The ``pred`` narrowing a ``values:{type:'dynamic'}`` source, if any.

    ``{type:'dynamic',path:[20,0],pred:{type:'slot',slot:'type',
    pred:{type:'number',value:[12]}}}`` -- the source table is every
    interface, and the pred says which of its rows this field may name: the
    ones whose ``type`` slot is 12, i.e. the bridges. Without it a bridge
    port's Bridge box offered the whole interface list.

    It rides on the same node :func:`_enum_map` takes the path from, so the
    descent through the wrappers matches: whichever dynamic source won the
    path is the one whose pred applies.
    """
    if not isinstance(values, dict):
        return None
    vtype = values.get("type")
    if vtype == "dynamic":
        p = values.get("path")
        if isinstance(p, list) and all(isinstance(i, int) for i in p):
            return values.get("pred")
        return None
    if vtype in ("defenum", "enumfilter", "offsetenum", "remapenum"):
        return _enum_source_pred(values.get("values"))
    if vtype == "pair":
        # The static half has no rows to filter; the pred belongs to the
        # dynamic half, and to the same one _enum_map kept the path of.
        for child in _as_node_list(values.get("c")):
            _m, path = _enum_map(child)
            if path:
                return _enum_source_pred(child)
        return None
    return None


def _enum_query(values: Any) -> dict[str, Any] | None:
    """A ``queryenum`` dropdown: the request that fills it in.

    ``{type:'queryenum',path:[88,3],cmd:6,valuesid:'U4',bvaluesid:'U5',
    params:[{name:'Band',id:'u81a'},…]}`` -- wireless Frequency asks the
    router which frequencies this band/width/country allows, passing the
    form's current values as the query's parameters.
    """
    if not isinstance(values, dict):
        return None
    if values.get("type") != "queryenum":
        return _enum_query(values.get("values"))
    path = values.get("path")
    if not (isinstance(path, list) and all(isinstance(i, int) for i in path)):
        return None
    vals = _split_id(values.get("valuesid"))
    labels = _split_id(values.get("bvaluesid"))
    params: list[tuple[str, int]] = []
    for p in _as_node_list(values.get("params")):
        if not isinstance(p, dict):
            continue
        split = _split_id(p.get("id"))
        if split and isinstance(p.get("name"), str):
            params.append((str(p["name"]), split[1]))
    out: dict[str, Any] = {
        "path": tuple(path),
        "cmd": _as_int(values.get("cmd")),
        "params": params,
    }
    if vals:
        out["values_id"] = vals[1]
    if labels:
        out["labels_id"] = labels[1]
    return out


def _defenum_default(values: Any) -> int | None:
    """The synthetic default id of a `defenum` values block, if any.

    ``{type:'defenum', defid:0, defname:'all', values:{…}}`` prepends a stand-in
    entry (id 0 = "all") to a dynamic list and makes it the default selection.
    The map is captured by :func:`_enum_map`; this recovers the default id so a
    new record shows "all" / "auto" instead of a blank enum."""
    if isinstance(values, dict) and values.get("type") == "defenum":
        try:
            return int(values.get("defid", 0))
        except (TypeError, ValueError):
            return None
    return None


def _field_variant(
    fields: dict[int, Field], code: int, prefix: str | None
) -> "Field | None":
    """The declaration of ``code`` with this wire type -- see Menu.field_for."""
    fld = fields.get(code)
    if fld is None or not prefix or fld.prefix == prefix:
        return fld
    return fld.variants.get(prefix, fld)


def _new_declaration(out: dict[int, Field], code: int, prefix: str) -> bool:
    """Is this the menu's first declaration of ``code`` at this wire type?

    The same wire type twice is one field seen twice (a `request` echo, two
    deck panes carrying the same row) and gets a single form row. A different
    wire type is a different field that happens to share the id -- Ethernet's
    `u469` "PoE Out Status" and `Q469` "Tx Bytes" -- and each earns its own
    row on its own tab, or the second one is simply never drawn.
    """
    prev = out.get(code)
    if prev is None:
        return True
    if code == _NO_ID_CODE:
        # 'xffffffff' is the .jg's "this row has no wire id" -- several rows in
        # one menu can carry it and they are not each other's variants.
        return False
    return prefix != prev.prefix and prefix not in prev.variants


#: `.jg` ``width:0xffffffff`` -- an explicit "this is not a column". Every
#: field whose column a ``columnalias`` supplies carries it (the firewall
#: Action enm on the Action tab, Wireless Registration's Strength), as do the
#: form-only rows that would otherwise look column-worthy: Files ▸ Directory
#: and Contents, Users ▸ SSH Keys ▸ User and Key.
NO_COLUMN_WIDTH = 0xFFFFFFFF


def _column_width(node: dict[str, Any]) -> int | None:
    """The .jg ``width`` of a node, or None when it declares no column."""
    width = node.get("width")
    if not isinstance(width, int) or isinstance(width, bool):
        return None
    if width == NO_COLUMN_WIDTH or width <= 0:
        return None
    return int(width)


def _set_columns(node: dict[str, Any]) -> int | None:
    """The .jg ``columns`` of a ``set``: members per row before it wraps.

    Winbox lays the wireless rates out four to a row, so an eight-member
    Supported Rates A/G is two rows of four -- and the second row's checkboxes
    line up under the first's. Only ``set`` carries the key.
    """
    columns = node.get("columns")
    if not isinstance(columns, int) or isinstance(columns, bool) or columns <= 0:
        return None
    return int(columns)


def _make_field(node: dict[str, Any], code: int, prefix: str) -> Field:
    enum, enum_path = _enum_map(node.get("values"))
    mask = _split_id(node.get("maskid"))
    port = _split_id(node.get("portid"))
    # `id2` on a multinumberrange is the same role one level up: the field's own
    # id holds the array of range *lows* and `id2` the array of highs (Traffic
    # Generator's port lists). Confirmed on firmware 7.23.3 -- see
    # format_split_range_list.
    # `idhigh` is the same thing as `highid` -- three integerrange fields spell
    # it the other way round, and reading only one left them with no high end.
    high = (_split_id(node.get("highid")) or _split_id(node.get("idhigh"))
            or _split_id(node.get("id2")))
    hint = node.get("hint")
    if hint is not None:
        hint = str(hint)
    default = node.get("def")
    if default is None:
        default = _defenum_default(node.get("values"))
    src = node.get("values")
    offset = _as_int(node.get("offset"))
    jg_type = str(node.get("type") or "")
    scale = node.get("scale")
    postfix = node.get("postfix")
    inline = bool(node.get("inlinepostfix"))
    # An enm wrapping a lone fixedpoint is a number with a few named values:
    # Ping's Time is {4294967295:'timeout'} around {fixedpoint, scale:1000}.
    # Read as a bare enm, a 12.044ms reply rendered "12044ms".
    kids = node.get("c")
    if (
        jg_type == "enm" and isinstance(kids, list) and len(kids) == 1
        and isinstance(kids[0], dict) and kids[0].get("type") == "fixedpoint"
    ):
        jg_type = "fixedpoint"
        scale = kids[0].get("scale", scale)
        # ...and its unit can sit there too: Traceroute's Last is "ms" inside.
        # A unit declared inside reads in the cell ("6.5ms"); one on the
        # field itself heads the column (Ping's "Time (ms)").
        if postfix is None and kids[0].get("postfix") is not None:
            postfix = kids[0].get("postfix")
            inline = True
    return Field(
        invert=bool(node.get("inv")),
        default_kind=(
            "today" if node.get("todaydef") else
            "now" if node.get("nowdef") else
            "random" if node.get("randomdef") else
            "user" if node.get("defuser") else None
        ),
        default_mask=(
            node["deflen"] if node.get("deflen") is not None
            else node.get("defmask")
        ),
        default_low=node.get("deflow"),
        default_high=node.get("defhigh"),
        sort_by_value=bool(node.get("sortbyvalue")),
        sort_alpha=bool(node.get("sorted")),
        cols=_as_int(node.get("cols")),
        rows=_as_int(node.get("rows")),
        small=bool(node.get("small")),
        last_column=bool(node.get("lastcol")),
        trim_zeros=bool(node.get("trimzeros")),
        inline_postfix=inline,
        precise=bool(node.get("precise")),
        no_filter=bool(node.get("nofilter")),
        unset_value=node.get("optval"),
        private=bool(node.get("private")),
        maskid=mask[1] if mask else None,
        portid=port[1] if port else None,
        offset=offset,
        radio=bool(node.get("radio")),
        compact=bool(node.get("compact")),
        set_columns=_set_columns(node),
        bar_colors=_bar_colors(node),
        enum_filters=_enum_filters(src),
        enum_pred=_enum_source_pred(src),
        enum_slots=_enum_slots(src),
        enum_offset_slot=_enum_offset_slot(src),
        enum_query=_enum_query(src),
        code=code,
        prefix=prefix,
        name=str(node.get("name") or node.get("title") or ""),
        jg_type=jg_type,
        ro=bool(node.get("ro")),
        optional=bool(node.get("opt")),
        nonpublic=bool(node.get("nonpublic")),
        scale=scale,
        postfix=postfix,
        enum=enum,
        enum_path=enum_path,
        minimum=node.get("min"),
        maximum=node.get("max"),
        default=default,
        hint=hint,
        relative=bool(node.get("relative")),
        is_range=bool(node.get("range")),
        required=bool(node.get("req")),
        highid=high[1] if high else None,
        radix=int(node.get("radix") or 10),
        zeroinvalid=bool(node.get("zeroinvalid")),
        hide_on_empty=bool(node.get("hideonempty")),
        column_width=_column_width(node),
    )


# Standard "disabled" flag used by many menus even when not re-declared.
FLAG_DISABLED_CODE = 0xFE000A
FLAG_DISABLED_HINT = "X"
# Dynamic (learned, not configured: a DHCP-client address, a route from a
# routing protocol) and invalid. Like Disabled these ride on every menu that
# can have them and are not declared per-menu in the .jg, so a schema-only
# reading of the flags column misses them entirely -- which is why a DHCP
# address showed up looking exactly like a static one.
FLAG_DYNAMIC_CODE = 0xFE0007
FLAG_DYNAMIC_HINT = "D"
FLAG_INVALID_CODE = 0xFE0008
FLAG_INVALID_HINT = "I"
#: Winbox legend order: "X - disabled, I - invalid, D - dynamic".
UNIVERSAL_FLAGS: tuple[tuple[int, str], ...] = (
    (FLAG_DISABLED_CODE, FLAG_DISABLED_HINT),
    (FLAG_INVALID_CODE, FLAG_INVALID_HINT),
    (FLAG_DYNAMIC_CODE, FLAG_DYNAMIC_HINT),
)
_UNIVERSAL_CODES = frozenset(code for code, _h in UNIVERSAL_FLAGS)
#: The same three by the name a `cond`'s ``on:`` slot calls them.
_UNIVERSAL_SLOTS: dict[str, int] = {
    "dynamic": FLAG_DYNAMIC_CODE,
    "disabled": FLAG_DISABLED_CODE,
    "invalid": FLAG_INVALID_CODE,
}
# Object `.id` (list GETALL / GETOBJ); never a property-form row.
STD_OBJ_ID_CODE = 0xFE0001
#: The .jg's placeholder id for a row with no wire field of its own.
_NO_ID_CODE = 0xFFFFFFFF


def _as_node_list(node: Any) -> list[Any]:
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


def _statusbar_decls(items: list[dict[str, Any]] | None) -> set[tuple[int, str]]:
    """(code, wire prefix) of every tray cell, for telling a tray field from a
    form field that reuses the same id -- an ethernet's tray Status is `u44c`
    while `Q44c` is "Rx Broadcast" on Rx Stats."""
    out: set[tuple[int, str]] = set()
    for item in items or []:
        code = item.get("code")
        prefix = item.get("prefix")
        if isinstance(code, int) and isinstance(prefix, str):
            out.add((code, prefix))
        codes = item.get("codes") or []
        prefixes = list(item.get("prefixes") or [])
        prefixes += [None] * (len(codes) - len(prefixes))
        for c, p in zip(codes, prefixes):
            if isinstance(c, int) and isinstance(p, str):
                out.add((c, p))
    return out


def _statusbar_codes(items: list[dict[str, Any]] | None) -> set[int]:
    out: set[int] = set()
    for item in items or []:
        code = item.get("code")
        if isinstance(code, int):
            out.add(code)
        for c in item.get("codes") or []:
            if isinstance(c, int):
                out.add(c)
    return out


def _layout_item_codes(item: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    if item.get("kind") == "field" and isinstance(item.get("code"), int):
        codes.add(item["code"])
    for c in item.get("codes") or []:
        if isinstance(c, int):
            codes.add(c)
    return codes


def _merged_tab_rank(base: "Menu", overlay: "Menu") -> dict[str, int]:
    """Winbox tab order for a generic menu plus its type overlay.

    Winbox keeps the generic menu's own tabs in place but shows the
    type-specific ones straight after the first (General) tab, so an Ethernet
    interface reads General, Ethernet, Loop Protect, …, Status, Traffic —
    not General, Status, Traffic, then the Ethernet-only tabs on the end.
    """
    base_tabs = base.tab_names()
    overlay_tabs = overlay.tab_names()
    if not base_tabs:
        ordered = list(overlay_tabs)
    else:
        overlay_only = [t for t in overlay_tabs if t not in base_tabs]
        ordered = [base_tabs[0], *overlay_only, *base_tabs[1:]]
    return {tab: i for i, tab in enumerate(ordered)}


def _sort_rows_by_tab(rows, tab_rank: dict[str, int], tab_of):
    """Stable-sort form rows into ``tab_rank`` order.

    Stability matters: within a tab the base rows must stay ahead of the
    overlay's, which is what keeps General as Name, Default Name, MTU, …
    before the overlay's Max L2 MTU / MAC Address / ARP.
    """
    if not tab_rank:
        return rows
    fallback = len(tab_rank)
    return sorted(rows, key=lambda r: tab_rank.get(tab_of(r), fallback))


def statusbar_owns(
    code: int, prefix: str | None, sb_codes: set[int],
    sb_decls: set[tuple[int, str]] | None = None,
) -> bool:
    """Is this declaration the window tray's, rather than a form row?

    By code alone when the tray's wire types are unknown; by (code, wire type)
    when they are, so a counter that merely shares an id with a tray cell
    keeps its row.
    """
    if code not in sb_codes:
        return False
    if not sb_decls or prefix is None:
        return True
    if (code, prefix) in sb_decls:
        return True
    return not any(c == code for c, _p in sb_decls)


def _strip_statusbar_from_form(
    form_layout: list[dict[str, Any]],
    form_order: list[tuple[str, int]],
    sb_codes: set[int],
    form_decls: list[tuple[str, int, str]] | None = None,
    sb_decls: set[tuple[int, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, int]], list[tuple[str, int, str]]]:
    """Remove statusbar wire codes from the property form layout/order."""
    decls = list(form_decls or [])
    if not sb_codes:
        return form_layout, form_order, decls

    def owned(code: Any, prefix: Any) -> bool:
        return isinstance(code, int) and statusbar_owns(
            code, prefix if isinstance(prefix, str) else None, sb_codes, sb_decls,
        )

    def item_owned(item: dict[str, Any]) -> bool:
        if item.get("kind") == "field":
            return owned(item.get("code"), item.get("prefix"))
        codes = list(_layout_item_codes(item))
        if not codes:
            return False
        prefixes = list(item.get("prefixes") or [])
        prefixes += [None] * (len(codes) - len(prefixes))
        return any(owned(c, p) for c, p in zip(codes, prefixes))

    layout = [item for item in form_layout if not item_owned(item)]
    decls = [d for d in decls if not owned(d[1], d[2])]
    kept = {c for _t, c, _p in decls}
    if form_decls is None:
        order = [(t, c) for t, c in form_order if c not in sb_codes]
    else:
        order = [
            (t, c) for t, c in form_order if c not in sb_codes or c in kept
        ]
    return layout, order, decls


def _ingest_statusbar(
    node: dict[str, Any],
    out: dict[int, Field],
    layout_ctx: dict[str, Any] | None,
) -> None:
    """Parse a ``type:statusbar`` node into footer items + RO fields.

    Handles nested opt/cond wrappers, multi-field bars, and tuples (Queue
    "X queued", System Resources "used of", …) — not only a single Status enum.
    """
    withouttitle = bool(node.get("withouttitle"))
    if layout_ctx is not None:
        meta: dict[str, Any] = dict(layout_ctx.get("statusbar_meta") or {})
        p = node.get("path")
        if isinstance(p, list) and all(isinstance(i, int) for i in p):
            meta["path"] = tuple(p)
        if node.get("autorefresh") is not None:
            try:
                meta["autorefresh"] = int(node["autorefresh"])
            except (TypeError, ValueError):
                pass
        if node.get("cmd") is not None:
            try:
                meta["cmd"] = int(node["cmd"])
            except (TypeError, ValueError):
                pass
        if meta:
            layout_ctx["statusbar_meta"] = meta

    # A bar with a path of its own is read with a request of its own, which
    # x3 does not make: Connections' Max Entries comes from Connection
    # Tracking (20,17), Queue Tree's "queued" totals from its cmd 3. Its cells
    # are not this menu's fields. Taken as such, 7.19's Connections labelled
    # its reply port (u14) "Max Entries", Simple Queues' tray read each
    # queue's Name and Parent back as totals, and the change watch, which
    # skips tray codes, stopped seeing those two as configuration.
    if isinstance(node.get("path"), list):
        return

    def walk(ch: Any, *, force_no_title: bool = False) -> None:
        if not isinstance(ch, dict):
            return
        ctype = ch.get("type")
        # Containers that only wrap statusbar content.
        if ctype in ("opt", "cond", "group", "union", "set"):
            for sub in _as_node_list(ch.get("c")):
                walk(sub, force_no_title=force_no_title)
            return
        if ctype == "separator":
            return
        if ctype == "tuple":
            codes: list[int] = []
            prefixes: list[str] = []
            for sub in _as_node_list(ch.get("c")):
                if not isinstance(sub, dict):
                    continue
                split = _split_id(sub.get("id"))
                if not split:
                    continue
                prefix, code = split
                fld = _make_field(sub, code, prefix)
                fld.ro = True
                _merge_field(out, fld)
                codes.append(code)
                prefixes.append(prefix)
            if codes and layout_ctx is not None:
                title = ch.get("name") or ch.get("title")
                layout_ctx.setdefault("statusbar", []).append({
                    "kind": "tuple",
                    "codes": codes,
                    "prefixes": prefixes,
                    "sep": str(ch.get("sep") if ch.get("sep") is not None else " / "),
                    "postfix": ch.get("postfix"),
                    "name": str(title) if title else None,
                    "withouttitle": withouttitle or force_no_title or not title,
                })
            return

        split = _split_id(ch.get("id"))
        if not split:
            # Unrecognized chrome — still try children (e.g. nested tables).
            for sub in _as_node_list(ch.get("c")):
                walk(sub, force_no_title=force_no_title)
            return
        prefix, code = split
        fld = _make_field(ch, code, prefix)
        fld.ro = True
        _merge_field(out, fld)
        if layout_ctx is not None:
            name = fld.name or None
            # Unnamed statusbar cells show value only (no "Status:" prefix).
            no_title = withouttitle or force_no_title or not name
            layout_ctx.setdefault("statusbar", []).append({
                "kind": "field",
                "code": code,
                "prefix": prefix,
                "name": name,
                "withouttitle": no_title,
                "postfix": fld.postfix,
            })

    for ch in _as_node_list(node.get("c")):
        walk(ch)


def _split_id(fid: Any) -> tuple[str, int] | None:
    if not isinstance(fid, str) or len(fid) < 2:
        return None
    prefix, rest = fid[0], fid[1:]
    if prefix not in CANONICAL_TAG:
        return None
    try:
        return prefix, int(rest, 16)
    except ValueError:
        return None


def _adopt_element_enum(node: dict[str, Any], fld: Field) -> None:
    """Copy enum metadata from a multi field's element child when present.

    Example: ``{type:'multinumber', id:'U12', c:[{type:'enm', values:{...}}]}``
    — the parent field needs the element enum for display.
    """
    children = node.get("c")
    if children is None:
        return
    if isinstance(children, dict):
        children = [children]
    if not isinstance(children, list):
        return
    want_enum = fld.enum is None and fld.enum_path is None
    for child in children:
        if not isinstance(child, dict):
            continue
        # The element type ({type:'bigbytes'}) travels with the enum: both
        # describe one item of the array rather than the array itself.
        if (
            fld.element_type is None
            and not child.get("id")
            and isinstance(child.get("type"), str)
            # `gridmultinumber` is the same shape without the prefix: an array
            # whose element type is an idless child. Its own `values` map names
            # the array's *positions*, not its values, so the element type is
            # the only thing that says how to read a cell -- without it a Tx
            # Power of 17 dBm rendered as the rate named 17.
            and (fld.jg_type.startswith("multi")
                 or fld.jg_type in ("gridmultinumber", "numbertable"))
        ):
            fld.element_type = str(child["type"])
            # The element's own bounds, too: the parent's `max` is how many
            # entries the array may hold, not how large one may be.
            if fld.element_min is None:
                fld.element_min = child.get("min")
            if fld.element_max is None:
                fld.element_max = child.get("max")
        # An id-less child may also carry the add-time default for the field
        # it belongs to: Scheduler's Start Time is `{type:'enm',id:'u12e',
        # c:[{type:'clocktime',nowdef:1}]}` -- the "now" is on the child.
        if fld.default_kind is None:
            fld.default_kind = (
                "today" if child.get("todaydef") else
                "now" if child.get("nowdef") else
                "random" if child.get("randomdef") else
                "user" if child.get("defuser") else None
            )
        if not want_enum:
            continue
        enum, enum_path = _enum_map(child.get("values"))
        if enum is not None or enum_path is not None:
            fld.enum = enum
            fld.enum_path = enum_path
            fld.enum_pred = _enum_source_pred(child.get("values"))
            want_enum = False


def _merge_field(out: dict[int, Field], fld: Field) -> None:
    prev = out.get(fld.code)
    if prev is None:
        out[fld.code] = fld
        return
    # Same id declared twice in one menu. These can be genuinely different
    # fields told apart only by wire type -- DHCP Client declares 0x12 as both
    # `u12` 'Add Default Route' (scalar enum) and `U12` 'DHCP Options' (u32
    # array), and the device sends both in a single reply. Keep the named
    # declaration as primary and retain the other whole, so for_tag() can pick.
    keep, other = (prev, fld) if (prev.name or not fld.name) else (fld, prev)
    keep.alt_prefixes |= {other.prefix} | other.alt_prefixes
    keep.alt_prefixes.discard(keep.prefix)
    for pfx, var in other.variants.items():
        keep.variants.setdefault(pfx, var)
    other.variants = {}
    if other.prefix != keep.prefix:
        keep.variants.setdefault(other.prefix, other)
    if not keep.name:
        keep.name = other.name
    if keep.enum is None:
        keep.enum = other.enum
    if keep.enum_path is None:
        keep.enum_path = other.enum_path
        # The pred belongs to the path: a source table inherited from the other
        # declaration keeps the rows-of-it that declaration named.
        keep.enum_pred = other.enum_pred
    # A menu often declares one id twice for two different jobs: ARP's 0x5 is
    # `Published` (a form checkbox) and `published` (the P flag, carrying
    # `hint`, `inv` and `nofilter`). Whichever declaration ends up primary, the
    # presentation the other one carried is still true of that field.
    if not keep.hint and other.hint:
        keep.hint = other.hint
    keep.invert = keep.invert or other.invert
    keep.no_filter = keep.no_filter or other.no_filter
    if keep.unset_value is None:
        keep.unset_value = other.unset_value
    out[fld.code] = keep



def _collect(
    nodes: Any,
    out: dict[int, Field],
    form_order: list[tuple[str, int]] | None = None,
    tab: str = "General",
    inherited_name: str | None = None,
    inherited_ro: bool = False,
    form_layout: list[dict[str, Any]] | None = None,
    layout_ctx: dict[str, Any] | None = None,
    *,
    form_decls: list[tuple[str, int, str]] | None = None,
    _in_tuple: bool = False,
    inherited_optional: bool = False,
    inherited_width: int | None = None,
) -> str:
    """Gather sibling fields (and optional form tab layout).

    Returns the active tab name after scanning (tabs are *siblings* in .jg
    lists, not parents). ``layout_ctx`` accumulates tab_on, tab_conditions,
    and named type:cond nodes for Winbox visibility (PoE/SFP/...).

    ``inherited_width`` carries a column width down from a wrapper that has no
    id of its own: 185 columns are declared on an ``opt`` (firewall Src.
    Address), 43 on a ``union`` (a DHCP lease Address), and the width belongs
    to the value inside it.
    """
    if isinstance(nodes, list):
        for n in nodes:
            tab = _collect(
                n, out, form_order, tab, inherited_name, inherited_ro, form_layout,
                layout_ctx, form_decls=form_decls, _in_tuple=_in_tuple,
                inherited_optional=inherited_optional,
                inherited_width=inherited_width,
            )
        return tab
    if not isinstance(nodes, dict):
        return tab
    node = nodes

    def _add_column(entry: dict[str, Any]) -> None:
        """Record one list column, in .jg order. Ignored off the main pass."""
        if layout_ctx is None:
            return
        layout_ctx.setdefault("columns", []).append(entry)

    if node.get("type") == "cond" and isinstance(node.get("name"), str) and layout_ctx is not None:
        layout_ctx.setdefault("conditions", {})[str(node["name"])] = node
        return tab

    # A named gridcell heads a *column* of the form, not a tab: Winbox draws
    # Traceroute's request as Basic and Advanced side by side, Torch's as
    # Basic and Filters. The fields after it belong to it, as after a tab.
    if node.get("type") == "gridcell" and isinstance(node.get("name"), str):
        if layout_ctx is not None:
            layout_ctx.setdefault("gridcells", []).append(str(node["name"]))
        return str(node["name"])

    if node.get("type") == "tab" and isinstance(node.get("name") or node.get("title"), str):
        name = str(node.get("name") or node.get("title"))
        on = node.get("on")
        if layout_ctx is not None:
            priority = _as_int(node.get("priority"))
            if priority is not None:
                layout_ctx.setdefault("tab_priority", {})[name] = priority
        # `inline:1` is not a tab at all: it heads a *section* of the page it is
        # on. Every one of the 120 in 7.21 is a Quick Set mode, which Winbox
        # draws as one long form with named rules rather than as 18 tabs.
        if node.get("inline") and form_layout is not None:
            section_on = None
            if layout_ctx is not None:
                section_on = (
                    str(on) if isinstance(on, str) and on
                    else layout_ctx.get("block_on")
                )
                layout_ctx["tab_on"] = section_on
            form_layout.append({
                "kind": "label", "tab": tab, "name": name, "on": section_on,
            })
            return tab
        if layout_ctx is not None:
            # A tab inside a `type:object` block (Quick Set's CPE declares one
            # per connection state) is still inside it, so the block's own
            # condition survives a tab that declares none.
            layout_ctx["tab_on"] = (
                str(on) if isinstance(on, str) and on
                else layout_ctx.get("block_on")
            )
            if isinstance(on, str) and on:
                layout_ctx.setdefault("tab_conditions", {})[name] = on
        return name

    def _item_on() -> str | None:
        fon = node.get("on")
        if isinstance(fon, str) and fon:
            return fon
        if layout_ctx is not None:
            return layout_ctx.get("tab_on")
        return None

    if node.get("type") == "separator" and form_layout is not None and not _in_tuple:
        form_layout.append({"kind": "separator", "tab": tab, "on": _item_on()})
        return tab

    # type:lookup -- a column whose value is in *another* handler: find the row
    # of `path` whose `dst` field equals this row's `src`, show its `val`. Not
    # a field of this menu at all, so it only ever becomes a column.
    if node.get("type") == "lookup" and layout_ctx is not None:
        path = node.get("path")
        name = node.get("name") or node.get("title")
        if (
            isinstance(name, str) and name
            and isinstance(path, list)
            and all(isinstance(i, int) for i in path)
            and isinstance(node.get("src"), str)
            and isinstance(node.get("dst"), str)
            and isinstance(node.get("val"), str)
        ):
            _add_column({
                "kind": "lookup",
                "name": str(name),
                "path": tuple(path),
                "src": str(node["src"]),
                "dst": str(node["dst"]),
                "val": str(node["val"]),
                "width": _column_width(node),
            })
        return tab

    # type:filter -- the dropdown above a list that narrows it: Firewall by
    # Chain, Address Lists by List. `filteron` names the field to match,
    # `values` where the choices come from, and `conds` adds entries that are
    # conditions rather than values (a firewall list offers static / dynamic).
    if node.get("type") == "filter" and layout_ctx is not None:
        filteron = node.get("filteron")
        if isinstance(filteron, str) and filteron:
            _enum, path = _enum_map(node.get("values"))
            conds = []
            for cond in _as_node_list(node.get("conds")):
                if not isinstance(cond, dict):
                    continue
                cname, con = cond.get("name"), cond.get("on")
                if isinstance(cname, str) and isinstance(con, str):
                    conds.append({"name": cname, "on": con})
            layout_ctx.setdefault("list_filters", []).append({
                "filteron": filteron,
                "path": path,
                "conds": conds,
            })
        return tab

    # type:hyperlink -- a value that names an object of *another* menu: a
    # route's Immediate Gateway is a row of (44,16), its Session a row of
    # (44,33). The id is the object's, so the target menu is what can name it
    # -- exactly what a `values:{type:'dynamic'}` enum does -- and the row
    # doubles as a way into that menu.
    if node.get("type") == "hyperlink" and form_layout is not None:
        split = _split_id(node.get("id"))
        target = node.get("path")
        if split and isinstance(target, list) and all(
            isinstance(i, int) for i in target
        ):
            prefix, code = split
            fld = _make_field(node, code, prefix)
            fld.enum_path = tuple(target)
            fld.link_path = tuple(target)
            if inherited_ro and not fld.ro:
                fld.ro = True
            first_time = code not in out
            new_decl = _new_declaration(out, code, prefix)
            _merge_field(out, fld)
            if form_order is not None and first_time:
                form_order.append((tab, code))
            if form_decls is not None and new_decl:
                form_decls.append((tab, code, prefix))
            if not _in_tuple and new_decl:
                form_layout.append({
                    "kind": "field", "tab": tab, "code": code,
                    "prefix": prefix, "on": _item_on(),
                })
            width = _column_width(node)
            if width is not None:
                _add_column({
                    "kind": "field", "code": code, "prefix": prefix,
                    "name": fld.name, "width": width,
                })
        return tab

    # type:label -- a caption with no value: Quick Set's Home AP Dual puts
    # "2GHz" and "5GHz" above the two halves of its form. Winbox draws it the
    # way it draws a named group, as a rule with the name in it.
    if node.get("type") == "label" and _in_tuple and layout_ctx is not None:
        # A caption inside a tuple heads one of the pair's columns: Quick Set's
        # dual-band form has `{type:'tuple',c:[{type:'label',name:'2GHz'},
        # {type:'label',name:'5GHz'}]}` over the rows that pair the two bands.
        caption = node.get("name") or node.get("title")
        if isinstance(caption, str) and caption:
            layout_ctx.setdefault("tuple_labels", []).append(str(caption))
        return tab

    if node.get("type") == "label" and form_layout is not None and not _in_tuple:
        label_name = node.get("name") or node.get("title")
        if isinstance(label_name, str) and label_name:
            form_layout.append({
                "kind": "label", "tab": tab, "name": str(label_name),
                "on": _item_on(),
            })
        return tab

    # type:alias -- the field of this name, placed here as well. The Mangle
    # action deck declares Passthrough once (in the pane that owns the id) and
    # aliases it into every other pane, so each action shows it.
    if node.get("type") == "alias" and form_layout is not None and not _in_tuple:
        alias_name = node.get("name") or node.get("title")
        if isinstance(alias_name, str) and alias_name:
            form_layout.append({
                "kind": "alias", "tab": tab, "name": str(alias_name),
                "on": _item_on(),
            })
        return tab

    # type:objtype -- the object's family spelled out ("Ethernet", "Bridge").
    # It has no id: the value is the title of the type overlay the object's
    # `typeon` slot selects, which is what Winbox shows as Interface ▸ Type.
    if node.get("type") == "objtype" and form_layout is not None and not _in_tuple:
        name = str(node.get("name") or node.get("title") or "Type")
        form_layout.append({
            "kind": "objtype",
            "tab": tab,
            "name": name,
            "on": _item_on(),
        })
        width = _column_width(node)
        if width is not None:
            _add_column({"kind": "objtype", "name": name, "width": width})
        return tab

    # The change-rate family: a column with no wire id of its own, whose value
    # is how fast *another* field is moving. `value:` names that field, and the
    # rate is its change per second -- Firewall ▸ Filter Rules' Packet Rate off
    # Packets, Hotspot ▸ Active's Rx/Tx Rate off Bytes In/Out. Three spellings,
    # differing only in how the answer reads:
    #   changerate     u32 count/s, postfix from the node ("p/s")
    #   bigchangerate  u64, same
    #   bigbitchangerate  u64 *bytes*, shown as a bit rate ("1.50 Mbps")
    # Two samples are needed, so the list pane keeps the previous reading per
    # row and fills the cell (see ListPane._rate_cells); everything here does
    # is declare the column and say which field it watches.
    if node.get("type") in RATE_TYPES:
        name = str(node.get("name") or node.get("title") or "")
        of = node.get("value")
        if name and isinstance(of, str) and of:
            _add_column({
                "kind": "rate",
                "name": name,
                "of": of,
                "bits": node.get("type") == "bigbitchangerate",
                "postfix": str(node.get("postfix") or ""),
                "width": _column_width(node),
            })
        return tab

    # type:columnalias -- a column the list draws that the form does not: the
    # firewall Action icon column, DNS Static's Value. It has no id; the value
    # comes from the menu's field of the same name, whose own declaration
    # carries width:0xffffffff so it is not also a column where it sits.
    if node.get("type") == "columnalias":
        width = _column_width(node)
        name = str(node.get("name") or node.get("title") or "")
        if width is not None and name:
            _add_column({"kind": "alias", "name": name, "width": width})
        return tab

    # Window tray (type:statusbar) — never a form row. Applies to every menu.
    if node.get("type") == "statusbar":
        _ingest_statusbar(node, out, layout_ctx)
        return tab

    # In-form status panel (type:status): counters on the current tab (RADIUS
    # Requests/Accepts, DHCP Relay Requests/Responses, …) — not the tray.
    if node.get("type") == "status":
        for ch in _as_node_list(node.get("c")):
            if isinstance(ch, dict):
                ch = dict(ch)
                ch["ro"] = 1
                tab = _collect(
                    ch, out, form_order, tab, None, True, form_layout,
                    layout_ctx, form_decls=form_decls, _in_tuple=_in_tuple,
                )
        return tab

    if node.get("type") == "numflag":
        split = _split_id(node.get("id"))
        letters = {}
        for key, pair in (node.get("c") or {}).items():
            if isinstance(pair, list) and len(pair) == 2:
                try:
                    letters[int(key)] = str(pair[1])
                except (TypeError, ValueError):
                    continue
        if layout_ctx is not None and split is not None and letters:
            layout_ctx.setdefault("numflags", []).append({
                "code": split[1], "letters": letters,
                "band": bool(node.get("band")),
            })
        return tab

    # type:enable → Disabled flag chrome (list X letter + form footer Enabled).
    if node.get("type") == "enable":
        if layout_ctx is not None:
            layout_ctx["has_enable"] = True
        return tab

    # type:comment → Comment side-button on the property form.
    if node.get("type") == "comment":
        if layout_ctx is not None:
            layout_ctx["has_comment"] = True
        return tab

    # type:object -- a block of fields that belongs to one state of the object:
    # Quick Set's CPE declares `{type:'object',on:'cpediscon',c:[…]}` and again
    # for `cpeconn`, and only one of the two applies. The condition is on the
    # block, so it has to reach the fields inside it; a field with an `on:` of
    # its own keeps that one.
    if node.get("type") == "object" and layout_ctx is not None:
        saved = layout_ctx.get("tab_on")
        saved_block = layout_ctx.get("block_on")
        on = node.get("on")
        if isinstance(on, str) and on:
            layout_ctx["tab_on"] = on
            layout_ctx["block_on"] = on
        for sub in _as_node_list(node.get("c")):
            tab = _collect(
                sub, out, form_order, tab, inherited_name, inherited_ro,
                form_layout, layout_ctx, form_decls=form_decls,
                _in_tuple=_in_tuple, inherited_optional=inherited_optional,
                inherited_width=inherited_width,
            )
        layout_ctx["tab_on"] = saved
        layout_ctx["block_on"] = saved_block
        return tab

    # type:toolbar → a row of buttons on the window, holding `doit` commands
    # and `contextmenu` entries. Every one of the 121 in 7.21 is on a Quick Set
    # mode: Apply Configuration, Port Mapping, and a System group with Reboot /
    # Reset Configuration / Password. Its children are the same node types as
    # anywhere else, so they are collected as usual -- with a note of where
    # they were declared, since a toolbar button is not a right-click entry.
    if node.get("type") == "toolbar":
        saved = (layout_ctx or {}).get("in_toolbar")
        if layout_ctx is not None:
            layout_ctx["in_toolbar"] = True
        for sub in _as_node_list(node.get("c")):
            tab = _collect(
                sub, out, form_order, tab, inherited_name, inherited_ro,
                form_layout, layout_ctx, form_decls=form_decls,
                _in_tuple=_in_tuple, inherited_optional=inherited_optional,
                inherited_width=inherited_width,
            )
        if layout_ctx is not None:
            layout_ctx["in_toolbar"] = saved
        return tab

    # type:contextmenu → a right-click entry on this menu's rows. `open:` is
    # the *title* of the menu it opens, `link:` maps this row's fields onto
    # that menu's ({dst:'Ping To',src:'Address'}), or writes a literal
    # ({conv:'put',dst:'Type',val:1}), and `autostart:1` runs it on open.
    if node.get("type") == "contextmenu" and layout_ctx is not None:
        # Three shapes: `open:` names another menu to open; `doset:1` is
        # "apply this form" (Quick Set's Apply Configuration); and a bare
        # `{type:'contextmenu'}` with no name at all is a separator between
        # the entries around it.
        links: list[dict[str, Any]] = []
        for lnk in _as_node_list(node.get("link")):
            if not isinstance(lnk, dict) or not isinstance(lnk.get("dst"), str):
                continue
            links.append({
                "src": str(lnk["src"]) if isinstance(lnk.get("src"), str) else None,
                "dst": str(lnk["dst"]),
                "val": lnk.get("val"),
                "conv": str(lnk["conv"]) if isinstance(lnk.get("conv"), str) else None,
            })
        on = node.get("on")
        raw_name = node.get("name") or node.get("title")
        layout_ctx.setdefault("context_actions", []).append({
            "kind": "contextmenu",
            "name": str(raw_name) if isinstance(raw_name, str) and raw_name else "",
            #: `doset:1`: the button applies the form rather than opening a
            #: menu (Quick Set's Apply Configuration).
            "doset": bool(node.get("doset")),
            "group": str(node["group"]) if isinstance(node.get("group"), str) else None,
            "open": str(node["open"]) if isinstance(node.get("open"), str) else None,
            "tab": str(node["tab"]) if isinstance(node.get("tab"), str) else None,
            "autostart": bool(node.get("autostart")),
            "on": str(on) if isinstance(on, str) and on else None,
            #: Declared inside a `type:toolbar`: a button on the window, not an
            #: entry in a row's right-click menu.
            "toolbar": bool(layout_ctx.get("in_toolbar")),
            "link": links,
        })
        return tab

    # type:toggle → the mode button in the form's button column, and with it a
    # pseudo-slot. `{name:'umode',type:'toggle',id:1001,modes:['Simple Mode',
    # 'Advanced Mode']}` is not a field: id 1001 is a dialog-local widget id
    # (as is the `buttonsep` 1000 above it), so _split_id rejects it and no
    # wire code is ever involved. What matters is the name, which the
    # `adv` / `ap-adv` / `__deck:umode:1` conditions read as their `on:` slot.
    if node.get("type") == "toggle" and layout_ctx is not None:
        modes = [m for m in _as_node_list(node.get("modes")) if isinstance(m, str)]
        name = node.get("name")
        if isinstance(name, str) and name and len(modes) >= 2:
            layout_ctx["mode_toggle"] = {"name": name, "modes": modes}
        return tab

    # type:buttonsep → separator in the button column, above the mode button.
    if node.get("type") == "buttonsep":
        return tab

    # type:preset → optional Preset chrome on the form.
    if node.get("type") == "preset":
        if layout_ctx is not None:
            layout_ctx["has_preset"] = True
        return tab

    # type:doit nested in a map/item → right-side action (Release, Renew, …).
    # Standalone doit menus with their own path are still created by _walk.
    if node.get("type") == "doit" and layout_ctx is not None:
        name = str(node.get("name") or node.get("title") or "Action")
        cmd = node.get("cmd")
        try:
            cmd_i = int(cmd) if cmd is not None else 0
        except (TypeError, ValueError):
            cmd_i = 0
        on = node.get("on")
        layout_ctx.setdefault("form_actions", []).append({
            "kind": "doit",
            "name": name,
            "cmd": cmd_i,
            "on": str(on) if isinstance(on, str) and on else None,
            "global": bool(node.get("global")),
        })
        return tab

    if node.get("type") == "graphbox" and form_layout is not None and not _in_tuple:
        graphs = []
        for g in node.get("graphs") or node.get("c") or []:
            if not isinstance(g, dict):
                continue
            curves: list[str] = []
            for curve in g.get("curves") or g.get("c") or []:
                if isinstance(curve, dict) and curve.get("name"):
                    curves.append(str(curve["name"]))
                elif isinstance(curve, str):
                    curves.append(curve)
            graphs.append({
                "name": str(g.get("name") or g.get("title") or "Graph"),
                "curves": curves,
            })
        form_layout.append({
            "kind": "graphbox", "tab": tab, "graphs": graphs, "on": _item_on(),
        })
        return tab

    # `multituple` is a tuple whose members are arrays (the ethernet stats
    # pairs: Tx/Rx Bytes, Tx/Rx 64, ...). Winbox lays it out exactly like a
    # tuple -- two boxes on one row -- so it takes the same branch.
    if (
        node.get("type") in ("tuple", "multituple")
        and form_layout is not None
        and not _in_tuple
    ):
        title = str(node.get("name") or node.get("title") or "")
        sep = str(node.get("sep") if node.get("sep") is not None else " / ")
        child_ro = inherited_ro or bool(node.get("ro"))
        before = len(form_decls) if form_decls is not None else 0
        before_order = len(form_order) if form_order is not None else 0
        codes_before = set(out.keys())
        # A tuple's width means one of two things, and `separate` decides
        # which. With it, each member is its own column (Interface Tx and Rx,
        # 120px each); without it, the pair is a single column joined by `sep`
        # (Bridge Port "Tx/Rx BPDU's", a connection's "Orig./Repl. Bytes").
        # The two never overlap: no non-separate tuple's members carry widths.
        separate = bool(node.get("separate"))
        width = _column_width(node)
        tab = _collect(
            node.get("c"), out, form_order, tab, None, child_ro, form_layout,
            layout_ctx, form_decls=form_decls, _in_tuple=True,
            inherited_width=width if separate else None,
        )
        captions = list((layout_ctx or {}).get("tuple_labels") or [])
        if layout_ctx is not None:
            layout_ctx["tuple_labels"] = []
        codes: list[int] = []
        prefixes: list[str | None] = []
        if form_decls is not None:
            # Declarations, not codes: a member whose id is also declared
            # elsewhere in the menu (Tx Bytes shares 0x469 with PoE Out
            # Status) is still this tuple's own member.
            for _t, code, pfx in form_decls[before:]:
                if code not in codes:
                    codes.append(code)
                    prefixes.append(pfx)
        elif form_order is not None:
            for _t, code in form_order[before_order:]:
                if code not in codes:
                    codes.append(code)
                    prefixes.append(None)
        else:
            for code in out.keys():
                if code not in codes_before and code not in codes:
                    codes.append(code)
                    prefixes.append(None)
        if not codes and captions:
            # Nothing but captions: the header over the pair of columns.
            form_layout.append({
                "kind": "label", "tab": tab, "name": sep.join(captions),
                "on": _item_on(),
            })
            return tab
        if codes:
            names = [
                ((_field_variant(out, c, p) or Field(c, "u", "", "")).name
                 or f"<{c:#x}>")
                for c, p in zip(codes, prefixes)
            ]
            form_layout.append({
                "kind": "tuple",
                "tab": tab,
                "title": title or " / ".join(names),
                "sep": sep,
                "codes": codes,
                "prefixes": prefixes,
                "separate": separate,
                "hide_on_empty": bool(node.get("hideonempty")),
                "on": _item_on(),
            })
            if width is not None and not separate:
                _add_column({
                    "kind": "tuple",
                    "name": title or " / ".join(names),
                    "codes": codes,
                    "sep": sep,
                    "width": width,
                })
        return tab

    if (
        node.get("type") == "union"
        and node.get("single")
        and form_layout is not None
        and not _in_tuple
    ):
        # A `single` union is one logical field the user fills as any one of its
        # variants (Allow Address = IPv4 network or IPv6 network; Frequency = a
        # channel enum or a raw number). WinBox shows a single row. Emit the
        # first id-bearing variant as that row and keep the rest in `out` only,
        # so the device can still send either wire form back without spawning a
        # duplicate empty row per variant.
        union_name = node.get("name") or node.get("title")
        child_ro = inherited_ro or bool(node.get("ro"))
        # A nonpublic / read-only union is a list column-alias (DNS Static's
        # `Value` aggregates whichever record field is set) that reuses the
        # deck fields' own codes. Register its variants for decoding, but never
        # emit a form row or set union_alts -- that would turn the deck's public
        # Address field into a switcher and duplicate the row.
        hidden = bool(node.get("nonpublic") or node.get("ro"))
        union_width = _column_width(node) or inherited_width
        variant_codes: list[int] = []
        for sub in _as_node_list(node.get("c")):
            if not isinstance(sub, dict):
                continue
            child_split = _split_id(sub.get("id"))
            if not child_split:
                continue
            prefix, code = child_split
            fld = _make_field(sub, code, prefix)
            if not fld.name and union_name:
                fld.name = str(union_name)
            # The unit sits on the union, not on its members: Frequency is
            # `{name:'Frequency',type:'union',postfix:'MHz',c:[…]}`, and without
            # this every variant of it read as a bare number.
            if fld.postfix is None and node.get("postfix") is not None:
                fld.postfix = str(node["postfix"])
            if not variant_codes and fld.column_width is None:
                fld.column_width = union_width
            if child_ro and not fld.ro:
                fld.ro = True
            _collect(sub.get("c"), fld.nested, None, "General", fld.name or None, fld.ro)
            _adopt_element_enum(sub, fld)
            first_time = code not in out
            new_decl = _new_declaration(out, code, prefix)
            _merge_field(out, fld)
            if not variant_codes and not hidden:
                if form_order is not None and first_time:
                    form_order.append((tab, code))
                if form_decls is not None and new_decl:
                    form_decls.append((tab, code, prefix))
                form_layout.append({
                    "kind": "field", "tab": tab, "code": code,
                    "prefix": prefix, "on": _item_on(),
                })
                if union_width is not None:
                    _add_column({
                        "kind": "field", "code": code, "prefix": prefix,
                        "name": fld.name, "width": union_width,
                    })
            variant_codes.append(code)
        # Hang the alternate variants off the primary so the form can offer a
        # switcher; they stay in `out` too, so the codec still decodes either.
        if variant_codes and not hidden:
            primary = out.get(variant_codes[0])
            if primary is not None:
                primary.union_alts = [
                    out[c] for c in variant_codes[1:] if c in out and c != primary.code
                ]
        return tab

    if node.get("type") == "deck" and form_layout is not None and not _in_tuple:
        # A deck shows one pane's fields at a time, chosen by the `selon`
        # field's value (`vals:[…]` per pane) -- DNS Static swaps Address /
        # CName / MX by record Type. Synthesize a value-match condition per
        # pane and hang the pane's fields off it, so the existing
        # hidedynamicly pipeline builds them all and toggles visibility live.
        # Panes gated by a named `on:` (stats decks) reuse that condition.
        #
        # A *named* deck is a tab, not an inline swap: across the whole 7.21
        # schema every one of them is a Winbox v3 tab title (Data Rates,
        # Advanced, HT MCS, Tx Power, Current Tx Power, Advanced Status, HT),
        # while the anonymous ones are the in-place field swaps (firewall
        # Action, DNS Type, IPsec Auth. Method). So a named deck opens its own
        # tab, gated on the pane condition -- which is how Data Rates and
        # Advanced exist only in Advanced Mode -- and the enclosing tab
        # resumes after it.
        selon = node.get("selon")
        selon = str(selon) if isinstance(selon, str) and selon else None
        depth = int((layout_ctx or {}).get("deck_depth") or 0)
        deck_name = node.get("name")
        # Only a deck the menu declares directly is a tab. The wlan 'Interface'
        # overlay nests an 'Advanced Status' deck inside the Status tab's
        # mode-swap deck; that one is a section of the tab it sits in, not a
        # thirteenth tab of its own.
        deck_tab = (
            str(deck_name)
            if depth == 0 and isinstance(deck_name, str) and deck_name
            else None
        )
        saved_on = layout_ctx.get("tab_on") if layout_ctx is not None else None
        for pane in _as_node_list(node.get("panes")):
            if not isinstance(pane, dict):
                continue
            cond_name = None
            if layout_ctx is not None:
                vals = pane.get("vals")
                ivals = [int(v) for v in vals if isinstance(v, int)] if isinstance(
                    vals, list
                ) else []
                if selon and ivals:
                    cond_name = f"__deck:{selon}:{','.join(str(v) for v in ivals)}"
                    layout_ctx.setdefault("conditions", {})[cond_name] = {
                        "hidedynamicly": 1,
                        "c": [{"on": selon, "pred": {"type": "number", "value": ivals}}],
                    }
                elif isinstance(pane.get("on"), str) and pane.get("on"):
                    cond_name = pane["on"]
                layout_ctx["tab_on"] = cond_name or saved_on
                if deck_tab and cond_name:
                    # A deck may share its name with a real tab that has an
                    # `on:` of its own (wlan6 declares both an HT tab and an HT
                    # deck); the tab's own condition is the one that decides
                    # whether it exists at all, so it wins.
                    layout_ctx.setdefault("tab_conditions", {}).setdefault(
                        deck_tab, cond_name
                    )
            if layout_ctx is not None:
                layout_ctx["deck_depth"] = depth + 1
            new_tab = _collect(
                pane.get("c"), out, form_order, deck_tab or tab, inherited_name,
                inherited_ro, form_layout, layout_ctx, form_decls=form_decls,
                _in_tuple=_in_tuple, inherited_optional=inherited_optional,
            )
            if layout_ctx is not None:
                layout_ctx["deck_depth"] = depth
            # An anonymous deck's panes sit inside the current tab and may end
            # it; a named one owns a tab of its own and never changes the
            # enclosing one.
            if not deck_tab:
                tab = new_tab
        if layout_ctx is not None:
            layout_ctx["tab_on"] = saved_on
        return tab

    # A named `group` is a fold: Winbox draws `─ ▼ Limit ────────` where it is
    # declared and hides its children until the triangle is clicked (a firewall
    # rule's Extra tab is five of them; a CAPs Rate is six). The group's own id,
    # where it has one, is a bool -- "this match is in use" -- which is what
    # decides whether the fold starts open. So the header is a row of its own
    # and the children are stamped with the group they belong to; the fields
    # themselves are collected exactly as before.
    if node.get("type") == "group" and form_layout is not None and not _in_tuple:
        title = node.get("name")
        title = str(title) if isinstance(title, str) and title.strip() else None
        split = _split_id(node.get("id")) if title else None
        key = f"{tab}:{title}:{len(form_layout)}" if title else None
        if title:
            entry: dict[str, Any] = {
                "kind": "group", "tab": tab, "title": title, "group": key,
                "on": _item_on(),
            }
            if split:
                prefix, code = split
                entry["code"], entry["prefix"] = code, prefix
                fld = _make_field(node, code, prefix)
                # The node says `group`; the id says bool (`b1c7`), and a bool
                # is what the fold writes.
                fld.jg_type = "bool"
                fld.name = title
                if inherited_ro and not fld.ro:
                    fld.ro = True
                first_time = code not in out
                new_decl = _new_declaration(out, code, prefix)
                _merge_field(out, fld)
                if form_order is not None and first_time:
                    form_order.append((tab, code))
                if form_decls is not None and new_decl:
                    form_decls.append((tab, code, prefix))
            form_layout.append(entry)
        start = len(form_layout)
        for sub in _as_node_list(node.get("c")):
            tab = _collect(
                sub, out, form_order, tab, inherited_name, inherited_ro,
                form_layout, layout_ctx, form_decls=form_decls, _in_tuple=_in_tuple,
                inherited_optional=inherited_optional,
                inherited_width=inherited_width,
            )
        if key:
            for item in form_layout[start:]:
                # setdefault: a group inside a group keeps the inner one.
                item.setdefault("group", key)
        return tab

    # A bare `not` (Discovery Interface List, TOS Value) is a negatable value:
    # the node's id is the negate flag, its single child is the value. Surface
    # the value as the field and remember the negate code for a "!" toggle.
    if node.get("type") == "not" and form_layout is not None and not _in_tuple:
        neg_split = _split_id(node.get("id"))
        not_name = node.get("name")
        not_width = _column_width(node) or inherited_width
        for sub in _as_node_list(node.get("c")):
            child_split = _split_id(sub.get("id"))
            if not child_split:
                continue
            prefix, code = child_split
            fld = _make_field(sub, code, prefix)
            if not fld.name and not_name:
                fld.name = str(not_name)
            if fld.column_width is None:
                fld.column_width = not_width
            if inherited_ro and not fld.ro:
                fld.ro = True
            if neg_split:
                fld.negate_code = neg_split[1]
            _collect(sub.get("c"), fld.nested, None, "General", fld.name or None, fld.ro)
            _adopt_element_enum(sub, fld)
            first_time = code not in out
            new_decl = _new_declaration(out, code, prefix)
            _merge_field(out, fld)
            if form_order is not None and first_time:
                form_order.append((tab, code))
            if form_decls is not None and new_decl:
                form_decls.append((tab, code, prefix))
            form_layout.append({
                "kind": "field", "tab": tab, "code": code,
                "prefix": prefix, "on": _item_on(),
            })
            if fld.column_width is not None:
                _add_column({
                    "kind": "field", "code": code, "prefix": prefix,
                    "name": fld.name, "width": fld.column_width,
                })
            break  # a not wraps exactly one value
        return tab

    # A list column drawn as a little chart: Traceroute's History is
    # {type:'graph', model:{type:'multinumber', id:'U15'}, max:100, colors:[...]}
    # -- the values are (probe, level) pairs and each colour holds up to its
    # level, 101 being a lost probe. Only the kind with a model and a width is
    # a column; a form's rate graph declares curves instead.
    model = node.get("model")
    if node.get("type") == "graph" and isinstance(model, dict) and node.get("width"):
        msplit = _split_id(model.get("id"))
        if msplit is not None:
            mprefix, mcode = msplit
            if mcode not in out:
                gfld = _make_field(model, mcode, mprefix)
                gfld.name = str(node.get("name") or "")
                out[mcode] = gfld
            _add_column({
                "kind": "graph", "code": mcode, "prefix": mprefix,
                "name": str(node.get("name") or ""), "width": node.get("width"),
                "max": node.get("max"),
                "colors": [
                    (c.get("level"), c.get("color"))
                    for c in node.get("colors") or []
                    if isinstance(c, dict) and c.get("color")
                ],
            })
        return tab

    split = _split_id(node.get("id"))
    if split:
        prefix, code = split
        fld = _make_field(node, code, prefix)
        if not fld.name and inherited_name:
            fld.name = inherited_name
        if inherited_ro and not fld.ro:
            fld.ro = True
        # An `opt` wrapper with no id of its own (address-list Timeout) makes
        # its inner field optional -- WinBox shows a "+" until you add it.
        if inherited_optional and not fld.optional:
            fld.optional = True
        if fld.column_width is None:
            fld.column_width = inherited_width
        _collect(node.get("c"), fld.nested, None, "General", fld.name or None, fld.ro)
        _adopt_element_enum(node, fld)
        first_time = code not in out
        new_decl = _new_declaration(out, code, prefix)
        _merge_field(out, fld)
        if form_order is not None and first_time:
            form_order.append((tab, code))
        if form_decls is not None and new_decl:
            form_decls.append((tab, code, prefix))
        # A row per declaration, not per code: the second declaration of an id
        # is a different field on a different tab (see _new_declaration), and
        # dropping it left Overall Stats with 8 of its 27 counters.
        #
        # `type:'flag'` is not one of those rows. A flag is chrome: a letter in
        # the list's flags column and a word in the form's tray. Interface
        # declares running / slave / passthrough / inactive at the very end of
        # its template, after the last tab has been opened, so treating them as
        # ordinary bools put four checkboxes under the traffic graphs -- ones
        # Winbox never draws, and which offered to write state the router owns.
        # The field itself is still collected (flag_fields, the letters, the
        # tray); only the form row is not.
        if (
            form_layout is not None and new_decl and not _in_tuple
            and node.get("type") != "flag"
        ):
            form_layout.append({
                "kind": "field", "tab": tab, "code": code,
                "prefix": prefix, "on": _item_on(),
            })
        # A column per declaration too. Nonpublic ones are recorded but not
        # resolved (see default_columns): the generic Interface declares MTU
        # nonpublic with a width, and only the type overlays that re-declare
        # it public show the column -- in the position the base gave it.
        if new_decl and fld.column_width is not None:
            _add_column({
                "kind": "field", "code": code, "prefix": prefix,
                "name": fld.name, "width": fld.column_width,
            })
        # Several columns behind one switch: Traceroute's Avg., Best, Worst
        # and Std. Dev. are each `{type:'opt',id:'b14',c:[{id:'u10'..'u13'}]}`.
        # Keyed on the shared b14 they were one field, and only Avg. stayed a
        # column. A re-declaration of the switch is a column of its own value.
        kids = node.get("c")
        if (
            not new_decl and node.get("type") == "opt"
            and fld.column_width is not None and fld.name
            and isinstance(kids, list) and len(kids) == 1
            and isinstance(kids[0], dict)
        ):
            inner = _split_id(kids[0].get("id"))
            if inner is not None and inner[1] not in out:
                value = _make_field(kids[0], inner[1], inner[0])
                value.name = fld.name
                out[inner[1]] = value
                _add_column({
                    "kind": "field", "code": inner[1], "prefix": inner[0],
                    "name": fld.name, "width": fld.column_width,
                })
        return tab

    child_name = inherited_name
    child_ro = inherited_ro
    # An idless `opt` container marks whatever it wraps as optional; `union`
    # already has its own branch, so this only reaches plain optionals.
    child_optional = inherited_optional or node.get("type") == "opt"
    child_width = _column_width(node) or inherited_width
    if node.get("type") in NAME_FROM_CONTAINER:
        if isinstance(node.get("name"), str):
            child_name = node["name"]
        if node.get("ro"):
            child_ro = True
    for key in ("c", "request"):
        if key in node:
            # `showdef` rides on the wrapper, not on the field: mark whatever
            # the wrapper turns out to contain (see Field.show_default).
            before = set(out) if node.get("showdef") else None
            tab = _collect(
                node[key], out, form_order, tab, child_name, child_ro, form_layout,
                layout_ctx, form_decls=form_decls, _in_tuple=_in_tuple,
                inherited_optional=child_optional,
                inherited_width=child_width,
            )
            if before is not None:
                for code in set(out) - before:
                    out[code].show_default = True
    return tab


def _walk(
    node: Any,
    path: tuple[int, ...] | None,
    source: str,
    menus: list[Menu],
    group: str | None = None,
    overlays: list[Menu] | None = None,
) -> None:
    if isinstance(node, list):
        for n in node:
            _walk(n, path, source, menus, group, overlays)
        return
    if not isinstance(node, dict):
        return
    p = node.get("path")
    if isinstance(p, list) and all(isinstance(i, int) for i in p):
        path = tuple(p)
    # The UI group is declared on the top-level menu and inherited by children.
    if isinstance(node.get("group"), str):
        group = node["group"]
    kind = node.get("type")
    inherit = node.get("inherit")
    typevalue = node.get("typevalue")
    generic = node.get("generic")

    if kind in MENU_TYPES:
        fields: dict[int, Field] = {}
        form_order: list[tuple[str, int]] = []
        form_decls: list[tuple[str, int, str]] = []
        form_layout: list[dict[str, Any]] = []
        layout_ctx: dict[str, Any] = {
            "tab_on": None,
            "tab_conditions": {},
            "conditions": {},
            "statusbar": [],
            "statusbar_meta": {},
            "has_enable": False,
            "has_comment": False,
            "has_preset": False,
            "form_actions": [],
            "mode_toggle": None,
            "columns": [],
        }
        # ``ro:1`` on the menu node marks the whole menu device output, so it
        # seeds the same inheritance a ``ro`` container already had: no field
        # under System ▸ Resources or Log is a setting. A query's ``request``
        # is exempt below -- those are the parameters you fill in, and QoS
        # Monitor is `ro` precisely because its *results* are readings.
        menu_ro = bool(node.get("ro"))
        _collect(
            node.get("c"), fields, form_order, inherited_ro=menu_ro,
            form_layout=form_layout, layout_ctx=layout_ctx, form_decls=form_decls,
        )
        # Query/action menus keep their request form in its own namespace: the
        # parameter codes overlap the result-column codes (Ping's u3 is both
        # "Packet Size" and "Reply Size"), and merging silently loses one.
        req_fields: dict[int, Field] = {}
        req_order: list[tuple[str, int]] = []
        req_decls: list[tuple[str, int, str]] = []
        req_layout: list[dict[str, Any]] = []
        req_gridcells: list[str] = []
        if kind in _COMMAND_TYPES:
            req_ctx: dict[str, Any] = {
                "tab_on": None, "tab_conditions": {}, "conditions": {},
                "statusbar": [], "statusbar_meta": {}, "has_enable": False,
                "has_comment": False, "has_preset": False, "form_actions": [],
                "mode_toggle": None, "context_actions": [],
                "in_toolbar": False,
            }
            _collect(
                node.get("request"), req_fields, req_order,
                form_layout=req_layout, layout_ctx=req_ctx, form_decls=req_decls,
            )
            req_gridcells = list(req_ctx.get("gridcells") or [])
            # Ping's min/avg/max tray lives under `status`, not `c`.
            _collect(
                node.get("status"), fields, form_order, inherited_ro=menu_ro,
                form_layout=form_layout, layout_ctx=layout_ctx,
                form_decls=form_decls,
            )
            for key in ("conditions", "tab_conditions"):
                layout_ctx[key] = {**(layout_ctx.get(key) or {}),
                                   **(req_ctx.get(key) or {})}
        else:
            _collect(
                node.get("request"), fields, form_order, inherited_ro=menu_ro,
                form_layout=form_layout, layout_ctx=layout_ctx,
                form_decls=form_decls,
            )
        tree_on = node.get("treeon")
        tree_on = str(tree_on) if isinstance(tree_on, str) and tree_on else None
        icons_raw = node.get("icons")
        icon_map: dict[int, int] = {}
        if isinstance(icons_raw, dict):
            for k, v in icons_raw.items():
                try:
                    icon_map[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        icon_on = node.get("iconon")
        icon_on = str(icon_on) if isinstance(icon_on, str) and icon_on else None
        highlight_on = node.get("highlight")
        highlight_on = (
            str(highlight_on) if isinstance(highlight_on, str) and highlight_on
            else None
        )
        max_objects = _as_int(node.get("maxobjs"))
        max_objects_message = node.get("maxobjsmsg")
        max_objects_message = (
            str(max_objects_message)
            if isinstance(max_objects_message, str) and max_objects_message
            else None
        )
        tab_priority = dict(layout_ctx.get("tab_priority") or {})
        full_title = node.get("fulltitle")
        full_title = str(full_title) if isinstance(full_title, str) else None
        addable = not bool(node.get("nonaddable"))
        movable = not bool(node.get("nonmovable"))
        removable_declared = bool(node.get("removable"))
        # `def:'mfe0024'` -- the nested message an object's defaults arrive in.
        defaults_split = _split_id(node.get("def"))
        defaults_code = (
            defaults_split[1]
            if defaults_split and defaults_split[0] == "m"
            else None
        )
        conds = dict(layout_ctx.get("conditions") or {})
        tab_conds = dict(layout_ctx.get("tab_conditions") or {})
        statusbar = list(layout_ctx.get("statusbar") or [])
        statusbar_meta = dict(layout_ctx.get("statusbar_meta") or {})
        has_enable = bool(layout_ctx.get("has_enable"))
        has_comment = bool(layout_ctx.get("has_comment"))
        has_preset = bool(layout_ctx.get("has_preset"))
        mode_toggle = layout_ctx.get("mode_toggle") or None
        context_actions = list(layout_ctx.get("context_actions") or [])
        numflags = list(layout_ctx.get("numflags") or [])
        list_filters = list(layout_ctx.get("list_filters") or [])
        form_actions = list(layout_ctx.get("form_actions") or [])
        column_decls = list(layout_ctx.get("columns") or [])
        form_layout, form_order, form_decls = _strip_statusbar_from_form(
            form_layout, form_order, _statusbar_codes(statusbar), form_decls,
            _statusbar_decls(statusbar),
        )
        # Path-backed menus (normal) and path-less type overlays (Ethernet).
        if path is not None:
            menus.append(Menu(
                path=path,
                name=str(node.get("name") or node.get("title") or "?"),
                title=str(node.get("title") or node.get("name") or "?"),
                kind=kind,
                group=group,
                cli_prefix=node.get("prefix"),
                nameval=node.get("nameval"),
                typeon=node.get("typeon"),
                refresh_filter=_as_int(node.get("refreshfilter")),
                command=node.get("cmd") or node.get("startcmd"),
                write_perm=node.get("writeperm"),
                source=source,
                fields=fields,
                form_order=form_order,
                form_decls=form_decls,
                form_layout=form_layout,
                column_decls=column_decls,
                ordered=bool(node.get("ordered")),
                conditions=conds,
                tab_conditions=tab_conds,
                statusbar=statusbar,
                statusbar_meta=statusbar_meta,
                has_enable=has_enable,
                has_comment=has_comment,
                has_preset=has_preset,
                mode_toggle=mode_toggle,
                defaults_code=defaults_code,
                addable=addable,
                movable=movable,
                tree_on=tree_on,
                tab_priority=tab_priority,
                icon_on=icon_on,
                icon_map=icon_map,
                highlight_on=highlight_on,
                max_objects=max_objects,
                max_objects_message=max_objects_message,
                removable_declared=removable_declared,
                full_title=full_title,
                context_actions=context_actions,
                numflags=numflags,
                list_filters=list_filters,
                form_actions=form_actions,
                generic=str(generic) if isinstance(generic, str) else None,
                request_fields=req_fields,
                request_order=req_order,
                request_decls=req_decls,
                request_layout=req_layout,
                request_gridcells=req_gridcells,
                start_command=_as_int(node.get("startcmd")),
                cancel_command=_as_int(node.get("cancelcmd")),
                auto_close=bool(node.get("autoclose")),
                poll_command=_as_int(node.get("pollcmd")),
                autorefresh_ms=_as_int(node.get("autorefresh")),
                auto_limit=_as_int(node.get("autolimit")),
                auto_scroll=bool(node.get("autoscroll")),
                uptime_field=(
                    str(node["uptime"]) if isinstance(node.get("uptime"), str) else None
                ),
                # Read here as well as on the overlay branch below: the .jg
                # marks ordinary menus nonpublic too, and leaving the key off
                # this call made every one of them come back public.
                nonpublic=bool(node.get("nonpublic")),
                read_only=menu_ro,
            ))
        elif (
            overlays is not None
            and isinstance(inherit, str)
            and typevalue is not None
        ):
            try:
                tv = int(typevalue)
            except (TypeError, ValueError):
                tv = None
            if tv is not None:
                overlays.append(Menu(
                    path=(),
                    name=str(node.get("name") or node.get("title") or "?"),
                    title=str(node.get("title") or node.get("name") or "?"),
                    kind=kind,
                    group=group,
                    # Same passthrough as the path-backed branch above: without
                    # these, 12 of these overlays lost their `prefix` (the
                    # default-name stem: vlan, wg, eoip-tunnel...) and the
                    # chained `generic` that resolves iface -> ppp -> pptp-in.
                    cli_prefix=node.get("prefix"),
                    nameval=node.get("nameval"),
                    typeon=node.get("typeon"),
                    refresh_filter=_as_int(node.get("refreshfilter")),
                    command=node.get("cmd") or node.get("startcmd"),
                    write_perm=node.get("writeperm"),
                    source=source,
                    fields=fields,
                    form_order=form_order,
                    form_decls=form_decls,
                    form_layout=form_layout,
                    column_decls=column_decls,
                    ordered=bool(node.get("ordered")),
                    conditions=conds,
                    tab_conditions=tab_conds,
                    statusbar=statusbar,
                    statusbar_meta=statusbar_meta,
                    has_enable=has_enable,
                    has_comment=has_comment,
                    has_preset=has_preset,
                    mode_toggle=mode_toggle,
                    defaults_code=defaults_code,
                    addable=addable,
                    movable=movable,
                    tree_on=tree_on,
                    tab_priority=tab_priority,
                    icon_on=icon_on,
                    icon_map=icon_map,
                    highlight_on=highlight_on,
                    max_objects=max_objects,
                    max_objects_message=max_objects_message,
                    removable_declared=removable_declared,
                    full_title=full_title,
                    context_actions=context_actions,
                    numflags=numflags,
                    list_filters=list_filters,
                    form_actions=form_actions,
                    generic=str(generic) if isinstance(generic, str) else None,
                    inherit=inherit,
                    typevalue=tv,
                    nonpublic=bool(node.get("nonpublic")),
                    read_only=menu_ro,
                    # The rest of what the path-backed branch reads. No overlay
                    # in 7.21.1 declares any of them -- the merge inherits the
                    # base's answer, which is what fixed PPP's refresh rate --
                    # but an overlay that does declare one has to be able to
                    # say so, and reading the node is the only way it can.
                    request_fields=req_fields,
                    request_order=req_order,
                    request_decls=req_decls,
                    request_layout=req_layout,
                    request_gridcells=req_gridcells,
                    start_command=_as_int(node.get("startcmd")),
                    cancel_command=_as_int(node.get("cancelcmd")),
                    auto_close=bool(node.get("autoclose")),
                    poll_command=_as_int(node.get("pollcmd")),
                    autorefresh_ms=_as_int(node.get("autorefresh")),
                    auto_limit=_as_int(node.get("autolimit")),
                    auto_scroll=bool(node.get("autoscroll")),
                    uptime_field=(
                        str(node["uptime"])
                        if isinstance(node.get("uptime"), str) else None
                    ),
                ))
    # Recurse for nested menus (a map may contain doit actions). Field
    # collection above already consumed this node's own attributes, and
    # values:/pred: reference other menus so are not descended.
    for key in ("c", "request"):
        if key in node:
            _walk(node[key], path, source, menus, group, overlays)


# Types that are field-level (or chrome), not navigation leaves.
_FIELDISH = {
    "number", "enm", "string", "bool", "flag", "ipaddr", "ip6addr", "macaddr",
    "raw", "secret", "password", "script", "multilinestring", "interval", "age",
    "dateandtime", "bignumber", "bigbytes", "bigbitrate", "bigdecimal", "decimal",
    "fixedpoint", "objtype", "bytes", "message", "addr", "network6", "macnetwork",
    "numberrange", "multinumber", "multistring", "tuple", "multituple", "table",
    "graphbox", "contextmenu", "statusbar", "file", "about", "dynamic", "invalid",
    "enable", "comment", "separator", "preset", "filter", "cond", "opt", "union",
    "set", "tab", "group", "selector", "alias",
}

# Field types Winbox masks unless Settings > Show Passwords is on. Note that
# Field.nonpublic is "hide from the UI", not secrecy -- these two are secrecy.
SECRET_TYPES = frozenset({"secret", "password"})


def secret_leaf(field: "Field | None") -> "Field | None":
    """The secret this field holds, at or below itself.

    The .jg puts secrecy on the leaf, and the leaf is not always the row it is
    labelled on. A tunnel's IPsec Secret is an ``opt`` wrapping one::

        {name:'IPsec Secret',type:'opt',id:'b7da',c:[{type:'secret',id:'s7db'}]}

    -- and so are Quick Set's WiFi Password and a disk's Self Encryption
    Password. Winbox builds its editor from the leaf, so the mask lands
    wherever the leaf is; asking the row for its own type answered "not a
    secret" for all three and the value went to screen in the clear.

    The leaf carries its own wire code, sent as a sibling of the opt's, which
    is why callers that mask by code want the returned field and not just a
    yes/no.
    """
    if field is None:
        return None
    if field.jg_type in SECRET_TYPES:
        return field
    for child in (field.nested or {}).values():
        found = secret_leaf(child)
        if found is not None:
            return found
    return None


def is_secret(field: "Field | None") -> bool:
    """True when a field's value should be masked in lists and forms."""
    return secret_leaf(field) is not None


#: Inner field code of an ``addr`` value. Winbox wraps the address in a nested
#: message rather than sending a bare u32 -- confirmed on the wire: Ping To is
#: ``0x16 tag 0x28 { 0xfeff20: u32 }``. See the query-tools section of README.
ADDR_INNER_V4 = 0xFEFF20

#: The rest of an ``addr`` record, read off a 7.25 device's Route table, where
#: Dst. Address, Gateway and Immediate Gateway all arrive this way:
#:
#:   {kind 1, prefix 24, v4 10.18.50.0}   -> 10.18.50.0/24
#:   {kind 1, v4 203.0.113.1}             -> 203.0.113.1
#:   {iface 4, kind 1, v4 203.0.113.1}    -> 203.0.113.1%ether1
#:   {iface 9, kind 9}                    -> bridge
#:   {iface 9, prefix 64, kind 2, v6 fe80::} -> fe80::%bridge/64
#:
#: The interface is an object id in Interface (20, 0).
ADDR_KIND = 0xFEFF1F
ADDR_INNER_V6 = 0xFEFF21
ADDR_IFACE = 0xFEFF22
ADDR_PREFIX = 0xFEFF25
ADDR_KIND_V4, ADDR_KIND_V6, ADDR_KIND_IFACE = 1, 2, 9
ADDR_IFACE_PATH = (20, 0)


def addr_has_iface(value: Any) -> bool:
    """Whether an ``addr`` value (or a list of them) names an interface."""
    if isinstance(value, list):
        return any(addr_has_iface(v) for v in value)
    return isinstance(value, m.Message) and any(
        code == ADDR_IFACE for code, _tag, _val in value
    )


def format_addr(value: "m.Message", iface_names: dict[int, str] | None = None) -> str:
    """An ``addr`` record as the router writes it: address, ``%zone``, ``/prefix``.

    The IPv4 sits first octet in the low byte, like every record menu's (the
    Route table's 10.18.50.0/24 only reads as a network that way), with a
    kind or without. Read live on 7.23.3: a disabled route, a routing rule, a
    Netwatch host, an NTP server and a WireGuard endpoint come without one,
    and taking that to mean network order showed 192.168.1.3 as 3.1.168.192.
    Network order is only what Ping and Traceroute are sent; see
    :func:`parse_ipv4_net`.
    """
    by_code: dict[int, Any] = {}
    for code, _tag, val in value:
        by_code.setdefault(code, val)
    iface = by_code.get(ADDR_IFACE)
    zone = ""
    if isinstance(iface, int):
        zone = (iface_names or {}).get(iface) or f"*{iface:X}"
    v4, v6 = by_code.get(ADDR_INNER_V4), by_code.get(ADDR_INNER_V6)
    if isinstance(v6, (bytes, bytearray)) and len(v6) == 16:
        import ipaddress

        text = str(ipaddress.IPv6Address(bytes(v6)))
    elif isinstance(v4, int):
        text = format_ipv4(v4)
    else:
        # Kind 9, or nothing but an interface: the interface is the value.
        return zone
    if zone:
        text = f"{text}%{zone}"
    prefix = by_code.get(ADDR_PREFIX)
    if isinstance(prefix, int):
        text = f"{text}/{prefix}"
    return text


def _ipv4_octets(text: Any) -> list[int]:
    parts = str(text).strip().split(".")
    if len(parts) != 4:
        raise ValueError(f"not an IPv4 address: {text!r}")
    out = []
    for part in parts:
        octet = int(part)
        if not 0 <= octet <= 255:
            raise ValueError(f"not an IPv4 address: {text!r}")
        out.append(octet)
    return out


def parse_ipv4(text: Any) -> int:
    """Dotted quad -> u32 with the first octet in the LOW byte.

    This is the order a record menu's ``ipaddr`` / ``network`` field uses:
    192.168.88.1 is 0x0158A8C0. Confirmed twice over -- it is what
    :meth:`Field.render` decodes (and IP → Address renders correctly on a live
    device), and it is what real Winbox sent in the 2026-08-06 ADD capture,
    where 192.168.234.1 went out as 32155840.

    It is also the order inside a record's nested ``addr`` value. The one
    exception is what Ping and Traceroute are sent, which is network order:
    :func:`parse_ipv4_net`.

    An int passes through: an unset address is simply 0 on the wire, and
    callers reusing a decoded value already hold the u32.
    """
    if isinstance(text, bool):
        raise ValueError(f"not an IPv4 address: {text!r}")
    if isinstance(text, int):
        return text & 0xFFFFFFFF
    if not str(text).strip():
        return 0
    out = 0
    for shift, octet in zip((0, 8, 16, 24), _ipv4_octets(text)):
        out |= octet << shift
    return out


def format_ipv4_range(low: str, high: str) -> str:
    """An address match's low and high ends, the way the router writes them.

    One address alone; a range that is exactly one subnet as that subnet --
    192.168.88.0 to 192.168.88.255 is 192.168.88.0/24, which is what the CLI
    prints and Winbox shows; anything else as ``low-high``.
    """
    import ipaddress

    if not high or high == low:
        return low
    try:
        nets = list(ipaddress.summarize_address_range(
            ipaddress.IPv4Address(low), ipaddress.IPv4Address(high)))
    except (ValueError, TypeError):
        return f"{low}-{high}"
    return str(nets[0]) if len(nets) == 1 else f"{low}-{high}"


def parse_ipv4_range(text: str) -> tuple[str, str]:
    """``(low, high)`` dotted quads from an address, a ``low-high`` range, or
    a ``net/prefix`` -- the three ways one is written. Raises ValueError."""
    import ipaddress

    text = str(text).strip()
    if "/" in text:
        net = ipaddress.IPv4Network(text, strict=False)
        return str(net.network_address), str(net.broadcast_address)
    low, _sep, high = text.partition("-")
    low, high = low.strip(), (high.strip() or low.strip())
    ipaddress.IPv4Address(low)
    ipaddress.IPv4Address(high)
    return low, high


def format_ipv4(value: int) -> str:
    """Inverse of :func:`parse_ipv4` — first octet from the low byte."""
    v = int(value) & 0xFFFFFFFF
    return ".".join(str((v >> shift) & 0xFF) for shift in (0, 8, 16, 24))


#: What ``5K`` / ``5M`` / ``5G`` multiply by in a ``unit`` box. Decimal, not
#: 1024: a firewall Limit's Burst is declared ``max:2000000000``, which is 2G
#: exactly on this scale and nothing round at all on the binary one.
_UNIT_SUFFIX = {"K": 1000, "M": 1000_000, "G": 1000_000_000}


def parse_unit(text: Any) -> int:
    """A ``unit`` / ``bigunit`` box: a plain number, or one with K/M/G.

    Winbox accepts ``5K``, ``5M`` and ``5G`` here and shows the number back
    plainly -- type 1048576 and it stays 1048576 -- so the suffix is input
    shorthand and never a rendering. Which is why :meth:`Field.render` has no
    matching branch: there is nothing to render differently.
    """
    if isinstance(text, (int, float)):
        return int(text)
    raw = str(text or "").strip().replace(" ", "")
    if not raw:
        return 0
    mult = _UNIT_SUFFIX.get(raw[-1:].upper())
    if mult is None:
        return int(raw, 0)
    head = raw[:-1] or "0"
    # "1.5M" is 1500000; the scale makes a fraction meaningful, unlike a bare
    # count where int() is the whole story.
    return int(round(float(head) * mult))


def _prefix_dotted(bits: int) -> str:
    """``24`` -> ``"255.255.255.0"``."""
    if not 0 <= bits <= 32:
        raise ValueError(f"prefix length out of range: {bits}")
    packed = ((1 << bits) - 1) << (32 - bits) if bits else 0
    return ".".join(str((packed >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def parse_netmask(text: Any) -> int:
    """A ``netmask`` box, in either notation Winbox takes.

    ``255.255.255.0`` and ``/24`` (and a bare ``24``) are the same mask. The
    encoding is :func:`parse_ipv4`'s, and the .jg says so outright: a switch
    rule's Src. Address is ``{tuple,sep:'/',c:[{ipaddr,id:'u1b'},{union,c:[
    {ipaddr,id:'u1c'},{netmask,id:'u1c'}]}]}`` -- one wire code declared both
    ways, which is the union offering the two notations for one value.
    """
    if isinstance(text, int):
        return _mask_from_int(text)
    raw = str(text or "").strip()
    if not raw:
        return 0
    if raw.startswith("/"):
        raw = raw[1:].strip()
    if "." in raw:
        return parse_ipv4(raw)
    return parse_ipv4(_prefix_dotted(int(raw, 10)))


#: Every value that is a contiguous netmask, in the wire's own octet order.
_VALID_MASKS = frozenset(parse_ipv4(_prefix_dotted(n)) for n in range(33))


def _mask_from_int(value: int) -> int:
    """Whichever of the two encodings a stored number is, as a wire mask.

    Most declarations carry the mask itself (three default to ``4294967295``,
    which is 255.255.255.255 and no prefix length at all), but the router's own
    CLI takes 0-32 for a DHCP network's netmask, so a stored ``24`` is worth
    reading rather than showing as 0.0.0.24. No guessing is involved: the two
    domains are disjoint. The smallest non-zero mask is 128 (that is /1), so
    nothing in 1..32 can be one, and 0 means the same in both.
    """
    value = int(value) & 0xFFFFFFFF
    if value in _VALID_MASKS or not 1 <= value <= 32:
        return value
    return parse_ipv4(_prefix_dotted(value))


def render_netmask(value: Any) -> str:
    """A netmask as its dotted quad, from either encoding."""
    try:
        return format_ipv4(_mask_from_int(int(value)))
    except (TypeError, ValueError):
        return ""


def parse_ipv4_net(text: Any) -> int:
    """Dotted quad -> u32 in network order (first octet in the HIGH byte).

    The order Ping To and Traceroute To are sent in (the global start, see
    :meth:`DeviceClient.start_query`). Confirmed live: Ping To 192.168.87.1 and
    8.8.4.4 both reach their target with this encoding, where the record-menu
    order would have aimed at 1.87.168.192. Ping's reply carries its host as
    a bare u32 the same way round. It is never a record's order: an ``addr``
    read from a menu is low byte first, kind or not (:func:`format_addr`).
    """
    if isinstance(text, bool):
        raise ValueError(f"not an IPv4 address: {text!r}")
    if isinstance(text, int):
        return text & 0xFFFFFFFF
    if not str(text).strip():
        return 0
    out = 0
    for octet in _ipv4_octets(text):
        out = (out << 8) | octet
    return out


def format_ipv4_net(value: int) -> str:
    """Inverse of :func:`parse_ipv4_net` — first octet from the high byte."""
    v = int(value) & 0xFFFFFFFF
    return ".".join(str((v >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def encode_form_value(field: "Field", text: Any) -> tuple[int, Any]:
    """A request-form value as ``(wire_tag, value)`` ready for a Message.

    Driven by the semantic .jg type rather than the id letter: ``addr`` is a
    nested message while ``ipaddr`` is a bare u32, and only the type says so.
    """
    jg = field.jg_type
    if isinstance(text, str):
        text = text.strip()

    # A two-option enum can still be a *bool* on the wire, and only the id
    # letter says which. IP ▸ Cloud's "DDNS Enabled" is `b`-prefixed with
    # values {0: auto, 1: yes}: sent as a u32 the device ignored it silently.
    # "Back To Home VPN" on the same menu is `u`-prefixed and really is a
    # number, so the prefix is the only thing that tells them apart.
    if jg == "enm" and field.prefix == "b":
        try:
            value = bool(int(text))
        except (TypeError, ValueError):
            value = str(text).lower() in ("1", "true", "yes", "on")
        return (m.T_BOOL_TRUE if value else m.T_BOOL_FALSE), value

    if jg in ("bool", "flag", "opt"):
        if isinstance(text, str):
            value = text.lower() in ("1", "true", "yes", "on")
        else:
            value = bool(text)
        if field.invert:
            value = not value      # the box shows the opposite of the wire
        return (m.T_BOOL_TRUE if value else m.T_BOOL_FALSE), value

    if jg == "addr":
        # Nested {0xfeff20: u32}; IPv6 uses a different inner code we have not
        # captured yet, so only v4 is claimed here. Network order, unlike the
        # bare ipaddr below -- see parse_ipv4_net.
        inner = m.Message().add_u32(ADDR_INNER_V4, parse_ipv4_net(text))
        return m.T_MESSAGE, inner

    if jg in ("ipaddr", "network"):
        return m.T_U32, parse_ipv4(text)

    if jg == "netmask":
        # Typed either way round -- "255.255.255.0" or "/24" -- and sent as the
        # mask, which is the encoding `ipaddr` uses and the one the switch-rule
        # union proves they share. See :func:`parse_netmask`.
        return m.T_U32, parse_netmask(text)

    if jg in ("unit", "bigunit"):
        # "5M" is shorthand you type, never a rendering: Winbox shows the
        # number back plainly. `unit` is the u32 and `bigunit` the u64, but the
        # value decides the tag here as it does for a plain number.
        value = parse_unit(text)
        return (m.T_U64 if value > 0xFFFFFFFF else m.T_U32), value

    if jg == "ip6addr":
        raw = bytes.fromhex(str(text).replace(":", "")) if text else b"\x00" * 16
        return m.T_IP6, raw.ljust(16, b"\x00")[:16]

    if jg == "macaddr":
        raw = bytes.fromhex(str(text).replace(":", "").replace("-", ""))
        return m.T_RAW, raw

    if jg == "interval":
        return m.T_U32, parse_interval(text)

    if jg == "clockdate":
        return m.T_U32, parse_clock_date(text)

    if jg == "clocktime":
        return m.T_U32, parse_clock_time(text)

    if jg == "timezone":
        return m.T_U32, parse_gmt_offset(text)

    if jg == "dateandtime":
        # Rendered as ``2026-08-05 14:05:51``; the wire wants the timestamp
        # back. Without this the form sent the display string, and a string
        # where the device expects a u32 is a write it discards.
        return m.T_U32, parse_dateandtime(text, scale=field.scale)

    # An array field whose editor hands back the list itself -- a multi-enum's
    # rows (Bridge VLAN Tagged, Dot1X EAP Methods). Each element is encoded as
    # the field's *element* type and the array's own tag carries them; without
    # this the list reached int() below and the save raised TypeError.
    if isinstance(text, (list, tuple)) and field.prefix in ARRAY_TAGS:
        elem = field.element_view()
        out = []
        for item in text:
            if isinstance(item, m.Message):
                out.append(item)          # already a nested message
                continue
            _tag, value = encode_form_value(elem, item)
            out.append(value)
        return ARRAY_TAGS[field.prefix], out

    # A range list is typed as text ("5,7-9,300") and sent as the flat array
    # of its (low, high) pairs. Without this the box's own string went out
    # under a `U` field id, which the device ignores.
    if jg in RANGE_LIST_TYPES:
        return m.T_U32_ARRAY, parse_range_list(text)

    if jg in ("number", "enm", "bignumber", "decimal", "numberrange",
              "multinumber", "objtype", "bytes"):
        if isinstance(text, str) and not text:
            return m.T_U32, 0
        # base 0 accepts a "0x…" hex field (bridge Priority, TOS) as well as
        # plain decimal.
        value = int(text, 0) if isinstance(text, str) else int(text)
        scale = getattr(field, "scale", None)
        if scale:
            value = int(round(value * scale))
        return (m.T_U64 if value > 0xFFFFFFFF else m.T_U32), value

    return m.T_STRING, "" if text is None else str(text)


def offers_new_window(menu: "Menu") -> bool:
    """Whether a tool window gets Winbox's New Window button.

    The .jg has no flag for it, so this is read off what it does say: a run
    you start and stop (``cancelcmd``), not a one-shot that closes itself
    (``autoclose``), filed under Tools. All three are needed -- a cancelcmd
    alone also takes in Format Drive, Sign and Renew License (System), where
    a second window of the same thing is no help. Under Tools it is the
    measuring set Winbox v3 gives the button: Ping, Traceroute, Torch, IP
    Scan, Bandwidth Test, Speed Test, Profile, and the Traffic Generator runs.
    """
    return (
        menu.cancel_command is not None
        and not menu.auto_close
        and menu.group == "Tools"
    )


def query_columns(menu: "Menu") -> list["Field"]:
    """Result columns for a query, in declaration order.

    Unlike ``preferred_columns`` this neither reorders nor caps: a Ping window
    shows exactly the columns the .jg declares, and Torch has 18 of them.
    """
    out: list[Field] = []
    seen: set[int] = set()
    for _tab, code in menu.form_order:
        fld = menu.fields.get(code)
        if fld is None or code in seen:
            continue
        seen.add(code)
        if fld.nonpublic or not fld.name or fld.jg_type in ("flag",):
            continue
        out.append(fld)
    for code, fld in menu.fields.items():
        if code in seen or fld.nonpublic or not fld.name:
            continue
        if fld.jg_type in ("flag",):
            continue
        out.append(fld)
    return out


# Prefer these names first when choosing table columns.
_COLUMN_PRIORITY = (
    "name", "address", "interface", "type", "dst. address", "dst address",
    "gateway", "chain", "action", "protocol", "comment", "network", "mac address",
    "time", "topics", "message", "group", "last logged in", "size", "file name",
)


@dataclass
class NavNode:
    """One node of the Winbox left-hand navigation tree (from .jg layout)."""

    name: str
    title: str
    kind: str  # folder | map | item | terminal | other
    path: tuple[int, ...] | None = None
    icon: int | None = None
    children: list["NavNode"] = dc_field(default_factory=list)
    source: str = ""
    #: .jg ``prio``. Winbox sorts the top level on it; for a folder we
    #: synthesize from a ``group`` it is the smallest prio among its members.
    prio: int | None = None
    #: ``typevalue`` of an ``inherit`` overlay (e.g. Ethernet is iface type 1).
    type_value: int | None = None
    #: What the menu calls this entry, when that differs from the window it
    #: opens. Only top-level entries and members of a ``group`` (the IP
    #: submenu, say) are menu entries. Where such an entry is a container
    #: (no ``type``), Winbox labels it with its .jg ``name`` and the window
    #: with its ``title``: 7.9's ``{name:'Addresses', title:'Address List',
    #: group:'IP', c:[...]}`` is "Addresses" in the menu and "Address List" on
    #: the window, and ``New Terminal`` opens "Terminal". A typed entry
    #: (``{name:'Log Entry', title:'Log', type:'map'}``) is labelled with its
    #: title. Empty means the title.
    label: str = ""
    #: The overlay family this node inherits from, e.g. ``"iface"``.
    inherit: str | None = None
    #: ``generic`` of an overlay that is itself the base of a family
    #: (``wlan`` for Wireless ▸ WiFi Interfaces, ``ppp`` for PPP ▸
    #: Interface). Such a node lists every type in its family rather than one,
    #: so it carries the family name instead of a ``type_value``.
    generic: str | None = None
    #: `.jg` ``pred``: a device-capability guard on the menu itself. Winbox
    #: leaves the node out entirely when it is false -- W60G only exists on a
    #: 60GHz board, Console only on x86.
    pred: Any = None
    #: .jg ``hide:1``. Winbox leaves these off a window's tab strip and reaches
    #: them from a button inside the window instead (VTEP, FDB, LTE APNs...) --
    #: the menu that owns the button declares it as a ``type:contextmenu`` with
    #: no ``link:``, so nothing here has to guess which tab it belongs to.
    hidden: bool = False
    #: Sibling menus Winbox puts on this node's toolbar rather than in the tab
    #: strip: the ``item`` forms and the ``doit``/``action``/``query`` commands
    #: a folder declares *after* this map (Ethernet ▸ Power Cycle, LTE ▸
    #: Modem, Bonding ▸ Monitor Slaves). On a folder node it holds the ones
    #: declared before its first map instead -- those belong to the window
    #: rather than to one tab (IP ▸ DNS ▸ Settings), so every tab shows them.
    actions: list["NavNode"] = dc_field(default_factory=list)

    @property
    def openable(self) -> bool:
        return self.kind in ("map", "item") and self.path is not None

    @property
    def node_id(self) -> str:
        if self.path is None:
            return "f:" + self.title
        base = "p:" + ",".join(str(x) for x in self.path) + f":{self.name}"
        # 22 iface overlays are all literally named 'Interface' and share path
        # (20,0), so the type has to be part of the identity or they collapse
        # into one window. Family bases have no type of their own and PPP's is
        # named 'Interface' too, so those key on the family instead.
        if self.type_value is not None:
            return f"{base}#t{self.type_value}"
        if self.generic:
            return f"{base}#g{self.generic}"
        return base


#: ``typevalue`` meaning "any type" -- the base of an overlay namespace rather
#: than a concrete interface type.
_TYPE_ANY = 0xFFFFFFFF

#: Menu kinds that run something rather than list objects: Ping, Torch,
#: Traceroute (query), Reboot (action/doit).
_COMMAND_TYPES = ("query", "action", "doit")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nav_is_tab(node: NavNode) -> bool:
    """Whether a folder child is one of the tabs of the folder's window.

    A visible record list, whether it names its own handler (Interface List) or
    inherits one as a type overlay (Ethernet). `hide:1` lists are not tabs --
    Winbox reaches those from a button, which is why the Interface window shows
    17 tabs and not 23.
    """
    return (
        node.kind == "map"
        and not node.hidden
        and (node.path is not None or node.inherit is not None)
    )


def _nav_is_side_action(node: NavNode) -> bool:
    """Whether a folder child is a toolbar button on a tab rather than a tab.

    The `item` single-object forms (Interfaces ▸ Detect Internet, LTE ▸ Modem)
    and the command menus (Ethernet ▸ Power Cycle, Bonding ▸ Monitor Slaves).
    A `hide:1` sibling is deliberately not one of these: the list it opens is
    named by a `type:contextmenu` with no `link:` on the menu that owns the
    button (Interface List ▸ Lists), so the folder never has to guess.
    """
    return not node.hidden and (
        node.kind in _COMMAND_TYPES
        or (node.kind == "item" and node.path is not None)
    )


def _nav_from_jg(
    node: Any, source: str, *, allow_commands: bool = False
) -> list[NavNode]:
    """Build navigation nodes from one .jg value (list or dict).

    ``allow_commands`` lets ``query``/``action``/``doit`` leaves through. It is
    only ever set for the direct children of a top-level entry, so that Ping and
    Torch reach the nav while the per-window command buttons (Packet Sniffer's
    Quick, RoMON's Ping, Monitor Slaves) stay out of it -- Winbox shows those
    inside a window, not in the menu.
    """
    if isinstance(node, list):
        out: list[NavNode] = []
        for n in node:
            out.extend(_nav_from_jg(n, source, allow_commands=allow_commands))
        return out
    if not isinstance(node, dict):
        return []
    if node.get("nonpublic"):
        return []

    typ = node.get("type")
    name = str(node.get("name") or node.get("title") or "")
    title = str(node.get("title") or node.get("name") or name or "?")
    icon = node.get("icon") if isinstance(node.get("icon"), int) else None
    path = None
    p = node.get("path")
    if isinstance(p, list) and all(isinstance(i, int) for i in p):
        path = tuple(p)

    hidden = bool(node.get("hide"))
    pred = node.get("pred") if isinstance(node.get("pred"), dict) else None

    # A nameless `map` is a vocabulary, not a menu. All 24 of them in 7.21 are
    # the same shape -- no `name`, no `title`, `nameval:'Name'` and a single
    # `Name` column at the universal name code 0xfe0010 -- and every one is the
    # target of a `values:{type:'dynamic',path:[...]}` dropdown somewhere else:
    # (20,2) fills a filter rule's Chain and Jump Target, (20,34) the Address
    # List boxes in firewall, DHCP and Hotspot, (138,32) WiFi Security's Multi
    # Passphrase Group, (13,3) -- the one exception, Name/Alias/Type and
    # `autostart` -- the policy list a user group is built from. They stay in
    # `records` so `enum_map` can read them; putting them in the nav gave the
    # WiFi, Firewall, Logging, Bridge, Routing and Users windows a tab labelled
    # '?' that lists names with nothing to do.
    if typ == "map" and not node.get("name") and not node.get("title"):
        return []

    # Openable record menus, plus command menus when the caller allows them
    if (
        typ in ("map", "item")
        or (allow_commands and typ in _COMMAND_TYPES)
    ) and path is not None:
        return [NavNode(
            name=name or title, title=title, kind=typ, path=path,
            icon=icon, source=source, hidden=hidden, pred=pred,
        )]

    # A `termopen` dialog (Tools ▸ Telnet) has no handler path: it collects a
    # type, an address and a port, and the connection is the client's to make.
    # It is still a leaf of the menu tree, and the right-click Telnet / SSH /
    # MAC Telnet entries on ARP, Neighbors and Registration name it by title.
    if typ == "termopen" and allow_commands:
        return [NavNode(
            name=name or title, title=title, kind="termopen", path=None,
            icon=icon, source=source, hidden=hidden, pred=pred,
        )]

    # Interface-type overlays: `type:'map'` with no path of their own, reading
    # the handler of the generic they inherit (Ethernet, VLAN, Bonding, ...).
    # The path is filled in later by _resolve_nav_overlays, once every file has
    # been walked and the generic → path index exists.
    inherit = node.get("inherit")
    typevalue = node.get("typevalue")
    generic = node.get("generic")
    if (
        typ in ("map", "item")
        and path is None
        and isinstance(inherit, str)
        and isinstance(typevalue, int)
    ):
        if typevalue == _TYPE_ANY:
            # The base of an overlay family, not one concrete type: Wireless ▸
            # WiFi Interfaces (generic 'wlan') and PPP ▸ Interface ('ppp').
            # Winbox opens each as a real tab listing every type in the family,
            # so the node carries the family name and the list filters on the
            # set of typevalues under it (see Schema.family_type_values).
            if not isinstance(generic, str) or not generic:
                return []
            return [NavNode(
                name=name or title, title=title, kind=typ, path=None,
                icon=icon, source=source, inherit=inherit, generic=generic,
                hidden=hidden, pred=pred,
            )]
        # `generic` is deliberately not carried here: on a concrete type it
        # only names the sub-family the node heads (35 is 'ath'), and the node
        # still lists exactly one type. Only a family base sets it.
        return [NavNode(
            name=name or title, title=title, kind=typ, path=None,
            icon=icon, source=source, type_value=typevalue, inherit=inherit,
            hidden=hidden, pred=pred,
        )]

    # Terminal alias
    if title in ("New Terminal", "Terminal") or name in ("New Terminal", "Terminal"):
        return [NavNode(
            name=name or "New Terminal", title=title or "New Terminal",
            kind="terminal", icon=icon if icon is not None else 16, source=source,
        )]

    # Skip pure field / chrome nodes
    if typ in _FIELDISH or typ in ("doit", "action", "query"):
        return []

    # Folder / unnamed container with children.
    #
    # Walked one child at a time rather than flattened, because in Winbox v3 the
    # .jg order *is* the association between a window's tabs and its toolbars:
    # every tab has its own set of buttons, and a button is the `item` form or
    # command menu declared after the map it belongs to. Interfaces ▸ Ethernet
    # is followed by Power Cycle, Bonding by Monitor Slaves, LTE by Modem and
    # Antenna Scan -- and anything declared before the first map (IP ▸ DNS ▸
    # Settings) belongs to the window instead, so every tab gets it.
    kids_raw = node.get("c")
    kids: list[NavNode] = []
    window_actions: list[NavNode] = []
    owner: NavNode | None = None
    for raw in _as_node_list(kids_raw):
        for k in _nav_from_jg(raw, source, allow_commands=True):
            if _nav_is_side_action(k):
                (window_actions if owner is None else owner.actions).append(k)
                # An `item` is a menu in its own right as well -- Quick Set's
                # modes are items, and the nav opens them. A command menu never
                # was: Winbox shows those in a window, not in the tree.
                if k.kind not in _COMMAND_TYPES:
                    kids.append(k)
                continue
            # Drop empty folders and field-only noise
            if (
                k.kind == "folder" or k.openable or k.kind == "terminal"
                or k.inherit is not None  # overlay leaf, path resolved later
            ):
                kids.append(k)
            if _nav_is_tab(k):
                owner = k
    if not kids and kids_raw is not None and (name or title):
        # A wrapper whose only content is one command: Tools > Ping is a
        # container named "Ping" holding a single type:'query'. Surface it as
        # that command, keeping the wrapper's label and icon. Only when there is
        # exactly one -- never guess between several.
        cmds = [
            k for k in _nav_from_jg(kids_raw, source, allow_commands=True)
            if (k.kind in _COMMAND_TYPES and k.path is not None)
            # A termopen dialog has no handler path of its own -- the wrapper
            # around it (Tools ▸ Telnet) is still the menu entry.
            or k.kind == "termopen"
        ]
        if len(cmds) == 1:
            only = cmds[0]
            return [NavNode(
                name=name or title, title=title, kind=only.kind, path=only.path,
                icon=icon if icon is not None else only.icon, source=source,
                hidden=hidden or only.hidden,
            )]
    if not name and not title:
        # An unnamed wrapper is spliced into its parent, so it has no window of
        # its own for a window-level button to sit on: give those to the first
        # map it wraps.
        first = next((k for k in kids if _nav_is_tab(k)), None)
        if window_actions and first is not None:
            first.actions[:0] = window_actions
        return kids
    if kids and typ not in ("map", "item"):
        # Collapse single-child folder with same name
        if len(kids) == 1 and kids[0].title == title and kids[0].openable:
            if icon is not None and kids[0].icon is None:
                kids[0].icon = icon
            # `is not kids[0]`: a folder holding one `item` collapses onto it,
            # and that item is its own window action -- System ▸ SSH would
            # otherwise carry a button that reopens itself.
            kids[0].actions[:0] = [a for a in window_actions if a is not kids[0]]
            return kids
        return [NavNode(
            name=name or title, title=title, kind="folder", icon=icon,
            children=kids, source=source, actions=window_actions,
        )]
    return []


def _dialogs_from_jg(root: Any, source: str) -> list[Menu]:
    """Menus for the dialogs a .jg declares with no handler path of their own.

    Two node types live here. ``type:'termopen'`` (Tools ▸ Telnet) collects a
    type, an address and a port, and the *client* makes the connection -- there
    is no handler to send it to. ``type:'setup'`` (DHCP Setup, Hotspot Setup)
    is a wizard: each ``tab`` inside it is a page whose name is the page's
    prompt ("Select interface to run DHCP server on"), and the fields that
    follow belong to that page. It *does* carry a handler path, but it is not a
    menu you browse to -- Winbox offers it as a button on the window whose
    handler it sets up -- so it is kept apart from ``by_path`` too.

    Either way the fields are collected like any other menu's, so the dialog is
    built from the schema rather than from a hardcoded form.
    """
    out: list[Menu] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") in ("termopen", "setup"):
            title = str(node.get("title") or node.get("name") or "Dialog")
            kind = str(node["type"])
            raw_path = node.get("path")
            path = (
                tuple(raw_path)
                if isinstance(raw_path, list)
                and all(isinstance(i, int) for i in raw_path)
                else ()
            )
            fields: dict[int, Field] = {}
            form_order: list[tuple[str, int]] = []
            form_decls: list[tuple[str, int, str]] = []
            form_layout: list[dict[str, Any]] = []
            ctx: dict[str, Any] = {
                "tab_on": None, "tab_conditions": {}, "conditions": {},
                "statusbar": [], "statusbar_meta": {}, "has_enable": False,
                "has_comment": False, "has_preset": False, "form_actions": [],
                "mode_toggle": None, "context_actions": [], "in_toolbar": False,
                "block_on": None, "tuple_labels": [], "columns": [],
            }
            _collect(
                node.get("c"), fields, form_order, form_layout=form_layout,
                layout_ctx=ctx, form_decls=form_decls,
            )
            out.append(Menu(
                path=path, name=title, title=title, kind=kind,
                source=source, fields=fields, form_order=form_order,
                form_decls=form_decls, form_layout=form_layout,
                conditions=dict(ctx.get("conditions") or {}),
                tab_conditions=dict(ctx.get("tab_conditions") or {}),
            ))
            return
        for value in node.values():
            if isinstance(value, (list, dict)):
                walk(value)

    walk(root)
    return out


def _norm_plugin(source: str) -> str:
    """A nav node's source .jg normalised to the device index's naming
    (``wave2.jg.gz`` → ``wave2.jg``)."""
    s = source or ""
    return s[:-3] if s.endswith(".gz") else s


def filter_nav_by_plugins(nodes: list["NavNode"], plugins) -> list["NavNode"]:
    """Drop nav nodes whose source ``.jg`` the device does not advertise.

    ``plugins`` is the device's set of ``*.jg`` names
    (``DeviceClient.device_plugins``). A node is kept when its plugin is present,
    when it has no attributable source, or when it is a folder that still has
    surviving children — so e.g. the legacy Wireless folder (``wlan6.jg``)
    disappears on a device that only serves ``wave2.jg``. Falsy ``plugins`` means
    "unknown", so nothing is filtered. Returns new nodes; the input (the cached
    ``schema.nav``) is never mutated.
    """
    if not plugins:
        return list(nodes)
    def keeps(node: NavNode) -> bool:
        src = _norm_plugin(getattr(node, "source", "") or "")
        return (not src) or (src in plugins)

    out: list[NavNode] = []
    for n in nodes:
        kids = filter_nav_by_plugins(n.children or [], plugins)
        if keeps(n) or kids:
            # A toolbar button is a menu like any other: it goes when the
            # plugin that declared it is not on the device.
            out.append(replace(
                n, children=kids,
                actions=[a for a in (n.actions or []) if keeps(a)],
            ))
    return out


def menu_pred_holds(
    pred: Any,
    *,
    board: str | None = None,
    arch: str | None = None,
    syscaps: frozenset[int] | set[int] | None = None,
    plugins=None,
) -> bool:
    """Whether a menu-level ``pred`` guard holds on this device.

    Distinct from the field-level :func:`condition_is_true`: these predicates
    read the *device*, not the object -- board name, CPU architecture,
    capability bits -- and Winbox drops the whole menu when one is false.
    Wireless ▸ W60G is the case in hand: it is declared ``board ~ .*60ad.*``,
    so it must not appear on a router without a 60GHz radio.

    ``syscap`` is answered when the device told us its capability bits (see
    DeviceClient.system_caps) and skipped when it did not. ``addon``,
    ``quickset`` and ``supportmsgid`` describe things we still do not read, so
    they are treated as true -- showing a menu that Winbox would hide is
    recoverable, hiding one it shows is not.
    """
    if not isinstance(pred, dict):
        return True
    ptype = pred.get("type")
    if ptype == "not":
        return not menu_pred_holds(
            pred.get("pred"), board=board, arch=arch, syscaps=syscaps,
            plugins=plugins,
        )
    if ptype in ("or", "and"):
        inner = pred.get("pred")
        if not isinstance(inner, list):
            return menu_pred_holds(
                inner, board=board, arch=arch, syscaps=syscaps,
                plugins=plugins,
            )
        fn = any if ptype == "or" else all
        return fn(
            menu_pred_holds(
                p, board=board, arch=arch, syscaps=syscaps, plugins=plugins
            )
            for p in inner
        )
    if ptype == "board":
        return _board_matches(board, pred.get("value"), bool(pred.get("re")))
    if ptype == "arch":
        want = pred.get("value")
        return bool(arch) and want is not None and str(arch) == str(want)
    if ptype == "syscap":
        return _syscap_holds(pred.get("value"), syscaps)
    if ptype == "addon":
        return _addon_holds(pred.get("value"), plugins)
    return True


def _addon_holds(value: Any, plugins) -> bool:
    """Whether the package an ``addon`` predicate names is on the device.

    The four values in 7.21 are ``ipv6``, ``wlan6``, ``storage`` and ``oflow``.
    The first two ship a plugin of their own, so their absence is visible in
    the device's file index -- the same set :func:`filter_nav_by_plugins` uses.
    The other two ship no .jg, so nothing here can tell, and unanswerable reads
    as true.
    """
    if not plugins or not isinstance(value, str) or not value:
        return True
    if value not in _JG_ADDONS:
        return True
    return f"{value}.jg" in {str(p) for p in plugins}


#: `addon` values that ship a .jg of their own, so their absence is knowable.
_JG_ADDONS = frozenset({"ipv6", "wlan6", "wave2", "dhcp", "hotspot", "ppp",
                        "secure", "advtool", "ups"})


def _syscap_holds(
    value: Any, syscaps: frozenset[int] | set[int] | None
) -> bool:
    """Whether the device reports capability ``value``.

    True when we have no capability list: the .jg gates VETH, BTRFS, SwOS, PTP
    and every switch-chip feature this way, and a menu wrongly shown is a menu
    the device will simply refuse, while a menu wrongly hidden is unreachable.
    """
    if syscaps is None:
        return True
    try:
        return int(value) in syscaps
    except (TypeError, ValueError):
        return True


def filter_nav_by_pred(
    nodes: list["NavNode"],
    *,
    board: str | None = None,
    arch: str | None = None,
    syscaps: frozenset[int] | set[int] | None = None,
    plugins=None,
) -> list["NavNode"]:
    """Drop nav nodes whose ``pred`` guard this device fails.

    Needs a board name to mean anything -- with no device info every ``board``
    term answers false and menus would vanish wholesale -- so an empty
    ``board`` returns the tree untouched. Folders survive on their children,
    the same way :func:`filter_nav_by_plugins` treats them.

    ``syscaps`` answers the capability terms: VETH is ``syscap:2``, so a board
    with no container support does not offer it.
    """
    if not board:
        return list(nodes)
    out: list[NavNode] = []
    for n in nodes:
        if not menu_pred_holds(
            getattr(n, "pred", None), board=board, arch=arch, syscaps=syscaps,
            plugins=plugins,
        ):
            continue
        kids = filter_nav_by_pred(
            n.children or [], board=board, arch=arch, syscaps=syscaps,
            plugins=plugins,
        )
        if n.kind == "folder" and (n.children or []) and not kids:
            continue
        out.append(replace(
            n, children=kids,
            # Interfaces ▸ LTE ▸ Antenna Scan is board-guarded, like a tab.
            actions=[
                a for a in (n.actions or [])
                if menu_pred_holds(
                    getattr(a, "pred", None), board=board, arch=arch,
                    syscaps=syscaps, plugins=plugins,
                )
            ],
        ))
    return out


#: Header for the rule-number column on ordered tables.
INDEX_LABEL = "#"

#: Tables where position decides the outcome, so the list must show its order
#: and allow reordering. The .jg says so itself -- ``ordered:1`` on the map
#: node, which is what :attr:`Menu.ordered` carries -- so this is only the
#: fallback for a caller that has a path and no menu.
ORDERED_PATHS: frozenset = frozenset({
    (20, 3),    # IP ▸ Firewall ▸ Filter Rules
    (20, 5),    # IP ▸ Firewall ▸ NAT
    (20, 7),    # IP ▸ Firewall ▸ Mangle
    (20, 87),   # IP ▸ Firewall ▸ Raw
})


def is_ordered_path(path: tuple[int, ...] | None) -> bool:
    """Whether this table is evaluated in order, by path alone."""
    return tuple(path or ()) in ORDERED_PATHS


def is_ordered(menu: "Menu | None", path: tuple[int, ...] | None = None) -> bool:
    """Whether this table is evaluated in order, so its position matters.

    The menu is the authority: 61 of them declare ``ordered:1`` and only four
    were ever hardcoded, which left Simple Queues, DNS Static, IPsec Policies
    and Bridge Ports without the rule number their semantics depend on.
    """
    if menu is not None and getattr(menu, "ordered", False):
        return True
    if path is None and menu is not None:
        path = menu.path
    return is_ordered_path(path)


@dataclass
class Column:
    """One column of a list, as Winbox would draw it.

    Most are a single field, but three kinds are not: a non-separate tuple is
    one column holding both its members (``Tx/Rx BPDU's`` = "12/9"), a
    ``type:objtype`` column spells out the row's type overlay ("Ethernet"),
    and neither has a wire code of its own.
    """

    name: str
    kind: str = "field"             # field | tuple | objtype
    field: Field | None = None      # kind == "field"
    codes: tuple[int, ...] = ()     # kind == "tuple"
    sep: str = " / "                # kind == "tuple"
    #: kind == "lookup": the handler to search, and the three field *names*
    #: that make the search -- find the row of ``path`` whose ``dst`` equals
    #: this row's ``src``, and show its ``val``. ARP ▸ Bridge Port is the
    #: bridge port whose MAC Address matches this row's, and DHCP Leases ▸ Host
    #: Name is the lease whose Active MAC Address matches.
    lookup_path: tuple[int, ...] | None = None
    lookup_src: str = ""
    lookup_dst: str = ""
    lookup_val: str = ""
    #: kind == "rate": the field this column watches, and how its movement
    #: reads. ``rate_code`` is that field's wire code (resolved from the .jg's
    #: ``value:`` name), ``rate_bits`` says the counter is bytes and the answer
    #: is a bit rate. The cell itself needs two samples and so is filled by the
    #: list pane, not by :meth:`DeviceClient._column_text`.
    rate_code: int | None = None
    rate_bits: bool = False
    #: The unit to append, where the .jg gives one ("p/s").
    postfix: str = ""
    #: The .jg's own pixel width, or None for a column it does not declare.
    width: int | None = None
    #: kind == "graph": the top of the scale and the (level, colour) bands.
    graph_max: int | None = None
    graph_colors: tuple = ()
    #: True when the .jg declares this column visible from the start.
    default: bool = True

    @property
    def code(self) -> int | None:
        return self.field.code if self.field is not None else None


def _column_field(menu: Menu, name: str) -> Field | None:
    """The menu's field of this name, for a ``columnalias`` to point at."""
    want = (name or "").strip().lower()
    if not want:
        return None
    for fld in menu.fields.values():
        if (fld.name or "").strip().lower() == want:
            return fld
    return None


def _is_showable(fld: Field | None) -> bool:
    return bool(
        fld is not None
        and fld.name
        and not fld.nonpublic
        and not fld.name.startswith("<0x")
        and fld.jg_type not in ("flag",)
    )


def default_columns(menu: Menu) -> list[Column]:
    """The columns Winbox shows on this list before the user touches it.

    Straight out of the .jg: a field node that carries a ``width`` *is* a
    default column, at that pixel width, in declaration order -- across tabs,
    so an interface's Tx and Rx (Traffic tab) are columns just as Name is.
    A field with no width exists but starts hidden, offered by the header's
    column menu; ``width:0xffffffff`` says never a column at all.

    Empty when the menu declares no widths -- 103 of the 465 in 7.21 are type
    overlays living off the columns of the base they inherit, and the merged
    menu is the one that has them. Callers fall back to
    :func:`preferred_columns`' heuristic for the handful that still come up
    dry.

    Not every declared column can be filled from the row. A ``type:lookup``
    column (ARP ▸ Bridge Port, resolved out of the bridge-port table) is
    offered and filled by a second read the client makes and caches -- see
    :attr:`Column.lookup_path`. The change-rate family (Hotspot ▸ Rx Rate,
    a firewall rule's Packet Rate) needs two samples, so it is declared here
    with the counter it watches (:attr:`Column.rate_code`) and filled by the
    list pane, which is what holds the previous one.
    """
    out: list[Column] = []
    seen: set[str] = set()
    for decl in menu.column_decls or []:
        kind = str(decl.get("kind") or "field")
        width = decl.get("width")
        name = str(decl.get("name") or "")
        col: Column | None = None
        if kind == "field":
            fld = menu.field_for(int(decl["code"]), decl.get("prefix"))
            if _is_showable(fld):
                col = Column(
                    name=fld.name, kind="field", field=fld, width=width
                )
        elif kind == "alias":
            # The alias names the field it draws; the field's own declaration
            # is marked "not a column" so it only appears here.
            fld = _column_field(menu, name)
            if fld is not None and fld.name:
                col = Column(name=fld.name, kind="field", field=fld, width=width)
        elif kind == "tuple":
            # The name is the tuple's own -- its members have none
            # (``{name:"Tx/Rx BPDU\'s", c:[{id:'ue0'},{id:'ue1'}]}``) -- so a
            # member only has to exist, not to be showable in its own right.
            codes = tuple(
                c for c in (decl.get("codes") or [])
                if c in menu.fields and not menu.fields[c].nonpublic
            )
            if codes and name:
                col = Column(
                    name=name, kind="tuple", codes=codes,
                    sep=str(decl.get("sep") or " / "), width=width,
                )
        elif kind == "rate" and name:
            # `value:` names the counter by its display name, the same way a
            # columnalias points at a field. No counter, no column: an empty
            # one is worse than one the header menu does not list.
            src = _column_field(menu, str(decl.get("of") or ""))
            if src is not None:
                col = Column(
                    name=name, kind="rate", width=width,
                    rate_code=src.code,
                    rate_bits=bool(decl.get("bits")),
                    postfix=str(decl.get("postfix") or ""),
                    # Same rule as every other column: a `width` is what makes
                    # one visible from the start. Hotspot ▸ Active declares 50
                    # for its Rx/Tx Rate and Winbox shows them; a firewall
                    # rule's Packet Rate declares none and lives in the header
                    # menu until asked for.
                    default=width is not None,
                )
        elif kind == "graph" and name:
            fld = menu.fields.get(int(decl["code"]))
            if fld is not None:
                col = Column(
                    name=name, kind="graph", field=fld, width=width,
                    graph_max=_as_int(decl.get("max")),
                    graph_colors=tuple(decl.get("colors") or ()),
                )
        elif kind == "objtype" and name:
            col = Column(name=name, kind="objtype", width=width)
        elif kind == "lookup" and name:
            col = Column(
                name=name, kind="lookup", width=width,
                lookup_path=decl.get("path"),
                lookup_src=str(decl.get("src") or ""),
                lookup_dst=str(decl.get("dst") or ""),
                lookup_val=str(decl.get("val") or ""),
            )
        # One column per name: a menu may declare a field twice (once per UI
        # mode, gated on a `cond`) and each declaration carries the width.
        if col is None or col.name in seen:
            continue
        seen.add(col.name)
        out.append(col)
    return out


def list_columns(menu: Menu) -> list[Column]:
    """Every column the list can show, the .jg's default set first.

    The tail is what the header's column menu offers. Carrying only the
    default set left no way to add Dst. Port or To Address to a firewall list
    -- the data was never decoded that far.
    """
    chosen = default_columns(menu) or [
        Column(name=f.name, field=f, width=f.column_width)
        for f in preferred_columns(menu)
    ]
    seen_codes = {
        c for col in chosen
        for c in (col.codes or ((col.code,) if col.code is not None else ()))
    }
    seen_names = {col.name for col in chosen}
    rest = [
        Column(name=f.name, field=f, width=f.column_width, default=False)
        for f in menu.fields.values()
        if f.code not in seen_codes and f.name not in seen_names
        and _is_showable(f)
    ]
    return chosen + rest


def default_column_names(menu: Menu) -> list[str]:
    """Names of the columns a fresh list shows, in order."""
    return [c.name for c in list_columns(menu) if c.default]


def default_column_widths(menu: Menu) -> dict[str, int]:
    """Column name → the pixel width the .jg gives it, where it gives one."""
    return {
        c.name: int(c.width)
        for c in list_columns(menu)
        if isinstance(c.width, int) and c.width > 0
    }


def all_columns(menu: Menu, limit: int = 8) -> list[Field]:
    """Every field that can be shown as a column, default-visible ones first.

    The field-backed view of :func:`list_columns`, kept for callers that only
    want Fields (an enum prefetch, a secrecy lookup). A combined-tuple or
    ``objtype`` column has no single field behind it and is not in here.
    """
    seen: set[int] = set()
    out: list[Field] = []
    for col in list_columns(menu):
        if col.field is None or col.field.code in seen:
            continue
        seen.add(col.field.code)
        out.append(col.field)
    return out


def preferred_columns(menu: Menu, limit: int = 8) -> list[Field]:
    """Pick a dense, useful column set for a list table.

    The .jg's own answer when the menu declares one (see
    :func:`default_columns`); otherwise the old name-priority heuristic, which
    is all a menu that declares no widths at all can offer.
    """
    declared = [c.field for c in default_columns(menu) if c.field is not None]
    if declared:
        return declared
    fields = [
        f for f in menu.fields.values()
        if not f.nonpublic and f.name and f.jg_type not in ("flag",)
    ]
    # Order: priority names, then declaration order
    prio = {n: i for i, n in enumerate(_COLUMN_PRIORITY)}

    def sort_key(f: Field) -> tuple[int, int]:
        key = f.name.lower()
        return (prio.get(key, 1000), list(menu.fields.keys()).index(f.code))

    fields_sorted = sorted(fields, key=sort_key)
    # Prefer at least one identity-ish column
    chosen: list[Field] = []
    seen: set[int] = set()
    for f in fields_sorted:
        if f.code in seen:
            continue
        chosen.append(f)
        seen.add(f.code)
        if len(chosen) >= limit:
            break
    if not chosen:
        # fall back to first few fields
        for f in menu.fields.values():
            if f.code not in seen:
                chosen.append(f)
                seen.add(f.code)
            if len(chosen) >= limit:
                break
    return chosen


def _generic_paths(
    menus: list["Menu"], overlays: list["Menu"]
) -> dict[str, tuple[int, ...]]:
    """family name → the handler path its members read.

    Path-backed menus declare most families ('iface' is Interface at (20,0)),
    but an overlay can declare one too: ``WiFi Interfaces`` is generic 'wlan'
    inheriting 'iface', and ``Wireless (Hardware)`` is 'athhw' inheriting
    'ath'. Those read whatever handler they inherit, so they are resolved
    transitively -- otherwise Wireless ▸ Nstreme Dual (``inherit:'wlan'``)
    resolves to no path at all and drops out of the nav.
    """
    out: dict[str, tuple[int, ...]] = {}
    for menu in menus:
        if menu.generic and menu.kind in ("map", "item") and menu.path:
            out.setdefault(menu.generic, menu.path)
    pending = [ov for ov in overlays if ov.generic and ov.inherit]
    changed = True
    while changed:
        changed = False
        for ov in pending:
            if ov.generic in out:
                continue
            parent = out.get(ov.inherit or "")
            if parent is not None:
                out[ov.generic] = parent
                changed = True
    return out


def _chain_slot_value(
    base: Menu, chain: list[Menu], slot: str, values: dict[int, Any]
) -> int | None:
    """The row's value for a named slot ('hwtype'), looked up across a chain.

    The slot that picks the next overlay is declared by the overlay above it,
    so the field only exists once part of the chain is merged.
    """
    want = slot.strip().lower()
    for menu in (*reversed(chain), base):
        for code, fld in menu.fields.items():
            if (fld.name or "").strip().lower() != want:
                continue
            val = values.get(code)
            if isinstance(val, int):
                return int(val)
    return None


def _chain_tab_rank(base: "Menu", chain: list["Menu"]) -> dict[str, int]:
    """Tab order for a base plus a whole overlay chain.

    Same rule as :func:`_merged_tab_rank` -- type tabs go after General and
    before the generic's Status/Traffic -- but computed once for the chain, so
    a deeper overlay's tabs queue up behind the shallower one's instead of
    jumping ahead of them (a wireless card reads General, Wireless, HT, WDS,
    …, not General, HT, …, Wireless).
    """
    base_tabs = base.tab_names()
    extra: list[str] = []
    for overlay in chain:
        for tab in overlay.tab_names():
            if tab not in base_tabs and tab not in extra:
                extra.append(tab)
    ordered = extra if not base_tabs else [base_tabs[0], *extra, *base_tabs[1:]]
    return {tab: i for i, tab in enumerate(ordered)}


def _merge_overlay_into(
    base: Menu, overlay: Menu, tab_rank: dict[str, int] | None = None
) -> Menu:
    """``base`` with one type overlay's fields, tabs and chrome folded in.

    ``tab_rank`` overrides the pairwise tab order, so a chain of overlays can
    be folded one at a time against a single ordering.
    """
    fields: dict[int, Field] = dict(base.fields)
    for code, fld in overlay.fields.items():
        if code in fields:
            # Overlay wins for same id (Ethernet overrides MTU presentation).
            keep = fld
            other = fields[code]
            keep.alt_prefixes |= {other.prefix} | other.alt_prefixes
            keep.alt_prefixes.discard(keep.prefix)
            for pfx, var in other.variants.items():
                keep.variants.setdefault(pfx, var)
            if other.prefix != keep.prefix:
                keep.variants.setdefault(other.prefix, other)
            if not keep.name:
                keep.name = other.name
            if keep.enum is None:
                keep.enum = other.enum
            if keep.enum_path is None:
                keep.enum_path = other.enum_path
                keep.enum_pred = other.enum_pred
            fields[code] = keep
        else:
            fields[code] = fld

    seen: set[int] = set()
    form_order: list[tuple[str, int]] = []
    for tab, code in base.form_order:
        if code in seen:
            continue
        form_order.append((tab, code))
        seen.add(code)
    for tab, code in overlay.form_order:
        if code in seen:
            continue
        form_order.append((tab, code))
        seen.add(code)

    # Declarations follow form_layout's rule below: the base owns any code it
    # declares, and the overlay keeps each of its own wire types for the rest.
    base_decl_codes = {c for _t, c, _p in base.form_decls}
    form_decls: list[tuple[str, int, str]] = list(base.form_decls)
    seen_decls = {(c, p) for _t, c, p in base.form_decls}
    for tab, code, prefix in overlay.form_decls:
        if code in base_decl_codes or (code, prefix) in seen_decls:
            continue
        form_decls.append((tab, code, prefix))
        seen_decls.add((code, prefix))

    # Merge form_layout: base first, then overlay rows whose codes are new
    # (or chrome with no codes). Tuple/field codes already in base are skipped.
    form_layout: list[dict[str, Any]] = list(base.form_layout or [])
    base_codes = {
        c
        for item in form_layout
        for c in (
            item.get("codes") or ([item["code"]] if item.get("kind") == "field" else [])
        )
    }
    has_objtype = any(
        it.get("kind") == "objtype" for it in form_layout
    )
    # Within the overlay a code may be declared more than once (PoE Out
    # Status and Tx Bytes are both 0x469): those are separate rows, so the
    # overlay's own dedupe is per (code, wire type), not per code. Against the
    # base it stays per code -- there the overlay is overriding, not adding.
    overlay_seen: set[tuple[int, Any]] = set()
    for item in overlay.form_layout or []:
        kind = item.get("kind")
        if kind == "objtype":
            # Bridge re-declares Type with override:1. One row, in the base's
            # position -- a second one would just say the same thing twice.
            if has_objtype:
                continue
            form_layout.append(dict(item))
            has_objtype = True
            continue
        if kind in ("separator", "graphbox"):
            form_layout.append(dict(item))
            continue
        if kind == "field":
            code = item.get("code")
            key = (code, item.get("prefix"))
            if code in base_codes or key in overlay_seen:
                continue
            form_layout.append(dict(item))
            overlay_seen.add(key)
        elif kind == "tuple":
            pairs = list(zip(
                item.get("codes") or [],
                (item.get("prefixes") or []) + [None] * len(item.get("codes") or []),
            ))
            pairs = [
                (c, p) for c, p in pairs
                if c not in base_codes and (c, p) not in overlay_seen
            ]
            if not pairs:
                continue
            row = dict(item)
            row["codes"] = [c for c, _p in pairs]
            row["prefixes"] = [p for _c, p in pairs]
            form_layout.append(row)
            overlay_seen.update(pairs)

    # Columns merge like form_layout: the base owns the position of anything
    # it declares, the overlay contributes its own (Ethernet's Rate, a route's
    # Distance) and wins on width where both declare one.
    column_decls: list[dict[str, Any]] = [dict(d) for d in base.column_decls or []]
    by_key = {
        (d.get("kind"), d.get("code"), str(d.get("name") or "").lower()): d
        for d in column_decls
    }
    for decl in overlay.column_decls or []:
        key = (decl.get("kind"), decl.get("code"), str(decl.get("name") or "").lower())
        prev = by_key.get(key)
        if prev is not None:
            if decl.get("width") is not None:
                prev["width"] = decl["width"]
            continue
        row = dict(decl)
        column_decls.append(row)
        by_key[key] = row

    conditions = dict(base.conditions or {})
    conditions.update(overlay.conditions or {})
    tab_conditions = dict(base.tab_conditions or {})
    tab_conditions.update(overlay.tab_conditions or {})
    statusbar = list(base.statusbar or [])
    seen_sb = _statusbar_codes(statusbar)
    for item in overlay.statusbar or []:
        item_codes = _statusbar_codes([item])
        if item_codes and item_codes <= seen_sb:
            continue
        statusbar.append(item)
        seen_sb |= item_codes
    statusbar_meta = dict(base.statusbar_meta or {})
    statusbar_meta.update(overlay.statusbar_meta or {})
    has_enable = bool(base.has_enable or overlay.has_enable)
    # The base declares them and the type inherits: Route's A/C/S/d sit on
    # All Routes, and the IPv4 Routes overlay the list is drawn with has none.
    numflags = list(base.numflags) + [
        nf for nf in overlay.numflags
        if nf["code"] not in {b["code"] for b in base.numflags}
    ]
    has_comment = bool(base.has_comment or overlay.has_comment)
    has_preset = bool(base.has_preset or overlay.has_preset)
    # The mode button is declared on the overlay that owns the advanced fields
    # (the wireless hardware overlay), not on the generic interface base.
    mode_toggle = overlay.mode_toggle or base.mode_toggle
    defaults_code = overlay.defaults_code or base.defaults_code
    # The overlay is the concrete list being shown, so its tree wins: the base
    # Interface map declares none and every sub-interface type declares one.
    tree_on = overlay.tree_on or base.tree_on
    tab_priority = {**(base.tab_priority or {}), **(overlay.tab_priority or {})}
    icon_on = overlay.icon_on or base.icon_on
    icon_map = {**(base.icon_map or {}), **(overlay.icon_map or {})}
    highlight_on = overlay.highlight_on or base.highlight_on
    max_objects = overlay.max_objects or base.max_objects
    max_objects_message = (
        overlay.max_objects_message or base.max_objects_message
    )
    # An overlay is the concrete thing being shown, so its own `nonaddable`
    # decides -- and the proof that the base's does not carry into it is that
    # the .jg bothers to declare it on the overlays too. Ethernet, LTE, W60G,
    # PWR, Loopback, VRF each say `nonaddable` for themselves, which would be
    # dead weight if inheriting from the nonaddable iface base were enough.
    # AND-ing the two put a dead + on the 22 iface overlays that declare
    # nothing -- 36 of the 43 concrete types, counting the ppp ones that
    # inherit through it: Bridge, Bonding, VLAN, VXLAN, VETH, VRRP, VPLS,
    # MACVLAN, MACsec, Mesh, WireGuard, WiFi, CAP, every tunnel and every ppp
    # client. Lists whose entire purpose is that you add to them. Winbox draws
    # the + on exactly the overlays that do not declare the flag.
    addable = overlay.addable
    movable = overlay.movable and base.movable
    removable_declared = overlay.removable_declared or base.removable_declared
    full_title = overlay.full_title or base.full_title
    list_filters = list(base.list_filters or []) or list(overlay.list_filters or [])
    context_actions = list(base.context_actions or [])
    seen_ctx = {(a.get("name"), a.get("open")) for a in context_actions}
    for act in overlay.context_actions or []:
        if (act.get("name"), act.get("open")) not in seen_ctx:
            context_actions.append(dict(act))
            seen_ctx.add((act.get("name"), act.get("open")))
    form_actions = list(base.form_actions or [])
    seen_act = {(a.get("kind"), a.get("name"), a.get("cmd")) for a in form_actions}
    for act in overlay.form_actions or []:
        key = (act.get("kind"), act.get("name"), act.get("cmd"))
        if key not in seen_act:
            form_actions.append(dict(act))
            seen_act.add(key)
    form_layout, form_order, form_decls = _strip_statusbar_from_form(
        form_layout, form_order, _statusbar_codes(statusbar), form_decls,
        _statusbar_decls(statusbar),
    )
    if tab_rank is None:
        tab_rank = _merged_tab_rank(base, overlay)
    form_order = _sort_rows_by_tab(form_order, tab_rank, lambda r: r[0])
    form_decls = _sort_rows_by_tab(form_decls, tab_rank, lambda r: r[0])
    form_layout = _sort_rows_by_tab(
        form_layout, tab_rank, lambda r: str(r.get("tab") or "General")
    )

    return Menu(
        path=base.path,
        name=base.name,
        title=base.title,
        kind=base.kind,
        group=base.group,
        # The default-name stem (vlan, eoip-tunnel, …) lives on the type
        # overlay, not the generic iface base -- keep it so a new row can be
        # prefilled "vlan1" like Winbox.
        cli_prefix=overlay.cli_prefix or base.cli_prefix,
        nameval=base.nameval,
        typeon=base.typeon,
        refresh_filter=base.refresh_filter,
        command=base.command,
        write_perm=base.write_perm,
        source=base.source,
        fields=fields,
        form_order=form_order,
        form_decls=form_decls,
        form_layout=form_layout,
        column_decls=column_decls,
        ordered=bool(base.ordered or overlay.ordered),
        conditions=conditions,
        tab_conditions=tab_conditions,
        statusbar=statusbar,
        statusbar_meta=statusbar_meta,
        has_enable=has_enable,
        has_comment=has_comment,
        has_preset=has_preset,
        mode_toggle=mode_toggle,
        defaults_code=defaults_code,
        addable=addable,
        movable=movable,
        tree_on=tree_on,
        tab_priority=tab_priority,
        icon_on=icon_on,
        icon_map=icon_map,
        highlight_on=highlight_on,
        max_objects=max_objects,
        max_objects_message=max_objects_message,
        removable_declared=removable_declared,
        full_title=full_title,
        context_actions=context_actions,
        numflags=numflags,
        list_filters=list_filters,
        form_actions=form_actions,
        generic=base.generic,
        inherit=overlay.inherit,
        typevalue=overlay.typevalue,
        # Deepest overlay wins: a wireless card folds 'WiFi Interfaces' then
        # the hardware overlay, and the hardware one is the type Winbox names.
        type_title=overlay.title or base.type_title,
        # Everything below used to be left off this call, which does not mean
        # "keep the base's" -- it means the dataclass default, so the merge
        # threw the base's answer away. That is how a type-filtered interface
        # window lost its refresh rate: the Interface menu declares
        # autorefresh 1000, PPP ▸ Interface (type 73) merges an overlay that
        # declares none, and the merged menu came back with None, so the list
        # fell through to the 5s age-field fallback and its counters moved
        # every five seconds instead of every second. Same rule as the rest of
        # this call: the overlay wins where it declares, else the base stands.
        autorefresh_ms=overlay.autorefresh_ms or base.autorefresh_ms,
        uptime_field=overlay.uptime_field or base.uptime_field,
        auto_limit=overlay.auto_limit or base.auto_limit,
        auto_scroll=bool(overlay.auto_scroll or base.auto_scroll),
        start_command=overlay.start_command or base.start_command,
        cancel_command=overlay.cancel_command or base.cancel_command,
        auto_close=bool(overlay.auto_close or base.auto_close),
        poll_command=overlay.poll_command or base.poll_command,
        request_fields=overlay.request_fields or base.request_fields,
        request_order=overlay.request_order or base.request_order,
        request_layout=overlay.request_layout or base.request_layout,
        request_decls=overlay.request_decls or base.request_decls,
        # Either end may hide it: the base hides the menu, an overlay hides
        # one type of it (ppp.jg marks the dynamic inbound types nonpublic,
        # wlan6.jg its 20 hardware ones). Neither can say the opposite, since
        # the .jg never writes a zero, so `or` is the whole rule.
        nonpublic=bool(overlay.nonpublic or base.nonpublic),
        # Same rule, same reason: the .jg never writes a zero, so neither end
        # can un-say the other's `ro`.
        read_only=bool(overlay.read_only or base.read_only),
    )


class Schema:
    """All menus from one Winbox plugin-cache directory."""

    def __init__(
        self,
        menus: list[Menu],
        version: str = "",
        nav: list[NavNode] | None = None,
        directory: str = "",
        overlays: list[Menu] | None = None,
        dialogs: list[Menu] | None = None,
    ) -> None:
        #: Path-less dialogs, by title (see _dialogs_from_jg).
        self.dialogs: dict[str, Menu] = {d.title: d for d in (dialogs or [])}
        self.menus = menus
        self.version = version
        self.directory = directory
        self.nav: list[NavNode] = list(nav or [])
        # Every overlay in load order, including the ones that collide below.
        self.overlay_list: list[Menu] = list(overlays or [])
        self._build_indexes()

    def __getstate__(self) -> dict[str, Any]:
        """Only the parsed .jg data; every index is derived (see load()).

        One of them -- ``_overlay_key`` -- is keyed on ``id()``, which does not
        survive a round trip: carrying it would silently collapse every overlay
        chain onto one cache entry.
        """
        return {
            "menus": self.menus,
            "version": self.version,
            "directory": self.directory,
            "nav": self.nav,
            "overlay_list": self.overlay_list,
            "dialogs": self.dialogs,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.menus = state["menus"]
        self.version = state.get("version", "")
        self.directory = state.get("directory", "")
        self.nav = state.get("nav") or []
        self.overlay_list = state.get("overlay_list") or []
        self.dialogs = state.get("dialogs") or {}
        self._build_indexes()

    def _build_indexes(self) -> None:
        """(Re)build every lookup derived from menus + overlays."""
        menus = self.menus
        self.by_path: dict[tuple[int, ...], list[Menu]] = {}
        for menu in menus:
            self.by_path.setdefault(menu.path, []).append(menu)
        # inherit name → typevalue → overlay menu (Ethernet, Bridge, VLAN…)
        self.overlays: dict[str, dict[int, Menu]] = {}
        for ov in self.overlay_list:
            if not ov.inherit or ov.typevalue is None:
                continue
            slot = self.overlays.setdefault(ov.inherit, {})
            prev = slot.get(ov.typevalue)
            # typevalue 0xffffffff is declared twice under inherit:'iface'
            # (ppp.jg's generic:'ppp' and wlan6.jg's generic:'wlan'), so pick
            # deterministically instead of letting file order decide.
            if prev is None or len(ov.fields) > len(prev.fields):
                slot[ov.typevalue] = ov
        # inherit name → every overlay declared under it, in load order. The
        # flat map above cannot express the tree: an interface's type picks
        # 'wlan', and 'wlan' has children of its own picked on a second slot.
        self.overlay_children: dict[str, list[Menu]] = {}
        for ov in self.overlay_list:
            if ov.inherit:
                self.overlay_children.setdefault(ov.inherit, []).append(ov)
        # family name → the overlay that declares it ('wlan' → WiFi Interfaces).
        self.overlay_by_generic: dict[str, Menu] = {}
        for ov in self.overlay_list:
            if ov.generic:
                self.overlay_by_generic.setdefault(ov.generic, ov)
        # Merged (base + chain) menus, keyed on the chain rather than on a
        # type value: decode() runs per row, and a wireless interface folds
        # four overlays, so a 40-port device would rebuild ~160 menus per list
        # read otherwise.
        self._overlay_key: dict[int, int] = {
            id(ov): i for i, ov in enumerate(self.overlay_list)
        }
        self._merged_cache: dict[tuple, Menu] = {}
        self._other_dialog_cache: dict[tuple, frozenset[int]] = {}
        self._family_secrets: dict[str, set[int]] = {}
        # generic family name → the path of the menu that defines it, so a
        # path-less overlay can be resolved to the handler it reads from
        # ('iface' → (20,0)).
        self.generic_paths: dict[str, tuple[int, ...]] = _generic_paths(
            menus, self.overlay_list
        )

    @classmethod
    def load(cls, directory: str, *, cache: bool = True) -> "Schema":
        """Parse a plugin-cache directory, reusing a prebuilt copy when there
        is one.

        Parsing is the single most expensive thing a connect does — a 7.21
        set is about 1.2 MB of JavaScript object literal and roughly half a
        second of Python — and it produces the same answer every time for a
        given firmware build. So the result is kept alongside the .jg files it
        came from and reused until they change (see :func:`load_schema_cache`).

        ``cache=False`` parses unconditionally and writes nothing.
        """
        if cache:
            hit = load_schema_cache(directory)
            if hit is not None:
                return hit
        schema = cls.parse(directory)
        if cache:
            store_schema_cache(directory, schema)
        return schema

    @classmethod
    def parse(cls, directory: str) -> "Schema":
        """Build a schema by reading every .jg in a directory."""
        files = sorted(glob.glob(os.path.join(directory, "*.jg")))
        files += sorted(glob.glob(os.path.join(directory, "*.jg.gz")))
        if not files:
            raise FileNotFoundError(f"no .jg files in {directory!r}")
        menus: list[Menu] = []
        overlays: list[Menu] = []
        dialogs: list[Menu] = []
        nav: list[NavNode] = []
        for f in files:
            root = jg.load(f)
            base = os.path.basename(f)
            _walk(root, None, base, menus, overlays=overlays)
            dialogs.extend(_dialogs_from_jg(root, base))
            nav.extend(_nav_top_level(root, base))
        generic_paths = _generic_paths(menus, overlays)
        nav = _resolve_nav_overlays(nav, generic_paths)
        nav = _dedupe_nav(nav)
        # Winbox orders the left menu on the .jg prio key.
        nav = _order_nav(nav)
        return cls(
            menus,
            version=os.path.basename(os.path.normpath(directory)),
            nav=nav,
            directory=directory,
            overlays=overlays,
            dialogs=dialogs,
        )

    def wizards_for(self, path: tuple[int, ...]) -> list[Menu]:
        """The ``type:setup`` wizards that belong on the window for ``path``.

        A wizard is not a menu you browse to: Winbox puts DHCP Setup on the
        DHCP Server window and Hotspot Setup on the Hotspot one. The .jg says
        which by the handler the wizard writes to -- (23,0) for DHCP, (63) for
        Hotspot -- so a window offers every wizard whose path is a prefix of
        one of its own.
        """
        target = tuple(path or ())
        if not target:
            return []
        return [
            d for d in self.dialogs.values()
            if d.kind == "setup" and d.path
            and target[:len(d.path)] == tuple(d.path)
        ]

    @property
    def field_count(self) -> int:
        return sum(len(x.fields) for x in self.menus)

    def record(
        self, path: tuple[int, ...], *, name: str | None = None
    ) -> Menu | None:
        """The map/item menu for a path -- the one whose fields a GET/GETALL returns.

        A path is not always one menu. Handler ``(20, 17)`` is a single
        settings object that six unrelated dialogs each show a slice of -- IP
        Settings, IPv6 Settings, Bridge Settings, Connection Tracking,
        Discovery Settings, L3 Hw Settings -- and their field sets are
        disjoint, so guessing picks the wrong form outright. ``name`` is the
        `.jg` ``name`` of the menu the caller means (the nav node carries it);
        it is unique within a path where the titles are not, which is why
        Bridge's "Settings" and IP's "Settings" can be told apart at all.

        Without a ``name``, or when nothing at this path answers to it, the
        largest field set wins -- the old behaviour, and still the only thing
        available to a caller that reached a path without going through the
        nav.
        """
        candidates = [
            menu for menu in self.by_path.get(path, [])
            if menu.kind in ("map", "item")
        ]
        if name:
            named = [menu for menu in candidates if menu.name == name]
            if named:
                candidates = named
        best = None
        for menu in candidates:
            if best is None or len(menu.fields) > len(best.fields):
                best = menu
        return best

    def command_menu(
        self,
        path: tuple[int, ...],
        kind: str | None = None,
        title: str | None = None,
    ) -> Menu | None:
        """The query/action/doit menu at a path.

        A path can host several: (20,61) has three queries and (119,1) has an
        item, an action and a query, so ``kind`` and ``title`` disambiguate.
        """
        best = None
        for menu in self.by_path.get(path, []):
            if menu.kind not in _COMMAND_TYPES:
                continue
            if kind is not None and menu.kind != kind:
                continue
            if title is not None and menu.title != title:
                continue
            if best is None or len(menu.fields) > len(best.fields):
                best = menu
        return best

    def record_for(
        self,
        path: tuple[int, ...],
        type_value: int | None = None,
        values: dict[int, Any] | None = None,
        *,
        name: str | None = None,
    ) -> Menu | None:
        """Base menu for ``path``, merged with a type overlay when applicable.

        Example: Interface ``(20, 0)`` has ``generic='iface'``; Ethernet ports
        have wire type ``1`` and pick up Rate / Full Duplex / Auto Negotiation
        from the Ethernet overlay's Status tab.

        ``values`` is the object's wire row. Overlays nest, and the deeper
        levels key on a *different* slot than the first: a wireless interface
        is type 35 ('ath'), and which Atheros form it gets is then decided by
        its ``hwtype``. Without the row those levels cannot be resolved, so
        the chain stops at the last one the type alone can reach.

        ``name`` picks between menus sharing the path (see :meth:`record`).
        """
        base = self.record(path, name=name)
        if base is None:
            return None
        return self.merge_type_overlay(base, type_value, values)

    def other_dialog_codes(self, path: tuple[int, ...],
                           menu: "Menu | None") -> frozenset[int]:
        """Codes at *path* that belong to a different dialog than ``menu``.

        A settings handler can be one object that several unrelated dialogs
        each read a slice of: (20,17) answers a bare GET with all of IP
        Settings, IPv6 Settings, Connection Tracking, Discovery, L3 Hw *and*
        Bridge Settings -- 129 codes for a Bridge Settings form that declares
        nine of them, and Winbox draws only those nine. The other 120 are not
        wire leftovers to be surfaced; they are another window's fields, and
        showing them here is the same category error as decoding the reply
        against the wrong menu in the first place.

        Only for ``item`` menus -- a single-object dialog. The overlays sharing
        a map's path (37 interface types on (20,0)) are the same list seen
        through different types, where a field the current overlay does not
        declare is worth seeing rather than hiding.
        """
        if menu is None or menu.kind != "item":
            return frozenset()
        key = (tuple(path), menu.name, menu.title)
        hit = self._other_dialog_cache.get(key)
        if hit is not None:
            return hit
        mine = set(menu.fields)
        theirs: set[int] = set()
        for other in self.by_path.get(tuple(path), []):
            if other is menu or other.kind not in ("item", "map"):
                continue
            if (other.name, other.title) == (menu.name, menu.title):
                continue
            theirs |= set(other.fields)
        out = frozenset(theirs - mine)
        self._other_dialog_cache[key] = out
        return out

    def family_secret_codes(self, generic: str) -> set[int]:
        """Every wire code that carries a secret in *some* subtype of a family.

        :meth:`record_for` needs the row's type to say which overlay applies,
        and a write does not carry one -- a SETOBJ holds the fields being
        written and nothing else. The code a passphrase arrives under is the
        same code whichever subtype owns it, though, so a caller that must not
        let a secret past (the session transcript) can fall back to the family:
        every subtype's secret at once, opt-wrapped leaves included.
        """
        hit = self._family_secrets.get(generic)
        if hit is not None:
            return hit
        out: set[int] = set()
        pending, seen = [generic], set()
        while pending:
            family = pending.pop()
            if not family or family in seen:
                continue
            seen.add(family)
            for overlay in (self.overlays.get(family) or {}).values():
                if overlay.generic:
                    pending.append(overlay.generic)
                for code, fld in (overlay.fields or {}).items():
                    leaf = secret_leaf(fld)
                    if leaf is not None:
                        out.update((code, leaf.code))
        self._family_secrets[generic] = out
        return out

    def record_for_family(
        self, path: tuple[int, ...], generic: str, *, name: str | None = None
    ) -> Menu | None:
        """Base menu for ``path`` merged with the overlay that *declares* a
        family, e.g. 'wlan' for Wireless ▸ WiFi Interfaces.

        The family base is not a type of its own -- ``typevalue`` is
        0xffffffff and both 'wlan' and 'ppp' claim it -- so it is addressed by
        name rather than through :meth:`merge_type_overlay`.
        """
        base = self.record(path, name=name)
        if base is None:
            return None
        overlay = self.overlay_by_generic.get(generic)
        if overlay is None:
            return base
        return self._merged(base, [overlay])

    def family_type_values(self, generic: str) -> set[int]:
        """Every concrete wire type in a family, for filtering its list.

        Wireless ▸ WiFi Interfaces shows types {35, 36, 38} (ath, WDS, Nstreme
        Dual); PPP ▸ Interface shows the twelve tunnel bindings. Descends only
        through 0xffffffff members, which are namespaces on the same slot --
        an 'ath' child is keyed on ``hwtype``, not on the interface type, and
        would be a different number space.
        """
        return self._family_type_values(generic, set())

    def _family_type_values(self, generic: str, seen: set[str]) -> set[int]:
        if not generic or generic in seen:
            return set()
        seen.add(generic)
        out: set[int] = set()
        for kid in self.overlay_children.get(generic) or []:
            if kid.typevalue is None:
                continue
            if kid.typevalue == _TYPE_ANY:
                if kid.generic:
                    out |= self._family_type_values(kid.generic, seen)
            else:
                out.add(int(kid.typevalue))
        return out

    def _pick_overlays(
        self, generic: str, slot: str | None, value: int | None,
        seen: set[str] | None = None,
    ) -> list[Menu]:
        """Overlays to apply for ``value`` on ``slot``, outermost first.

        A 0xffffffff member is a namespace, not a type: 'iface' holds 'ppp'
        and 'wlan' under that one number and each keeps matching on the same
        slot one level down, which is how interface type 35 reaches 'ath'
        through 'wlan'. When exactly one such member exists it doubles as the
        family default (an Atheros card whose ``hwtype`` we do not recognise
        still gets the hardware form); when several do, the value belongs to
        no family we can tell apart and nothing is applied.
        """
        seen = set() if seen is None else seen
        if generic in seen:
            return []
        seen.add(generic)
        kids = self.overlay_children.get(generic) or []
        if not kids:
            return []
        if value is not None:
            hit = None
            for kid in kids:
                if kid.typevalue == value and (
                    hit is None or len(kid.fields) > len(hit.fields)
                ):
                    hit = kid
            if hit is not None:
                return [hit]
        wild = [k for k in kids if k.typevalue == _TYPE_ANY]
        for kid in wild:
            if not kid.generic:
                continue
            sub_slot = kid.typeon if isinstance(kid.typeon, str) else slot
            if sub_slot != slot:
                continue
            sub = self._pick_overlays(kid.generic, slot, value, seen)
            if sub:
                return [kid] + sub
        return wild[:1] if len(wild) == 1 else []

    def overlay_chain(
        self,
        base: Menu,
        type_value: int | None,
        values: dict[int, Any] | None = None,
    ) -> list[Menu]:
        """The overlays that apply to one object, in merge order."""
        if not base.generic:
            return []
        chain: list[Menu] = []
        node = base
        slot = base.typeon if isinstance(base.typeon, str) else None
        value = None if type_value is None else int(type_value)
        for _ in range(len(self.overlay_children) + 1):
            if not node.generic:
                break
            picked = self._pick_overlays(node.generic, slot, value)
            if not picked:
                break
            chain.extend(picked)
            node = picked[-1]
            nxt = node.typeon if isinstance(node.typeon, str) else None
            if nxt is None or nxt == slot or values is None:
                break
            slot = nxt
            value = _chain_slot_value(base, chain, nxt, values)
        return chain

    def type_title_for(
        self,
        base: Menu,
        type_value: int | None,
        values: dict[int, Any] | None = None,
    ) -> str:
        """The object's type in words -- what a ``type:objtype`` column shows.

        The deepest overlay that applies is the one Winbox names: a wireless
        card folds 'WiFi Interfaces' and then the hardware overlay, and reads
        'Wireless'. Empty when no overlay claims the value.
        """
        if type_value is None or not base.generic:
            return ""
        chain = self.overlay_chain(base, type_value, values)
        return chain[-1].title if chain else ""

    def merge_type_overlay(
        self,
        base: Menu,
        type_value: int | None,
        values: dict[int, Any] | None = None,
    ) -> Menu:
        """Return ``base`` plus fields/tabs from every overlay that applies."""
        if type_value is None or not base.generic:
            return base
        return self._merged(base, self.overlay_chain(base, type_value, values))

    def _merged(self, base: Menu, chain: list[Menu]) -> Menu:
        """``base`` folded with a chain of overlays, memoised on the chain.

        Only a base this schema owns is cached: ``id()`` is the cheap identity
        here, and a caller-built temporary could have its id reused after it is
        collected, which would hand back somebody else's menu.
        """
        if not chain:
            return base
        key = None
        ids = [self._overlay_key.get(id(o)) for o in chain]
        if self.record(base.path) is base and None not in ids:
            # An overlay this schema does not own has no stable identity, so
            # it does not get to share a cache slot with the ones that do.
            key = (base.path, tuple(ids))
            hit = self._merged_cache.get(key)
            if hit is not None:
                return hit
        tab_rank = _chain_tab_rank(base, chain)
        merged = base
        for overlay in chain:
            merged = _merge_overlay_into(merged, overlay, tab_rank)
        if key is not None:
            self._merged_cache[key] = merged
        return merged



    def records(self) -> list[Menu]:
        seen: set[tuple[int, ...]] = set()
        out = []
        for menu in sorted(self.menus, key=lambda x: x.path):
            if menu.kind in ("map", "item") and menu.path not in seen:
                rec = self.record(menu.path)
                if rec:
                    seen.add(menu.path)
                    out.append(rec)
        return out

    def find(self, needle: str) -> list[Menu]:
        """Menus whose name, title or CLI prefix contains `needle` (case-insensitive)."""
        n = needle.lower()
        return [
            x for x in self.menus
            if n in x.name.lower() or n in x.title.lower() or (x.cli_prefix and n in x.cli_prefix.lower())
        ]

    def decode(self, path: tuple[int, ...], msg: Any,
               resolver=None,
               uptime: float | None = None,
               menu: "Menu | None" = None) -> list[tuple[int, str, str, bool]]:
        """Annotate a reply's fields with schema names.

        Returns (code, name, rendered_value, type_ok) per non-system field.
        `name` falls back to a hex label when the schema does not know the field.

        `resolver(field, value)` may return a display string for values the
        schema can only describe indirectly -- a `values:{type:'dynamic'}` enum
        names its options by object id from another handler, which needs a live
        device to resolve. Returning None falls back to Field.render().

        `menu` names the declaration to read the reply against, for a frame
        that is not a record of the handler's own list: a progress poll on
        Make Supout.rif answers with the *command* menu's fields, and (24,25)
        has no record menu at all, so without it the percentage decodes as a
        bare `<0x00000001>`.
        """
        # The base record names the type field (`typeon`); the overlay is
        # then picked with the value that field actually carries. The row goes
        # in too: overlays nest, and the slot that picks the next one down is
        # a field of the row (a wireless card's `hwtype`), so without it every
        # field below the first overlay decodes as <0x…>.
        # Doubles as the `opt` sibling map: the device flattens opt/not/value
        # into separate codes on the same row (see _render_opt).
        others: dict[int, Any] = {}
        for code, _tag, value in msg:
            others.setdefault(code, value)
        if menu is None:
            base = self.record_for(path, None)
            menu = self.record_for(path, wire_type_value(msg, base), others)
        order = {code: i for i, code in enumerate(menu.fields)} if menu else {}
        # Fields of another dialog sharing this handler -- not ours to show.
        foreign = self.other_dialog_codes(path, menu)
        known, unknown = [], []
        for code, tag, value in msg:
            if not _is_payload(code, menu):
                continue
            fld = menu.fields.get(code) if menu else None
            if fld is None:
                if code in foreign:
                    continue
                unknown.append((code, f"<{code:#08x}>", repr(value), True))
            else:
                # One id can be two different fields, told apart by wire tag.
                fld = fld.for_tag(tag)
                known.append((order[code], code, fld.name or f"<{code:#08x}>",
                              _render_top(fld, value, resolver, uptime, others,
                                      menu),
                              fld.accepts(tag)))
        known.sort()
        return [(c, n, v, ok) for _i, c, n, v, ok in known] + sorted(unknown)

    def decode_form(
        self, path: tuple[int, ...], msg: Any, resolver=None,
        uptime: float | None = None, menu: "Menu | None" = None,
    ) -> list[tuple[str, int, str, str, bool]]:
        """Like :meth:`decode` but ordered by property-form tabs.

        Returns ``(tab, code, name, rendered_value, type_ok)``.

        - Schema form layout drives named tabs (DHCP / Advanced / Status…).
        - Named schema fields not on the form go under ``Extra``.
        - Wire fields the schema does not know (``<0x……>``) go under
          ``Unknown`` so they do not clutter General.
        - Type overlays (Ethernet / Bridge / …) are merged when the object
          carries an interface type id, so Status includes Rate, duplex, etc.
        """
        # The base record names the type field (`typeon`); the overlay is then
        # picked with the value that field actually carries, and the row is
        # what resolves the levels below it (see :meth:`decode`).
        slots: dict[int, Any] = {}
        for code, _tag, value in msg:
            slots.setdefault(code, value)
        if menu is None:
            base = self.record_for(path, None)
            menu = self.record_for(path, wire_type_value(msg, base), slots)
        by_code: dict[int, list[tuple[int, Any]]] = {}
        for code, tag, value in msg:
            if _is_payload(code, menu):
                by_code.setdefault(code, []).append((tag, value))

        if menu is None:
            return [
                ("Unknown", c, f"<{c:#08x}>", _render_unknown(v), True)
                for c, entries in sorted(by_code.items()) for _t, v in entries
            ]

        # Flat view of the row for `opt` matches, whose value sits under a
        # sibling code rather than their own (see _render_opt).
        flat: dict[int, Any] = {
            c: entries[0][1] for c, entries in by_code.items() if entries
        }

        # One id can be declared twice as different fields on different tabs
        # (Ethernet: u469 "PoE Out Status" on PoE, Q469 "Tx Bytes" on Overall
        # Stats). for_tag() already names the right one; this puts it on the
        # right tab too, instead of on whichever tab declared the id first.
        decl_tab = menu.decl_tabs()

        def rows(tab: str, code: int) -> list[tuple[str, int, str, str, bool]]:
            base = menu.fields.get(code)
            out = []
            for tag, value in by_code[code]:
                if base is None:
                    out.append((tab, code, f"<{code:#08x}>", _render_unknown(value), True))
                    continue
                fld = base.for_tag(tag)
                if fld.jg_type == "flag" and fld.hint:
                    # A flag is not a form row: it is a letter in the list's
                    # flags column and a word in the form's tray. Declaration
                    # rather than code, so a counter that merely shares an id
                    # with a flag keeps its row.
                    continue
                name = fld.name or f"<{code:#08x}>"
                # Unnamed schema fields still count as unknown for UI purposes
                use_tab = decl_tab.get((code, fld.prefix), tab)
                if name.startswith("<0x"):
                    use_tab = "Unknown"
                out.append((use_tab, code, name,
                            _render_top(fld, value, resolver, uptime, flat,
                                    menu),
                            fld.accepts(tag)))
            return out

        # Tray / enable chrome from the template — never Extra form rows.
        chrome = menu.form_chrome_codes()

        out: list[tuple[str, int, str, str, bool]] = []
        seen: set[int] = set()
        for tab, code in menu.form_order:
            if code not in by_code or code in seen:
                continue
            if code in chrome:
                seen.add(code)
                continue
            seen.add(code)
            out += rows(tab, code)
        foreign = self.other_dialog_codes(path, menu)
        for code in sorted(by_code):
            if code in seen or code in chrome:
                continue
            base = menu.fields.get(code)
            if base is None:
                if code in foreign:
                    # Another dialog at this path declares it. Winbox shows it
                    # in that window, not this one.
                    continue
                out += rows("Unknown", code)
            elif not base.name:
                out += rows("Unknown", code)
            else:
                out += rows("Extra", code)
        return out


def type_field_code(menu: "Menu | None") -> int | None:
    """The field code a menu's type overlays key on, per its own ``typeon``.

    Same reasoning as :func:`name_field_code`: the code is not predictable.
    Interface declares ``typeon:'type'`` and keeps it at 0x10001, while Route
    declares ``typeon:'rtype'`` and keeps it at 0x81 — reading interface codes
    off a route matches nothing, which is how IP → Routes came to filter every
    row away.
    """
    if menu is None or not isinstance(menu.typeon, str):
        return None
    want = menu.typeon
    for code, fld in menu.fields.items():
        if fld.name == want or (fld.name or "").strip().lower() == want.lower():
            return code
    return None


def wire_type_value(msg: Any, menu: "Menu | None" = None) -> int | None:
    """Object type id used for Winbox type overlays (Interface ``u10001``…).

    Accepts a Message, a RowObject or a code→value dict. ``menu`` lets the
    schema name the field (``typeon``) instead of guessing at codes.
    """
    if msg is None:
        return None
    declared = type_field_code(menu)
    # Once the schema names the field, do not also try the fallbacks: a route's
    # 0x01 is a real field with a real integer in it, so guessing would answer
    # confidently with the wrong number rather than admit it does not know.
    codes = (declared,) if declared is not None else (0x10001, 0x01, 0x02, 0x03)
    for code in codes:
        try:
            hit = msg.find(code) if hasattr(msg, "find") else None
        except Exception:
            hit = None
        if hit is not None and isinstance(hit[1], int):
            return int(hit[1])
        if isinstance(msg, dict) and isinstance(msg.get(code), int):
            return int(msg[code])
    # RowObject-style values_by_code
    by = getattr(msg, "values_by_code", None)
    if isinstance(by, dict):
        for code in codes:
            if isinstance(by.get(code), int):
                return int(by[code])
    return None



def condition_is_true(
    menu: Menu | None,
    on_name: str | None,
    values_by_code: dict[int, Any] | None,
    *,
    board: str | None = None,
    ui_slots: dict[str, Any] | None = None,
    syscaps: frozenset[int] | set[int] | None = None,
) -> bool:
    """Whether a Winbox on: visibility condition currently holds.

    Real Winbox hides tabs like PoE / SFP when their named type:cond is false
    (e.g. sfp bool off, or caps missing the PoE bit). Unknown conditions
    default to True so we never hide chrome we cannot evaluate. Missing slot
    values for a known cond default to False (capability absent).

    ``ui_slots`` supplies values for slots that are not wire fields at all --
    the ``umode`` mode button (see :attr:`Menu.mode_toggle`). Without it every
    ``on:'adv'`` row evaluates False and the advanced half of a wireless
    interface is unreachable.
    """
    if not on_name:
        return True
    if menu is None:
        return True
    cond = (menu.conditions or {}).get(on_name)
    if cond is None:
        return True
    values = values_by_code or {}
    kids = cond.get("c")
    if kids is None:
        return True
    if isinstance(kids, dict):
        kids = [kids]
    if not isinstance(kids, list) or not kids:
        return True
    for kid in kids:
        if not isinstance(kid, dict):
            continue
        pred = kid.get("pred")
        slot = kid.get("on")
        slot_val = (
            _slot_value(menu, slot, values, ui_slots)
            if isinstance(slot, str)
            else None
        )
        if pred is None:
            if not bool(slot_val):
                return False
            continue
        if not _eval_pred(
            pred, slot_val, menu, values, board=board, ui_slots=ui_slots,
            syscaps=syscaps,
        ):
            return False
    return True


#: Array fields whose entries the router will not let overlap inside one object,
#: by name, within one menu.
#:
#: Nothing in the .jg says so. The router enforces it and refuses the write:
#: a bridge VLAN with one port in both lists answers
#: ``failure: interface cannot be in tagged and untagged at the same time``
#: -- on ``add`` and on ``set`` alike (verified on a 7.24 virtual router). Egress cannot
#: be both tagged and untagged for one VLAN, so it is not a state to reach.
#:
#: Only *within* one object: the same port tagged in one bridge VLAN row and
#: untagged in another is a trunk with a native VLAN, which is ordinary and
#: must keep working. And only between these names -- MVRP Forbidden draws on
#: the same interface table and is orthogonal to both.
EXCLUSIVE_ARRAY_NAMES: tuple[frozenset[str], ...] = (
    frozenset({"tagged", "untagged"}),
)


def exclusive_array_peers(menu: Menu | None, field: "Field | None") -> list["Field"]:
    """The editable array fields whose entries ``field`` must not repeat.

    Matched on the declaration, not on a hardcoded menu path: an editable
    ``multi`` field named in one :data:`EXCLUSIVE_ARRAY_NAMES` group, peered
    with the other editable fields of that group drawing on the *same*
    vocabulary. So Bridge ▸ VLANs pairs Tagged with Untagged and leaves
    Current Tagged out of it -- that one is ``ro``, a reading rather than a
    setting, and the two readings do overlap while a change is pending.
    """
    if menu is None or field is None:
        return []
    if not (field.jg_type or "").startswith("multi") or getattr(field, "ro", False):
        return []
    name = (field.name or "").strip().lower()
    group = next((g for g in EXCLUSIVE_ARRAY_NAMES if name in g), None)
    if group is None:
        return []
    out: list[Field] = []
    for code, other in (menu.fields or {}).items():
        if code == field.code or getattr(other, "ro", False):
            continue
        if (other.name or "").strip().lower() not in group:
            continue
        if not (other.jg_type or "").startswith("multi"):
            continue
        if other.enum_path != field.enum_path:
            continue        # a different table is a different question
        out.append(other)
    return out


def field_code_by_name(menu: Menu | None, name: str | None) -> int | None:
    """The wire code of the field with this name, or None.

    Names are how the .jg points across handlers -- a `type:filter`'s
    ``filteron``, a `queryenum`'s parameters, a `cond`'s slot.
    """
    if menu is None or not name:
        return None
    want = str(name).strip()
    for code, fld in (menu.fields or {}).items():
        if (fld.name or "").strip() == want:
            return code
    return None


def field_value_by_name(
    menu: Menu | None, name: str | None, values_by_code: dict[int, Any] | None
) -> Any:
    """This object's value for the field with this *name*.

    The .jg addresses a field by name wherever the two ends of a reference
    live in different handlers and so cannot share a code: a ``cond``'s ``on:``
    slot, and a ``queryenum``'s parameters -- Quick Set keeps Band at 0x259 and
    sends it to the frequency query as 0x81a, matched up by the name alone.
    """
    if menu is None or not name:
        return None
    return _slot_value(menu, str(name), values_by_code or {})


#: `.jg` types whose wire value is a *range list*: one flat number array
#: holding two elements per range, low then high. ``multinumberrange`` is the
#: growable one (a bridge VLAN's "VLAN IDs", one box per range in Winbox),
#: ``numberrangelist`` the single-box one (firewall "Dst. Port", QoS PCP/DSCP).
RANGE_LIST_TYPES = frozenset({"multinumberrange", "numberrangelist"})


def range_list_pairs(value: Any) -> list[tuple[int, int]] | None:
    """A range list's flat array split into its inclusive (low, high) pairs.

    Confirmed against firmware 7.23.3: a bridge VLAN whose ``vlan-ids`` is
    ``5,7-9,300`` sends ``U1 = [5, 5, 7, 9, 300, 300]``. A single VLAN is a
    range whose ends are equal, so ``100`` arrives as ``[100, 100]``.

    ``None`` when the array is not a whole number of pairs, or holds something
    that is not a number -- the caller then shows the elements as they are
    rather than inventing a range that is not there.
    """
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    if len(items) % 2:
        return None
    pairs: list[tuple[int, int]] = []
    for i in range(0, len(items), 2):
        low, high = _as_int(items[i]), _as_int(items[i + 1])
        if low is None or high is None:
            return None
        pairs.append((low, high))
    return pairs


def format_range_list(value: Any, fmt=str) -> str | None:
    """A range list the way Winbox shows it: ``5,7-9,300``.

    Equal ends are one number, not ``5-5``: rendering the array element by
    element is what printed a bridge VLAN's ``100`` as ``100, 100``.

    ``fmt`` writes one end -- a `radix:16` list (System ▸ Resources ▸ PCI's
    Memory and IO windows) is read in hex, not decimal.

    ``None`` when the array is not pairs -- see :func:`range_list_pairs`.
    """
    pairs = range_list_pairs(value)
    if pairs is None:
        return None
    return ",".join(
        fmt(low) if low == high else f"{fmt(low)}-{fmt(high)}"
        for low, high in pairs
    )


def format_split_range_list(lows: Any, highs: Any, fmt=str) -> str | None:
    """A range list carried as *two* parallel arrays: the field's own id holds
    the lows, its ``id2`` the highs.

    Confirmed against firmware 7.23.3: a Traffic Generator packet template with
    ``udp-dst-port=80-90,443,1000`` sends ``Ud6 = [80, 443, 1000]`` and
    ``Ue1 = [90, 443, 1000]`` -- one element per range in each, not the flat
    pairs the single-array form uses.

    ``None`` when the two do not line up, so the caller can fall back to the
    numbers it does have rather than pairing them up wrongly.
    """
    if not isinstance(lows, (list, tuple)) or not isinstance(highs, (list, tuple)):
        return None
    if len(lows) != len(highs):
        return None
    out = []
    for low, high in zip(lows, highs):
        lo, hi = _as_int(low), _as_int(high)
        if lo is None or hi is None:
            return None
        out.append(fmt(lo) if lo == hi else f"{fmt(lo)}-{fmt(hi)}")
    return ",".join(out)


def format_range(low: Any, high: Any) -> str:
    """One range of a range list: ``7-9``, or ``5`` when its ends are equal."""
    lo, hi = _as_int(low), _as_int(high)
    if lo is None:
        return ""
    return str(lo) if hi is None or hi == lo else f"{lo}-{hi}"


def parse_range(text: str) -> tuple[int, int]:
    """``7-9`` or ``5`` to the (low, high) pair the wire wants.

    A bare number is a range whose ends are equal, which is how the router
    itself sends it. Raises ValueError on anything else, so a typo is reported
    rather than written as a range that means something else.
    """
    entry = str(text).strip()
    if not entry:
        raise ValueError("empty range")
    low, dash, high = entry.partition("-")
    lo = int(low.strip(), 0)
    if not dash:
        return lo, lo
    hi = int(high.strip(), 0)
    if hi < lo:
        raise ValueError(f"range {entry!r} ends below where it starts")
    return lo, hi


def parse_range_list(text: Any) -> list[int]:
    """``5,7-9,300`` back to the flat (low, high) array the wire wants.

    Blank is the empty list -- the field is simply not set. Whitespace around
    an entry is allowed because Winbox accepts it. A value that is already the
    array does not come through here: :func:`encode_form_value` passes a list
    to the array branch above, which would otherwise double every element.
    """
    out: list[int] = []
    for entry in str(text or "").split(","):
        if not entry.strip():
            continue
        low, high = parse_range(entry)
        out += [low, high]
    return out


def number_table_columns(field: "Field | None") -> list[tuple[int, "Field"]]:
    """A ``numbertable``'s columns as (offset, field), in declaration order.

    The children of a numbertable carry their *position* as their id -- Signal
    Strengths declares ``u1`` Strength, ``u0`` Rate, ``u2`` Last Measured, and
    shows them in that order while reading them from offsets 1, 0 and 2 of each
    row. So the key is where the value is and the order is how to show it.
    """
    if field is None or not field.nested:
        return []
    return [(code, fld) for code, fld in field.nested.items()]


def number_table_rows(
    field: "Field | None", value: Any
) -> list[list[tuple["Field", Any]]] | None:
    """Split a ``numbertable``'s flat array into its rows.

    Wireless reports these as one number array holding a table: each row is as
    many consecutive elements as the field declares columns, at the offsets the
    columns name. A registration's Signal Strengths is (rate, strength, last
    measured) per row; a radio's Current Tx Powers is (rate, tx, real, total).

    ``None`` when the array does not fit that shape -- the length is not a
    whole number of rows, or a column with a vocabulary holds a value that is
    not in it. The caller then shows the raw numbers rather than a table of
    confidently mislabelled ones. (The alternative reading, one packed element
    per row, fails that test: it would put a rate index of 200 in the first
    column.)
    """
    cols = number_table_columns(field)
    if not cols or value is None:
        return None
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    width = max(offset for offset, _f in cols) + 1
    if width < 2 or not items or len(items) % width:
        return None
    rows: list[list[tuple[Field, Any]]] = []
    for start in range(0, len(items), width):
        chunk = items[start:start + width]
        row: list[tuple[Field, Any]] = []
        for offset, col in cols:
            cell = chunk[offset]
            if col.enum and _as_int(cell) not in col.enum:
                return None      # not this shape after all
            row.append((col, cell))
        rows.append(row)
    return rows


def enum_allowed_ids(
    field: "Field | None",
    menu: Menu | None,
    values_by_code: dict[int, Any] | None,
    *,
    board: str | None = None,
    ui_slots: dict[str, Any] | None = None,
    syscaps: frozenset[int] | set[int] | None = None,
) -> set[int] | None:
    """Which of a field's enum entries this object may actually be set to.

    ``None`` when the field carries no ``enumfilter`` -- every entry is
    offerable. An entry with no ``pred`` is always offerable; the rest are
    decided against this object's own values, which is why this needs the row
    and not just the schema. Wireless Band is the case in hand: the vocabulary
    is eleven bands, the radio's ``wphymodes`` bits say which four of them this
    card has.
    """
    if field is None or menu is None or not field.enum_filters:
        return None
    values = values_by_code or {}
    out: set[int] = set()
    for fid, pred in field.enum_filters:
        if pred is None or _eval_pred(
            pred, None, menu, values, board=board, ui_slots=ui_slots,
            syscaps=syscaps,
        ):
            out.add(fid)
    return out


def enum_source_allows(
    menu: Menu | None,
    pred: Any,
    values_by_code: dict[int, Any] | None,
    *,
    board: str | None = None,
    syscaps: frozenset[int] | set[int] | None = None,
) -> bool:
    """Whether one row of a dynamic dropdown's source menu may be offered.

    ``Field.enum_pred`` narrows the *source table*, so the question is asked of
    each of its rows against its own menu: a bridge port's Bridge reads (20,0)
    with ``{slot:'type', pred:{number,[12]}}``, and ether1 (type 1) answers no
    while bridge1 (type 12) answers yes.

    A row the reply left the slot out of is **kept**. The slots these preds
    read are the source menu's own bookkeeping fields (``type``, ``caps``,
    ``family``), and a handler that does not volunteer one would otherwise
    empty the dropdown entirely -- offering too much beats offering nothing.
    """
    if not isinstance(pred, dict) or menu is None:
        return True
    values = values_by_code or {}
    if pred.get("type") == "slot":
        slot = pred.get("slot")
        if isinstance(slot, str) and _slot_value(menu, slot, values) is None:
            return True
    return _eval_pred(
        pred, None, menu, values, board=board, syscaps=syscaps,
    )


def enum_from_slots(
    field: "Field | None",
    menu: Menu | None,
    values_by_code: dict[int, Any] | None,
) -> dict[int, str] | None:
    """A ``slotenum``'s entries, read off the object that carries them.

    Wireless Tx/Rx Chains: the card reports its chains in two parallel arrays
    -- ``tx-ht-chain-names`` (['chain0','chain1',…]) and ``tx-ht-chains`` (the
    wire value of each) -- and the checkbox list is built from them, so a
    two-chain radio offers two boxes and a four-chain one four. Neither array
    is in the schema as a value: they are ``nonpublic`` fields on the object.
    """
    if field is None or menu is None or not field.enum_slots:
        return None
    names_slot, values_slot = field.enum_slots
    values = values_by_code or {}
    labels = _slot_value(menu, names_slot, values)
    if isinstance(labels, (str, bytes)):
        labels = [labels]
    if not isinstance(labels, (list, tuple)) or not labels:
        return None
    wires = _slot_value(menu, values_slot, values)
    if isinstance(wires, int):
        wires = [wires]
    if not isinstance(wires, (list, tuple)) or len(wires) != len(labels):
        # A card that reports names but not their values numbers them in order.
        wires = list(range(len(labels)))
    out: dict[int, str] = {}
    for wire, label in zip(wires, labels):
        try:
            out[int(wire)] = (
                label.decode("utf-8", "replace")
                if isinstance(label, (bytes, bytearray))
                else str(label)
            )
        except (TypeError, ValueError):
            continue
    return out or None


def condition_hides(menu: Menu | None, on_name: str | None) -> bool:
    """Whether a condition decides that a row *exists* at all.

    Winbox has three kinds of ``on:``, told apart by which flag the ``cond``
    carries, and only two of them are about visibility:

    ``hide:1``            decided once from a capability slot (wcaps, hwtype,
                          wphymodes): the row is simply absent.
    ``hidedynamicly:1``   depends on another field and flips while the form is
                          open: Speed appears once Auto Negotiation is off.
    neither               the row stays *visible and greys out* while the
                          condition is false -- Supported Rates with Rate on
                          `default`, the WPA pre-shared key with WPA unticked,
                          a wireless sniffer's server with streaming off.

    So a flagless condition must never drop a row; see
    :func:`condition_enable_on`.
    """
    if not on_name or menu is None:
        return True
    cond = (menu.conditions or {}).get(on_name)
    if not isinstance(cond, dict):
        return True      # unknown name: treat as visibility, and default True
    return bool(cond.get("hide") or cond.get("hidedynamicly"))


def condition_enable_on(menu: Menu | None, on_name: str | None) -> str | None:
    """Name of the condition that greys this row out, if any.

    Two shapes reach here. A flagless ``cond`` gates its own rows' *enabled*
    state (``rate`` → Rate is `configured`). And a ``hide:1`` cond may carry
    ``oron:'other'``, which splits the two jobs between two conditions:
    ``{name:'bmode',hide:1,oron:'rate',c:[wphymodes has the B band]}`` puts
    Supported Rates B on a 2.4GHz-capable radio only, and greys it there until
    Rate is `configured`.
    """
    if not on_name or menu is None:
        return None
    cond = (menu.conditions or {}).get(on_name)
    if not isinstance(cond, dict):
        return None
    oron = cond.get("oron")
    if isinstance(oron, str) and oron:
        return oron
    if not cond.get("hide") and not cond.get("hidedynamicly"):
        return on_name
    return None


def condition_is_dynamic(menu: Menu | None, on_name: str | None) -> bool:
    """True for ``hidedynamicly`` conditions, which re-evaluate as you edit.

    Winbox splits ``on:`` conditions in two. ``hide`` ones are decided once
    from board capabilities (PoE, SFP, extrastats) — a row that fails is
    simply absent. ``hidedynamicly`` ones depend on other field values and
    flip while the form is open: Speed appears only once Auto Negotiation is
    unchecked. Those rows have to be built and then shown/hidden, never
    dropped at build time.
    """
    if not on_name or menu is None:
        return False
    cond = (menu.conditions or {}).get(on_name)
    if not isinstance(cond, dict):
        return False
    return bool(cond.get("hidedynamicly"))


def tab_is_visible(
    menu: Menu | None,
    tab: str,
    values_by_code: dict[int, Any] | None,
    *,
    board: str | None = None,
    ui_slots: dict[str, Any] | None = None,
    syscaps: frozenset[int] | set[int] | None = None,
) -> bool:
    """Whether a property-form tab should be shown for this object."""
    if menu is None:
        return True
    on = (menu.tab_conditions or {}).get(tab)
    return condition_is_true(
        menu, on, values_by_code, board=board, ui_slots=ui_slots,
        syscaps=syscaps,
    )


def tab_condition(menu: Menu | None, tab: str) -> str | None:
    """The named condition gating a whole tab, if it has one."""
    if menu is None:
        return None
    on = (menu.tab_conditions or {}).get(tab)
    return str(on) if isinstance(on, str) and on else None


def _board_matches(board: str | None, want: Any, is_regex: bool) -> bool:
    """Match a device board name against a type:board predicate value."""
    if not board or want is None:
        return False
    want_s = str(want)
    if is_regex:
        import re as _re

        try:
            return _re.fullmatch(want_s, board) is not None
        except _re.error:
            return False
    return board == want_s


def _slot_value(
    menu: Menu,
    slot: str | None,
    values: dict[int, Any],
    ui_slots: dict[str, Any] | None = None,
) -> Any:
    """Resolve a cond on: slot name to a wire value on this object.

    A slot is normally a field name, but not always: ``umode`` is the mode
    button, which has no wire code at all, so a caller-supplied value wins
    over the field scan.
    """
    if not slot:
        return None
    if ui_slots and slot in ui_slots:
        return ui_slots[slot]
    # The three flags every menu can have and none declares: a firewall list's
    # `static` / `dynamic` filter reads a slot called 'dynamic', and no field
    # of that name exists anywhere (see UNIVERSAL_FLAGS).
    universal = _UNIVERSAL_SLOTS.get(slot)
    if universal is not None and universal in values:
        return values[universal]
    for code, fld in menu.fields.items():
        if fld.name == slot and code in values:
            return values[code]
    for code, fld in menu.fields.items():
        if fld.name == slot:
            return values.get(code)
    return None


def _eval_pred(
    pred: Any,
    value: Any,
    menu: Menu,
    values: dict[int, Any],
    *,
    board: str | None = None,
    ui_slots: dict[str, Any] | None = None,
    syscaps: frozenset[int] | set[int] | None = None,
) -> bool:
    if not isinstance(pred, dict):
        return True
    ptype = pred.get("type")
    if ptype == "slot":
        # Test a *different* field of this object: `{type:'slot',slot:'wbands',
        # pred:{…}}`. Winbox uses it wherever the thing being decided is not
        # the thing being read -- an enum entry gated on the radio's band bits,
        # an MCS index gated on hwtype. Falling through to True offered 5GHz
        # channels on 2.4GHz-only cards and MCS 16-31 on two-chain radios.
        slot_val = _slot_value(menu, pred.get("slot"), values, ui_slots)
        return _eval_pred(
            pred.get("pred"), slot_val, menu, values, board=board,
            ui_slots=ui_slots, syscaps=syscaps,
        )
    if ptype == "syscap":
        # The switch-chip conds (prestera, aldrin2, switch_mv88e6xxx) and
        # `qstats` / `hasvpn` are all this: a capability the device reports.
        return _syscap_holds(pred.get("value"), syscaps)
    if ptype == "not":
        return not _eval_pred(
            pred.get("pred"), value, menu, values, board=board,
            ui_slots=ui_slots, syscaps=syscaps,
        )
    if ptype == "bool":
        want = pred.get("value", 1)
        if isinstance(want, list):
            want = want[0] if want else 1
        try:
            want_b = bool(int(want))
        except (TypeError, ValueError):
            want_b = bool(want)
        if value is None:
            return not want_b
        try:
            have = bool(int(value)) if not isinstance(value, bool) else value
        except (TypeError, ValueError):
            have = bool(value)
        return have is want_b or have == want_b
    if ptype == "bitmap":
        mask = int(pred.get("mask") or 0)
        expect = int(pred.get("value") or 0)
        if value is None:
            return False
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return False
        return (iv & mask) == expect
    if ptype == "number":
        allowed = pred.get("value")
        if not isinstance(allowed, list):
            allowed = [allowed]
        if value is None:
            return False
        try:
            iv = int(value)
            return iv in {int(x) for x in allowed}
        except (TypeError, ValueError):
            return value in allowed
    if ptype == "string":
        # ``{"type":"string","value":""}`` asks "is this slot equal to ''?".
        # Winbox wraps it in a not: for "show once this has a value" rows such
        # as Default Name. Without this branch the fall-through True made
        # not(...) always False and those rows never appeared.
        allowed = pred.get("value", "")
        if not isinstance(allowed, list):
            allowed = [allowed]
        have = "" if value is None else str(value)
        return any(have == ("" if a is None else str(a)) for a in allowed)
    if ptype == "numarrayany":
        # True when any element of an array slot matches one of the values.
        allowed_raw = pred.get("value")
        if not isinstance(allowed_raw, list):
            allowed_raw = [allowed_raw]
        try:
            allowed_set = {int(x) for x in allowed_raw if x is not None}
        except (TypeError, ValueError):
            return False
        if value is None:
            return False
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            try:
                if int(item) in allowed_set:
                    return True
            except (TypeError, ValueError):
                continue
        return False
    if ptype == "board":
        # "is this board X", plain or regex per the re flag. Unknown board must
        # answer False, not True: the Interface stats tabs are gated on
        # not(board x86), and answering True hid them on every hardware router.
        return _board_matches(board, pred.get("value"), bool(pred.get("re")))
    if ptype in ("or", "and"):
        fn = any if ptype == "or" else all
        inner = pred.get("pred")
        if isinstance(inner, list):
            return fn(
                _eval_pred(
                    p, value, menu, values, board=board, ui_slots=ui_slots,
                    syscaps=syscaps,
                )
                for p in inner
            )
        return _eval_pred(
            inner, value, menu, values, board=board, ui_slots=ui_slots,
            syscaps=syscaps,
        )
    return True


def _render_bitrate(bps: int) -> str:
    """Human-readable bit/s for schema ``bigbitrate`` (Rx/Tx rates)."""
    v = float(bps)
    if v < 1000:
        return f"{v:.0f} bps"
    if v < 1_000_000:
        return f"{v / 1000:.2f} kbps"
    if v < 1_000_000_000:
        return f"{v / 1_000_000:.2f} Mbps"
    return f"{v / 1_000_000_000:.2f} Gbps"


def render_bitrate(bps: int) -> str:
    """How a bit rate reads, for callers outside this module.

    The list pane's ``bigbitchangerate`` columns (Hotspot ▸ Active ▸ Rx Rate)
    have to read exactly like an interface's own Rx Rate, which is the same
    function -- so they share it rather than growing a second one that rounds
    differently.
    """
    return _render_bitrate(bps)


#: Units a ``bigbytes`` value climbs through, smallest first.
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
#: Winbox changes unit only once the number would need a fifth digit, not at
#: every power of 1024. Two counters off the same router fix this: 2521615565
#: tx bytes reads "2404.8 MiB" (2.3 GiB, and it stays in MiB), while 27.7 GiB
#: of it does read "27.7 GiB" -- so the step is somewhere above 2404.8 MiB and
#: no higher than 28355.8 MiB. 10000 is the round number in that window, and
#: the one that keeps :func:`format_kbytes`'s "1024 MB, not 1 GB" true.
_BYTE_UNIT_STEP = 10000


#: How a byte count reads.
#:
#: ``winbox``  what Winbox 3 and 4 print -- see :data:`_BYTE_UNIT_STEP`.
#: ``binary``  a new unit at every 1024, the way most tools round (2.3 GiB).
#: ``exact``   the counter itself, for pasting into something that will do
#:             arithmetic on it.
BYTE_UNIT_MODES = ("compact", "binary", "exact")
_byte_unit_mode = "compact"


def byte_unit_mode() -> str:
    """The mode :func:`format_bytes` is currently using."""
    return _byte_unit_mode


def set_byte_unit_mode(mode: str) -> str:
    """Choose how byte counts read. Unknown values fall back to ``winbox``.

    A process-wide setting rather than an argument: it is a reading
    preference, and every caller of :func:`format_bytes` is a render path
    several layers below where anyone would thread it through.
    """
    global _byte_unit_mode
    mode = str(mode or "").strip().lower()
    _byte_unit_mode = mode if mode in BYTE_UNIT_MODES else "compact"
    return _byte_unit_mode


def format_bytes(n: int) -> str:
    """Human-readable byte count for schema ``bigbytes``, the way Winbox reads.

    One decimal, kept even when it is zero (574.0 MiB), and whole bytes below
    a kilobyte. See :data:`_BYTE_UNIT_STEP` for when the unit changes, and
    :func:`set_byte_unit_mode` for reading them another way.
    """
    if _byte_unit_mode == "exact":
        return f"{int(n)} B"
    step = 1024 if _byte_unit_mode == "binary" else _BYTE_UNIT_STEP
    v = float(n)
    unit = 0
    while abs(v) >= step and unit < len(_BYTE_UNITS) - 1:
        v /= 1024
        unit += 1
    if unit == 0:
        return f"{int(v)} B"
    return f"{v:.1f} {_BYTE_UNITS[unit]}"


def format_kbytes(kb: int) -> str:
    """Winbox memory sizes: a kilobyte count as ``883.4 MB``.

    Winbox stays in MB even past a gigabyte (it reads ``1024 MB``, not
    ``1 GB``), so this does not promote units.
    """
    if kb < 0:
        return str(kb)
    if kb < 1024:
        return f"{kb} KB"
    text = f"{kb / 1024:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text} MB"


def format_interval(seconds: float | int | None) -> str:
    """Winbox ``type:interval`` text: 300 -> ``00:05:00``.

    Past a day the router prefixes the day count (``3d04:15:00``). Fractional
    seconds only appear for sub-second scaled fields, where dropping them
    would round a real value to zero.
    """
    if seconds is None:
        return ""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if secs and secs != int(secs):
        sec_text = f"{secs:06.3f}".rstrip("0").rstrip(".")
    else:
        sec_text = f"{int(secs):02d}"
    body = f"{int(hours):02d}:{int(minutes):02d}:{sec_text}"
    return f"{sign}{int(days)}d {body}" if days else f"{sign}{body}"


def format_age_stamp(stamp: int, uptime: float) -> str:
    """Winbox ``type:'age'`` text: a stamp on the uptime clock → a duration.

    The router puts these on the wire as the *uptime reading* of the event, in whole
    seconds -- confirmed by polling twice: the value does not tick, it only
    jumps when the event recurs. So the duration is the gap to the device's
    current uptime (:func:`DeviceClient.uptime`).

    Past stamps (``Last Seen``, ``Age``) and future ones (``Expires After``)
    share the type with nothing to tell them apart, so this reports the
    magnitude and lets the column name carry the direction -- which is what
    Winbox shows too.
    """
    return format_interval(abs(float(uptime) - stamp))


def parse_interval(text: str) -> int | None:
    """Parse Winbox / the router duration text back to whole seconds.

    Accepts what a user might reasonably type: ``300``, ``00:05:00``,
    ``5:00``, ``3d04:15:00`` and the router suffix form ``1h30m``.
    Returns None when the text is not a duration.
    """
    if text is None:
        return None
    s = str(text).strip().lower()
    if not s:
        return None
    neg = s.startswith("-")
    if neg:
        s = s[1:].lstrip()

    days = 0
    if "d" in s:
        head, _, tail = s.partition("d")
        # "3d" alone, or "3d04:15:00"; but not the suffix form "3d4h"
        if head.replace(".", "", 1).isdigit() and (not tail or ":" in tail or not tail.strip()):
            try:
                days = int(float(head))
            except ValueError:
                return None
            s = tail.strip()
            if not s:
                return -days * 86400 if neg else days * 86400

    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3:
            return None
        try:
            nums = [float(p) if p.strip() else 0.0 for p in parts]
        except ValueError:
            return None
        while len(nums) < 3:
            nums.insert(0, 0.0)  # "5:00" means mm:ss
        total = nums[0] * 3600 + nums[1] * 60 + nums[2]
        total += days * 86400
        return int(-total if neg else total)

    # Bare number of seconds
    try:
        total = float(s) + days * 86400
        return int(-total if neg else total)
    except ValueError:
        pass

    # The router suffix form: 1w2d3h4m5s
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0.0
    num = ""
    matched = False
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in units:
            if not num:
                return None
            total += float(num) * units[ch]
            num = ""
            matched = True
        elif ch.isspace():
            continue
        else:
            return None
    if num or not matched:
        return None
    total += days * 86400
    return int(-total if neg else total)


def parse_dateandtime(text: Any, *, scale: int | None = None) -> int:
    """``2026-08-05 14:05:51`` back to the wire's units.

    The reverse of :func:`_render_dateandtime` for the shape it writes. A
    scaled field (link up/down times are centiseconds) multiplies back, the
    same way :func:`parse_interval` does for an interval.
    """
    import datetime

    raw = str(text or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%b/%d/%Y %H:%M:%S", "%b/%d/%Y"):
        try:
            when = datetime.datetime.strptime(raw.title(), fmt)
        except ValueError:
            continue
        secs = when.replace(tzinfo=datetime.UTC).timestamp()
        return int(round(secs * (scale if scale and scale > 0 else 1)))
    raise ValueError(f"not a date and time: {raw!r}")


def format_clock_date(value: int) -> str:
    """A ``clockdate`` -- Unix seconds at midnight -- as Winbox writes it."""
    import datetime

    if value in (0, 0xFFFFFFFF):
        return ""
    try:
        day = datetime.datetime.fromtimestamp(int(value), datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return str(value)
    return f"{_MONTHS[day.month - 1]}/{day.day:02d}/{day.year}"


def parse_clock_date(text: Any) -> int:
    """``aug/05/2026`` (or ``2026-08-05``) back to Unix seconds at midnight."""
    import datetime

    raw = str(text or "").strip()
    if not raw:
        return 0
    for fmt in ("%b/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            day = datetime.datetime.strptime(raw.title(), fmt)
        except ValueError:
            continue
        return int(day.replace(tzinfo=datetime.UTC).timestamp())
    raise ValueError(f"not a date: {raw!r}")


def format_clock_time(value: int) -> str:
    """A ``clocktime`` -- seconds since midnight -- as ``14:05:51``."""
    secs = int(value) % 86400
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def parse_clock_time(text: Any) -> int:
    """``14:05:51`` back to seconds since midnight."""
    raw = str(text or "").strip()
    if not raw:
        return 0
    parts = raw.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"not a time: {raw!r}")
    nums = [int(p or 0) for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0] * 3600 + nums[1] * 60 + nums[2]) % 86400


def format_gmt_offset(value: int) -> str:
    """A ``timezone`` -- signed seconds -- as ``-04:00``."""
    iv = int(value)
    if iv >= 0x80000000:
        iv -= 0x1_0000_0000
    sign = "-" if iv < 0 else "+"
    iv = abs(iv)
    return f"{sign}{iv // 3600:02d}:{iv % 3600 // 60:02d}"


def parse_gmt_offset(text: Any) -> int:
    """``-04:00`` back to signed seconds (as a u32 two's complement)."""
    raw = str(text or "").strip()
    if not raw:
        return 0
    sign = -1 if raw.startswith("-") else 1
    raw = raw.lstrip("+-").strip()
    parts = raw.split(":") if raw else ["0"]
    hours = int(parts[0] or 0)
    minutes = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    return (sign * (hours * 3600 + minutes * 60)) & 0xFFFFFFFF


#: Month names as the router writes a date (aug/05/2026).
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def _render_dateandtime(
    iv: int,
    *,
    scale: int | None = None,
    relative: bool = False,
) -> str:
    """Format a Winbox ``dateandtime`` wire value.

    Schema often sets ``scale:100`` (centiseconds) on link up/down times.
    The wire may carry:

    - absolute Unix time in whole seconds,
    - absolute Unix time in 1/scale-second units (needs a wider type),
    - or, with ``relative:1``, an age in 1/scale-second units that Winbox
      shows as an absolute clock time (now − age).

    Values that used to display as 1978–1980 were absolute-seconds decoding
    of relative/centisecond payloads.
    """
    import datetime

    scale_n = scale if scale and scale > 0 else 1
    candidates: list[float] = []
    if scale_n != 1:
        candidates.append(iv / scale_n)
    candidates.append(float(iv))
    if iv >= 10**12:  # milliseconds since epoch
        candidates.append(iv / 1000.0)

    def _year(secs: float) -> int | None:
        try:
            return datetime.datetime.fromtimestamp(secs, datetime.UTC).year
        except (OverflowError, OSError, ValueError):
            return None

    # A relative:1 field carries an age, so now - age is the answer. Guessing
    # an absolute interpretation first is how a 192-day-old link timestamp got
    # read as a 2022 date: the raw centisecond count happens to look like a
    # plausible Unix time.
    if relative:
        age = iv / scale_n
        if 0 < age < 86400 * 365 * 25:  # up to 25 years
            dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    chosen: float | None = None
    for secs in candidates:
        y = _year(secs)
        if y is not None and 2000 <= y <= 2100:
            chosen = secs
            break
    if chosen is None:
        for secs in candidates:
            y = _year(secs)
            if y is not None and 1990 <= y <= 2100:
                chosen = secs
                break

    # relative:1 fields (Last Link Up/Down) — if no plausible absolute, treat
    # the scaled value as age before "now" (same units as interval/age scale).
    if chosen is None and relative:
        age = iv / scale_n
        if 0 < age < 86400 * 365 * 25:  # up to 25 years
            dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    if chosen is None:
        chosen = candidates[0]

    try:
        return datetime.datetime.fromtimestamp(chosen, datetime.UTC).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OverflowError, OSError, ValueError):
        return str(iv)


def _render_unknown(value: Any) -> str:
    """Compact display for wire values with no schema field."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if 0 <= value <= 0xFFFFFFFF and value > 0xFFFF:
            return f"{value} (0x{value:x})"
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            text = value.decode("utf-8")
            if text.isprintable():
                return text
        except UnicodeDecodeError:
            pass
        return value.hex() if len(value) <= 32 else value[:32].hex() + "…"
    if isinstance(value, m.Message):
        return f"<message {len(list(value))} fields>"
    if isinstance(value, list):
        if not value:
            return "[]"
        if len(value) <= 4:
            return "[" + ", ".join(_render_unknown(v) for v in value) + "]"
        return f"[{len(value)} items]"
    return repr(value)


def _is_payload(code: int, menu: "Menu | None") -> bool:
    """Whether a field code carries menu data rather than transport plumbing.

    Codes at or above 0xFE0000 are normally framing (SYS_TO, SYS_FROM, the
    GETALL object array...), but a menu may legitimately declare a field up
    there: DHCP Client keeps its `Name` at 0xFE0010 as `sfe0010`, and the
    device really sends it. Skipping the whole range hid that field, which is
    why the DHCP Client list showed a blank Name column.
    """
    if code < 0xFE0000:
        return True
    return menu is not None and code in menu.fields


#: Codes a menu may keep its display name on when it declares no field for it.
#: 0xFE0010 first: DHCP Client keeps `Name` there, and the firewall chain table
#: sends nothing else at all.
STD_NAME_CODES: tuple[int, ...] = (0xFE0010, 0x01, 0x02, 0x03)


def name_field_code(menu: Menu | None) -> int | None:
    """The field code holding a menu's display name, per its own `nameval`.

    The code behind that name is not predictable and must not be guessed:
    Interface keeps 'Name' at 0x10006 while Address keeps its identity at 0x01,
    so reading a fixed low code off an interface returns an unrelated string.
    """
    if menu is None:
        return None
    want = menu.nameval if isinstance(menu.nameval, str) else "Name"
    for candidate in (want, "Name"):
        for code, fld in menu.fields.items():
            if fld.name == candidate:
                return code
    return None


def nameval_field_names(menu: Menu | None) -> list[str]:
    """Every field name a menu's ``nameval`` reads, in order.

    ``nameval`` is usually one name ('Name', 'IP Address'), but 55 menus in
    7.21 build a compound label out of a ``concat`` tree instead -- a DHCP
    lease is `Address,Active Address`, a walled-garden entry `Dst. Host/Path`,
    and a firewall rule nests two of them into
    `Src. Address:Src. Port->Dst. Address:Dst. Port`.
    """
    out: list[str] = []

    def walk(spec: Any) -> None:
        if isinstance(spec, str):
            if spec.strip():
                out.append(spec.strip())
            return
        if isinstance(spec, dict) and spec.get("type") == "concat":
            walk(spec.get("first"))
            walk(spec.get("second"))

    walk(menu.nameval if menu is not None else None)
    return out


def object_label(menu: Menu | None, by_name: dict[str, str] | None) -> str:
    """What Winbox calls one object of this menu, per its own ``nameval``.

    ``by_name`` maps field name to the value as shown. Parts that are missing
    or empty drop out with their separator -- a lease that is not active reads
    as just its address, not as "192.168.88.10," -- so a partly-filled row
    still gets a name rather than punctuation.
    """
    if menu is None or not by_name:
        return ""

    def walk(spec: Any) -> str:
        if isinstance(spec, str):
            return str(by_name.get(spec.strip(), "") or "").strip()
        if isinstance(spec, dict) and spec.get("type") == "concat":
            first = walk(spec.get("first"))
            second = walk(spec.get("second"))
            sep = str(spec.get("sep") or "")
            if first and second:
                # Winbox drops a repeat: a lease whose active address equals
                # its requested one reads as one address, not "x,x".
                if first == second:
                    return first
                return f"{first}{sep}{second}"
            return first or second
        return ""

    return walk(menu.nameval)


def enum_map_from_objects(menu: Menu | None, objects) -> dict[int, str]:
    """Build {object_id: label} from a handler's GETALL objects.

    This is what a `values:{type:'dynamic'}` enum needs: the stored number is
    the target object's own id (0xFE0001) and the label is its display name.
    Menus that name nothing useful -- DHCP Client Option leaves Name empty and
    identifies rows by their option code -- fall back to the first string the
    object carries, then to the bare id.
    """
    code = name_field_code(menu)
    out: dict[int, str] = {}
    for obj in objects:
        oid = obj.get_u32(0xFE0001) if hasattr(obj, "get_u32") else None
        if oid is None:
            continue
        label = ""
        if code is not None:
            label = _text_of(obj.find(code))
        if not label:
            # Some source tables declare no fields at all and keep the label on
            # a standard code. The firewall chain table is one: it sends only
            # {id, 0xFE0010: b'input'}, and the generic scan below skips
            # 0xFE0010 as a system code, so every chain came back as "#1".
            for c in STD_NAME_CODES:
                label = _text_of(obj.find(c))
                if label:
                    break
        if not label:
            for c, _tag, value in obj:
                if c >= 0xFE0000:
                    continue
                label = _text_of((0, value))
                if label:
                    break
        out[oid] = label or f"#{oid}"
    return out


def _text_of(hit) -> str:
    if hit is None:
        return ""
    value = hit[1]
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return value if isinstance(value, str) else ""


#: Inner ``enm`` matches such as In./Out. Interface carry an interface id, but
#: the .jg leaves the enum source off them, so there is nothing to turn the id
#: into a name. Winbox resolves them against the interface table; so do we.
IFACE_ENUM_PATH = (20, 0)


def _implied_enum_path(fld: Field) -> tuple[int, ...] | None:
    """The enum source for a field, including ones the .jg does not name."""
    if fld.enum_path:
        return fld.enum_path
    if fld.jg_type != "enm" or fld.enum:
        return None
    name = (fld.name or "").strip().lower()
    # "In. Interface" yes; "In. Interface List" is a different table entirely.
    return IFACE_ENUM_PATH if name.endswith("interface") else None


def opt_chain(fld: Field) -> tuple[Field | None, Field | None]:
    """Unwrap an ``opt`` match into its negate toggle and its value field.

    The .jg models an optional firewall match as ``opt -> not -> value``, but
    the device flattens all three into sibling codes on one row: the opt code
    carries only "this match is set", the not code the ``!``, and the value
    lives under the innermost field's own code. Rendering the opt field on its
    own is what made every configured match read as ``True``.

    Not every optional field is negatable, though, and 267 of the 629 in 7.21
    are the shorter shape -- ``opt -> value``, with no ``!`` to give:
    ``{name:'MAC Address',type:'opt',id:'b7',c:[{type:'macaddr',id:'r2'}]}`` on
    an ARP entry, a lease's Parent Queue, a DHCP network's Next Server. Reading
    every opt as the three-level kind left those with no value field at all, so
    each of them rendered as the bare word "set". The middle level is a ``not``
    in all 362 of the negatable ones, which is what tells the shapes apart.
    """
    if not fld.nested:
        return None, None
    child = next(iter(fld.nested.values()), None)
    if child is None:
        return None, None
    if child.jg_type == "not":
        return child, next(iter(child.nested.values()), None)
    return None, child


def _render_opt(
    fld: Field,
    value: Any,
    others: dict[int, Any] | None,
    resolver=None,
    uptime: float | None = None,
) -> str:
    """Render an optional match by following it to the sibling holding its value."""
    if value is not True and value != 1:
        return ""  # the match is not set — Winbox shows nothing, not "false"
    neg, inner = opt_chain(fld)
    if inner is None or others is None or inner.code not in others:
        # Set, but we cannot say to what; better than claiming "true".
        return "set"
    text = _render(inner, others[inner.code], resolver, uptime, others)
    # Address matches arrive as a from/to pair. Equal ends are one address,
    # which is how 127.0.0.1 should read rather than "127.0.0.1-127.0.0.1".
    if inner.maskid is not None and inner.maskid in others:
        high = _render(inner, others[inner.maskid], resolver, uptime, others)
        if high and high != text:
            text = format_ipv4_range(text, high)
    if neg is not None and others.get(neg.code) in (True, 1):
        text = f"!{text}"
    return text


def _render_top(
    fld: Field,
    value: Any,
    resolver=None,
    uptime: float | None = None,
    others: dict[int, Any] | None = None,
    menu: "Menu | None" = None,
) -> str:
    """A record's own field, as its list and its form both show it.

    IP ▸ Addresses keeps the mask in a field of its own (0x3), and Winbox
    joins the two: 10.0.2.15/24. Joined here, the form opens on what the list
    shows and the prefix can be edited in place; the form writes the two
    fields back apart. Not in :func:`_render` itself, which an opt match also
    goes through and which reads its maskid as a range's other end.
    """
    text = _render(fld, value, resolver, uptime, others, menu)
    if (
        text and fld.jg_type == "network" and fld.maskid is not None
        and not fld.is_range and others and fld.maskid in others
    ):
        bits = mask_prefix_len(others[fld.maskid])
        if bits is not None:
            text = f"{text}/{bits}"
    # An ipaddrandport is the same idea with a port: 10.0.2.2:57670. A
    # connection with no ports (ICMP) sends none, and shows the address alone.
    if text and fld.jg_type == "ipaddrandport" and fld.portid is not None and others:
        port = others.get(fld.portid)
        if isinstance(port, int) and not isinstance(port, bool) and port:
            text = f"{text}:{port}"
    return text


def _render(
    fld: Field,
    value: Any,
    resolver=None,
    uptime: float | None = None,
    others: dict[int, Any] | None = None,
    menu: "Menu | None" = None,
) -> str:
    """Render `value` for `fld`, resolving dynamic enums when possible.

    `.jg` describes some enums as `values:{type:'dynamic',path:[20,0]}`: the
    stored number is another object's id and only that handler knows its name.
    Interface 8 says nothing on its own; asking (20,0) turns it into 'bridge'.

    `resolver(path)` returns that {id: label} map -- DeviceClient.enum_map is
    exactly this. It feeds Field.render's `lookup`, so array-valued dynamic
    enums (DHCP Options is a u32 array of option ids) resolve element-wise
    rather than only scalars.
    """
    if fld.jg_type == "opt":
        return _render_opt(fld, value, others, resolver, uptime)
    if fld.enum_slots and others and menu is not None:
        # A slotenum's labels live on the object, not in the schema: without
        # them wireless Tx Chains renders as the bitmask "3" instead of
        # "chain0,chain1". `others` is the flat row, which is where they are.
        entries = enum_from_slots(fld, menu, others)
        if entries:
            fld = replace(fld, enum=entries, enum_slots=None)
    # A range list whose highs are a sibling array (`id2`). The flat row is the
    # only place the second half is, so this is where the two are joined.
    if fld.jg_type in RANGE_LIST_TYPES and fld.highid is not None and others:
        ranges = format_split_range_list(
            value, others.get(fld.highid), fld._number
        )
        if ranges is not None:
            return ranges

    lookup = None
    enum_path = _implied_enum_path(fld)
    if fld.jg_type == "addr" and addr_has_iface(value):
        # The interface inside an addr record names a row of (20, 0); asked
        # only when one is there, so a plain gateway costs no lookup.
        enum_path = ADDR_IFACE_PATH
    if resolver is not None and enum_path:
        try:
            lookup = resolver(enum_path)
        except Exception:
            lookup = None
    return fld.render(value, lookup=lookup, uptime=uptime)


def _nav_top_level(root: Any, source: str) -> list[NavNode]:
    """Build top-level nav, nesting entries that declare a UI ``group``."""
    if not isinstance(root, list):
        return _nav_from_jg(root, source)

    ungrouped: list[NavNode] = []
    by_group: dict[str, list[NavNode]] = {}
    group_icon: dict[str, int | None] = {}
    group_prio: dict[str, int | None] = {}

    for item in root:
        if not isinstance(item, dict) or item.get("nonpublic"):
            continue
        built = _nav_from_jg(item, source)
        if not built:
            continue
        # A container entry (no ``type``) is labelled with its name in the menu
        # and opens a window titled with its title; a typed entry (a ``map``
        # leaf such as Log or Scheduler) is labelled with its title, its name
        # being the record's own.
        if not item.get("type"):
            for node in built:
                if node.kind == "folder":
                    node.label = node.name
        prio = item.get("prio") if isinstance(item.get("prio"), int) else None
        group = item.get("group")
        if isinstance(group, str) and group:
            by_group.setdefault(group, []).extend(built)
            # A group's position is its earliest member's. On this corpus that
            # is always the member carrying groupIcon, i.e. the group "leader".
            group_prio[group] = _min_prio(group_prio.get(group), prio)
            gi = item.get("groupIcon")
            if isinstance(gi, int):
                group_icon[group] = gi
            elif isinstance(item.get("icon"), int) and group not in group_icon:
                group_icon[group] = item["icon"]
        else:
            for node in built:
                if node.prio is None:
                    node.prio = prio
            ungrouped.extend(built)

    out = list(ungrouped)
    for gname, children in by_group.items():
        # If an ungrouped folder already has this title, merge into it
        existing = next((n for n in out if n.kind == "folder" and n.title == gname), None)
        if existing is not None:
            existing.children.extend(children)
            existing.prio = _min_prio(existing.prio, group_prio.get(gname))
            if existing.icon is None:
                existing.icon = group_icon.get(gname)
        else:
            out.append(NavNode(
                name=gname,
                title=gname,
                kind="folder",
                icon=group_icon.get(gname),
                children=list(children),
                source=source,
                prio=group_prio.get(gname),
            ))
    return out


def _resolve_nav_overlays(
    nodes: list[NavNode], generic_paths: dict[str, tuple[int, ...]]
) -> list[NavNode]:
    """Give overlay nav nodes the path of the generic they inherit.

    Has to run after every file is walked: ``iface``'s base menu lives in
    roteros.jg, but ipv6.jg and ppp.jg declare overlays inheriting from it and
    are walked earlier.

    A container whose only content is overlays is folded into whichever
    top-level entry owns that generic's path -- otherwise ipv6.jg's ``6to4`` and
    ``gre6`` wrappers show up as top-level nav entries of their own.
    """

    def resolve(node: NavNode) -> bool:
        """Fill in paths depth-first; False when the node has nothing left."""
        if node.inherit is not None and node.path is None:
            node.path = generic_paths.get(node.inherit)
            return node.path is not None
        node.children = [k for k in node.children if resolve(k)]
        # A leaf with no path is not automatically empty: New Terminal is one,
        # and so is a `termopen` dialog (Tools ▸ Telnet), whose connection the
        # client makes rather than a handler.
        return (
            bool(node.children)
            or node.path is not None
            or node.kind in ("terminal", "termopen")
        )

    keep = [n for n in nodes if resolve(n)]

    # Re-home overlay-only containers under the owner of the generic's path.
    # Only a menu that owns its path can host them: a family base (PPP ▸
    # Interface) reads (20,0) without owning it, and ppp.jg is walked before
    # roteros.jg, so counting it here handed the iface overlays to PPP.
    by_path: dict[tuple[int, ...], NavNode] = {}
    for node in keep:
        for kid in node.children:
            if kid.path is not None and kid.type_value is None and kid.inherit is None:
                by_path.setdefault(kid.path, node)

    out: list[NavNode] = []
    for node in keep:
        overlay_only = (
            node.kind == "folder"
            and node.prio is None
            and node.children
            and all(
                k.type_value is not None or k.generic is not None
                for k in node.children
            )
        )
        host = None
        if overlay_only:
            paths = {k.path for k in node.children if k.path is not None}
            if len(paths) == 1:
                host = by_path.get(next(iter(paths)))
        if host is not None and host is not node:
            host.children.extend(node.children)
            continue
        out.append(node)
    return out


def _min_prio(a: int | None, b: int | None) -> int | None:
    """Smallest declared prio, ignoring absent ones."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _merge_nav_folder(dst: NavNode, src: NavNode) -> None:
    """Fold ``src`` into an equally titled folder, filling gaps.

    Plugins are walked in filename order, so whichever file happens to sort
    first used to win outright -- that is why IP, IPv6 and Tools had no icon
    (dhcp.jg / advtool.jg name those groups without a groupIcon, and roteros.jg's
    real icon arrived later and was discarded). Prio would hit the same trap.
    """
    dst.children = _dedupe_nav(dst.children + src.children)
    dst.actions = list(dst.actions) + list(src.actions)
    dst.prio = _min_prio(dst.prio, src.prio)
    if dst.icon is None:
        dst.icon = src.icon


def _dedupe_nav(nodes: list[NavNode]) -> list[NavNode]:
    """Merge duplicate folder titles from multiple plugins; keep first leaf."""
    out: list[NavNode] = []
    folders: dict[str, NavNode] = {}
    seen_leaf: set[tuple] = set()
    for n in nodes:
        if n.kind == "folder":
            key = n.title
            if key in folders:
                _merge_nav_folder(folders[key], n)
            else:
                n.children = _dedupe_nav(n.children)
                folders[key] = n
                out.append(n)
        elif n.kind == "terminal":
            if ("terminal",) not in seen_leaf:
                seen_leaf.add(("terminal",))
                out.append(n)
        else:
            key = (n.path, n.name, n.type_value)
            if key not in seen_leaf:
                seen_leaf.add(key)
                out.append(n)
    return out


#: Where a top-level entry with no .jg prio sits: after Queues (130) and before
#: Partition (999), which is where Winbox shows Files / Log / Tools / Terminal.
_DEFAULT_NAV_PRIO = 500

#: Relative order among the prio-less entries only. Everything else is ranked by
#: its own prio; a hardcoded list as the primary key would contradict the schema
#: (it put Bridge before IP, Mesh after Tools, PPP after Wireless).
_NAV_PRIO_TIEBREAK = ("Files", "Log", "RADIUS", "Tools", "New Terminal", "Terminal")


def _order_nav(nodes: list[NavNode]) -> list[NavNode]:
    rank = {name: i for i, name in enumerate(_NAV_PRIO_TIEBREAK)}
    unranked = len(_NAV_PRIO_TIEBREAK)

    def key(n: NavNode) -> tuple[int, int, str]:
        prio = _DEFAULT_NAV_PRIO if n.prio is None else n.prio
        # Alphabetical title breaks real prio ties the way Winbox shows them
        # (Bridge/PPP/Switch at 101, IP/IPv6/MPLS at 110). Declaration order
        # would not: ppp.jg is walked before roteros.jg.
        return (prio, rank.get(n.title, unranked), n.title.lower())

    return sorted(nodes, key=key)


def wire_name(index_name: str) -> str:
    """The filename to ask the device for, given an index entry's `name`.

    Plugin definitions are served gzipped under ``<name>.gz`` and the index's
    ``size`` is the *compressed* length; the PNG icon sheets are served under
    their bare name. Asking for a bare ``roteros.jg`` fails with "cannot open
    source file", which is what made an early ``mirror`` fetch only the icons.

    The index also carries a ``unique`` field -- ``roteros-7bf05b783f0b.jg`` --
    and it is tempting to assume that is the wire name. It is not: a decrypted
    Winbox 4 session against 7.21.1 shows the client asking for ``roteros.jg.gz``
    and the device answering with a handle and a size. What ``unique`` is for is
    not established; a client does not need it to fetch anything.
    """
    return index_name + ".gz" if index_name.endswith(".jg") else index_name


def download_plugin_set(entries, build_id, dest, fetch, log=None) -> tuple[int, int, int]:
    """Mirror a plugin index into `dest`, Winbox-cache layout.

    `fetch` is a callable taking a wire filename and returning bytes, so this
    stays independent of the transport. Files are stored decompressed under
    their index name, exactly as Winbox caches them, which makes the resulting
    directory interchangeable with Winbox's own `<version>-<build_id>/`.

    Each file gets the `<stem>.crc` marker Winbox writes, holding that file's
    own CRC from the index, and an existing file whose marker still matches is
    left alone. Returns (downloaded, cached, failed).
    """
    import gzip

    os.makedirs(dest, exist_ok=True)
    downloaded = cached = failed = 0

    def note(msg):
        if log:
            log(msg)

    for entry in entries:
        name = entry.get("name")
        if not name:
            continue
        local = os.path.join(dest, name)
        marker = os.path.join(dest, os.path.splitext(name)[0] + ".crc")
        crc = entry.get("crc")

        if os.path.exists(local) and crc is not None and os.path.exists(marker):
            try:
                with open(marker) as fh:
                    if fh.read().strip() == str(crc):
                        note(f"  = {name} (cached)")
                        cached += 1
                        continue
            except OSError:
                pass

        try:
            data = fetch(wire_name(name))
        except Exception as e:
            note(f"  ! {name}: {e}")
            failed += 1
            continue

        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError as e:
                note(f"  ! {name}: gunzip failed: {e}")
                failed += 1
                continue

        with open(local, "wb") as fh:
            fh.write(data)
        if crc is not None:
            with open(marker, "w") as fh:
                fh.write(f"{crc}\r\n")
        note(f"  + {name} ({len(data)} bytes)")
        downloaded += 1

    return downloaded, cached, failed


def plugin_index_entries(index_bytes: bytes) -> list[dict]:
    """The `list` plugin index as entries for :func:`download_plugin_set`.

    Each ``{ name: "x.jg", crc: 123, ... }`` block becomes a dict; quoted
    values stay strings and bare numbers become ints.
    """
    text = index_bytes.decode("utf-8", errors="replace")
    entries: list[dict] = []
    for blk in re.finditer(r"\{([^}]+)\}", text):
        entry: dict = {}
        for kv in re.finditer(r"(\w+):\s*(\"([^\"]*)\"|(\d+))", blk.group(1)):
            entry[kv.group(1)] = kv.group(3) if kv.group(3) is not None else int(kv.group(4))
        if "name" in entry:
            entries.append(entry)
    return entries


#: Bump when the parsed shape changes in a way an old cache file would get
#: wrong. schema.py's own size and mtime are folded in as well, so an edit
#: here invalidates every cache without anyone having to remember to -- except
#: in a packaged build, where there is no schema.py on disk to stat; there the
#: x3 version in the key is what makes each release parse afresh.
#: 3: NavNode.label (a menu entry's own name, e.g. 7.9's IP ▸ Addresses).
#: 4: Field.portid, and a tray with a path of its own no longer parsed into
#:    its menu (Connections' Max Entries).
_SCHEMA_CACHE_VERSION = 4

#: Set to disable reading and writing the parsed-schema cache entirely.
_NO_CACHE_ENV = "X3_NO_SCHEMA_CACHE"


def schema_cache_dir() -> str:
    """Where parsed schemas are kept. Ours, not Winbox's plugin cache."""
    from x3 import paths

    return str(paths.cache_dir() / "schema")


def _schema_cache_key(directory: str) -> tuple | None:
    """Identity of the .jg set in ``directory``, or None if there is none.

    Size and mtime rather than a content hash: the files are ~1.2 MB and the
    point of the cache is not to read them at all.
    """
    files = sorted(glob.glob(os.path.join(directory, "*.jg")))
    files += sorted(glob.glob(os.path.join(directory, "*.jg.gz")))
    if not files:
        return None
    stamps = []
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            return None
        stamps.append((os.path.basename(f), st.st_size, st.st_mtime_ns))
    try:
        me = os.stat(os.path.abspath(__file__))
        mine = (me.st_size, me.st_mtime_ns)
    except OSError:
        mine = (0, 0)
    from x3 import __version__

    return (_SCHEMA_CACHE_VERSION, __version__, mine, tuple(stamps))


def _schema_cache_path(directory: str) -> str:
    import hashlib

    name = hashlib.sha256(
        os.path.abspath(directory).encode("utf-8", "replace")
    ).hexdigest()[:16]
    return os.path.join(schema_cache_dir(), f"{name}.pickle")


def load_schema_cache(directory: str) -> "Schema | None":
    """A previously built schema for this directory, if it is still current.

    The file is pickled, so it is only ever read back from a directory the
    user owns -- the same trust as ``~/.config``. Anything unexpected in it
    (truncated, written by another version, unpickling to the wrong type) is
    treated as a miss rather than an error: the .jg files are still there.
    """
    if os.environ.get(_NO_CACHE_ENV):
        return None
    key = _schema_cache_key(directory)
    if key is None:
        return None
    try:
        with open(_schema_cache_path(directory), "rb") as fh:
            stored_key, schema = pickle.load(fh)
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError,
            ImportError, IndexError, TypeError, ValueError):
        return None
    if stored_key != key or not isinstance(schema, Schema):
        return None
    return schema


def store_schema_cache(directory: str, schema: "Schema") -> bool:
    """Save a parsed schema next to nothing in particular; best effort."""
    if os.environ.get(_NO_CACHE_ENV):
        return False
    key = _schema_cache_key(directory)
    if key is None:
        return False
    path = _schema_cache_path(directory)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            pickle.dump((key, schema), fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
        return True
    except (OSError, pickle.PicklingError, RecursionError):
        try:
            os.unlink(tmp)
        except (OSError, UnboundLocalError, NameError):
            pass
        return False


def user_cache_dir() -> str:
    """Where x3ctl keeps plugin sets it downloads itself."""
    from x3 import paths

    return str(paths.cache_dir() / "plugins")


def search_bases() -> list[str]:
    """Directories that may hold `<version>-<build_id>` plugin caches."""
    out = []
    winbox = default_cache_dir()
    if winbox:
        out.append(winbox)
    out.append(user_cache_dir())
    return out


def find_exact_dir(version: str, build_id) -> str | None:
    """An existing cache directory for this precise firmware build, if any."""
    for base in search_bases():
        candidate = os.path.join(base, f"{version}-{build_id}")
        if glob.glob(os.path.join(candidate, "*.jg")):
            return candidate
    return None


def default_cache_dir() -> str | None:
    """The Winbox plugin cache Wine/Windows writes, if present."""
    import getpass

    from .cdb import CLIENT_DIRS

    user = getpass.getuser()
    vendor = CLIENT_DIRS[0].split("/", 1)[0]
    roots = [os.environ["APPDATA"]] if os.environ.get("APPDATA") else []
    roots += [
        os.path.expanduser(f"~/.wine/drive_c/users/{user}/AppData/Roaming"),
        os.path.expanduser("~/AppData/Roaming"),
        os.path.expanduser(f"~/Documents/{vendor}Data"),
    ]
    # Only version 3 keeps a usable cache here: one folder per firmware build,
    # plain .jg files. Version 4's cache is hashed .jg.gz names without the
    # build folders, so x3 fetches its own from the router instead.
    for base in (os.path.join(root, d) for root in roots for d in CLIENT_DIRS):
        if os.path.isdir(base):
            return base
    return None


def build_id_of_dir(directory: str) -> int | None:
    """The build id (roteros.jg CRC) a cache directory holds, or None.

    ``roteros.crc`` is the device's own word for it; the ``<version>-<id>``
    directory name is the fallback for a set saved without its .crc files.
    """
    try:
        with open(os.path.join(directory, "roteros.crc")) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        pass
    tail = os.path.basename(os.path.normpath(directory)).rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _cache_dir_rank(directory: str) -> tuple:
    """Order for guessing between same-version caches: fullest set first.

    One firmware version can be cached several times over -- one directory per
    package set, and (here) one per build of our own router, which advertises
    a firmware version it does not implement. With no build id to match on,
    any of them is a guess, so guess the most complete one: a schema that
    offers a menu the device lacks is recoverable, one that hides a menu the
    device has looks like the menu does not exist. Ties break on build id, so
    the answer is stable rather than filesystem-order.
    """
    try:
        size = os.path.getsize(os.path.join(directory, "roteros.jg"))
    except OSError:
        size = 0
    return (size, build_id_of_dir(directory) or -1)


def find_version_dir(
    version: str, base: str | None = None, *, build_id: int | None = None
) -> str | None:
    """Locate a '<version>-<build_id>' cache directory by version prefix.

    With a ``build_id`` the set that firmware actually serves wins outright.
    Without one -- or when this device's build was never cached -- the pick
    falls to :func:`_cache_dir_rank` rather than to the lexicographically last
    name, which sorted '922503154' above '151146055' and so preferred a 5 KB
    stub over the real 918 KB set.
    """
    bases = [base] if base else search_bases()
    hits = []
    for b in bases:
        hits += [d for d in glob.glob(os.path.join(b, f"{version}-*"))
                 if glob.glob(os.path.join(d, "*.jg"))]
    if not hits:
        return None
    if build_id is not None:
        exact = [d for d in hits if build_id_of_dir(d) == int(build_id)]
        if exact:
            return max(exact, key=_cache_dir_rank)
    return max(hits, key=_cache_dir_rank)


def resolve_schema_dir(
    *,
    explicit: str | None = None,
    version: str | None = None,
    build_id: int | None = None,
    search_paths: list[str] | None = None,
) -> str | None:
    """Find a plugin-cache directory with .jg files.

    Order: explicit path → exact ``version-build_id`` under known bases →
    version prefix match → newest cache under bases → first search_paths hit.
    """
    if explicit and os.path.isdir(explicit):
        return explicit

    bases: list[str] = []
    # user_cache_dir() is where --decode drops plugin sets it downloads itself,
    # so it has to be searched alongside Winbox's own cache.
    for b in (default_cache_dir(), user_cache_dir(), *(search_paths or [])):
        if b and os.path.isdir(b) and b not in bases:
            bases.append(b)
    # repo-local mirrors (cwd relatives)
    for rel in (".", "mirror"):
        if os.path.isdir(rel):
            bases.append(rel)

    if version and build_id is not None:
        name = f"{version}-{build_id}"
        for base in bases:
            cand = os.path.join(base, name)
            if os.path.isdir(cand) and glob.glob(os.path.join(cand, "*.jg*")):
                return cand
            # also allow base itself named that way
            if os.path.basename(os.path.normpath(base)) == name and glob.glob(
                os.path.join(base, "*.jg*")
            ):
                return base

    if version:
        # A build id we hold outranks directory order: several caches can share
        # a version and only one of them is this firmware's.
        if build_id is not None:
            for base in bases:
                hit = find_version_dir(version, base, build_id=build_id)
                if hit and build_id_of_dir(hit) == int(build_id):
                    return hit
        for base in bases:
            hit = find_version_dir(version, base)
            if hit:
                return hit

    # Newest directory containing .jg under each base
    candidates: list[str] = []
    for base in bases:
        if glob.glob(os.path.join(base, "*.jg*")):
            candidates.append(base)
        for d in glob.glob(os.path.join(base, "*")):
            if os.path.isdir(d) and glob.glob(os.path.join(d, "*.jg*")):
                candidates.append(d)
    if not candidates:
        return None
    # Newest, but only among sets big enough to be firmware: this is the branch
    # taken when the device told us nothing at all, and a partial set (our own
    # router serves a few menus under a real firmware version) would otherwise win
    # the moment it was written.
    firmware = [d for d in candidates if _looks_like_firmware(d)]
    pool = firmware or candidates
    want = _version_key(version) if version else None
    if want:
        # The device named a version we hold no set for. The nearest release
        # of the same major is a far better guess than whatever was cached
        # last: newest-by-mtime handed a 7.25 device a 6.49 schema, whose IP
        # menu has no Services entry for that firmware to answer.
        return max(pool, key=lambda d: _nearness(_version_key(_dir_version(d)), want)
                   + (os.path.getmtime(d),))
    return max(pool, key=os.path.getmtime)


def _dir_version(directory: str) -> str:
    """'7.24.4' from a '<version>-<build_id>' cache directory name."""
    return os.path.basename(os.path.normpath(directory)).rsplit("-", 1)[0]


def _version_key(version: str) -> tuple[int, ...] | None:
    """(7, 24, 4) from '7.24.4'; None when it is not a dotted release."""
    mo = re.match(r"(\d+(?:\.\d+)*)", version or "")
    return tuple(int(p) for p in mo.group(1).split(".")) if mo else None


def _nearness(have: tuple[int, ...] | None, want: tuple[int, ...]) -> tuple:
    """Sort key: same major first, then not newer than the device, then closest."""
    if not have:
        return (0, 0, ())
    return (have[0] == want[0], have <= want, have if have <= want else tuple(-p for p in have))


#: A full the router ``roteros.jg`` is ~1 MB; every real one in the corpus (6.0 → 7.24)
#: is above 300 KB. Anything this small describes a handful of menus, so it is a
#: stub -- ours, or a partial download -- and never the best guess for a device.
_FIRMWARE_JG_MIN = 100_000


def _looks_like_firmware(directory: str) -> bool:
    """Whether a cache directory holds a device-sized ``roteros.jg``."""
    try:
        return os.path.getsize(os.path.join(directory, "roteros.jg")) >= _FIRMWARE_JG_MIN
    except OSError:
        return False
