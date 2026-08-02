#!/usr/bin/env bash
# Put Claude Browser in the XFCE application menu and on $PATH.
#
# Everything lands under $HOME -- no sudo, nothing outside your user account.
# The app itself keeps running from this checkout, so `git pull` updates the
# installed copy too; the desktop entry just points here.
#
#   ./install.sh              install
#   ./install.sh --uninstall  remove everything it created

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
BIN="$HOME/.local/bin"
DESKTOP="$APPS/claude-browser.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -fv "$DESKTOP" "$ICONS/claude-browser.svg" \
           "$BIN/claude-browser" "$BIN/cbctl" "$BIN/cb-mcp"
    update-desktop-database "$APPS" 2>/dev/null || true
    gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
    echo "Removed. The checkout at $HERE is untouched."
    exit 0
fi

mkdir -p "$APPS" "$ICONS" "$BIN"

install -m 0644 "$HERE/packaging/claude-browser.svg" "$ICONS/claude-browser.svg"

# Absolute Exec path: a desktop entry is launched by the session, which does not
# inherit your shell's PATH or working directory.
sed "s|@CB@|$HERE/cb|g" "$HERE/packaging/claude-browser.desktop.in" > "$DESKTOP"
chmod 0644 "$DESKTOP"

ln -sfn "$HERE/cb"     "$BIN/claude-browser"
ln -sfn "$HERE/cbctl"  "$BIN/cbctl"
ln -sfn "$HERE/cb-mcp" "$BIN/cb-mcp"

# XFCE's menu watches these directories, but the caches are what make the entry
# appear immediately rather than at next login.
update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

# Detached, and never waited on: `xfce4-panel --restart` re-execs the panel and
# keeps the calling terminal attached, so a plain call hangs this script until
# the session ends. Failure here is cosmetic -- the entry still appears, just at
# next login rather than immediately.
if command -v xfce4-panel >/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    setsid xfce4-panel --restart </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi

if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$DESKTOP" && echo "desktop entry validates"
fi

echo
echo "Installed:"
echo "  menu entry   $DESKTOP"
echo "  icon         $ICONS/claude-browser.svg"
echo "  commands     $BIN/{claude-browser,cbctl,cb-mcp}"
echo
echo "Find it in Applications > Internet, or search 'Claude Browser'."
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN is not on your PATH -- add it to use cbctl from a shell." ;;
esac
