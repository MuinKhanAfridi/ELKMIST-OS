#!/usr/bin/env python3
"""
calibrate.py - guided calibration page for the ELKMIST OS Auto Slicer.

Page:  http://<printer>/slicer/calibrate     (plugged in with calibrate.register(app))

Three guided tests, each driven by buttons:
  1. Extruder 100 mm test   -> corrects rotation_distance
  2. PID tuning             -> hotend and heated bed
  3. First layer + Z offset -> test print, live nozzle height buttons, save

Results are NEVER written with Klipper's SAVE_CONFIG (that would clash with the
generated printer.cfg). They go into ~/printer_data/config/machine.local.ini and
printer.cfg is regenerated, exactly like the setup wizard does:
    rotation_distance  -> [extruder] rotation_distance
    PID                -> [extruder] / [bed]  pid_kp pid_ki pid_kd
    Z offset           -> [axis_z] endstop_position (and position_min if needed)

Safety: nothing starts unless Klipper is "ready" and motion is enabled
(machine.ini complete). "Stop everything" restarts the firmware, which switches
all heaters off.
"""
import json
import math
import os
import re
import threading
import time

import requests
from flask import Blueprint, Response, jsonify, request

import wizard   # same folder: Moonraker URL, config helpers, regeneration

bp = Blueprint("calibrate", __name__)

STATE_DIR = wizard.STATE_DIR
HISTORY_FILE = os.path.join(STATE_DIR, "calibration.json")

EXTRUDE_MM = 100.0         # requested extrusion for the 100 mm test
DEFAULT_MARK_MM = 120.0    # where the client marks the filament
MAX_Z_OFFSET = 1.0         # largest allowed live nozzle adjustment (mm)

_op_lock = threading.Lock()
_op = {"name": None, "status": "idle", "msg": "", "result": None, "started": 0}


# ----------------------------------------------------------------- Moonraker
class Fail(Exception):
    """An error whose text can be shown to the client."""


def mr_url(path):
    return wizard.moonraker_url() + path


def mr_get(path, timeout=6):
    try:
        r = requests.get(mr_url(path), timeout=timeout)
        r.raise_for_status()
        return r.json().get("result", {})
    except Exception as e:
        raise Fail("Cannot reach the printer software (%s)." % e)


def gcode(script, timeout=60):
    """Run G-code and wait until Klipper finished it."""
    try:
        r = requests.post(mr_url("/printer/gcode/script"), json={"script": script},
                          timeout=timeout)
    except requests.Timeout:
        raise Fail("The printer took too long to answer.")
    except Exception as e:
        raise Fail("Cannot reach the printer software (%s)." % e)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = r.text[:200]
        raise Fail(msg or "The printer refused the command.")


def query(*names):
    path = "/printer/objects/query?" + "&".join(names)
    return mr_get(path).get("status", {})


def has_object(name):
    try:
        return name in mr_get("/printer/objects/list").get("objects", [])
    except Fail:
        return False


def gcode_store(count=100):
    return mr_get("/server/gcode_store?count=%d" % count).get("gcode_store", [])


def restart_firmware():
    try:
        requests.post(mr_url("/printer/firmware_restart"), timeout=10)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------- settings
def fmt(x):
    return wizard.gc().fmt(x)


def material_temps():
    """Hotend / bed temperature of the default material (settings.json)."""
    s = wizard.read_settings()
    m = (s.get("materials") or {}).get(s.get("default_material", "PLA"), {})

    def num(key, default):
        try:
            return float(m.get(key, default))
        except (TypeError, ValueError):
            return float(default)
    return {"hotend": num("temperature", 210), "bed": num("bed_temperature", 60),
            "hotend_first": num("first_layer_temperature", 215),
            "bed_first": num("first_layer_bed_temperature", 60)}


def motion_enabled():
    return bool(wizard.read_settings().get("motion_enabled"))


