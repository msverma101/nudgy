#!/bin/sh
# Installs Nudgy for the current user: the `nudgy` command, a systemd user service that starts it
# at login, and (optionally) Super+T / Super+Shift+T shortcuts and the WhatsApp bridge. Never uses sudo.
#
#   ./install.sh                  install and start
#   ./install.sh --with-whatsapp  also install the unofficial WhatsApp bridge (needs Node.js 20+)
#   ./install.sh --no-shortcut    don't add the Super+T and Super+Shift+T shortcuts
#   ./install.sh --no-start       install, but don't start or enable the service
#
# Remove it again with ./uninstall.sh.
set -eu

whatsapp=no shortcut=yes start=yes
for arg in "$@"; do
    case "$arg" in
        --with-whatsapp) whatsapp=yes ;;
        --no-shortcut) shortcut=no ;;
        --no-start) start=no ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

fail() { echo "nudgy: $*" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
case "$here" in *[\"\$\`\\]*) fail "move the folder to a path without quotes, \$, \` or \\ first." ;; esac
[ "$(id -u)" -ne 0 ] || fail "run this as your normal user, not root. Nudgy installs only into your home folder."

# --- requirements --------------------------------------------------------------------------------
[ "$(uname -s)" = Linux ] || fail "Nudgy runs on Linux desktops only."
[ -x /usr/bin/python3 ] || fail "python3 is missing. On Ubuntu/Debian: sudo apt install python3"
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || fail "Python 3.10 or newer is needed; you have $(/usr/bin/python3 -V 2>&1)."
/usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null \
    || fail "GTK 3 for Python is missing. On Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0 python3-gi-cairo
        (Fedora: sudo dnf install python3-gobject gtk3 python3-cairo)"
/usr/bin/python3 -c 'import cairo, gi; gi.require_foreign("cairo")' 2>/dev/null \
    || fail "Cairo for Python (the pet's drawing) is missing. On Ubuntu/Debian: sudo apt install python3-gi-cairo
        (Fedora: sudo dnf install python3-cairo)"
command -v systemctl >/dev/null || fail "systemd is needed to start Nudgy at login."
if ! /usr/bin/python3 -c 'import gi; gi.require_version("AyatanaAppIndicator3", "0.1")' 2>/dev/null; then
    echo "Note: no top-bar icon support. For it: sudo apt install gir1.2-ayatanaappindicator3-0.1"
fi
if [ "$whatsapp" = yes ]; then
    command -v node >/dev/null && command -v npm >/dev/null || fail "--with-whatsapp needs Node.js 20+ and npm."
    node -e 'process.exit(parseInt(process.versions.node) < 20 ? 1 : 0)' \
        || fail "--with-whatsapp needs Node.js 20 or newer; you have $(node -v)."
fi

# --- upgrade from Taskpin (Nudgy's old name): stop it, keep its settings and sign-ins ---------------
config="${XDG_CONFIG_HOME:-$HOME/.config}/nudgy"
data="${XDG_DATA_HOME:-$HOME/.local/share}/nudgy"
if [ -e "$HOME/.config/systemd/user/taskpin.service" ] || [ -e "$HOME/.local/bin/taskpin" ]; then
    echo "Upgrading from Taskpin: your settings, sign-ins and linked devices are kept."
    for unit in taskpin.service taskpin-whatsapp.service; do
        systemctl --user disable --now "$unit" 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/$unit"
    done
    rm -f "$HOME/.local/bin/taskpin"
    old=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/taskpin/
    keys=org.gnome.settings-daemon.plugins.media-keys
    if command -v gsettings >/dev/null && gsettings get $keys custom-keybindings 2>/dev/null | grep -q "$old"; then
        rest=$(gsettings get $keys custom-keybindings | sed "s/@as //; s|, *'$old'||; s|'$old', *||; s|'$old'||")
        gsettings set $keys custom-keybindings "$rest"
        gsettings reset-recursively $keys.custom-keybinding:$old
    fi
fi
for pair in "${XDG_CONFIG_HOME:-$HOME/.config}/taskpin:$config" "${XDG_DATA_HOME:-$HOME/.local/share}/taskpin:$data"; do
    from=${pair%%:*} to=${pair#*:}
    if [ -d "$from" ] && [ ! -e "$to" ]; then mv "$from" "$to"; fi
done

# --- private folders for tokens and cached mail/chat text ------------------------------------------
mkdir -p "$config" "$data"
chmod 700 "$config" "$data"

# --- command and services ----------------------------------------------------------------------
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
cat > "$HOME/.local/bin/nudgy" <<EOF
#!/bin/sh
# Runs Nudgy from $here. Moving that folder breaks this; run install.sh again from the new place.
export PYTHONPATH="$here"
exec /usr/bin/python3 -m nudgy "\$@"
EOF
chmod 755 "$HOME/.local/bin/nudgy"
install -m 644 "$here/nudgy.service" "$HOME/.config/systemd/user/nudgy.service"
install -m 644 "$here/nudgy-whatsapp.service" "$HOME/.config/systemd/user/nudgy-whatsapp.service"

if [ "$whatsapp" = yes ]; then
    # Exact versions from package-lock.json. Install scripts are skipped: the two packages that have
    # them only check the Node version and print notices, and skipping them means no third-party
    # code runs during the install.
    (cd "$here/bridges/whatsapp" && npm ci --omit=dev --ignore-scripts --no-fund --no-audit)
    (cd "$here/bridges/whatsapp" && npm audit --omit=dev) || echo "Warning: npm audit reported issues (above)."
fi

systemctl --user daemon-reload
if [ "$start" = yes ]; then
    systemctl --user enable --now nudgy.service
fi

# --- Super+T shows or hides the pet, Super+Shift+T talks to it; skipped if the keys are taken ------------
media=org.gnome.settings-daemon.plugins.media-keys
# add_shortcut SLOT KEYS "LABEL" NAME COMMAND
add_shortcut() {
    path=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/$1/
    taken=no
    gsettings get $media custom-keybindings | grep -q "$path" && taken=ours
    if [ "$taken" = no ]; then
        for p in $(gsettings get $media custom-keybindings | tr -d "[],'@as"); do
            gsettings get $media.custom-keybinding:"$p" binding | grep -qi "'$2'" && taken=yes
        done
        gsettings list-recursively org.gnome.desktop.wm.keybindings 2>/dev/null | grep -qi "'$2'" && taken=yes
        gsettings list-recursively org.gnome.shell.keybindings 2>/dev/null | grep -qi "'$2'" && taken=yes
    fi
    if [ "$taken" = no ]; then
        gsettings set $media.custom-keybinding:$path name "$4"
        gsettings set $media.custom-keybinding:$path command "$HOME/.local/bin/nudgy $5"
        gsettings set $media.custom-keybinding:$path binding "$2"
        current=$(gsettings get $media custom-keybindings | sed "s/@as //; s/\[\]//; s/]$//; s/^\[//")
        gsettings set $media custom-keybindings "[${current:+$current, }'$path']"
    elif [ "$taken" = yes ]; then
        echo "$3 is already in use, so no shortcut was added. Bind \`nudgy $5\` yourself if you like."
    fi
}
if [ "$shortcut" = yes ] && command -v gsettings >/dev/null && gsettings list-schemas | grep -qx $media; then
    add_shortcut nudgy '<Super>t' "Super+T" 'Nudgy: show or hide the pet' toggle
    add_shortcut nudgy-talk '<Shift><Super>t' "Super+Shift+T" 'Nudgy: talk to the pet' talk
fi

echo
echo "Nudgy is installed. Next: connect your accounts (see README.md, \"Connecting Google\")."
echo "Everything stays on this computer. Features that send text to an AI model are off until you turn them on."
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH to use the \`nudgy\` command." ;; esac
