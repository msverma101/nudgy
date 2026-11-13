#!/bin/sh
# Removes Nudgy's command, services and shortcut.
#
#   ./uninstall.sh          keeps your settings, notes and sign-ins
#   ./uninstall.sh --purge  also deletes them: Google token, Signal/WhatsApp links, cached mail and chats
#
# Linked devices stay listed on your phone until you remove "Nudgy" there
# (Signal and WhatsApp > Settings > Linked devices). Google access: https://myaccount.google.com/permissions
set -eu

purge=no
case "${1:-}" in
    --purge) purge=yes ;;
    "") ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
esac

if [ "$purge" = yes ] && [ -x "$HOME/.local/bin/nudgy" ]; then
    "$HOME/.local/bin/nudgy" unlink whatsapp 2>/dev/null || true     # logs the bridge out on WhatsApp's side
fi
for unit in nudgy.service nudgy-whatsapp.service; do
    systemctl --user disable --now "$unit" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/$unit"
done
systemctl --user daemon-reload 2>/dev/null || true
if command -v pipx >/dev/null && pipx list --short 2>/dev/null | grep -q '^nudgy '; then
    pipx uninstall nudgy >/dev/null
fi
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/nudgy/app"     # the private install, if pipx wasn't used
rm -f "$HOME/.local/bin/nudgy"

media=org.gnome.settings-daemon.plugins.media-keys
for slot in nudgy nudgy-talk; do
    path=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/$slot/
    if command -v gsettings >/dev/null && gsettings get $media custom-keybindings 2>/dev/null | grep -q "$path"; then
        rest=$(gsettings get $media custom-keybindings | sed "s/@as //; s|, *'$path'||; s|'$path', *||; s|'$path'||")
        gsettings set $media custom-keybindings "$rest"
        gsettings reset-recursively $media.custom-keybinding:$path
    fi
done

config="${XDG_CONFIG_HOME:-$HOME/.config}/nudgy"
data="${XDG_DATA_HOME:-$HOME/.local/share}/nudgy"
if [ "$purge" = yes ]; then
    rm -rf "$config" "$data"
    echo "Nudgy and all its data are removed."
else
    echo "Nudgy is removed. Your settings and sign-ins are still in $config and $data"
    echo "(delete them with ./uninstall.sh --purge)."
fi
