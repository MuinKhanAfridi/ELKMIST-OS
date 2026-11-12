#!/usr/bin/env python3
"""
Auto-Slicer: upload a 3D model -> PrusaSlicer slices it -> G-code is sent to
Moonraker/Mainsail (optionally starts the print).

Runs on 127.0.0.1:<port>; nginx publishes it at  http://<printer>/slicer/
Settings are read from ~/printer_data/autoslice/settings.json (made by
tools/generate_config.py from machine.ini).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, render_template_string, request
from werkzeug.utils import secure_filename

STATE_DIR = os.path.expanduser("~/printer_data/autoslice")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
PROFILE = os.path.join(STATE_DIR, "prusaslicer.ini")
JOBS_DIR = os.path.join(STATE_DIR, "jobs")
MOONRAKER = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
SLICER_BIN = os.environ.get("PRUSA_SLICER_BIN", "prusa-slicer")
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT", "1800"))
ALLOWED = {".stl", ".3mf", ".obj", ".step", ".stp", ".amf"}

QUALITY = {            # label -> layer height (mm)
    "Draft 0.30 mm": "0.3",
    "Standard 0.20": "0.2",
    "Fine 0.12 mm": "0.12",
}

app = Flask(__name__)

# First-run setup wizard (autoslice/wizard.py). If it ever fails to load, the
# Auto Slicer still works normally.
try:
    import wizard
    wizard.register(app)
except Exception as _wizard_error:
    print("Setup wizard not loaded: %s" % _wizard_error)

os.makedirs(JOBS_DIR, exist_ok=True)
jobs = {}                       # id -> dict
jobs_lock = threading.Lock()
slice_lock = threading.Lock()   # one slice at a time (Raspberry Pi memory)


def settings():
    try:
        with open(SETTINGS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {"materials": {"PLA": {}}, "default_material": "PLA",
                "auto_start_print": False, "max_upload_mb": 300,
                "bed_x": 200, "bed_y": 200, "motion_enabled": False,
                "printer_name": "3D printer"}


def log(job, msg):
    job["log"].append(msg)
    job["log"] = job["log"][-60:]


def moonraker_state():
    """Return Klipper state string ('ready', 'startup', 'error', ...) or None."""
    try:
        r = requests.get(MOONRAKER + "/printer/info", timeout=5)
        return r.json()["result"]["state"]
    except Exception:
        return None


def slicer_command():
    cmd = [SLICER_BIN]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a"] + cmd
    return cmd


def parse_stats(gcode_path):
    """Read print-time / filament comments PrusaSlicer writes near the end."""
    stats = {}
    try:
        size = os.path.getsize(gcode_path)
        with open(gcode_path, "rb") as fh:
            fh.seek(max(0, size - 200000))
            tail = fh.read().decode("utf-8", "ignore")
        m = re.search(r"estimated printing time \(normal mode\) = (.+)", tail)
        if m:
            stats["time"] = m.group(1).strip()
        m = re.search(r"filament used \[g\] = ([\d.]+)", tail)
        if m:
            stats["filament_g"] = m.group(1)
    except Exception:
        pass
    return stats


def run_job(jid):
    job = jobs[jid]
    cfg = settings()
    mat = cfg["materials"].get(job["material"], {})
    with slice_lock:
        job["status"] = "slicing"
        src = job["src"]
        out = os.path.join(os.path.dirname(src),
                           os.path.splitext(job["name"])[0] + ".gcode")
        cmd = slicer_command() + [
            "--export-gcode", "--load", PROFILE, "--output", out,
            "--layer-height", job["layer_height"],
            "--fill-density", job["infill"],
        ]
        flag = {"temperature": "--temperature",
                "first_layer_temperature": "--first-layer-temperature",
                "bed_temperature": "--bed-temperature",
                "first_layer_bed_temperature": "--first-layer-bed-temperature",
                "max_fan_speed": "--max-fan-speed",
                "min_fan_speed": "--min-fan-speed"}
        for k, f in flag.items():
            if k in mat:
                cmd += [f, str(mat[k])]
        if job["support"]:
            cmd += ["--support-material"]
        if job["brim"]:
            cmd += ["--brim-width", "5"]
        if not src.lower().endswith(".3mf"):
            cmd += ["--center", "%g,%g" % (cfg["bed_x"] / 2.0, cfg["bed_y"] / 2.0)]
        cmd.append(src)
        log(job, "Slicing with PrusaSlicer ...")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=SLICE_TIMEOUT)
        except FileNotFoundError:
            job["status"] = "error"
            job["error"] = ("PrusaSlicer is not installed on this printer "
                            "(run install.sh again).")
            return
        except subprocess.TimeoutExpired:
            job["status"] = "error"
            job["error"] = "Slicing took too long and was stopped."
            return
        tail = (p.stdout + "\n" + p.stderr).strip().splitlines()[-8:]
        for line in tail:
            log(job, line.replace(os.path.dirname(src) + os.sep, ""))
        if p.returncode != 0 or not os.path.exists(out):
            job["status"] = "error"
            msg = " ".join(tail[-3:])
            if "too large" in msg.lower() or "outside" in msg.lower():
                msg = ("The model does not fit on the print bed (%g x %g mm). %s"
                       % (cfg["bed_x"], cfg["bed_y"], msg))
            job["error"] = msg or "PrusaSlicer failed."
            return
        job["gcode"] = out
        job["stats"] = parse_stats(out)

    # ---- upload to Moonraker
    job["status"] = "uploading"
    gname = os.path.basename(out)
    start = bool(job["start"])
    if start:
        state = moonraker_state()
        if state != "ready" or not cfg.get("motion_enabled"):
            start = False
            log(job, "Not starting automatically: printer is not ready "
                     "(state=%s). File was uploaded; press Print in Mainsail." % state)
    try:
        with open(out, "rb") as fh:
            r = requests.post(
                MOONRAKER + "/server/files/upload",
                files={"file": (gname, fh)},
                data={"root": "gcodes", "print": "true" if start else "false"},
                timeout=300)
        r.raise_for_status()
    except Exception as e:
        job["status"] = "error"
        job["error"] = "Could not send G-code to the printer software: %s" % e
        return
    job["uploaded_as"] = gname
    job["started"] = start
    job["status"] = "done"
    log(job, "Printing started." if start else "G-code is ready in Mainsail.")
    try:
        os.remove(src)
    except OSError:
        pass


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ s.printer_name }} - Auto Slicer</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1c2330;--mut:#667085;--acc:#1f6feb;--ok:#127a3d;--err:#b42318;--line:#d9dde4}
@media(prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e8ebf0;--mut:#9aa4b2;--acc:#5b9dff;--ok:#4ade80;--err:#ff8a80;--line:#2c3441}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:8px 0 2px}p.sub{margin:0 0 16px;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
#drop{border:2px dashed var(--line);border-radius:12px;padding:28px 12px;text-align:center;color:var(--mut);cursor:pointer}
#drop.over{border-color:var(--acc);color:var(--acc)}
label{display:block;font-weight:600;margin:12px 0 4px}
select,input[type=number]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:flex;gap:12px}.row>div{flex:1}
.chk{display:flex;gap:8px;align-items:center;margin:10px 0;font-weight:400}.chk input{width:18px;height:18px}
button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}button.sec{background:transparent;color:var(--acc);border:1px solid var(--acc)}
#fname{margin-top:8px;font-weight:600;word-break:break-all}
.st{font-weight:600}.st.ok{color:var(--ok)}.st.err{color:var(--err)}
pre{white-space:pre-wrap;background:var(--bg);border-radius:8px;padding:10px;font-size:.8rem;color:var(--mut);max-height:160px;overflow:auto}
a{color:var(--acc)}
</style></head><body><main>
<h1>{{ s.printer_name }}</h1>
<p class="sub">Auto Slicer &middot; bed {{ s.bed_x|int }} &times; {{ s.bed_y|int }} mm</p>
{% if not s.motion_enabled %}
<div class="card" style="border-color:var(--err)"><b>Setup not finished.</b>
The printer is still in safe mode, so printing is disabled. You can slice, but the print cannot start.</div>
{% endif %}
<div class="card">
 <div id="drop">Drop a 3D file here or tap to choose<br><small>STL, 3MF, OBJ, STEP, AMF</small></div>
 <input id="file" type="file" accept=".stl,.3mf,.obj,.step,.stp,.amf" hidden>
 <div id="fname"></div>
 <div class="row"><div><label for="mat">Material</label>
  <select id="mat">{% for m in s.materials %}<option {{ 'selected' if m==s.default_material }}>{{ m }}</option>{% endfor %}</select></div>
 <div><label for="q">Quality</label>
  <select id="q">{% for k,v in quality.items() %}<option value="{{ v }}" {{ 'selected' if v=='0.2' }}>{{ k }}</option>{% endfor %}</select></div></div>
 <label for="inf">Infill (%)</label><input id="inf" type="number" min="0" max="100" value="20">
 <label class="chk"><input id="sup" type="checkbox"> Add supports</label>
 <label class="chk"><input id="brim" type="checkbox"> Add brim (helps sticking)</label>
 <label class="chk"><input id="go" type="checkbox" {{ 'checked' if s.auto_start_print }}> Start printing right after slicing</label>
 <button id="btn" disabled>Slice</button>
</div>
<div class="card" id="res" hidden>
 <div class="st" id="state"></div><div id="stats"></div>
 <pre id="log"></pre>
 <button class="sec" id="open" hidden onclick="location.href='/'">Open Mainsail</button>
 <button id="startbtn" hidden>Print now</button>
</div>
<script>
const $=id=>document.getElementById(id);let file=null,jid=null;
const pick=f=>{file=f;$('fname').textContent=f.name;$('btn').disabled=false};
$('drop').onclick=()=>$('file').click();$('file').onchange=e=>e.target.files[0]&&pick(e.target.files[0]);
['dragover','dragenter'].forEach(t=>$('drop').addEventListener(t,e=>{e.preventDefault();$('drop').classList.add('over')}));
['dragleave','drop'].forEach(t=>$('drop').addEventListener(t,e=>{e.preventDefault();$('drop').classList.remove('over')}));
$('drop').addEventListener('drop',e=>e.dataTransfer.files[0]&&pick(e.dataTransfer.files[0]));
$('btn').onclick=async()=>{
 $('btn').disabled=true;$('res').hidden=false;$('open').hidden=true;$('startbtn').hidden=true;
 $('state').className='st';$('state').textContent='Uploading ...';$('stats').textContent='';$('log').textContent='';
 const fd=new FormData();fd.append('model',file);fd.append('material',$('mat').value);fd.append('layer_height',$('q').value);
 fd.append('infill',$('inf').value);fd.append('support',$('sup').checked?'1':'');fd.append('brim',$('brim').checked?'1':'');fd.append('start',$('go').checked?'1':'');
 try{const r=await fetch('upload',{method:'POST',body:fd});const j=await r.json();
  if(!r.ok){fail(j.error||'Upload failed');return}jid=j.id;poll()}catch(e){fail('Connection problem: '+e)}};
function fail(m){$('state').className='st err';$('state').textContent=m;$('btn').disabled=false}
async function poll(){
 const j=await (await fetch('api/job/'+jid)).json();
 $('log').textContent=(j.log||[]).join('\\n');
 const names={queued:'Waiting ...',slicing:'Slicing ... (can take a few minutes)',uploading:'Sending to printer ...'};
 if(j.status==='done'){$('state').className='st ok';$('state').textContent=j.started?'Printing started!':'Ready to print.';
  const t=j.stats||{};$('stats').textContent=[t.time&&('Estimated time: '+t.time),t.filament_g&&('Filament: '+t.filament_g+' g')].filter(Boolean).join('  |  ');
  $('open').hidden=false;$('btn').disabled=false;if(!j.started){$('startbtn').hidden=false}return}
 if(j.status==='error'){fail(j.error);return}
 $('state').textContent=names[j.status]||j.status;setTimeout(poll,1500)}
$('startbtn').onclick=async()=>{if(!confirm('Start printing now? Make sure the bed is clear and filament is loaded.'))return;
 const r=await fetch('api/start/'+jid,{method:'POST'});const j=await r.json();
 if(r.ok){$('state').textContent='Printing started!';$('startbtn').hidden=true}else{alert(j.error||'Could not start')}};
</script></main></body></html>"""


