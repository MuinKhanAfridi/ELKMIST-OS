#!/usr/bin/env python3
"""set_ui_name.py - show the machine name (machine.ini [general] printer_name) in Mainsail.
Mainsail keeps the name in Moonraker's database (namespace 'mainsail', key general.printername);
it then appears in the top bar, the sidebar and the browser tab title."""
import json, os, sys, time, urllib.request, configparser

kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cp = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
cp.read([os.path.join(kit, "machine.ini"),
         os.path.expanduser("~/printer_data/config/machine.local.ini")])
name = cp.get("general", "printer_name", fallback="").strip()
if name.lower() in ("", "unset"):
    print("printer_name is not set in machine.ini - Mainsail name unchanged.")
    sys.exit(0)

url = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125") + "/server/database/item"
body = json.dumps({"namespace": "mainsail", "key": "general.printername", "value": name}).encode()
for attempt in range(12):                      # Moonraker may still be starting
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        print("Mainsail name set to: %s" % name)
        sys.exit(0)
    except Exception:
        time.sleep(3)
print("Could not reach Moonraker to set the Mainsail name (will retry on next update.sh).")
