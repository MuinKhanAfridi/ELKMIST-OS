#!/usr/bin/env python3
"""ini_get.py SECTION KEY [DEFAULT]  - print a value from machine.ini (+ local overrides)."""
import configparser, os, sys
kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cp = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
cp.read([os.path.join(kit, "machine.ini"),
         os.path.expanduser("~/printer_data/config/machine.local.ini")])
sec, key = sys.argv[1], sys.argv[2]
default = sys.argv[3] if len(sys.argv) > 3 else ""
print(cp.get(sec, key, fallback=default).strip())
