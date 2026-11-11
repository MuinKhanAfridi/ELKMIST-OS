#!/usr/bin/env python3
"""
generate_config.py  -  turns machine.ini into:
    * printer.cfg          (Klipper)
    * moonraker.conf       (only if it does not exist yet)
    * prusaslicer.ini      (PrusaSlicer profile used by the auto-slicer)

Design rules (safety first):
  * Any value still "UNSET" disables the part of the machine that needs it.
  * Motion is enabled ONLY when every X/Y/Z value is present AND the TMC5240
    risk acknowledgement is "yes". Otherwise Klipper starts in SAFE MODE
    (kinematics: none) - no motor or heater can run.
  * The SAVE_CONFIG block that Klipper appends (PID results, offsets ...) is
    preserved when printer.cfg is regenerated.

Extruder models:
  * extruder_profiles.ini (next to machine.ini) lists the supported extruders.
  * The model is chosen ONCE during installation and stored in
    ~/printer_data/config/machine.local.ini  ->  [extruder] model = LGX
  * rotation_distance / gear_ratio / run_current come from the profile,
    unless machine.ini [extruder] sets its own value (that always wins).
  * Pins, hotend, sensors and drivers stay in machine.ini (same for all models).

Usage:  generate_config.py [--ini machine.ini] [--out DIR] [--check]
                           [--choose-extruder] [--extruder NAME] [--list-extruders]
Python 3.7+, standard library only.
"""
import argparse
import configparser
import glob
import os
import re
import sys

SAVE_MARK = "#*# <---------------------- SAVE_CONFIG ---------------------->"
UNSET_WORDS = {"", "unset", "todo", "tbd", "none-yet"}
DEFAULT_MODEL = "LGX"

KIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------- helpers
class Cfg:
    def __init__(self, paths):
        self.cp = configparser.ConfigParser(
            inline_comment_prefixes=("#",), interpolation=None)
        self.cp.optionxform = str
        read = self.cp.read(paths)
        if not read:
            sys.exit("ERROR: cannot read %s" % ", ".join(paths))

    def get(self, sec, key, default=None):
        """Value or None when missing / UNSET."""
        try:
            v = self.cp.get(sec, key).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default
        return default if v.lower() in UNSET_WORDS else v

    def num(self, sec, key, default=None):
        v = self.get(sec, key)
        if v is None:
            return default
        try:
            return float(v.rstrip("%"))
        except ValueError:
            sys.exit("ERROR: [%s] %s = %r is not a number" % (sec, key, v))

    def yes(self, sec, key, default=False):
        v = self.get(sec, key)
        if v is None:
            return default
        return v.lower() in ("yes", "true", "1", "on")


def fmt(x):
    """Number without trailing .0"""
    if x == int(x):
        return str(int(x))
    return ("%.5f" % x).rstrip("0").rstrip(".")


def pin(value, invert=False):
    """Add Klipper '!' prefix. Keeps user prefixes (^ ~ !)."""
    if invert and not value.startswith("!"):
        return "!" + value
    return value


# ----------------------------------------------------------------- extruder profiles
def model_key(name):
    """'bondtech lgx-lite' -> 'BONDTECH_LGX_LITE' (profile names are upper case)."""
    return re.sub(r"[\s\-]+", "_", (name or "").strip()).upper()


def load_profiles(paths):
    cp = configparser.ConfigParser(
        inline_comment_prefixes=("#",), interpolation=None)
    cp.optionxform = str
    cp.read([p for p in paths if os.path.exists(p)])
    return cp


def profile_value(profiles, section, key):
    try:
        v = profiles.get(section, key).strip()
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None
    return None if v.lower() in UNSET_WORDS else v


def profile_missing(profiles, section):
    """Which important values of a profile are still UNSET."""
    return [k for k in ("rotation_distance", "run_current")
            if profile_value(profiles, section, k) is None]


def list_profiles(profiles):
    lines = []
    for i, sec in enumerate(profiles.sections(), 1):
        title = profile_value(profiles, sec, "title") or sec
        vendor = profile_value(profiles, sec, "vendor") or ""
        origin = profile_value(profiles, sec, "origin") or ""
        miss = profile_missing(profiles, sec)
        tag = ("   [still needed: %s]" % ", ".join(miss)) if miss else ""
        lines.append("  %2d) %-34s %s, %s%s" % (i, title, vendor, origin, tag))
    return lines


