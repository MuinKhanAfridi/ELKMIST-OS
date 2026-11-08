#!/usr/bin/env bash
# Rebuild printer.cfg + slicer profile from machine.ini and restart services.
# Run after editing machine.ini:      ./update.sh
# Download a newer kit from GitHub:   ./update.sh --pull
# (Mainsail's "Update Manager" also runs this automatically after an update.)
set -Eeuo pipefail
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HOME/printer_data/config"

if [ "${1:-}" = "--pull" ]; then
  git -C "$KIT_DIR" pull --ff-only
fi

python3 "$KIT_DIR/tools/generate_config.py"
install -m 644 "$KIT_DIR/config/kit_macros.cfg" "$CONF/kit_macros.cfg"

if [ -x "$HOME/autoslice-env/bin/pip" ]; then
  "$HOME/autoslice-env/bin/pip" install --quiet -r "$KIT_DIR/autoslice/requirements.txt" || true
fi

# Restart Klipper (reload printer.cfg) - through Moonraker, no password needed
if ! curl -fsS -m 10 -X POST http://127.0.0.1:7125/printer/firmware_restart >/dev/null 2>&1; then
  sudo -n systemctl restart klipper 2>/dev/null || echo "Please restart Klipper from Mainsail (FIRMWARE_RESTART)."
fi
sudo -n systemctl restart autoslice 2>/dev/null || true
python3 "$KIT_DIR/tools/set_ui_name.py" || true
echo "Update finished."
