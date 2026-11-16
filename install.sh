#!/bin/sh
# Installs Nudgy for the current user: the `nudgy` command, a systemd user service that starts it
# at login, and (optionally) Super+T / Super+Shift+T shortcuts. Never uses sudo.
#
#   ./install.sh                  install and start
#   ./install.sh --no-shortcut    don't add the Super+T and Super+Shift+T shortcuts
#   ./install.sh --no-start       install, but don't start or enable the service
#   ./install.sh --editable       for development: changes in this folder apply without reinstalling
#
# Remove it again with ./uninstall.sh.
set -eu

shortcut=yes start=yes editable=no
for arg in "$@"; do
    case "$arg" in
        --with-whatsapp) echo "WhatsApp support was removed from Nudgy; installing without it." ;;
        --no-shortcut) shortcut=no ;;
        --no-start) start=no ;;
        --editable) editable=yes ;;
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
bin="$HOME/.local/bin/nudgy"
# Older installs wrote a small wrapper script here; it is replaced by the installed package.
if [ -f "$bin" ] && [ ! -L "$bin" ] && grep -q PYTHONPATH "$bin" 2>/dev/null; then rm -f "$bin"; fi
editable_flag=""
[ "$editable" = yes ] && editable_flag="--editable"
# The package has no pip dependencies: GTK and cairo come from the system, hence system site packages.
if command -v pipx >/dev/null; then
    echo "Installing the nudgy package with pipx..."
    pipx install --force --system-site-packages $editable_flag "$here" >/dev/null
elif /usr/bin/python3 -c 'import ensurepip' 2>/dev/null; then
    echo "Installing the nudgy package into $data/app..."
    rm -rf "$data/app"
    /usr/bin/python3 -m venv --system-site-packages "$data/app"
    "$data/app/bin/pip" install -q --disable-pip-version-check $editable_flag "$here"
    ln -sf "$data/app/bin/nudgy" "$bin"
else
    # No pipx and no venv module: run straight from this folder, as before.
    printf '#!/bin/sh\n# Runs Nudgy from %s. Moving that folder breaks this; run install.sh again.\n# (Install pipx or python3-venv and run install.sh again for a package install.)\nexport PYTHONPATH="%s"\nexec /usr/bin/python3 -m nudgy "$@"\n' "$here" "$here" > "$bin"
    chmod 755 "$bin"
fi
"$bin" --version >/dev/null || fail "the nudgy command doesn't run; see the messages above."
install -m 644 "$here/nudgy.service" "$HOME/.config/systemd/user/nudgy.service"

# --- WhatsApp support was removed: clean up an older install's bridge service and its data ----------
if [ -e "$HOME/.config/systemd/user/nudgy-whatsapp.service" ] || [ -e "$data/whatsapp-auth" ]; then
    echo "Removing the old WhatsApp bridge. If \"Nudgy\" is still listed on your phone under WhatsApp >"
    echo "Linked devices, remove it there."
    systemctl --user disable --now nudgy-whatsapp.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/nudgy-whatsapp.service"
    rm -rf "$data/whatsapp-auth" "$data/whatsapp-chats.json" "$data/whatsapp-bridge.log"
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
