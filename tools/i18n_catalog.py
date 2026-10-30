#!/usr/bin/env python3
"""Collect every English string x3 can show, for translators.

    python tools/i18n_catalog.py                      # refresh tools/i18n-catalog.json
    python tools/i18n_catalog.py --plugins DIR ...    # also read these plugin sets
    python tools/i18n_catalog.py --new de             # start de.json with every string
    python tools/i18n_catalog.py --update fr          # add strings fr.json lacks

Two sources:

* the router's labels -- ``name``, ``title``, ``tab``, ``group`` (a submenu),
  ``fulltitle`` and ``confirm`` (a question asked before a command) in every ``.jg`` file
  of every plugin set found (the reference client's caches, x3's own, and any
  ``--plugins`` folder). Internal identifiers (one lowercase word, such as
  ``adddns``) are left out: x3 never shows them.
* x3's own text -- the string literals in ``x3/ui`` that read like text for
  a person (see :func:`x3_strings`).

The catalog records how often each string occurs, so a translator can start
with what is seen most. It lives beside this tool, not in the package, and
only grows: router labels found today are added to the ones already listed, so
a run on a machine with no plugin sets keeps every label the catalog had. A
language file only ever gains keys here; nothing a translator wrote is
overwritten.
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "x3" / "ui" / "i18n"
CATALOG = ROOT / "tools" / "i18n-catalog.json"
sys.path.insert(0, str(ROOT))

_LABEL = re.compile(r"\b(?:name|title|tab|group|fulltitle|confirm)\s*:\s*'((?:[^'\\]|\\.)*)'")
_IDENTIFIER = re.compile(r"^[a-z0-9_\-.]+$")


def plugin_dirs(extra: list[str]) -> list[str]:
    from x3 import paths
    from x3.proto.cdb import CLIENT_DIRS

    home = Path.home()
    roots = [Path(os.environ["APPDATA"])] if os.environ.get("APPDATA") else []
    roots += [home / ".wine/drive_c/users" / home.name / "AppData/Roaming",
              home / "AppData/Roaming"]
    bases = [r / d for r in roots for d in CLIENT_DIRS]
    bases += [paths.cache_dir() / "plugins", home / ".cache/winboxctl/plugins", ROOT]
    found = []
    for base in bases + [Path(e) for e in extra]:
        if not base.is_dir():
            continue
        if glob.glob(str(base / "*.jg")):
            found.append(str(base))
        for d in sorted(base.iterdir()):
            if d.is_dir() and glob.glob(str(d / "*.jg")):
                found.append(str(d))
    return sorted(set(found))


def router_strings(dirs: list[str]) -> Counter:
    out: Counter = Counter()
    for d in dirs:
        for f in glob.glob(os.path.join(d, "*.jg")):
            text = Path(f).read_text(encoding="latin-1")
            for m in _LABEL.finditer(text):
                s = m.group(1).replace("\\'", "'").strip()
                if len(s) > 1 and not _IDENTIFIER.match(s) and not s.isdigit():
                    out[s] += 1
    return out


#: Calls whose string arguments are never shown: logging, lookups, styling.
_NOT_SHOWN = {
    "debug", "info", "exception", "getLogger", "setObjectName", "setProperty",
    "setStyleSheet", "compile", "match", "search", "sub", "startswith", "endswith",
    "split", "rsplit", "partition", "join", "replace", "strip", "get", "pop",
    "setdefault", "getattr", "hasattr", "setattr", "isinstance", "open", "Path",
    "QKeySequence", "QColor", "QFont", "QIcon", "icon_for_kind", "action_icon",
    "window_icon", "getenv", "connect", "emit", "encode", "decode", "index",
    "find", "count", "format", "strftime", "strptime", "fromisoformat", "glob",
    "setStyle", "setApplicationName", "setOrganizationName", "setDesktopFileName",
}
_LOGGERS = {"log", "_log", "logger", "LOG", "applog"}
_SHORTCUT = re.compile(r"^(Ctrl|Alt|Shift|Meta|F\d)\b")
_CONSTANT = re.compile(r"^[A-Z0-9_]+$")
_CAMEL = re.compile(r"^[A-Za-z][a-z0-9]+[A-Z][A-Za-z0-9]*$")    # setText, TitleTabBar
_SVG_PATH = re.compile(r"^[MLHVCSQTAZmlhvcsqtaz0-9.,\s\-]+$")
#: Modules holding no text for people: icon drawings and their notes.
_NO_TEXT = {"iconset.py"}


def _shown(s: str) -> bool:
    """Whether a string constant reads like text for a person."""
    return (
        bool(s.strip()) and any(ch.isalpha() for ch in s) and len(s) <= 4000
        and not _IDENTIFIER.match(s) and not _CONSTANT.match(s)
        and not _SHORTCUT.match(s) and not _CAMEL.match(s) and not _SVG_PATH.match(s)
        and "{" not in s and "%s" not in s
        and "%d" not in s and "%(" not in s and "://" not in s and "\\" not in s
        and s[0] not in "/~.#-_*;:=@$"
        and any(ch.isalpha() for ch in re.sub(r"<[^>]*>", "", s))   # not bare markup
        and not re.match(r"^[<>!=@][A-Za-z0-9]+$", s)                # struct formats
        and not ("=" in s and " " not in s)
    )


def x3_strings() -> Counter:
    """Every string x3's own code may put on screen.

    Any literal that reads like text: what goes to ``QLabel``, ``setToolTip``
    and the other calls :func:`x3.ui.i18n.install` translates, and just as
    much what sits in a table first -- theme names, menu choices, tooltips in
    a dict. Left out: docstrings, pieces of f-strings, and arguments to calls
    that never show them (logging, lookups, styling). A literal handed to
    ``tr()`` or ``trf()`` always counts. A few extra entries cost a translator a minute; a
    missing one leaves a word in English.
    """
    out: Counter = Counter()
    files = sorted((ROOT / "x3" / "ui").rglob("*.py")) + [ROOT / "x3" / "proto" / "audit.py"]
    for path in files:
        if path.name in _NO_TEXT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip: set[int] = set()
        for node in ast.walk(tree):
            # A literal handed to tr() or trf() is meant for translators
            # whatever it holds, a ``{n}`` placeholder included -- and so is
            # each side of a template picked by a condition.
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) in ("tr", "trf") and node.args):
                first = node.args[0]
                for arg in ((first.body, first.orelse) if isinstance(first, ast.IfExp)
                            else (first,)):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        out[arg.value] += 1
                        skip.add(id(arg))
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                skip.add(id(node.value))             # docstrings and the like
            elif isinstance(node, ast.JoinedStr):
                skip.update(id(v) for v in node.values)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                owner = getattr(func, "value", None)
                if (name in _NOT_SHOWN
                        or (isinstance(owner, ast.Name) and owner.id in _LOGGERS)):
                    for arg in list(node.args) + [k.value for k in node.keywords]:
                        skip.add(id(arg))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skip and _shown(node.value)):
                out[node.value] += 1
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--plugins", nargs="*", default=[], help="extra plugin-set folders")
    p.add_argument("--new", metavar="CODE", help="start CODE.json with every string")
    p.add_argument("--name", help="the language's own name, with --new (e.g. Deutsch)")
    p.add_argument("--update", metavar="CODE", help="add the strings CODE.json lacks")
    args = p.parse_args(argv)

    dirs = plugin_dirs(args.plugins)
    router = router_strings(dirs)
    own = x3_strings()
    before = (json.loads(CATALOG.read_text(encoding="utf-8"))
              if CATALOG.exists() else {"sources": {}, "strings": []})
    for entry in before["strings"]:
        if entry.get("router"):
            router[entry["text"]] = max(router.get(entry["text"], 0), entry["router"])
    catalog = {
        "sources": {"plugin_sets": max(len(dirs), before["sources"].get("plugin_sets", 0))},
        "strings": [
            {"text": s, "router": router.get(s, 0), "x3": own.get(s, 0)}
            for s in sorted(set(router) | set(own),
                            key=lambda s: (-(router.get(s, 0) + 10 * own.get(s, 0)), s))
        ],
    }
    CATALOG.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(catalog['strings'])} strings from {len(dirs)} plugin sets and x3 itself")

    code = args.new or args.update
    if code:
        target = I18N / f"{code}.json"
        if args.new and target.exists():
            print(f"{target} exists; use --update", file=sys.stderr)
            return 1
        data = (json.loads(target.read_text(encoding="utf-8")) if target.exists()
                else {"name": args.name or code, "strings": {}})
        strings = data.setdefault("strings", {})
        added = 0
        for entry in catalog["strings"]:
            if entry["text"] not in strings:
                strings[entry["text"]] = ""
                added += 1
        target.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
        missing = sum(1 for v in strings.values() if not v)
        print(f"{target.name}: {added} added, {missing} empty (kept in English, or still to do)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
