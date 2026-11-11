#!/usr/bin/env bash
# One-line installer for customers (Raspberry Pi OS, 64-bit):
#   curl -fsSL https://raw.githubusercontent.com/MuinKhanAfridi/ELKMIST-OS/main/bootstrap.sh | bash
set -Eeuo pipefail
REPO_URL="${KIT_REPO_URL:-https://github.com/MuinKhanAfridi/ELKMIST-OS.git}"
BRANCH="${KIT_BRANCH:-main}"
TARGET="$HOME/printer-kit"
if [ "$(id -u)" -eq 0 ]; then echo "Run as the normal user, not root."; exit 1; fi
sudo apt-get update -qq
sudo apt-get install -y -qq git ca-certificates
if [ -d "$TARGET/.git" ]; then
  git -C "$TARGET" pull --ff-only
else
  git clone --branch "$BRANCH" "$REPO_URL" "$TARGET"
fi
exec bash "$TARGET/install.sh" "$@"