def need_ready():
    """Raise Fail unless calibration may run right now."""
    if not motion_enabled():
        raise Fail("Machine setup is not finished (safe mode). Calibration is disabled.")
    st = query("print_stats", "webhooks")
    if st.get("webhooks", {}).get("state") != "ready":
        raise Fail("The printer is not ready. Check Mainsail.")
    if st.get("print_stats", {}).get("state") in ("printing", "paused"):
        raise Fail("A print is running. Finish or stop it first.")


def save_history(kind, data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            with open(HISTORY_FILE) as fh:
                hist = json.load(fh)
        except Exception:
            hist = []
        hist.append(dict(data, kind=kind, time=int(time.time())))
        with open(HISTORY_FILE, "w") as fh:
            json.dump(hist[-50:], fh, indent=1)
    except Exception:
        pass


def write_and_restart(values):
    """values: [(section, key, value)] -> machine.local.ini, regenerate, restart."""
    mod = wizard.gc()
    for sec, key, val in values:
        mod.set_local_value(wizard.LOCAL_INI, sec, key, val)
    code, out = wizard.run_generate()
    if code != 0:
        raise Fail("Could not save the settings.")
    return restart_firmware()


# ----------------------------------------------------------------- background operations
def start_op(name, fn, *args):
    with _op_lock:
        if _op["status"] == "running":
            raise Fail("Another test is still running.")
        _op.update(name=name, status="running", msg="Starting ...", result=None,
                   started=time.time())

    def runner():
        try:
            fn(*args)
            with _op_lock:
                if _op["status"] == "running":
                    _op["status"] = "done"
        except Fail as e:
            with _op_lock:
                _op.update(status="error", msg=str(e))
        except Exception as e:
            with _op_lock:
                _op.update(status="error", msg="Unexpected error: %s" % e)
    threading.Thread(target=runner, daemon=True).start()


def op_set(**kw):
    with _op_lock:
        _op.update(kw)


# ---- 1. extruder test
def job_heat(temp):
    op_set(msg="Heating the hotend to %d C ..." % temp)
    gcode("M104 S%d\nM109 S%d" % (temp, temp), timeout=600)
    op_set(msg="Hotend is hot. Now mark the filament, then press Extrude.")


def job_extrude():
    op_set(msg="Extruding %d mm slowly (about %d seconds) ..." % (EXTRUDE_MM, EXTRUDE_MM))
    # M83 = relative extrusion; F60 = 1 mm/s, the slow speed the Klipper guide asks for
    gcode("M83\nG1 E%d F60\nM400" % EXTRUDE_MM, timeout=int(EXTRUDE_MM) + 240)
    op_set(msg="Done. Measure the distance from the extruder to your mark now.")


def calc_rotation(old, mark, measured):
    """Klipper formula: new = old * actual / requested. Raises Fail on nonsense."""
    try:
        mark, measured = float(mark), float(measured)
    except (TypeError, ValueError):
        raise Fail("Please enter both distances as numbers.")
    actual = mark - measured
    if not 0 < actual < EXTRUDE_MM * 2:
        raise Fail("The two distances do not make sense (extruded %.1f mm). "
                   "Check the numbers and measure again." % actual)
    new = old * actual / EXTRUDE_MM
    ratio = new / old
    if not 0.75 <= ratio <= 1.25:
        raise Fail("Only %.1f mm came out instead of %d mm. That is too far off to correct "
                   "automatically. Check that the filament is loaded, the hotend is hot "
                   "and the extruder is not slipping." % (actual, EXTRUDE_MM))
    return round(new, 3), actual


# ---- 2. PID
PID_RE = re.compile(r"pid_Kp=([\d.]+)\s+pid_Ki=([\d.]+)\s+pid_Kd=([\d.]+)")


def job_pid(heater, target):
    t0 = time.time()
    label = "hotend" if heater == "extruder" else "heated bed"
    op_set(msg="Tuning the %s at %d C. Do not touch the printer. This takes several minutes ..."
           % (label, target))
    gcode("PID_CALIBRATE HEATER=%s TARGET=%d" % (heater, target), timeout=1800)
    found = None
    for entry in gcode_store(200):
        if entry.get("time", 0) >= t0 - 5:
            m = PID_RE.search(entry.get("message", ""))
            if m:
                found = m
    if not found:
        raise Fail("The test finished but no result was found. Run it again.")
    kp, ki, kd = (float(found.group(i)) for i in (1, 2, 3))
    op_set(msg="Finished. Press Save to keep the new values.",
           result={"heater": heater, "kp": kp, "ki": ki, "kd": kd, "target": target})


# ---- 3. first layer
def first_layer_gcode(bed_x, bed_y, nozzle, filament, layer_h, hotend_t, bed_t, has_bed):
    """One layer of parallel lines in the middle of the bed. Pure G-code, no slicer."""
    size = max(20.0, min(60.0, bed_x - 40.0, bed_y - 40.0))
    width = nozzle * 1.15
    x0, x1 = bed_x / 2.0 - size / 2.0, bed_x / 2.0 + size / 2.0
    y0 = bed_y / 2.0 - size / 2.0
    area = math.pi * (filament / 2.0) ** 2
    e_per_mm = width * layer_h / area
    rows = max(2, int(size / width))
    g = ["; ELKMIST OS first layer test (generated)",
         "M104 S%d" % hotend_t]
    if has_bed:
        g.append("M140 S%d" % bed_t)
    g += ["G28"]
    if has_bed:
        g.append("M190 S%d" % bed_t)
    g += ["M109 S%d" % hotend_t, "M83", "G90",
          "G1 Z5 F600",
          "G1 X%.2f Y%.2f F3000" % (x0, y0),
          "G1 Z%.3f F300" % layer_h,
          "G1 E2 F200",                       # small prime
          "G1 X%.2f Y%.2f F600" % (x0, y0)]
    x_now = x0
    for i in range(rows):
        y = y0 + i * width
        tx = x1 if x_now == x0 else x0
        if i > 0:
            g.append("G1 Y%.3f E%.4f F1500" % (y, width * e_per_mm))
        g.append("G1 X%.3f E%.4f F1500" % (tx, abs(tx - x_now) * e_per_mm))
        x_now = tx
    g += ["G1 E-0.8 F2100", "M104 S0", "M140 S0",
          "G91", "G1 Z10 F600", "G90", "M84"]
    return "\n".join(g) + "\n"


# ----------------------------------------------------------------- API
@bp.route("/api/cal/status")
def cal_status():
    out = {"op": dict(_op)}
    out["op"]["elapsed"] = int(time.time() - _op["started"]) if _op["started"] else 0
    out["motion_enabled"] = motion_enabled()
    out["temps"] = material_temps()
    try:
        st = query("extruder", "heater_bed", "print_stats", "gcode_move", "webhooks", "configfile")
    except Fail as e:
        out.update(reachable=False, ready=False, error=str(e))
        return jsonify(out)
    cfg = st.get("configfile", {}).get("settings", {})
    out.update(
        reachable=True,
        ready=st.get("webhooks", {}).get("state") == "ready",
        printing=st.get("print_stats", {}).get("state") in ("printing", "paused"),
        hotend=round(st.get("extruder", {}).get("temperature", 0), 1),
        hotend_target=st.get("extruder", {}).get("target", 0),
        has_bed="heater_bed" in st,
        bed=round(st.get("heater_bed", {}).get("temperature", 0), 1),
        bed_target=st.get("heater_bed", {}).get("target", 0),
        z_offset=round((st.get("gcode_move", {}).get("homing_origin") or [0, 0, 0])[2], 3),
        rotation_distance=cfg.get("extruder", {}).get("rotation_distance"),
        extrude_mm=EXTRUDE_MM, default_mark=DEFAULT_MARK_MM)
    return jsonify(out)


def api(fn):
    """Turn Fail into a JSON error."""
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Fail as e:
            return jsonify(error=str(e)), 409
    wrapper.__name__ = fn.__name__
    return wrapper


@bp.route("/api/cal/ext/heat", methods=["POST"])
@api
def ext_heat():
    need_ready()
    start_op("ext_heat", job_heat, int(material_temps()["hotend"]))
    return jsonify(ok=True)


@bp.route("/api/cal/ext/extrude", methods=["POST"])
@api
def ext_extrude():
    need_ready()
    st = query("extruder")["extruder"]
    if st.get("temperature", 0) < 170:
        raise Fail("The hotend is not hot enough. Press Heat first.")
    start_op("ext_extrude", job_extrude)
    return jsonify(ok=True)


@bp.route("/api/cal/ext/calc", methods=["POST"])
@api
def ext_calc():
    d = request.get_json(silent=True) or {}
    cfg = query("configfile")["configfile"]["settings"]
    old = cfg.get("extruder", {}).get("rotation_distance")
    if not old:
        raise Fail("Could not read the current extruder value.")
    new, actual = calc_rotation(float(old), d.get("mark"), d.get("measured"))
    if d.get("apply"):
        need_ready()
        restarted = write_and_restart([("extruder", "rotation_distance", fmt(new))])
        save_history("extruder", {"old": old, "new": new, "actual_mm": actual})
        return jsonify(ok=True, old=old, new=new, actual=actual, restarted=restarted)
    return jsonify(old=old, new=new, actual=actual)


@bp.route("/api/cal/pid/start", methods=["POST"])
@api
def pid_start():
    need_ready()
    heater = (request.get_json(silent=True) or {}).get("heater")
    t = material_temps()
    if heater == "extruder":
        target = int(t["hotend"])
    elif heater == "heater_bed":
        if not has_object("heater_bed"):
            raise Fail("This printer has no heated bed.")
        target = int(t["bed"])
    else:
        raise Fail("Unknown heater.")
    start_op("pid", job_pid, heater, target)
    return jsonify(ok=True)


@bp.route("/api/cal/pid/apply", methods=["POST"])
@api
def pid_apply():
    r = _op.get("result") if _op.get("name") == "pid" and _op["status"] == "done" else None
    if not r:
        raise Fail("There is no result to save. Run the test first.")
    sec = "extruder" if r["heater"] == "extruder" else "bed"
    restarted = write_and_restart([(sec, "pid_kp", fmt(r["kp"])),
                                   (sec, "pid_ki", fmt(r["ki"])),
                                   (sec, "pid_kd", fmt(r["kd"]))])
    save_history("pid_" + sec, {k: r[k] for k in ("kp", "ki", "kd", "target")})
    op_set(name=None, status="idle", msg="", result=None)
    return jsonify(ok=True, restarted=restarted)


@bp.route("/api/cal/z/start", methods=["POST"])
@api
def z_start():
    need_ready()
    st = query("configfile")["configfile"]["settings"]
    ex = st.get("extruder", {})
    s = wizard.read_settings()
    cfg = wizard.read_cfg()
    try:
        layer_h = float(cfg.get("slicer", "first_layer_height", "0.25")) if cfg else 0.25
    except ValueError:
        layer_h = 0.25
    t = material_temps()
    text = first_layer_gcode(
        float(s.get("bed_x", 200)), float(s.get("bed_y", 200)),
        float(ex.get("nozzle_diameter", 0.4)), float(ex.get("filament_diameter", 1.75)),
        layer_h, int(t["hotend_first"]), int(t["bed_first"]), has_object("heater_bed"))
    try:
        r = requests.post(mr_url("/server/files/upload"),
                          files={"file": ("elkmist_first_layer_test.gcode", text.encode())},
                          data={"root": "gcodes", "print": "true"}, timeout=60)
        r.raise_for_status()
    except Exception as e:
        raise Fail("Could not start the test print (%s)." % e)
    return jsonify(ok=True)


@bp.route("/api/cal/z/adjust", methods=["POST"])
@api
def z_adjust():
    d = request.get_json(silent=True) or {}
    try:
        delta = float(d.get("delta"))
    except (TypeError, ValueError):
        raise Fail("Bad value.")
    if abs(delta) > 0.2 or abs(delta) < 0.001:
        raise Fail("Each step must be between 0.001 and 0.2 mm.")
    if not motion_enabled():
        raise Fail("Machine setup is not finished.")
    cur = (query("gcode_move")["gcode_move"].get("homing_origin") or [0, 0, 0])[2]
    if abs(cur + delta) > MAX_Z_OFFSET:
        raise Fail("That is too far from the starting height (limit %.1f mm)." % MAX_Z_OFFSET)
    gcode("SET_GCODE_OFFSET Z_ADJUST=%.3f MOVE=1" % delta)
    return jsonify(ok=True, z_offset=round(cur + delta, 3))


@bp.route("/api/cal/z/save", methods=["POST"])
@api
def z_save():
    if not motion_enabled():
        raise Fail("Machine setup is not finished.")
    st = query("gcode_move", "print_stats", "configfile")
    offset = (st["gcode_move"].get("homing_origin") or [0, 0, 0])[2]    # read BEFORE stopping
    if abs(offset) < 0.005:
        raise Fail("Nothing to save yet. Adjust the nozzle height first.")
    z = st["configfile"]["settings"].get("stepper_z", {})
    old_end = z.get("position_endstop")
    pmin, pmax = z.get("position_min", 0), z.get("position_max")
    if old_end is None or pmax is None:
        raise Fail("Could not read the Z axis settings.")
    # Z=0 sits `offset` away from the bed. The endstop position must move the opposite way.
    new_end = round(old_end - offset, 3)
    if abs(new_end - old_end) > MAX_Z_OFFSET + 0.01 or new_end > pmax:
        raise Fail("The new Z value is outside the allowed range.")
    values = [("axis_z", "endstop_position", fmt(new_end))]
    if new_end < pmin:       # Klipper needs position_min <= position_endstop
        values.append(("axis_z", "position_min", fmt(round(new_end - 1.0, 3))))
    if st["print_stats"].get("state") in ("printing", "paused"):
        try:
            requests.post(mr_url("/printer/print/cancel"), timeout=15)
        except Exception:
            pass
        for _ in range(30):
            time.sleep(1)
            if query("print_stats")["print_stats"].get("state") not in ("printing", "paused"):
                break
    restarted = write_and_restart(values)
    save_history("z_offset", {"old": old_end, "new": new_end, "adjusted_by": offset})
    return jsonify(ok=True, old=old_end, new=new_end, restarted=restarted)


@bp.route("/api/cal/stop", methods=["POST"])
def cal_stop():
    """Emergency-style stop: restart the firmware (heaters off, moves cancelled)."""
    ok = restart_firmware()
    op_set(name=None, status="idle", msg="", result=None)
    return jsonify(ok=ok)


@bp.route("/calibrate")
def calibrate_page():
    return Response(PAGE, mimetype="text/html")


def register(app):
    app.register_blueprint(bp)


# ----------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calibration</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1c2330;--mut:#667085;--acc:#1f6feb;--ok:#127a3d;--err:#b42318;--warn:#9a6700;--line:#d9dde4}
@media(prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e8ebf0;--mut:#9aa4b2;--acc:#5b9dff;--ok:#4ade80;--err:#ff8a80;--warn:#f0b72f;--line:#2c3441}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:8px 0 2px}h2{font-size:1.1rem;margin:0 0 6px}
.sub{color:var(--mut);margin:0 0 14px}.small{color:var(--mut);font-size:.9rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.temps{display:flex;gap:16px;font-weight:600;margin:4px 0}
label{display:block;font-weight:600;margin:12px 0 4px}
input[type=number]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
button{width:100%;margin-top:10px;padding:12px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.45;cursor:default}button.sec{background:transparent;color:var(--acc);border:1px solid var(--acc)}
button.stop{background:var(--err)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.grid button{margin:0}
.msg{margin-top:10px;font-weight:600}.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}.msg.warn{color:var(--warn)}
.banner{border:1px solid var(--err);border-radius:12px;padding:12px;margin-bottom:16px;color:var(--err);font-weight:600}
.big{font-size:1.6rem;font-weight:700;text-align:center;margin:8px 0}
a{color:var(--acc)}
</style></head><body><main>
<h1 id="title">Calibration</h1>
<p class="sub">Three short tests make your prints accurate. Do them in this order. <a href="./">Back to slicer</a></p>
<div id="banner" class="banner" hidden></div>

<div class="card">
 <div class="temps"><span>Hotend: <span id="th">-</span></span><span id="bedbox">Bed: <span id="tb">-</span></span></div>
 <div id="busy" class="msg warn" hidden></div>
 <button class="stop" id="stop">Stop everything (heaters off)</button>
</div>

<div class="card" id="c1">
 <h2>1. Extruder test (100 mm)</h2>
 <p class="small">Checks that the extruder pushes exactly the right amount of filament. Load filament first. You need a ruler or caliper and a marker pen.</p>
 <button id="heat">A. Heat the hotend</button>
 <label for="mark">B. Mark the filament this many mm above the extruder inlet</label>
 <input id="mark" type="number" step="0.1" value="120">
 <button id="extrude" disabled>C. Extrude 100 mm (takes about 2 minutes)</button>
 <label for="after">D. Now measure the distance from the inlet to your mark (mm)</label>
 <input id="after" type="number" step="0.1" placeholder="for example 21.5">
 <button class="sec" id="calc">E. Calculate</button>
 <div id="calcres" class="big" hidden></div>
 <button id="apply" hidden>Save the new value</button>
 <div id="m1" class="msg"></div>
</div>

<div class="card" id="c2">
 <h2>2. Temperature tuning (PID)</h2>
 <p class="small">Makes the heaters hold their temperature steadily. Each test takes several minutes. Do not touch the printer.</p>
 <button id="pidh">Tune the hotend</button>
 <button id="pidb">Tune the heated bed</button>
 <div id="pidres" class="big" hidden></div>
 <button id="pidsave" hidden>Save the new values</button>
 <div id="m2" class="msg"></div>
</div>

<div class="card" id="c3">
 <h2>3. First layer and nozzle height</h2>
 <p class="small">Prints a flat pattern. While it prints, press the buttons until the lines are slightly flattened, touching each other with no gaps and no ridges. Then save.</p>
 <button id="zstart">Start the test print</button>
 <div class="big">Height change: <span id="zoff">0.00</span> mm</div>
 <div class="grid">
  <button class="sec" data-d="-0.05">Closer 0.05</button><button class="sec" data-d="-0.02">Closer 0.02</button><button class="sec" data-d="-0.01">Closer 0.01</button>
  <button class="sec" data-d="0.01">Farther 0.01</button><button class="sec" data-d="0.02">Farther 0.02</button><button class="sec" data-d="0.05">Farther 0.05</button>
 </div>
 <p class="small">Too thin and see-through lines or scratches: go farther. Rough, ridged or rounded lines: go closer.</p>
 <button id="zsave">Save this height (stops the test print)</button>
 <div id="m3" class="msg"></div>
</div>

<script>
const $=id=>document.getElementById(id);
let S=null,calcOK=false;
async function api(path,body){
 const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 let j={};try{j=await r.json()}catch(e){}
 if(!r.ok)throw new Error(j.error||('Error '+r.status));return j}
function say(id,cls,t){const m=$(id);m.className='msg '+cls;m.textContent=t}
async function act(btn,msgid,fn){
 btn.disabled=true;say(msgid,'','Working ...');
 try{await fn();if($(msgid).textContent==='Working ...')say(msgid,'ok','Done')}catch(e){say(msgid,'err',e.message)}
 refresh()}
async function refresh(){
 try{S=await api('api/cal/status')}catch(e){$('banner').hidden=false;$('banner').textContent='Cannot reach the printer: '+e.message;return}
 let b='';
 if(!S.reachable)b='Cannot reach the printer software.';
 else if(!S.motion_enabled)b='Machine setup is not finished (safe mode). Calibration is disabled until the installer completes the setup.';
 else if(!S.ready)b='The printer is not ready. Check Mainsail.';
 $('banner').hidden=!b;$('banner').textContent=b;
 const off=!!b,run=S.op.status==='running',pr=S.printing;
 $('th').textContent=S.hotend!=null?S.hotend+' C'+(S.hotend_target?' > '+S.hotend_target:''):'-';
 $('bedbox').hidden=!S.has_bed;if(S.has_bed)$('tb').textContent=S.bed+' C'+(S.bed_target?' > '+S.bed_target:'');
 const busy=$('busy');busy.hidden=!(run||S.op.status==='error');
 busy.className='msg '+(S.op.status==='error'?'err':'warn');
 busy.textContent=S.op.status==='error'?S.op.msg:(S.op.msg+(run?'  ('+S.op.elapsed+' s)':''));
 $('heat').disabled=off||run||pr;
 $('extrude').disabled=off||run||pr||!(S.hotend>=170);
 $('calc').disabled=off;
 $('pidh').disabled=off||run||pr;$('pidb').disabled=off||run||pr||!S.has_bed;$('pidb').hidden=!S.has_bed;
 $('zstart').disabled=off||run||pr;
 document.querySelectorAll('.grid button').forEach(x=>x.disabled=off||!pr);
 $('zsave').disabled=off||Math.abs(S.z_offset)<0.005;
 $('zoff').textContent=(S.z_offset>0?'+':'')+S.z_offset.toFixed(2);
 const r=S.op.result;
 if(S.op.name==='pid'&&S.op.status==='done'&&r){
  $('pidres').hidden=false;$('pidres').textContent='Kp '+r.kp+'   Ki '+r.ki+'   Kd '+r.kd;$('pidsave').hidden=false;$('pidsave').disabled=false
 }else{$('pidres').hidden=true;$('pidsave').hidden=true}
}
$('heat').onclick=e=>act(e.target,'m1',()=>api('api/cal/ext/heat',{}));
$('extrude').onclick=e=>act(e.target,'m1',()=>api('api/cal/ext/extrude',{}));
$('calc').onclick=async e=>{
 try{const j=await api('api/cal/ext/calc',{mark:$('mark').value,measured:$('after').value});
  $('calcres').hidden=false;$('calcres').textContent='Extruded '+j.actual.toFixed(1)+' mm  |  '+j.old+' > '+j.new;
  $('apply').hidden=false;$('apply').disabled=false;say('m1','','')}
 catch(err){$('calcres').hidden=true;$('apply').hidden=true;say('m1','err',err.message)}};
$('apply').onclick=e=>act(e.target,'m1',async()=>{
 const j=await api('api/cal/ext/calc',{mark:$('mark').value,measured:$('after').value,apply:true});
 $('apply').hidden=true;$('calcres').textContent='Saved: '+j.new;
 say('m1','ok','Saved. The printer software is restarting, wait a few seconds.')});
$('pidh').onclick=e=>act(e.target,'m2',()=>api('api/cal/pid/start',{heater:'extruder'}));
$('pidb').onclick=e=>act(e.target,'m2',()=>api('api/cal/pid/start',{heater:'heater_bed'}));
$('pidsave').onclick=e=>act(e.target,'m2',async()=>{await api('api/cal/pid/apply',{});say('m2','ok','Saved. The printer software is restarting.')});
$('zstart').onclick=e=>act(e.target,'m3',()=>api('api/cal/z/start',{}));
document.querySelectorAll('.grid button').forEach(b=>b.onclick=()=>act(b,'m3',()=>api('api/cal/z/adjust',{delta:parseFloat(b.dataset.d)})));
$('zsave').onclick=e=>act(e.target,'m3',async()=>{
 if(!confirm('Save this height? The test print will be stopped.'))return;
 const j=await api('api/cal/z/save',{});say('m3','ok','Saved ('+j.old+' > '+j.new+'). The printer software is restarting.')});
$('stop').onclick=async()=>{if(!confirm('Stop everything and switch the heaters off?'))return;
 try{await api('api/cal/stop',{});say('m1','','')}catch(e){alert(e.message)}refresh()};
refresh();setInterval(refresh,2000);
</script></main></body></html>
"""
