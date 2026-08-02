#!/usr/bin/env bash
# Put Claude Browser in the XFCE application menu and on $PATH.
#
# Everything lands under $HOME -- no sudo, nothing outside your user account.
# The app itself keeps running from this checkout, so `git pull` updates the
# installed copy too; the desktop entry just points here.
#
#   ./install.sh                install
#   ./install.sh --set-default  install, and make it the system web browser
#   ./install.sh --uninstall    remove everything it created

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
HICOLOR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
BIN="$HOME/.local/bin"
DESKTOP="$APPS/claude-browser.desktop"
XFCE_HELPERS="${XDG_DATA_HOME:-$HOME/.local/share}/xfce4/helpers"
XFCE_HELPER="$XFCE_HELPERS/claude-browser.desktop"
XFCE_RC="${XDG_CONFIG_HOME:-$HOME/.config}/xfce4/helpers.rc"
MIMEAPPS="${XDG_CONFIG_HOME:-$HOME/.config}/mimeapps.list"
SIZES=(16 22 24 32 48 64 128 256 512)

# Everything that has an opinion about "the web browser". They are separate
# registries that do not consult each other, which is why this box could report
# claude-browser for x-scheme-handler/http and google-chrome for
# `xdg-settings get default-web-browser` at the same time.
WEB_MIMES=(
    x-scheme-handler/http
    x-scheme-handler/https
    x-scheme-handler/about
    x-scheme-handler/unknown
    text/html
    application/xhtml+xml
)

set_default_browser() {
    # 1. The freedesktop registry. Written directly as well as through
    #    xdg-settings, because xdg-settings is a shell script whose XFCE branch
    #    has historically only touched helpers.rc.
    python3 - "$MIMEAPPS" "${WEB_MIMES[@]}" <<'PY'
import configparser, sys, pathlib

path, mimes = pathlib.Path(sys.argv[1]), sys.argv[2:]
parser = configparser.RawConfigParser()
parser.optionxform = str          # mime types are case-sensitive keys
if path.exists():
    parser.read(path, encoding="utf-8")
for section in ("Default Applications", "Added Associations"):
    if not parser.has_section(section):
        parser.add_section(section)
for mime in mimes:
    parser.set("Default Applications", mime, "claude-browser.desktop")
    existing = parser.get("Added Associations", mime, fallback="")
    entries = [e for e in existing.split(";") if e and e != "claude-browser.desktop"]
    parser.set("Added Associations", mime, ";".join(["claude-browser.desktop"] + entries) + ";")
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("w", encoding="utf-8") as handle:
    parser.write(handle, space_around_delimiters=False)
print("  mimeapps.list  %s" % path)
PY

    # 2. xdg-settings, for anything that asks the portal rather than the file.
    if command -v xdg-settings >/dev/null; then
        xdg-settings set default-web-browser claude-browser.desktop 2>/dev/null \
            && echo "  xdg-settings   default-web-browser" \
            || echo "  xdg-settings   declined (mimeapps.list still applies)"
    fi

    # 3. XFCE keeps its own answer in helpers.rc and ignores the two above --
    #    this is what made `xdg-settings get` say google-chrome while every mime
    #    query said claude-browser. A browser XFCE does not ship a helper for
    #    needs one written by hand.
    mkdir -p "$XFCE_HELPERS" "$(dirname "$XFCE_RC")"
    cat > "$XFCE_HELPER" <<EOF
[Desktop Entry]
Version=1.0
Encoding=UTF-8
Type=X-XFCE-Helper
X-XFCE-HelperType=WebBrowser
X-XFCE-Binaries=claude-browser;
X-XFCE-Commands=$BIN/claude-browser
X-XFCE-CommandsWithParameter=$BIN/claude-browser "%s"
Icon=claude-browser
Name=Claude Browser
EOF
    if [[ -f "$XFCE_RC" ]] && grep -q '^WebBrowser=' "$XFCE_RC"; then
        sed -i 's|^WebBrowser=.*|WebBrowser=claude-browser|' "$XFCE_RC"
    else
        echo "WebBrowser=claude-browser" >> "$XFCE_RC"
    fi
    echo "  xfce4          $XFCE_RC"

    if command -v gio >/dev/null; then
        for m in "${WEB_MIMES[@]}"; do
            gio mime "$m" claude-browser.desktop >/dev/null 2>&1 || true
        done
    fi
    update-desktop-database "$APPS" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
    for s in "${SIZES[@]}"; do
        rm -f "$HICOLOR/${s}x${s}/apps/claude-browser.png"
    done
    rm -fv "$DESKTOP" "$HICOLOR/scalable/apps/claude-browser.svg" \
           "$BIN/claude-browser" "$BIN/cbctl" "$BIN/cb-mcp" "$XFCE_HELPER"
    # Leave the browser named as the default only if it still exists to be one.
    if [[ -f "$XFCE_RC" ]] && grep -q '^WebBrowser=claude-browser$' "$XFCE_RC"; then
        sed -i '/^WebBrowser=claude-browser$/d' "$XFCE_RC"
    fi
    if [[ -f "$MIMEAPPS" ]]; then
        sed -i '/claude-browser.desktop/d' "$MIMEAPPS"
    fi
    update-desktop-database "$APPS" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$HICOLOR" 2>/dev/null || true
    echo "Removed. The checkout at $HERE is untouched."
    exit 0
fi

mkdir -p "$APPS" "$BIN"

if [[ ! -f "$HERE/packaging/icons/claude-browser-48.png" ]]; then
    echo "Icons are missing. Generating them from logo.png..."
    python3 "$HERE/packaging/make-icons.py"
fi

for s in "${SIZES[@]}"; do
    mkdir -p "$HICOLOR/${s}x${s}/apps"
    install -m 0644 "$HERE/packaging/icons/claude-browser-${s}.png" \
                    "$HICOLOR/${s}x${s}/apps/claude-browser.png"
done
# An old scalable entry would outrank every sized PNG in icon lookup, so a
# stale one from a previous install has to go.
rm -f "$HICOLOR/scalable/apps/claude-browser.svg"

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
gtk-update-icon-cache -f -t "$HICOLOR" 2>/dev/null || true

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

if [[ "${1:-}" == "--set-default" ]]; then
    echo
    echo "Making Claude Browser the default web browser:"
    set_default_browser
fi

echo
echo "Installed:"
echo "  menu entry   $DESKTOP"
echo "  icons        $HICOLOR/{16x16..512x512}/apps/claude-browser.png"
echo "  commands     $BIN/{claude-browser,cbctl,cb-mcp}"
echo
echo "Find it in Applications > Internet, or search 'Claude Browser'."
if [[ "${1:-}" != "--set-default" ]]; then
    echo "Run ./install.sh --set-default to make it the system web browser."
fi
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN is not on your PATH -- add it to use cbctl from a shell." ;;
esac