def chosen_model(cfg):
    """Model written in machine.ini or machine.local.ini, or None."""
    return cfg.get("extruder", "model")


def set_local_model(path, name):
    """Write  [extruder] model = NAME  into machine.local.ini, keeping the rest."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    if os.path.exists(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    out, in_ext, done, has_sec = [], False, False, False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if in_ext and not done:
                out.append("model = %s" % name)
                done = True
            in_ext = (s.lower() == "[extruder]")
            has_sec = has_sec or in_ext
        elif in_ext and re.match(r"model\s*=", s):
            ln = "model = %s" % name
            done = True
        out.append(ln)
    if in_ext and not done:
        out.append("model = %s" % name)
        done = True
    if not has_sec:
        out += ["", "[extruder]", "model = %s" % name]
    with open(path, "w") as fh:
        fh.write("\n".join(out).strip("\n") + "\n")


def ask_extruder(profiles):
    """Ask on the keyboard (works even when the script is started through a pipe).
    Returns a profile name, or None when nobody can answer."""
    names = profiles.sections()
    try:
        tty = open("/dev/tty", "r+")
    except OSError:
        return None
    try:
        tty.write("\n=== Which extruder is built into this printer? ===\n")
        tty.write("\n".join(list_profiles(profiles)) + "\n")
        for _ in range(5):
            tty.write("\nType the number (Enter = 1, %s): " % names[0])
            tty.flush()
            ans = tty.readline()
            if ans == "":
                return None
            ans = ans.strip()
            if ans == "":
                return names[0]
            if ans.isdigit() and 1 <= int(ans) <= len(names):
                return names[int(ans) - 1]
            if model_key(ans) in names:
                return model_key(ans)
            tty.write("Not a valid choice.\n")
        return None
    finally:
        tty.close()


# ----------------------------------------------------------------- printer.cfg
class Builder:
    def __init__(self, cfg, profiles):
        self.c = cfg
        self.profiles = profiles
        self.warn = []
        self.missing = []          # things needed for motion
        self.out = []
        self.motion = False
        self.extruder_on = False   # True only when [extruder] was really written
        # extruder model
        name = cfg.get("extruder", "model", DEFAULT_MODEL)
        self.model = model_key(name)
        self.profile_ok = self.model in profiles.sections()
        if not self.profile_ok:
            self.warn.append(
                "Unknown extruder model '%s'. Known models: %s. Extruder values must "
                "then be written in machine.ini [extruder]."
                % (name, ", ".join(profiles.sections()) or "none"))

    # ---- extruder values: machine.ini wins, the profile is the fallback
    def ext(self, key):
        v = self.c.get("extruder", key)
        if v is not None:
            return v
        if self.profile_ok:
            return profile_value(self.profiles, self.model, key)
        return None

    def extnum(self, key, default=None):
        v = self.ext(key)
        if v is None:
            return default
        try:
            return float(v.rstrip("%"))
        except ValueError:
            sys.exit("ERROR: extruder %s = %r is not a number" % (key, v))

    def ext_title(self):
        if self.profile_ok:
            return profile_value(self.profiles, self.model, "title") or self.model
        return self.model

    # ---- MCU
    def comm(self):
        return (self.c.get("mcu", "comm", "serial") or "serial").lower()

    def resolve_serial(self):
        s = self.c.get("mcu", "serial", "auto")
        if s.lower() != "auto":
            return s
        if self.comm() == "usb":
            pats = ["/dev/serial/by-id/usb-Klipper_sam3x8e*"]
        else:   # Due Programming port (through the 16U2 chip)
            pats = ["/dev/serial/by-id/usb-Arduino*", "/dev/serial/by-id/usb-Klipper_sam3x8e*"]
        for pat in pats:
            found = sorted(glob.glob(pat))
            if found:
                return found[0]
        self.warn.append(
            "MCU not found yet. Connect the Arduino Due, flash it "
            "(./tools/flash_mcu.sh), then run ./update.sh again.")
        return "/dev/serial/by-id/usb-NOT-FLASHED-YET"

    # ---- axis data
    AXES = ("x", "y", "z")

    def axis(self, a):
        sec = "axis_" + a
        g = lambda k: self.c.get(sec, k)
        need = ["step_pin", "dir_pin", "enable_pin", "cs_pin", "lead_mm",
                "run_current", "endstop_pin", "position_max"]
        miss = [k for k in need if g(k) is None]
        for k in miss:
            self.missing.append("[%s] %s" % (sec, k))
        return not miss

    def spi_lines(self):
        mode = (self.c.get("drivers", "spi_mode", "hardware") or "hardware").lower()
        if mode == "hardware":
            return ["spi_bus: spi0"]
        pins = {k: self.c.get("drivers", "spi_software_%s_pin" % k)
                for k in ("sclk", "mosi", "miso")}
        if None in pins.values():
            self.missing.append("[drivers] spi_software_*_pin")
            return []
        return ["spi_software_%s_pin: %s" % (k, v) for k, v in pins.items()]

    def vmax(self, a):
        """Speed limit from lead screw and motor rpm."""
        sec = "axis_" + a
        lead = self.c.num(sec, "lead_mm")
        rpm = self.c.num(sec, "max_rpm", 300)
        return lead * rpm / 60.0

    # ---- builders
    def build(self):
        c = self.c
        o = self.out
        name = c.get("general", "printer_name", "3D printer")
        o += ["# ======================================================",
              "# printer.cfg  -  GENERATED from machine.ini",
              "# Do NOT edit by hand: edit machine.ini and run ./update.sh",
              "# (the SAVE_CONFIG block at the bottom is kept automatically)",
              "# Printer: %s" % name,
              "# Extruder model: %s" % self.ext_title(),
              "# ======================================================", ""]

        # MCU
        o += ["[mcu]", "serial: %s" % self.resolve_serial()]
        if self.comm() == "serial":
            o.append("baud: 250000")
        o += ["restart_method: command", ""]

        # Motion readiness
        ok = {a: self.axis(a) for a in self.AXES}
        spi = self.spi_lines()
        acked = c.yes("drivers", "tmc5240_risk_acknowledged")
        if not acked:
            self.missing.append("[drivers] tmc5240_risk_acknowledged = yes")
        if c.get("drivers", "klipper_driver", "tmc2240") not in (
                "tmc2240", "tmc5160", "tmc2130", "tmc2209", "tmc5262"):
            self.missing.append("[drivers] klipper_driver (unsupported name)")
        motion = all(ok.values()) and bool(spi) and acked and not self.missing
        self.motion = motion

        # [printer]
        if motion:
            vxy = min(c.num("motion", "max_velocity", 80),
                      self.vmax("x"), self.vmax("y"))
            vz = min(c.num("motion", "max_z_velocity", 8), self.vmax("z"))
            o += ["[printer]", "kinematics: cartesian",
                  "max_velocity: %s" % fmt(vxy),
                  "max_accel: %s" % fmt(c.num("motion", "max_accel", 500)),
                  "max_z_velocity: %s" % fmt(vz),
                  "max_z_accel: %s" % fmt(c.num("motion", "max_z_accel", 50)), ""]
        else:
            o += ["# SAFE MODE: motion disabled until machine.ini is complete.",
                  "[printer]", "kinematics: none",
                  "max_velocity: 50", "max_accel: 100", ""]
            o += ["# Still missing:"] + ["#   - " + m for m in self.missing] + [""]

        # steppers
        if motion:
            for a in self.AXES:
                self.stepper(a)
        # extruder / heaters / fans (independent of motion, but each only if complete)
        self.extruder(motion)
        self.bed()
        self.fans()

        # common
        o += ["[virtual_sdcard]", "path: ~/printer_data/gcodes", "",
              "[pause_resume]", "", "[display_status]", "",
              "[idle_timeout]",
              "timeout: %d" % int(c.num("motion", "idle_timeout_seconds", 1800)), "",
              "[include mainsail.cfg]",
              "[include kit_macros.cfg]",
              "[include user_overrides.cfg]", ""]
        return "\n".join(o)

    def stepper(self, a):
        c, o = self.c, self.out
        sec = "axis_" + a
        g = lambda k: c.get(sec, k)
        inv_dir = c.yes(sec, "invert_dir")
        en_low = c.yes(sec, "enable_active_low", True)
        ms = int(c.num("drivers", "microsteps", 16))
        o += ["[stepper_%s]" % a,
              "step_pin: %s" % g("step_pin"),
              "dir_pin: %s" % pin(g("dir_pin"), inv_dir),
              "enable_pin: %s" % pin(g("enable_pin"), en_low),
              "microsteps: %d" % ms,
              "rotation_distance: %s" % fmt(c.num(sec, "lead_mm")),
              "full_steps_per_rotation: %d" % int(c.num(sec, "motor_full_steps", 200)),
              "endstop_pin: %s" % g("endstop_pin"),
              "position_endstop: %s" % fmt(c.num(sec, "endstop_position", 0)),
              "position_min: %s" % fmt(c.num(sec, "position_min", 0)),
              "position_max: %s" % fmt(c.num(sec, "position_max")),
              "homing_speed: %s" % fmt(c.num(sec, "homing_speed", 10)), ""]
        self.driver("stepper_" + a, g("cs_pin"), c.num(sec, "run_current"))

    def driver(self, stepper_name, cs, run_current):
        c, o = self.c, self.out
        drv = c.get("drivers", "klipper_driver", "tmc2240")
        o += ["[%s %s]" % (drv, stepper_name), "cs_pin: %s" % cs]
        o += self.spi_lines()
        o += ["run_current: %s" % fmt(run_current),
              "interpolate: True",
              "stealthchop_threshold: 0"]
        if drv == "tmc2240":
            o.append("rref: %s" % fmt(c.num("drivers", "rref", 12000)))
        o.append("")

    def heater_ok(self, sec, need):
        miss = [k for k in need if self.c.get(sec, k) is None]
        return not miss, miss

    def extruder(self, motion):
        c, o = self.c, self.out
        need = ["step_pin", "dir_pin", "enable_pin", "cs_pin", "run_current",
                "rotation_distance", "heater_pin", "sensor_type", "sensor_pin"]
        miss = [k for k in need if self.ext(k) is None]
        if miss:
            where = ("extruder_profiles.ini [%s] or machine.ini [extruder]" % self.model
                     if self.profile_ok else "machine.ini [extruder]")
            o += ["# [extruder] not generated yet (model: %s). Missing: %s"
                  % (self.ext_title(), ", ".join(miss)),
                  "#   rotation_distance / run_current / gear_ratio: see " + where, ""]
            return
        if not self.motion:
            o += ["# [extruder] complete but NOT generated: motion is still in SAFE MODE.", ""]
            return
        g = self.ext
        o += ["[extruder]",
              "step_pin: %s" % g("step_pin"),
              "dir_pin: %s" % pin(g("dir_pin"), c.yes("extruder", "invert_dir")),
              "enable_pin: %s" % pin(g("enable_pin"), c.yes("extruder", "enable_active_low", True)),
              "microsteps: %d" % int(c.num("drivers", "microsteps", 16)),
              "rotation_distance: %s" % fmt(self.extnum("rotation_distance"))]
        if g("gear_ratio"):
            o.append("gear_ratio: %s" % g("gear_ratio"))
        o += ["nozzle_diameter: %s" % fmt(self.extnum("nozzle_diameter", 0.4)),
              "filament_diameter: %s" % fmt(self.extnum("filament_diameter", 1.75)),
              "heater_pin: %s" % g("heater_pin"),
              "sensor_type: %s" % g("sensor_type"),
              "sensor_pin: %s" % g("sensor_pin"),
              "control: pid",
              "pid_Kp: %s" % fmt(self.extnum("pid_kp", 22.2)),
              "pid_Ki: %s" % fmt(self.extnum("pid_ki", 1.08)),
              "pid_Kd: %s" % fmt(self.extnum("pid_kd", 114)),
              "min_temp: 0",
              "max_temp: %s" % fmt(self.extnum("max_temp", 285)),
              "min_extrude_temp: %s" % fmt(self.extnum("min_extrude_temp", 170)),
              "pressure_advance: %s" % fmt(self.extnum("pressure_advance", 0)), ""]
        self.driver("extruder", g("cs_pin"), self.extnum("run_current"))
        self.extruder_on = True

    def bed(self):
        c, o = self.c, self.out
        if not c.yes("bed", "has_heated_bed"):
            return
        if not self.motion:
            o += ["# [heater_bed] not generated: motion is still in SAFE MODE.", ""]
            return
        ok, miss = self.heater_ok("bed", ["heater_pin", "sensor_type", "sensor_pin"])
        if not ok:
            o += ["# [heater_bed] not generated yet. Missing: " + ", ".join(miss), ""]
            return
        g = lambda k: c.get("bed", k)
        o += ["[heater_bed]",
              "heater_pin: %s" % g("heater_pin"),
              "sensor_type: %s" % g("sensor_type"),
              "sensor_pin: %s" % g("sensor_pin"),
              "control: pid",
              "pid_Kp: %s" % fmt(c.num("bed", "pid_kp", 54)),
              "pid_Ki: %s" % fmt(c.num("bed", "pid_ki", 0.8)),
              "pid_Kd: %s" % fmt(c.num("bed", "pid_kd", 400)),
              "min_temp: 0",
              "max_temp: %s" % fmt(c.num("bed", "max_temp", 110)), ""]

    def fans(self):
        c, o = self.c, self.out
        if not self.motion:
            return
        hp = c.get("fans", "hotend_fan_pin")
        if hp and self.extruder_on:   # the fan needs a real [extruder] heater
            o += ["[heater_fan hotend_fan]", "pin: %s" % hp,
                  "heater: extruder", "heater_temp: 50.0", ""]
        pp = c.get("fans", "part_fan_pin")
        if pp:
            o += ["[fan]", "pin: %s" % pp, ""]


# ----------------------------------------------------------------- slicer profile
def build_slicer_ini(cfg, vxy):
    """PrusaSlicer config (key = value). One material is applied per job via CLI."""
    c = cfg
    bx = c.num("axis_x", "position_max", 200)
    by = c.num("axis_y", "position_max", 200)
    bz = c.num("axis_z", "position_max", 200)
    nd = c.num("extruder", "nozzle_diameter", 0.4)
    fd = c.num("extruder", "filament_diameter", 1.75)
    s = lambda k, d: c.get("slicer", k, d)
    sn = lambda k, d: c.num("slicer", k, d)
    f = fmt
    # speeds as fractions of the real machine limit
    v = max(10.0, vxy)
    start_gcode = "START_PRINT BED={first_layer_bed_temperature[0]} EXTRUDER={first_layer_temperature[0]}"
    end_gcode = "END_PRINT"
    lines = [
        "# generated from machine.ini - do not edit",
        "gcode_flavor = klipper",
        "bed_shape = 0x0,%sx0,%sx%s,0x%s" % (f(bx), f(bx), f(by), f(by)),
        "max_print_height = %s" % f(bz),
        "nozzle_diameter = %s" % f(nd),
        "filament_diameter = %s" % f(fd),
        "extrusion_multiplier = 1",
        "use_relative_e_distances = 1",
        "use_firmware_retraction = 0",
        "layer_height = %s" % s("layer_height", "0.2"),
        "first_layer_height = %s" % s("first_layer_height", "0.25"),
        "perimeters = %d" % int(sn("perimeters", 3)),
        "top_solid_layers = %d" % int(sn("top_solid_layers", 5)),
        "bottom_solid_layers = %d" % int(sn("bottom_solid_layers", 4)),
        "fill_density = %s" % s("fill_density", "20%"),
        "retract_length = %s" % s("retract_length", "0.8"),
        "retract_speed = %s" % s("retract_speed", "35"),
        "deretract_speed = %s" % s("retract_speed", "35"),
        "retract_lift = %s" % s("retract_lift", "0.2"),
        "retract_before_travel = 2",
        "wipe = 0",
        # speeds scaled to the machine limit (lead screws are slow!)
        "perimeter_speed = %s" % f(round(v * 0.45)),
        "external_perimeter_speed = %s" % f(round(v * 0.35)),
        "small_perimeter_speed = %s" % f(round(v * 0.3)),
        "infill_speed = %s" % f(round(v * 0.6)),
        "solid_infill_speed = %s" % f(round(v * 0.5)),
        "top_solid_infill_speed = %s" % f(round(v * 0.3)),
        "support_material_speed = %s" % f(round(v * 0.5)),
        "bridge_speed = %s" % f(round(v * 0.35)),
        "gap_fill_speed = %s" % f(round(v * 0.3)),
        "first_layer_speed = %s" % f(round(v * 0.3)),
        "travel_speed = %s" % f(round(v * 0.9)),
        "max_print_speed = %s" % f(round(v)),
        "default_acceleration = 0",
        "start_gcode = " + start_gcode,
        "end_gcode = " + end_gcode,
        "before_layer_gcode =",
        "layer_gcode =",
        "toolchange_gcode =",
        "gcode_comments = 0",
        "gcode_label_objects = 0",
        "skirts = 1",
        "skirt_height = 1",
        "brim_width = 0",
        "support_material = 0",
    ]
    return "\n".join(lines) + "\n"


def material_overrides(cfg):
    """{name: [(prusaslicer_cli_flag, value), ...]}"""
    res = {}
    for sec in cfg.cp.sections():
        if not sec.startswith("material."):
            continue
        name = sec.split(".", 1)[1]
        g = lambda k, d: cfg.get(sec, k, d)
        res[name] = {
            "temperature": g("temperature", "210"),
            "first_layer_temperature": g("first_layer_temperature", "215"),
            "bed_temperature": g("bed_temperature", "60"),
            "first_layer_bed_temperature": g("first_layer_bed_temperature", "60"),
            "max_fan_speed": g("fan_speed", "100"),
            "min_fan_speed": g("fan_speed", "100"),
            "fan_always_on": "1",
        }
    return res


# ----------------------------------------------------------------- moonraker
MOONRAKER = """\
# GENERATED once by the installer. Safe to edit; it is not overwritten.
[server]
host: 0.0.0.0
port: 7125
klippy_uds_address: ~/printer_data/comms/klippy.sock

