#!/usr/bin/env bash
# Build Klipper firmware for the Arduino Due and flash it.
#   1. Connect the Due's "Programming" USB port (the one next to the power jack)
#      to the Raspberry Pi.
#   2. Run:  ~/printer-kit/tools/flash_mcu.sh
# Afterwards run ~/printer-kit/update.sh so printer.cfg finds the board.
set -Eeuo pipefail
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KLIPPER="$HOME/klipper"
[ -d "$KLIPPER" ] || { echo "Klipper is not installed. Run install.sh first."; exit 1; }

COMM="$(python3 "$KIT_DIR/tools/ini_get.py" mcu comm serial)"
case "$COMM" in
  serial) FRAG="$KIT_DIR/firmware/due_serial.config" ;;
  usb)    FRAG="$KIT_DIR/firmware/due_usb.config" ;;
  *) echo "machine.ini [mcu] comm must be 'serial' or 'usb'"; exit 1 ;;
esac
echo "Firmware profile: $(basename "$FRAG")"

cd "$KLIPPER"
cp "$FRAG" .config
make olddefconfig >/dev/null
grep -q '^CONFIG_MACH_SAM3X8E=y' .config || { echo "Firmware config did not apply."; exit 1; }
make clean >/dev/null
make -j"$(nproc)"

# find the Due's programming port
PORT=""
for p in /dev/serial/by-id/usb-Arduino*; do
  [ -e "$p" ] && PORT="$(readlink -f "$p")" && break
done
[ -n "$PORT" ] || PORT="$(ls /dev/ttyACM* 2>/dev/null | head -1 || true)"
[ -n "$PORT" ] || { echo "Arduino Due not found. Check the USB cable (Programming port) and try again."; exit 1; }
echo "Using port: $PORT"

sudo systemctl stop klipper 2>/dev/null || true
make flash FLASH_DEVICE="$PORT"
sleep 4
sudo systemctl start klipper 2>/dev/null || true
echo
echo "Flashing done. Now run:  $KIT_DIR/update.sh"
