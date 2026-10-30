# x3 — icon sources

"x3" reads as x³, so the mark is a cube: a box with three dimensions, the 3 is
the geometry itself. All colors come from the app's own themes (`x3/ui/style.py`),
and every glyph is a plain path — no font dependencies.

| File | Look | Notes |
| --- | --- | --- |
| `x3-cube-front.svg` | Face-on 3D box, white x³ | **Shipped** — installed in `x3/ui/resources/` |
| `x3-cube-iso.svg` | Isometric cube, x over the front edge | Prettier large, softer at 16 px |
| `x3-terminal.svg` | Slate console tile, green `x3` + cursor | Says "CLI" more than "box" |
| `x3-classic.svg` | Win32 bevel gray, navy x3 | Pairs with the Classic skin |

## Switching candidates

```sh
./render.sh x3-terminal.svg
```

Renders the eight PNG sizes plus `appicon.ico` into `x3/ui/resources/`
(needs `inkscape` and ImageMagick's `magick`). The Qt loader in
`x3/ui/icons.py` picks the files up automatically; if they're ever missing
it falls back to painting the cube-front mark itself.
