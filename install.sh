#!/usr/bin/env bash
# =====================================================================
#  Printer Kit installer
#  Installs: Klipper + Moonraker + Mainsail (web UI) + nginx +
#            PrusaSlicer (command line) + Auto-Slicer web page
#  Target:   Raspberry Pi 5, Raspberry Pi OS 64-bit (Debian 12/13)
#  Run as the normal user (NOT root):   ./install.sh [--flash]
#  Safe to run again: it updates instead of duplicating.
# =====================================================================
set -Eeuo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
source "$KIT_DIR/versions.env"

# Self-heal: files copied from Windows/zip may lose the "executable" flag
chmod +x "$KIT_DIR"/*.sh "$KIT_DIR"/tools/*.sh "$KIT_DIR"/tools/*.py 2>/dev/null || true
git -C "$KIT_DIR" config core.fileMode false 2>/dev/null || true

DATA="$HOME/printer_data"
CONF="$DATA/config"
LOGDIR="$DATA/logs"
USER_NAME="$(id -un)"
DO_FLASH=0
for a in "$@"; do [ "$a" = "--flash" ] && DO_FLASH=1; done

mkdir -p "$LOGDIR"
exec > >(tee -a "$LOGDIR/kit-install.log") 2>&1

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_off=$'\e[0m'
step() { echo; echo "${c_ok}==> $*${c_off}"; }
warn() { echo "${c_warn}WARNING: $*${c_off}"; WARNINGS+=("$*"); }
WARNINGS=()
trap 'echo "${c_err}Installation stopped at line $LINENO. See $LOGDIR/kit-install.log${c_off}"' ERR

# ---------------------------------------------------------------- checks
step "Checking the system"
[ "$(id -u)" -ne 0 ] || { echo "Do not run as root. Run as your normal user."; exit 1; }
command -v apt-get >/dev/null || { echo "This installer needs Raspberry Pi OS / Debian."; exit 1; }
sudo -v
ARCH="$(uname -m)"
case "$ARCH" in aarch64|armv7l|x86_64) ;; *) warn "Unusual CPU type: $ARCH" ;; esac
[ "$ARCH" = "aarch64" ] || warn "64-bit Raspberry Pi OS is recommended (found $ARCH)."
[ -r /proc/device-tree/model ] && echo "Hardware: $(tr -d '\0' < /proc/device-tree/model)"
FREE_MB="$(df -Pm "$HOME" | awk 'NR==2{print $4}')"
[ "${FREE_MB:-0}" -ge 3000 ] || { echo "Need at least 3 GB free disk space (have ${FREE_MB} MB)."; exit 1; }
command -v curl >/dev/null 2>&1 || { sudo apt-get update && sudo apt-get install -y curl ca-certificates git; }
curl -fsS -m 15 -o /dev/null https://github.com || { echo "No internet connection to github.com."; exit 1; }
[ "$KIT_DIR" = "$HOME/printer-kit" ] || warn "Kit is in $KIT_DIR, not ~/printer-kit. One-click updates from Mainsail will not work."

# ---------------------------------------------------------------- packages
step "Installing system packages (this takes a few minutes)"
sudo apt-get update
BASE_PKGS="git curl wget unzip ca-certificates nginx python3 python3-venv python3-dev python3-pip \
build-essential libffi-dev libncurses-dev libusb-1.0-0-dev pkg-config xvfb avahi-daemon"
ARM_PKGS="gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi"
# shellcheck disable=SC2086
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $BASE_PKGS $ARM_PKGS
sudo usermod -aG dialout,tty "$USER_NAME" || true

# ---------------------------------------------------------------- folders
step "Creating folders"
mkdir -p "$DATA"/{config,logs,gcodes,comms,systemd,autoslice}
chmod o+x "$HOME"                      # lets nginx read ~/mainsail

# helper: clone or update a git repo (optionally at a fixed ref)
clone_or_update() {  # repo dir ref
  local repo="$1" dir="$2" ref="${3:-}"
  if [ -d "$dir/.git" ]; then
    git -C "$dir" fetch --tags --quiet origin || warn "Could not update $dir"
  else
    git clone --quiet "$repo" "$dir"
  fi
  [ -z "$ref" ] || git -C "$dir" checkout --quiet "$ref"
  if [ -z "$ref" ]; then git -C "$dir" pull --ff-only --quiet || true; fi
}

# ---------------------------------------------------------------- config files
step "Creating printer configuration from machine.ini"
python3 "$KIT_DIR/tools/generate_config.py" || warn "Config generation reported a problem"
install -m 644 "$KIT_DIR/config/kit_macros.cfg" "$CONF/kit_macros.cfg"

# ---------------------------------------------------------------- Klipper
step "Installing Klipper"
clone_or_update "$KLIPPER_REPO" "$HOME/klipper" "$KLIPPER_REF"
[ -d "$HOME/klippy-env" ] || python3 -m venv "$HOME/klippy-env"
"$HOME/klippy-env/bin/pip" install --quiet --upgrade pip
"$HOME/klippy-env/bin/pip" install --quiet -r "$HOME/klipper/scripts/klippy-requirements.txt"
sudo tee /etc/systemd/system/klipper.service >/dev/null <<EOF
[Unit]
Description=Klipper 3D printer firmware host
After=network.target

[Service]
Type=simple
User=$USER_NAME
ExecStart=$HOME/klippy-env/bin/python $HOME/klipper/klippy/klippy.py $CONF/printer.cfg -I $DATA/comms/klippy.serial -l $LOGDIR/klippy.log -a $DATA/comms/klippy.sock
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- Moonraker
step "Installing Moonraker"
clone_or_update "$MOONRAKER_REPO" "$HOME/moonraker" "$MOONRAKER_REF"
"$HOME/moonraker/scripts/install-moonraker.sh" -f -d "$DATA"

# ---------------------------------------------------------------- Mainsail
step "Installing Mainsail"
clone_or_update "$MAINSAIL_CONFIG_REPO" "$HOME/mainsail-config" "$MAINSAIL_CONFIG_REF"
ln -sfn "$HOME/mainsail-config/mainsail.cfg" "$CONF/mainsail.cfg"
if [ "$MAINSAIL_VERSION" = "latest" ]; then
  MS_URL="https://github.com/mainsail-crew/mainsail/releases/latest/download/mainsail.zip"
else
  MS_URL="https://github.com/mainsail-crew/mainsail/releases/download/$MAINSAIL_VERSION/mainsail.zip"
fi
TMPZ="$(mktemp --suffix=.zip)"
curl -fsSL -o "$TMPZ" "$MS_URL"
rm -rf "$HOME/mainsail"; mkdir -p "$HOME/mainsail"
unzip -q -o "$TMPZ" -d "$HOME/mainsail"; rm -f "$TMPZ"

# sidebar link to the Auto Slicer
mkdir -p "$CONF/.theme"
cat > "$CONF/.theme/navi.json" <<'EOF'
[
  {
    "title": "Auto Slicer",
    "href": "/api/slicer/",
    "target": "_blank",
    "position": 85,
    "icon": "M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M13,7V11H17V13H13V17H11V13H7V11H11V7H13Z"
  }
]
EOF

# ---------------------------------------------------------------- nginx
step "Configuring the web server (nginx)"
sudo tee /etc/nginx/sites-available/printer >/dev/null <<EOF
map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
upstream apiserver { ip_hash; server 127.0.0.1:7125; }

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    access_log /var/log/nginx/printer-access.log;
    error_log  /var/log/nginx/printer-error.log;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml image/svg+xml;
    root $HOME/mainsail;
    index index.html;

    # big G-code uploads
    client_max_body_size 0;
    proxy_request_buffering off;

    location / { try_files \$uri \$uri/ /index.html; }
    location = /index.html { add_header Cache-Control "no-store, no-cache, must-revalidate"; }

    location /websocket {
        proxy_pass http://apiserver/websocket;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Scheme \$scheme;
        proxy_read_timeout 86400;
    }
    location ~ ^/(printer|api|access|machine|server)/ {
        proxy_pass http://apiserver\$request_uri;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Scheme \$scheme;
        proxy_read_timeout 600;
        proxy_send_timeout 600;
    }
    # Auto Slicer lives under /api/slicer/ because Mainsail's service worker
    # hijacks every other path and shows Mainsail instead (it skips /api/*).
    location = /api/slicer { return 301 /api/slicer/; }
    location ^~ /api/slicer/ {
        proxy_pass http://127.0.0.1:8088/;
        proxy_set_header Host \$http_host;
        proxy_read_timeout 600;
        proxy_send_timeout 600;
    }
    location = /slicer { return 301 /api/slicer/; }
}
EOF
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sfn /etc/nginx/sites-available/printer /etc/nginx/sites-enabled/printer
sudo nginx -t
sudo systemctl enable nginx >/dev/null 2>&1
sudo systemctl restart nginx

# ---------------------------------------------------------------- PrusaSlicer
step "Installing PrusaSlicer (command line slicer)"
SLICER_BIN=""
if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y prusa-slicer; then
  SLICER_BIN="$(command -v prusa-slicer || true)"
fi
if [ -z "$SLICER_BIN" ]; then
  warn "PrusaSlicer could not be installed from the system package list. Auto-slicing will not work until it is installed (sudo apt install prusa-slicer)."
else
  echo "PrusaSlicer: $("$SLICER_BIN" --help 2>/dev/null | head -1 || echo found)"
fi

# ---------------------------------------------------------------- Auto-Slicer
step "Installing the Auto-Slicer web page"
[ -d "$HOME/autoslice-env" ] || python3 -m venv "$HOME/autoslice-env"
"$HOME/autoslice-env/bin/pip" install --quiet --upgrade pip
"$HOME/autoslice-env/bin/pip" install --quiet -r "$KIT_DIR/autoslice/requirements.txt"
echo "PRUSA_SLICER_BIN=${SLICER_BIN:-prusa-slicer}" > "$DATA/systemd/autoslice.env"
sudo tee /etc/systemd/system/autoslice.service >/dev/null <<EOF
[Unit]
Description=Auto Slicer (upload a 3D model, slice, print)
After=network.target moonraker.service

[Service]
Type=simple
User=$USER_NAME
EnvironmentFile=$DATA/systemd/autoslice.env
ExecStart=$HOME/autoslice-env/bin/python $KIT_DIR/autoslice/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- self-test of slicing
if [ -n "$SLICER_BIN" ]; then
  step "Self-test: slicing a 20 mm test cube"
  T="$(mktemp -d)"
  python3 "$KIT_DIR/tools/make_test_cube.py" "$T/cube.stl"
  if xvfb-run -a "$SLICER_BIN" --export-gcode --load "$DATA/autoslice/prusaslicer.ini" \
       --center 50,50 --output "$T/cube.gcode" "$T/cube.stl" >"$T/log" 2>&1 \
     && grep -q "START_PRINT" "$T/cube.gcode"; then
    echo "Slicer self-test passed."
  else
    warn "Slicer self-test failed. Last lines: $(tail -3 "$T/log" | tr '\n' ' ')"
  fi
  rm -rf "$T"
fi

# ---------------------------------------------------------------- start everything
step "Starting services"
sudo systemctl daemon-reload
sudo systemctl enable klipper moonraker autoslice >/dev/null 2>&1
sudo systemctl restart klipper || warn "Klipper did not start (check Mainsail)."
sudo systemctl restart moonraker
sudo systemctl restart autoslice
python3 "$KIT_DIR/tools/set_ui_name.py" || true     # machine name in Mainsail

if [ "$DO_FLASH" -eq 1 ]; then
  step "Flashing the Arduino Due"
  "$KIT_DIR/tools/flash_mcu.sh" || warn "Flashing failed - see messages above."
fi

# ---------------------------------------------------------------- summary
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "${c_ok}=====================================================${c_off}"
echo "${c_ok} INSTALLATION FINISHED${c_off}"
echo "${c_ok}=====================================================${c_off}"
echo " Open in a browser on the same network:"
echo "     http://${IP:-<printer-ip>}/          (Mainsail)"
echo "     http://${IP:-<printer-ip>}/api/slicer/   (Auto Slicer: upload a 3D file)"
echo "     http://$(hostname).local/            (if your network supports it)"
echo
if [ "${#WARNINGS[@]}" -gt 0 ]; then
  echo "${c_warn} Things to check:${c_off}"
  for w in "${WARNINGS[@]}"; do echo "   - $w"; done
  echo
fi
echo " Next steps:"
echo "   1. Connect the Arduino Due by USB and run:  ~/printer-kit/tools/flash_mcu.sh"
echo "   2. Fill in missing values in ~/printer-kit/machine.ini, then run: ~/printer-kit/update.sh"
echo "   3. Follow docs/COMMISSIONING.md for the first power-on."
