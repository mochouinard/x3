"""``x3ctl mcp [--cdb PATH]`` (or ``python -m x3.mcp``): the x3 MCP server on stdio."""

from __future__ import annotations

import argparse
import sys

from .protocol import Server, serve
from .tools import INSTRUCTIONS, SavedDevices, ToolBox


def main(argv: list[str] | None = None, *, prog: str = "python -m x3.mcp") -> int:
    p = argparse.ArgumentParser(prog=prog, description=__doc__)
    p.add_argument("--cdb", help="Addresses.cdb to read (default: the one x3 finds).")
    args = p.parse_args(argv)
    # The package's own version, not its metadata: a packaged build has none.
    from .. import __version__ as ver

    source = SavedDevices(args.cdb)
    try:
        serve(Server(ToolBox(source).definitions(), version=ver, instructions=INSTRUCTIONS))
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
