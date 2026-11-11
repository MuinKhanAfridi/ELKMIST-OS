# Printer Kit

One-command software for our Cartesian 3D printer:

- **Klipper** (printer firmware host) + **Moonraker** (API) + **Mainsail** (web interface)
- **PrusaSlicer** (command line) behind an **Auto Slicer** page: upload a 3D file, it is sliced and sent to the printer
- Hardware: Raspberry Pi 5, Arduino Due (MCU), TMC5240 stepper drivers, Bondtech LGX extruder + Phaetus Rapido hotend, lead-screw X/Y/Z

> **Status: not yet tested on the real printer.** The scripts were checked for syntax, the config generator and
> the Auto Slicer were tested against stand-ins (a fake slicer and a fake Moonraker), and the firmware options were
> verified against the Klipper source. The first install on a real Raspberry Pi 5 + printer is the real test.
> Follow `docs/COMMISSIONING.md` and keep a hand on the power switch.

---

## For the customer: install

1. Write **Raspberry Pi OS Lite (64-bit)** to a microSD card with *Raspberry Pi Imager*. In the Imager settings set a hostname
   (e.g. `printer`), a user name and password, your Wi-Fi (or use a LAN cable) and enable SSH.
2. Put the card in the Raspberry Pi 5, power it on, then from your computer: `ssh YOUR-USER@printer.local`
3. Run the one-line installer (takes 10-20 minutes):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/MuinKhanAfridi/ELKMIST-OS/main/bootstrap.sh | bash
   ```

   (Private repository? Clone it yourself first: `git clone https://github.com/MuinKhanAfridi/ELKMIST-OS.git ~/printer-kit && ~/printer-kit/install.sh`)
4. Connect the Arduino Due (USB port next to the power jack) to the Pi and run `~/printer-kit/tools/flash_mcu.sh`
5. Open `http://printer.local/` for Mainsail. **Auto Slicer** is in the left menu (or `http://printer.local/api/slicer/`).

### Printing a model
Open **Auto Slicer** → drop an STL/3MF/OBJ/STEP file → choose material and quality → **Slice**.
The G-code appears in Mainsail. Press **Print now** (or tick "Start printing right after slicing").

### Updates
Mainsail → *Machine* → *Update Manager* → update *printer-kit* (and Klipper, Moonraker, Mainsail).

---

## For us (the vendor): fill in the machine

Everything machine-specific is in **one file: `machine.ini`**. Values still `UNSET` are information we still need.

```bash
nano ~/printer-kit/machine.ini      # or edit on GitHub and pull
~/printer-kit/update.sh             # rebuilds printer.cfg and the slicer profile, restarts Klipper
```

* **Safe mode.** Until every X/Y/Z value is filled in (and the TMC5240 acknowledgement below is set), Klipper starts with
  `kinematics: none`: no motor and no heater can run. Mainsail still opens.
  `update.sh` prints what is still missing.
* **TMC5240 warning.** Mainline Klipper (checked Oct 2026) has **no `[tmc5240]`** section. The closest is `[tmc2240]`
  (similar chip). It is **not confirmed** to work on a TMC5240, so motion stays off until `machine.ini` says
  `tmc5240_risk_acknowledged = yes`. Test at low current with the motors disconnected from the machine first
  (`docs/COMMISSIONING.md`, step 2). If register reads look wrong, ask the PCB designer about a TMC2240/TMC5160 or
  for Klipper-forum advice before going on.
* **Unsafe guesses are not allowed.** Pins, sensor type, motor current and extruder `rotation_distance` have no defaults.
* **Customer tweaks** go into `~/printer_data/config/user_overrides.cfg` (kept on update) or a `machine.local.ini`
  next to it (same format as `machine.ini`, overrides it).
* The `SAVE_CONFIG` block Klipper writes (PID tuning, offsets) is preserved when `printer.cfg` is regenerated.

### Before shipping to customers
1. Do a full install + print on a spare Pi and printer.
2. Pin the tested versions in `versions.env` so every customer gets exactly what you tested.
3. 

---

## What gets installed where

| Part | Location | Service |
|---|---|---|
| Klipper | `~/klipper`, `~/klippy-env` | `klipper` |
| Moonraker | `~/moonraker`, `~/moonraker-env` | `moonraker` |
| Mainsail | `~/mainsail` (served by nginx on port 80) | `nginx` |
| Auto Slicer | `~/printer-kit/autoslice`, `~/autoslice-env` | `autoslice` (127.0.0.1:8088, via `/slicer/`) |
| PrusaSlicer | system package `prusa-slicer` | used by `autoslice` |
| Config, G-code, logs | `~/printer_data/{config,gcodes,logs}` | |

## Repository layout

```
machine.ini              <- the ONLY file to edit
install.sh / bootstrap.sh / update.sh
tools/generate_config.py machine.ini -> printer.cfg, moonraker.conf, slicer profile
tools/flash_mcu.sh       build + flash Klipper to the Arduino Due
autoslice/app.py         upload -> slice -> send to printer
config/kit_macros.cfg    START_PRINT / END_PRINT
firmware/*.config        Klipper build options for the Due (serial or native USB)
docs/                    COMMISSIONING.md (first power-on), EDIT-GUIDE.bn.md (Bengali guide)
```

## Uninstall
```bash
~/printer-kit/tools/uninstall.sh                # removes the programs, keeps ~/printer_data
~/printer-kit/tools/uninstall.sh --delete-data  # also deletes config, G-code files and logs
~/printer-kit/tools/uninstall.sh --dry-run      # only shows what would be done
```
Other options: `--remove-nginx`, `--remove-packages`, `--yes`. The kit folder itself is never deleted.

## Safety notes
- The Auto Slicer and Mainsail have **no password**. Use them only on a trusted network. Do not expose the Pi to the internet.
- Auto-start of printing is **off** by default (`auto_start_print = no`). It only starts when Klipper reports "ready".
- Keep the printer supervised during the first prints. Heater limits (`max_temp`) are set in `machine.ini`; Klipper also
  has built-in heater-runaway protection.