[authorization]
trusted_clients:
    10.0.0.0/8
    127.0.0.0/8
    169.254.0.0/16
    172.16.0.0/12
    192.168.0.0/16
    FE80::/10
    ::1/128
cors_domains:
    *.local
    *.lan
    *://localhost
    *://localhost:*
    *://my.mainsail.xyz
    *://app.fluidd.xyz

[file_manager]
enable_object_processing: False

[octoprint_compat]

[history]

[announcements]
subscriptions:
    mainsail

[update_manager]
refresh_interval: 168
enable_auto_refresh: True

[update_manager mainsail]
type: web
channel: stable
repo: mainsail-crew/mainsail
path: ~/mainsail

[update_manager mainsail-config]
type: git_repo
primary_branch: master
path: ~/mainsail-config
origin: https://github.com/mainsail-crew/mainsail-config.git
managed_services: klipper

[update_manager printer-kit]
type: git_repo
path: ~/printer-kit
origin: {repo}
primary_branch: {branch}
is_system_service: False
managed_services: autoslice
install_script: update.sh
"""


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ini", default=os.path.join(KIT_DIR, "machine.ini"))
    ap.add_argument("--local-ini", default=os.path.expanduser(
        "~/printer_data/config/machine.local.ini"),
        help="optional overrides (same format), read after --ini")
    ap.add_argument("--profiles", default=os.path.join(KIT_DIR, "extruder_profiles.ini"),
                    help="list of supported extruders")
    ap.add_argument("--out", default=os.path.expanduser("~/printer_data/config"))
    ap.add_argument("--check", action="store_true",
                    help="print report only, write nothing")
    ap.add_argument("--choose-extruder", action="store_true",
                    help="ask which extruder is installed and remember the answer")
    ap.add_argument("--extruder", metavar="NAME",
                    help="set the extruder model without asking (e.g. LGX_LITE_PRO)")
    ap.add_argument("--list-extruders", action="store_true",
                    help="show the supported extruders and stop")
    a = ap.parse_args()

    local_profiles = os.path.join(os.path.dirname(a.local_ini), "extruder_profiles.local.ini")
    profiles = load_profiles([a.profiles, local_profiles])

    if a.list_extruders:
        print("Supported extruders:")
        print("\n".join(list_profiles(profiles)))
        return 0

    def read_cfg():
        return Cfg([p for p in (a.ini, a.local_ini) if os.path.exists(p)])

    cfg = read_cfg()
    notes = []

    # ---- which extruder? (asked once, during installation)
    if a.extruder:
        key = model_key(a.extruder)
        if key not in profiles.sections():
            sys.exit("ERROR: unknown extruder '%s'. Known: %s"
                     % (a.extruder, ", ".join(profiles.sections())))
        if not a.check:
            set_local_model(a.local_ini, key)
            cfg = read_cfg()
    elif (a.choose_extruder or chosen_model(cfg) is None) and not a.check:
        picked = ask_extruder(profiles) if profiles.sections() else None
        if picked:
            set_local_model(a.local_ini, picked)
            cfg = read_cfg()
        elif a.choose_extruder:
            sys.exit("ERROR: no keyboard available. Use  --extruder NAME  instead "
                     "(see --list-extruders).")
        else:
            notes.append(
                "No extruder chosen yet - using %s. To choose: "
                "python3 ~/printer-kit/tools/generate_config.py --choose-extruder"
                % DEFAULT_MODEL)

    b = Builder(cfg, profiles)
    text = b.build()

    # keep Klipper's SAVE_CONFIG block
    cfg_path = os.path.join(a.out, "printer.cfg")
    if os.path.exists(cfg_path):
        old = open(cfg_path).read()
        if SAVE_MARK in old:
            text = text.rstrip("\n") + "\n\n" + old[old.index(SAVE_MARK):]

    print("=" * 60)
    print("MODE: %s" % ("MOTION ENABLED" if b.motion else "SAFE MODE (no motion)"))
    print("EXTRUDER: %s" % b.ext_title())
    if b.missing:
        print("Still needed for motion:")
        for m in b.missing:
            print("   - " + m)
    for w in b.warn + notes:
        print("WARNING: " + w)
    print("=" * 60)
    if a.check:
        return 0

    os.makedirs(a.out, exist_ok=True)
    os.makedirs(os.path.expanduser("~/printer_data/gcodes"), exist_ok=True)
    with open(cfg_path, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")

    # one-time files
    ov = os.path.join(a.out, "user_overrides.cfg")
    if not os.path.exists(ov):
        open(ov, "w").write("# Your own Klipper additions (kept across updates)\n")
    mr = os.path.join(a.out, "moonraker.conf")
    if not os.path.exists(mr):
        open(mr, "w").write(MOONRAKER.format(
            repo=cfg.get("general", "kit_repo_url", ""),
            branch=cfg.get("general", "kit_branch", "main")))

    # slicer profile + materials (consumed by autoslice)
    vxy = 60.0
    if b.motion:
        vxy = min(cfg.num("motion", "max_velocity", 80), b.vmax("x"), b.vmax("y"))
    elif all(cfg.num("axis_" + k, "lead_mm") for k in "xy"):
        vxy = min(cfg.num("motion", "max_velocity", 80), b.vmax("x"), b.vmax("y"))
    import json
    state = os.path.expanduser("~/printer_data/autoslice")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "prusaslicer.ini"), "w") as fh:
        fh.write(build_slicer_ini(cfg, vxy))
    with open(os.path.join(state, "settings.json"), "w") as fh:
        json.dump({
            "materials": material_overrides(cfg),
            "default_material": cfg.get("autoslice", "default_material", "PLA"),
            "auto_start_print": cfg.yes("autoslice", "auto_start_print", False),
            "port": int(cfg.num("autoslice", "port", 8088)),
            "max_upload_mb": int(cfg.num("autoslice", "max_upload_mb", 300)),
            "bed_x": cfg.num("axis_x", "position_max", 200),
            "bed_y": cfg.num("axis_y", "position_max", 200),
            "motion_enabled": b.motion,
            "printer_name": cfg.get("general", "printer_name", "3D printer"),
            "extruder_model": b.model,
            "extruder_title": b.ext_title(),
        }, fh, indent=2)
    print("Wrote: %s" % cfg_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
