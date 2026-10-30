# Original vector icons

Every icon here was drawn for x3. They follow the *meanings* a router's `.jg`
plugins address (index 5 = interfaces, 16 = terminal, a plus means Add) so a
menu still looks like the thing it is, but they are our own paths: no pixels,
shapes or bitmaps are taken from any other client.

## How `.jg` names them

The plugins set `icon` / `groupIcon` to a **0-based cell index** (32 columns,
row-major). That number is the public name as far as the protocol is
concerned. Our slugs name what the *picture in that cell* is. A menu can reuse
a generic cell: Ping's `groupIcon` is **26**, the tools wrench. See
`INDEX_TO_NAME` and `JG_USED_BY` in `x3/ui/iconset.py`.

Toolbar actions (`Add`, `Remove`, …) are not `.jg` indices; they are loaded by
slug (`add.svg`, …).

## Replacing a glyph

1. Edit the matching file in `svg/<slug>.svg`, or
2. Drop an override in `~/.config/x3/icons/<slug>.svg` (wins over the shipped file).

Regenerate the shipped set from the Python originals:

```sh
python3 tools/build_icon_svgs.py
```

`svg-mono/` is the **symbolic** set: one ink (`currentColor`) you can tint to
any UI chrome, the same idea as GNOME symbolic icons.

Pick it in the app with View → **Icon Set** → *Monochrome*. The ink is the
active theme's text colour, so the glyphs follow a skin change instead of
staying black on a dark one. An override in `~/.config/x3/icons/<slug>.svg`
is converted on the fly, so it joins whichever set is selected.
