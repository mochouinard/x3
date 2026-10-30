#!/usr/bin/env bash
# Desktop integration for x3 (user-level, no root):
#  - icons into ~/.local/share/icons/hicolor/  (name: x3)
#  - ~/.local/share/applications/x3.desktop pointing at this repo's venv
# GNOME matches the running app to the desktop file via the Wayland app_id
# ("x3", set in x3/ui/app.py) and takes the dock icon from it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RES="$REPO/x3/ui/resources"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

for px in 16 24 32 48 64 128 256 512; do
    install -Dm644 "$RES/appicon-$px.png" "$ICONS/${px}x${px}/apps/x3.png"
done
install -Dm644 "$RES/appicon.svg" "$ICONS/scalable/apps/x3.svg"

mkdir -p "$APPS"
cat > "$APPS/x3.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=x3
Comment=Router management over the Winbox protocol
Exec=$REPO/.venv/bin/x3
Icon=x3
Terminal=false
Categories=Network;
StartupWMClass=x3
StartupNotify=true
EOF

command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICONS" || true
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
echo "installed: $APPS/x3.desktop (icon: x3)"
