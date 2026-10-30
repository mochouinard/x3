"""``python -m x3.ui``: the desktop client (same as ``python -m x3``)."""

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
