#!/usr/bin/env bash
# Install an icon candidate as the app icon: renders the PNG sizes + .ico
# into x3/ui/resources/. Usage: ./render.sh x3-cube-front.svg
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:?usage: render.sh <candidate.svg>}"
[[ -f "$SRC" ]] || SRC="$HERE/$SRC"
RES="$HERE/../../x3/ui/resources"
mkdir -p "$RES"
cp "$SRC" "$RES/appicon.svg"
for px in 16 24 32 48 64 128 256 512; do
    inkscape -w "$px" -h "$px" "$RES/appicon.svg" -o "$RES/appicon-$px.png"
done
magick "$RES"/appicon-{16,24,32,48,64,128,256}.png "$RES/appicon.ico"
echo "installed $(basename "$SRC") -> $RES"
