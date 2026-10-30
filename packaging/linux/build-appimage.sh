#!/usr/bin/env bash
# Build x3-<version>-x86_64.AppImage from the PyInstaller bundle.
#
#   pyinstaller --noconfirm packaging/pyinstaller/x3.spec   # dist/x3/
#   packaging/linux/build-appimage.sh                        # dist/x3-<v>-x86_64.AppImage
#
# appimagetool and the AppImage runtime are downloaded once (pinned versions,
# checksums verified) into build/. It runs with --appimage-extract-and-run, so FUSE is not needed on the
# build machine. The AppImage updates itself from the GitHub release (see
# x3/ui/updates.py); no zsync update information is embedded.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

TOOL_VERSION=1.9.1
# GitHub's published digest for that release asset.
TOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${TOOL_VERSION}/appimagetool-x86_64.AppImage"
TOOL="build/appimagetool-${TOOL_VERSION}-x86_64.AppImage"
# The runtime stub at the front of every AppImage. appimagetool would fetch
# the moving "continuous" build; pin a tagged one instead.
RUNTIME_VERSION=20251108
RUNTIME_SHA256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/${RUNTIME_VERSION}/runtime-x86_64"
RUNTIME="build/runtime-${RUNTIME_VERSION}-x86_64"
VERSION="$(python3 -c 'import re; print(re.search(r"__version__ = \"([^\"]+)\"", open("x3/__init__.py").read()).group(1))')"
APPDIR="build/x3.AppDir"
OUT="dist/x3-${VERSION}-x86_64.AppImage"

[ -x dist/x3/x3 ] || { echo "no dist/x3/x3: run pyinstaller first" >&2; exit 1; }

mkdir -p build
if [ ! -x "$TOOL" ]; then
    curl -fsSL -o "$TOOL.part" "$TOOL_URL"
    echo "$TOOL_SHA256  $TOOL.part" | sha256sum -c --quiet - \
        || { rm -f "$TOOL.part"; echo "appimagetool checksum mismatch" >&2; exit 1; }
    mv "$TOOL.part" "$TOOL"
    chmod +x "$TOOL"
fi

if [ ! -f "$RUNTIME" ]; then
    curl -fsSL -o "$RUNTIME.part" "$RUNTIME_URL"
    echo "$RUNTIME_SHA256  $RUNTIME.part" | sha256sum -c --quiet - \
        || { rm -f "$RUNTIME.part"; echo "AppImage runtime checksum mismatch" >&2; exit 1; }
    mv "$RUNTIME.part" "$RUNTIME"
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a dist/x3 "$APPDIR/usr/lib/x3"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# x3ctl when called through a link named x3ctl, or with "x3ctl" as the first
# argument; the desktop client otherwise.
HERE="$(dirname "$(readlink -f "$0")")"
case "$(basename "$ARGV0")" in
    x3ctl*) exec "$HERE/usr/lib/x3/x3ctl" "$@" ;;
esac
if [ "$1" = "x3ctl" ]; then
    shift
    exec "$HERE/usr/lib/x3/x3ctl" "$@"
fi
exec "$HERE/usr/lib/x3/x3" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/x3.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=x3
Comment=Router management over the Winbox protocol
Exec=x3
Icon=x3
Terminal=false
Categories=Network;
StartupWMClass=x3
EOF
cp "$APPDIR/x3.desktop" "$APPDIR/usr/share/applications/x3.desktop"
cp x3/ui/resources/appicon-256.png "$APPDIR/x3.png"
cp x3/ui/resources/appicon-256.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/x3.png"
ln -sf x3.png "$APPDIR/.DirIcon"

ARCH=x86_64 "$TOOL" --appimage-extract-and-run --no-appstream \
    --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
echo "built $OUT"