@app.route("/")
def index():
    return render_template_string(PAGE, s=settings(), quality=QUALITY)


@app.route("/upload", methods=["POST"])
def upload():
    s = settings()
    f = request.files.get("model")
    if not f or not f.filename:
        return jsonify(error="No file selected."), 400
    name = secure_filename(f.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED:
        return jsonify(error="Unsupported file type. Use STL, 3MF, OBJ, STEP or AMF."), 400
    material = request.form.get("material", s["default_material"])
    if material not in s["materials"]:
        return jsonify(error="Unknown material."), 400
    try:
        lh = "%g" % float(request.form.get("layer_height", "0.2"))
        inf = "%d%%" % max(0, min(100, int(float(request.form.get("infill", "20")))))
    except ValueError:
        return jsonify(error="Bad number in the form."), 400
    if not 0.05 <= float(lh) <= 0.35:
        return jsonify(error="Layer height must be 0.05 - 0.35 mm."), 400
    jid = uuid.uuid4().hex[:10]
    d = os.path.join(JOBS_DIR, jid)
    os.makedirs(d)
    src = os.path.join(d, name)
    f.save(src)
    job = dict(id=jid, name=name, src=src, status="queued", log=[], error=None,
               material=material, layer_height=lh, infill=inf,
               support=bool(request.form.get("support")),
               brim=bool(request.form.get("brim")),
               start=bool(request.form.get("start")), created=time.time())
    with jobs_lock:
        jobs[jid] = job
        for old in sorted(jobs, key=lambda k: jobs[k]["created"])[:-20]:
            shutil.rmtree(os.path.join(JOBS_DIR, old), ignore_errors=True)
            jobs.pop(old, None)
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return jsonify(id=jid)


@app.route("/api/job/<jid>")
def job_status(jid):
    j = jobs.get(jid)
    if not j:
        return jsonify(status="error", error="Unknown job."), 404
    return jsonify({k: j.get(k) for k in
                    ("status", "error", "log", "stats", "started", "uploaded_as")})


@app.route("/api/start/<jid>", methods=["POST"])
def start(jid):
    j = jobs.get(jid)
    if not j or j.get("status") != "done":
        return jsonify(error="Nothing to print."), 400
    if moonraker_state() != "ready" or not settings().get("motion_enabled"):
        return jsonify(error="Printer is not ready (see Mainsail)."), 409
    r = requests.post(MOONRAKER + "/printer/print/start",
                      params={"filename": j["uploaded_as"]}, timeout=15)
    if r.status_code != 200:
        return jsonify(error="Printer refused to start: %s" % r.text[:200]), 502
    j["started"] = True
    return jsonify(ok=True)


@app.route("/health")
def health():
    return jsonify(ok=True, slicer=bool(shutil.which(SLICER_BIN)))


def main():
    s = settings()
    app.config["MAX_CONTENT_LENGTH"] = int(s.get("max_upload_mb", 300)) * 1024 * 1024
    port = int(s.get("port", 8088))
    try:
        from waitress import serve
    except ImportError:          # fallback: Flask's own server (fine on a LAN)
        app.run(host="127.0.0.1", port=port, threaded=True)
        return
    serve(app, host="127.0.0.1", port=port, threads=4,
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"])


if __name__ == "__main__":
    main()
