#!/usr/bin/env python3
"""Write the original SVG icon set to x3/ui/resources/icons/svg/."""

from x3.ui.iconset import SVG_MONO_DIR, write_svg_files

if __name__ == "__main__":
    dest = write_svg_files()
    print(f"wrote color SVGs to {dest}")
    print(f"wrote symbolic SVGs to {SVG_MONO_DIR}")
