#!/usr/bin/env bash
# Removes what install.sh installed (Klipper, Moonraker, Mainsail, Auto Slicer, web server settings).
# The kit folder itself (~/printer-kit) is NEVER deleted.
#
#   ./tools/uninstall.sh                  remove programs, KEEP your data (~/printer_data)
#   ./tools/uninstall.sh --delete-data    also delete ~/printer_data (config, G-code files, logs)
#   ./tools/uninstall.sh --remove-nginx   also remove the nginx web server package
#   ./tools/uninstall.sh --remove-packages  also remove PrusaSlicer + build tools installed by the kit
#   ./tools/uninstall.sh --dry-run        only show what would be done
#   ./tools/uninstall.sh --yes            do not ask for confirmation
set -Eeuo pipefail

DELETE_DATA=0; RM_NGINX=0; RM_PKGS=0; DRY=0; YES=0
for a in "$@"; do
  case "$a" in
    --delete-data) DELETE_DATA=1 ;;
    --remove-nginx) RM_NGINX=1 ;;
    --remove-packages) RM_PKGS=1 ;;
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n 2,11p "$0"; exit 0 ;;
    *) echo "Unknown option: $a"; exit 1 ;;
  esac
done
[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not root."; exit 1; }

run() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; else "$@" || true; fi; }

echo "This will remove: Klipper, Moonraker, Mainsail, Auto Slicer and their services."
[ "$DELETE_DATA" -eq 1 ] && echo "  + DELETE ~/printer_data (config, G-code files, logs)" || echo "  (your data in ~/printer_data is kept)"
[ "$RM_NGINX" -eq 1 ] && echo "  + remove the nginx package"
[ "$RM_PKGS" -eq 1 ] && echo "  + remove PrusaSlicer and build tools"
echo "  ~/printer-kit is NOT touched."
if [ "$YES" -ne 1 ] && [ "$DRY" -ne 1 ]; then
  read -r -p "Continue? type yes: " ans
  [ "$ans" = "yes" ] || { echo "Cancelled."; exit 0; }
fi
[ "$DRY" -eq 1 ] || sudo -v

echo "==> Stopping services"
run sudo systemctl disable --now klipper moonraker autoslice
run sudo rm -f /etc/systemd/system/klipper.service /etc/systemd/system/moonraker.service /etc/systemd/system/autoslice.service
run sudo rm -f /etc/polkit-1/rules.d/moonraker.rules /usr/share/polkit-1/rules.d/moonraker.rules \
    /etc/polkit-1/localauthority/50-local.d/10-moonraker.pkla
run sudo systemctl daemon-reload

echo "==> Removing web server settings"
run sudo rm -f /etc/nginx/sites-enabled/printer /etc/nginx/sites-available/printer
if [ "$RM_NGINX" -eq 1 ]; then
  run sudo systemctl disable --now nginx
  run sudo apt-get purge -y nginx nginx-common
else
  [ -e /etc/nginx/sites-available/default ] && run sudo ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
  run sudo systemctl restart nginx
fi

echo "==> Removing program folders"
for d in klipper klippy-env moonraker moonraker-env mainsail mainsail-config autoslice-env; do
  run rm -rf "$HOME/$d"
done

if [ "$DELETE_DATA" -eq 1 ]; then
  echo "==> Deleting ~/printer_data"
  run rm -rf "$HOME/printer_data"
fi

if [ "$RM_PKGS" -eq 1 ]; then
  echo "==> Removing packages"
  run sudo apt-get purge -y prusa-slicer xvfb gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi
  run sudo apt-get autoremove -y
fi

echo
echo "Done. To install again:  cd ~/printer-kit && bash install.sh"
