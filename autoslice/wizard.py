#!/usr/bin/env python3
"""
wizard.py - first-run setup wizard for the ELKMIST OS Auto Slicer page.

Opened automatically the first time somebody visits the Auto Slicer
(http://<printer>/api/slicer/). Steps: welcome -> extruder -> material ->
Wi-Fi (optional) -> check and finish.

It is plugged into app.py with:    wizard.register(app)
Nothing here talks to the printer hardware. It only
  * writes choices into ~/printer_data/config/machine.local.ini
  * runs tools/generate_config.py to rebuild printer.cfg / slicer settings
  * asks Moonraker to restart Klipper
  * (optional) calls nmcli to join a Wi-Fi network
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import requests
from flask import Blueprint, Response, jsonify, redirect, request

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.expanduser("~/printer_data/autoslice")
DONE_FILE = os.path.join(STATE_DIR, "setup_done.json")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
LOCAL_INI = os.path.expanduser("~/printer_data/config/machine.local.ini")
LOCAL_PROFILES = os.path.expanduser("~/printer_data/config/extruder_profiles.local.ini")

bp = Blueprint("wizard", __name__)
_lock = threading.Lock()          # one config change at a time
_gc = None


# ----------------------------------------------------------------- helpers
def moonraker_url():
    return os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")


def gc():
    """generate_config.py loaded as a module (it only uses the standard library)."""
    global _gc
    if _gc is None:
        spec = importlib.util.spec_from_file_location(
            "kit_generate_config", os.path.join(KIT, "tools", "generate_config.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _gc = mod
    return _gc


def is_done():
    return os.path.exists(DONE_FILE)


def read_settings():
    try:
        with open(SETTINGS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def run_generate(*args):
    """Run tools/generate_config.py. Returns (exit_code, output)."""
    cmd = [sys.executable, os.path.join(KIT, "tools", "generate_config.py"),
           "--no-ask"] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return 1, "The configuration tool took too long."
    return p.returncode, (p.stdout + p.stderr)


def missing_items(output):
    """Lines under 'Still needed for motion:' in the generator report."""
    items, on = [], False
    for ln in output.splitlines():
        if ln.startswith("Still needed for motion"):
            on = True
            continue
        if on:
            if ln.startswith("   - "):
                items.append(ln[5:].strip())
            else:
                on = False
    return items


def line_value(output, prefix):
    for ln in output.splitlines():
        if ln.startswith(prefix):
            return ln[len(prefix):].strip()
    return ""


def klipper_state():
    try:
        r = requests.get(moonraker_url() + "/printer/info", timeout=4)
        return r.json()["result"]["state"]
    except Exception:
        return None


def restart_klipper():
    try:
        requests.post(moonraker_url() + "/printer/firmware_restart", timeout=10)
        return True
    except Exception:
        return False


def load_profiles():
    return gc().load_profiles([os.path.join(KIT, "extruder_profiles.ini"), LOCAL_PROFILES])


def read_cfg():
    paths = [p for p in (os.path.join(KIT, "machine.ini"), LOCAL_INI) if os.path.exists(p)]
    if not paths:
        return None
    return gc().Cfg(paths)


# ----------------------------------------------------------------- Wi-Fi (NetworkManager)
def parse_terse(line):
    """Split an 'nmcli -t' line on ':' (a literal colon is written '\\:')."""
    parts, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def wifi_available():
    return shutil.which("nmcli") is not None


def wifi_scan():
    p = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                       capture_output=True, text=True, timeout=30)
    best = {}
    for ln in p.stdout.splitlines():
        f = parse_terse(ln)
        if len(f) < 3 or not f[0]:
            continue
        try:
            sig = int(f[1])
        except ValueError:
            sig = 0
        if f[0] not in best or sig > best[f[0]]["signal"]:
            best[f[0]] = {"ssid": f[0], "signal": sig,
                          "secure": bool(f[2]) and f[2] != "--"}
    return sorted(best.values(), key=lambda n: -n["signal"])


# ----------------------------------------------------------------- API
@bp.route("/api/setup/status")
def setup_status():
    mod = gc()
    cfg = read_cfg()
    profiles = load_profiles()
    chosen = None
    if cfg is not None:
        chosen = mod.chosen_model(cfg)
    model = mod.model_key(chosen) if chosen else mod.DEFAULT_MODEL
    plist = []
    for sec in profiles.sections():
        plist.append({
            "key": sec,
            "title": mod.profile_value(profiles, sec, "title") or sec,
            "vendor": mod.profile_value(profiles, sec, "vendor") or "",
            "origin": mod.profile_value(profiles, sec, "origin") or "",
            "missing": mod.profile_missing(profiles, sec),
        })
    code, out = run_generate("--check")
    s = read_settings()
    warnings = [ln[len("WARNING: "):] for ln in out.splitlines() if ln.startswith("WARNING: ")]
    return jsonify(
        done=is_done(),
        printer_name=s.get("printer_name", "3D printer"),
        extruder_chosen=bool(chosen),
        extruder_model=model,
        extruder_title=line_value(out, "EXTRUDER:"),
        profiles=plist,
        materials=list((s.get("materials") or {}).keys()),
        default_material=s.get("default_material", "PLA"),
        wifi_available=wifi_available(),
        klipper_state=klipper_state(),
        slicer_installed=shutil.which(os.environ.get("PRUSA_SLICER_BIN", "prusa-slicer")) is not None,
        motion_enabled=line_value(out, "MODE:").startswith("MOTION"),
        missing=missing_items(out),
        warnings=warnings,
    )


@bp.route("/api/setup/extruder", methods=["POST"])
def setup_extruder():
    model = (request.get_json(silent=True) or {}).get("model", "")
    key = gc().model_key(model)
    if key not in load_profiles().sections():
        return jsonify(error="Unknown extruder."), 400
    with _lock:
        code, out = run_generate("--extruder", key)
        if code != 0:
            return jsonify(error=out.strip().splitlines()[-1] if out.strip() else "Failed."), 500
        restarted = restart_klipper()
    return jsonify(ok=True, restarted=restarted)


@bp.route("/api/setup/material", methods=["POST"])
def setup_material():
    material = (request.get_json(silent=True) or {}).get("material", "")
    if material not in (read_settings().get("materials") or {}):
        return jsonify(error="Unknown material."), 400
    with _lock:
        gc().set_local_value(LOCAL_INI, "autoslice", "default_material", material)
        code, out = run_generate()
        if code != 0:
            return jsonify(error="Could not save the material."), 500
    return jsonify(ok=True)


@bp.route("/api/setup/wifi/scan")
def setup_wifi_scan():
    if not wifi_available():
        return jsonify(error="Wi-Fi setup is not available on this system."), 404
    try:
        return jsonify(networks=wifi_scan())
    except Exception as e:
        return jsonify(error="Scan failed: %s" % e), 500


@bp.route("/api/setup/wifi", methods=["POST"])
def setup_wifi():
    if not wifi_available():
        return jsonify(error="Wi-Fi setup is not available on this system."), 404
    d = request.get_json(silent=True) or {}
    ssid = str(d.get("ssid", "")).strip()
    pw = str(d.get("password", ""))
    if not ssid or len(ssid.encode()) > 32 or ssid.startswith("-"):
        return jsonify(error="Please choose a valid network name."), 400
    if pw and not 8 <= len(pw) <= 63:
        return jsonify(error="A Wi-Fi password has 8 to 63 characters."), 400
    cmd = ["nmcli", "dev", "wifi", "connect", ssid] + (["password", pw] if pw else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return jsonify(error="Connecting took too long."), 504
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip().splitlines()
        return jsonify(error=msg[-1] if msg else "Could not connect."), 400
    return jsonify(ok=True)


@bp.route("/api/setup/finish", methods=["POST"])
def setup_finish():
    os.makedirs(STATE_DIR, exist_ok=True)
    s = read_settings()
    with open(DONE_FILE, "w") as fh:
        json.dump({"done": True, "time": int(time.time()),
                   "extruder": s.get("extruder_model")}, fh)
    return jsonify(ok=True)


@bp.route("/setup")
def setup_page():
    return Response(PAGE, mimetype="text/html")


def register(app):
    """Add the wizard to the Auto Slicer Flask app."""
    app.register_blueprint(bp)

    @app.before_request
    def _first_run():
        # Only the start page is redirected; ?skip=1 lets an installer bypass it.
        if (request.path == "/" and request.method == "GET"
                and not request.args.get("skip") and not is_done()):
            return redirect("setup")


# ----------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setup</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1c2330;--mut:#667085;--acc:#1f6feb;--ok:#127a3d;--err:#b42318;--warn:#9a6700;--line:#d9dde4}
@media(prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e8ebf0;--mut:#9aa4b2;--acc:#5b9dff;--ok:#4ade80;--err:#ff8a80;--warn:#f0b72f;--line:#2c3441}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:8px 0 2px}h2{font-size:1.15rem;margin:0 0 8px}
p.sub{margin:0 0 16px;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.dots{display:flex;gap:6px;margin:0 0 14px}.dots i{flex:1;height:5px;border-radius:3px;background:var(--line)}
.dots i.on{background:var(--acc)}
label{display:block;font-weight:600;margin:12px 0 4px}
select,input[type=password],input[type=text]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
.opt{display:flex;gap:10px;align-items:flex-start;padding:10px;border:1px solid var(--line);border-radius:8px;margin:8px 0;cursor:pointer;font-weight:400}
.opt input{margin-top:5px}.opt small{display:block;color:var(--mut)}
.opt.sel{border-color:var(--acc)}
button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}button.sec{background:transparent;color:var(--acc);border:1px solid var(--acc)}
.msg{margin-top:10px;font-weight:600}.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}.msg.warn{color:var(--warn)}
ul.chk{list-style:none;padding:0;margin:8px 0}ul.chk li{padding:6px 0;border-bottom:1px solid var(--line)}
ul.chk li:last-child{border:0}.good{color:var(--ok)}.bad{color:var(--err)}.wait{color:var(--warn)}
.small{color:var(--mut);font-size:.9rem}
a{color:var(--acc)}
</style></head><body><main>
<h1 id="title">Setup</h1>
<p class="sub">A few quick steps before the first print.</p>
<div class="dots" id="dots"></div>
<div class="card" id="panel">Loading ...</div>
<p class="small"><a href="./?skip=1">Skip setup for now</a></p>
<script>
const $=id=>document.getElementById(id);
let S=null,step=0,steps=[];
async function api(path,opts){const r=await fetch(path,opts);let j={};try{j=await r.json()}catch(e){}
 if(!r.ok)throw new Error(j.error||('Error '+r.status));return j}
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
function el(tag,attrs,kids){const e=document.createElement(tag);
 for(const k in (attrs||{})){if(k==='text')e.textContent=attrs[k];else if(k==='onclick')e.onclick=attrs[k];else e.setAttribute(k,attrs[k])}
 (kids||[]).forEach(c=>e.appendChild(typeof c==='string'?document.createTextNode(c):c));return e}
function msg(cls,text){const m=$('msg');if(m){m.className='msg '+cls;m.textContent=text}}
function head(text,sub){const p=$('panel');p.textContent='';p.appendChild(el('h2',{text:text}));if(sub)p.appendChild(el('p',{class:'small',text:sub}));return p}
function nav(p,label,fn){const b=el('button',{id:'next',text:label,onclick:fn});p.appendChild(b);p.appendChild(el('div',{id:'msg',class:'msg'}));return b}
function dots(){const d=$('dots');d.textContent='';steps.forEach((s,i)=>d.appendChild(el('i',{class:i<=step?'on':''})))}
function go(n){step=n;dots();views[steps[step]]()}
function next(){go(Math.min(step+1,steps.length-1))}

const views={
 welcome(){
  const p=head('Welcome','This takes about two minutes. You can change everything later.');
  p.appendChild(el('p',{text:'Machine: '+S.printer_name}));
  if(S.warnings.length)S.warnings.forEach(w=>p.appendChild(el('p',{class:'msg warn',text:w})));
  nav(p,'Start',next)},
 extruder(){
  const p=head('Which extruder is built into your printer?','Pick the model printed on the extruder or in its manual.');
  let sel=S.extruder_model;
  S.profiles.forEach(pr=>{
   const r=el('input',{type:'radio',name:'ex',value:pr.key});if(pr.key===sel)r.checked=true;
   const t=el('span',{},[pr.title,el('small',{text:pr.vendor+(pr.origin?', '+pr.origin:'')+(pr.missing.length?'  -  setup data incomplete (ask your installer)':'')})]);
   const lab=el('label',{class:'opt'+(pr.key===sel?' sel':'')},[r,t]);
   r.onchange=()=>{sel=pr.key;document.querySelectorAll('.opt').forEach(o=>o.classList.remove('sel'));lab.classList.add('sel')};
   p.appendChild(lab)});
  nav(p,'Save and continue',async()=>{
   $('next').disabled=true;msg('','Saving and restarting the printer software ...');
   try{await post('api/setup/extruder',{model:sel});S=await api('api/setup/status');next()}
   catch(e){msg('err',e.message);$('next').disabled=false}})},
 material(){
  const p=head('Which filament do you use most?','You can still choose another material for every print.');
  const s=el('select',{id:'mat'});
  S.materials.forEach(m=>{const o=el('option',{text:m,value:m});if(m===S.default_material)o.selected=true;s.appendChild(o)});
  p.appendChild(s);
  nav(p,'Save and continue',async()=>{
   $('next').disabled=true;
   try{await post('api/setup/material',{material:s.value});S=await api('api/setup/status');next()}
   catch(e){msg('err',e.message);$('next').disabled=false}})},
 wifi(){
  const p=head('Wi-Fi (optional)','Skip this if the printer is connected by a network cable.');
  const sel=el('select',{id:'net'});sel.appendChild(el('option',{text:'Press "Search" first',value:''}));
  p.appendChild(el('label',{text:'Network'}));p.appendChild(sel);
  p.appendChild(el('button',{class:'sec',text:'Search for networks',onclick:async()=>{
   msg('','Searching ...');
   try{const j=await api('api/setup/wifi/scan');sel.textContent='';
    j.networks.forEach(n=>sel.appendChild(el('option',{value:n.ssid,text:n.ssid+(n.secure?'  (secured)':'')+'  '+n.signal+'%'})));
    if(!j.networks.length)sel.appendChild(el('option',{text:'No networks found',value:''}));msg('','')}
   catch(e){msg('err',e.message)}}}));
  p.appendChild(el('label',{text:'Password'}));
  const pw=el('input',{type:'password',id:'pw',autocomplete:'off'});p.appendChild(pw);
  p.appendChild(el('p',{class:'small',text:'After connecting, the printer may get a new address. If this page stops answering, reconnect your device to the same Wi-Fi and open http://'+location.hostname+'/'}));
  const b=nav(p,'Connect',async()=>{
   if(!sel.value){msg('err','Choose a network first.');return}
   b.disabled=true;msg('','Connecting ... this can take up to a minute');
   try{await post('api/setup/wifi',{ssid:sel.value,password:pw.value});msg('ok','Connected.');b.disabled=false}
   catch(e){msg('err',e.message);b.disabled=false}});
  p.appendChild(el('button',{class:'sec',text:'Skip / Continue',onclick:next}))},
 async check(){
  const p=head('Check','Everything below should be green before the first print.');
  p.appendChild(el('div',{id:'chk',text:'Checking ...'}));
  try{S=await api('api/setup/status')}catch(e){$('chk').textContent=e.message}
  const ul=el('ul',{class:'chk'});
  const row=(ok,text,note)=>ul.appendChild(el('li',{},[el('span',{class:ok===true?'good':ok===false?'bad':'wait',text:(ok===true?'OK  ':ok===false?'!  ':'...  ')}),text+(note?' - '+note:'')]));
  row(true,'Extruder: '+(S.extruder_title||S.extruder_model));
  row(S.slicer_installed,'Slicer',S.slicer_installed?'installed':'not installed - ask your installer');
  row(S.klipper_state==='ready'?true:null,'Printer software',S.klipper_state||'not reachable yet');
  row(S.motion_enabled?true:null,'Motion',S.motion_enabled?'enabled':'safe mode (printing is disabled until the installer finishes the machine setup)');
  $('chk').textContent='';$('chk').appendChild(ul);
  if(!S.motion_enabled&&S.missing.length){
   const d=el('details',{},[el('summary',{text:'Still missing ('+S.missing.length+')'}),el('p',{class:'small',text:S.missing.join(', ')})]);$('chk').appendChild(d)}
  nav(p,'Finish',async()=>{$('next').disabled=true;
   try{await post('api/setup/finish');location.href='./?skip=1'}catch(e){msg('err',e.message);$('next').disabled=false}})}
};

(async()=>{
 try{S=await api('api/setup/status')}catch(e){$('panel').textContent='Could not load: '+e.message;return}
 document.title=S.printer_name+' - Setup';$('title').textContent=S.printer_name+' setup';
 steps=['welcome','extruder','material'].concat(S.wifi_available?['wifi']:[]).concat(['check']);
 go(0)})();
</script></main></body></html>
"""
