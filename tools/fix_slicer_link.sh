#!/usr/bin/env bash
# One-time fix for installs made before Nov 8: moves the Auto Slicer to /api/slicer/
# (Mainsail's service worker hijacks /slicer/ and shows Mainsail instead).
set -Eeuo pipefail
NGINX="${NGINX_FILE:-/etc/nginx/sites-available/printer}"
NAVI="$HOME/printer_data/config/.theme/navi.json"
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! grep -q 'api/slicer' "$NGINX"; then
  sudo NGINX_FILE="$NGINX" python3 - <<'PY'
import os
p = os.environ["NGINX_FILE"]
s = open(p).read()
new = """    location = /api/slicer { return 301 /api/slicer/; }
    location ^~ /api/slicer/ {
        proxy_pass http://127.0.0.1:8088/;
        proxy_set_header Host $http_host;
        proxy_read_timeout 600;
        proxy_send_timeout 600;
    }
    location = /slicer { return 301 /api/slicer/; }
"""
old_redirect = "    location = /slicer { return 301 /slicer/; }\n"
assert old_redirect in s, "unexpected nginx file - not changed"
s = s.replace(old_redirect, new)
open(p, "w").write(s)
PY
fi

sudo nginx -t
sudo systemctl reload nginx
[ -f "$NAVI" ] && sed -i 's#"/slicer/"#"/api/slicer/"#; s#"_self"#"_blank"#' "$NAVI"
sed -i "s#location.href='../'#location.href='/'#" "$KIT/autoslice/app.py"
sudo systemctl restart autoslice 2>/dev/null || true
echo "Done. In the browser press Ctrl+Shift+R once, then click Auto Slicer."
echo "Direct address: http://localhost/api/slicer/"
