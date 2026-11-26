from pathlib import Path
import html
import io
import json
import struct
import zipfile
import xml.etree.ElementTree as ET
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import requests
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

app = FastAPI()

MOONRAKER_URL = os.getenv("MOONRAKER_URL", "").rstrip("/")
SPOOLMAN_URL = os.getenv("SPOOLMAN_URL", "").rstrip("/")
FLUIDD_PUBLIC_URL = os.getenv("FLUIDD_PUBLIC_URL", "").rstrip("/")
SPOOLMAN_PUBLIC_URL = os.getenv("SPOOLMAN_PUBLIC_URL", "").rstrip("/")
FLAT_DIR = pathlib.Path("/profiles")
MACHINE_PROFILE = FLAT_DIR / "machine.json"

def get_bed_size():
    try:
        cfg = json.loads(pathlib.Path(MACHINE_PROFILE).read_text(encoding="utf-8"))
        area = cfg.get("printable_area") or []
        if isinstance(area, str):
            area = [pt.strip() for pt in area.split(",") if pt.strip()]
        xs, ys = [], []
        for pt in area:
            if isinstance(pt, str) and "x" in pt.lower():
                x, y = pt.lower().split("x", 1)
                xs.append(float(x))
                ys.append(float(y))
        if xs and ys:
            w = round(max(xs) - min(xs), 1)
            d = round(max(ys) - min(ys), 1)
            if w > 0 and d > 0:
                return w, d
    except Exception:
        pass
    return 250.0, 250.0


def _inject_ui_config(resp):
    bx, by = get_bed_size()
    repls = {
        "__BED_X__": str(bx),
        "__BED_Y__": str(by),
        "__HAS_SPOOLMAN__": "true" if SPOOLMAN_URL else "false",
        "__HAS_FLUIDD_LINK__": "true" if FLUIDD_PUBLIC_URL else "false",
        "__HAS_SPOOLMAN_LINK__": "true" if SPOOLMAN_PUBLIC_URL else "false",
    }
    if isinstance(resp, str):
        for k, v in repls.items():
            resp = resp.replace(k, v)
        return resp
    if hasattr(resp, "body") and isinstance(resp.body, (bytes, bytearray)):
        body = resp.body.decode("utf-8", errors="ignore")
        for k, v in repls.items():
            body = body.replace(k, v)
        return HTMLResponse(body)
    return resp

PROCESS_DIR = FLAT_DIR / "process"
FILAMENT_DIR = FLAT_DIR / "filament"
JOBS_DIR = pathlib.Path("/tmp/orca_jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)



def _extract_float(val):
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None:
        return None
    try:
        return float(str(val).strip())
    except Exception:
        return None


def list_profiles(directory: pathlib.Path, sort_mode: str = "name"):
    rows = []
    for p in sorted(directory.glob("*.json")):
        cfg = {}
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        name = cfg.get("name") or p.stem
        if sort_mode == "filament":
            cost = _extract_float(cfg.get("filament_cost"))
            key = (cost if (cost is not None and cost > 0) else float("inf"), name.lower())
        elif sort_mode == "process":
            lh = _extract_float(cfg.get("layer_height"))
            if lh is None:
                m = re.search(r"(\d+\.\d+)\s*mm", name)
                lh = float(m.group(1)) if m else 0.0
            key = (-lh, name.lower())
        else:
            key = (name.lower(),)
        rows.append((key, p.name, name))
    rows.sort(key=lambda x: x[0])
    return [(fname, label) for _, fname, label in rows]


def render_options(items):
    return "\n".join(
        f'<option value="{html.escape(key)}">{html.escape(label)}</option>'
        for key, label in items
    )


def normalize_hex(val: str) -> str:
    if not val:
        return ""
    first = val.split(",")[0].strip().lstrip("#")
    return f"#{first}" if re.fullmatch(r"[0-9a-fA-F]{6}", first) else ""

def fetch_slots_info():
    slots = {
        i: {"label": f"Slot {i + 1} (T{i})", "material": "", "color": "", "spool": None, "unit_price": None}
        for i in range(4)
    }
    try:
        r = requests.get(f"{MOONRAKER_URL}/printer/objects/query?save_variables", timeout=3)
        r.raise_for_status()
        variables = r.json().get("result", {}).get("status", {}).get("save_variables", {}).get("variables", {})
    except Exception:
        return slots

    spool_map = {}
    try:
        rs = requests.get(f"{SPOOLMAN_URL}/api/v1/spool", timeout=3)
        rs.raise_for_status()
        raw = rs.json()
        items = raw.get("items") if isinstance(raw, dict) else raw
        for s in items or []:
            spool_map[s.get("id")] = s
    except Exception:
        return slots

    for i in range(4):
        sid = variables.get(f"box1_slot{i + 1}__spool_id")
        spool = spool_map.get(sid)
        if not spool:
            continue
        fil = spool.get("filament") or {}
        vendor = (fil.get("vendor") or {}).get("name") or ""
        fname = fil.get("name") or ""
        mat = (fil.get("material") or "").strip()
        color = normalize_hex(fil.get("color_hex") or fil.get("multi_color_hexes") or "")
        rem = round(spool.get("remaining_weight") or 0)
        price = spool.get("price") if spool.get("price") is not None else fil.get("price")
        init_w = spool.get("initial_weight") or fil.get("weight") or 0
        unit_price = (float(price) / float(init_w)) if (price is not None and float(price) > 0 and float(init_w) > 0) else None
        full_name = " ".join(x for x in [vendor, fname] if x).strip() or f"Szpula #{sid}"
        meta = ", ".join(x for x in [mat, f"{rem} g"] if x)
        slots[i] = {
            "label": f"Slot {i + 1} (T{i}) — {full_name} ({meta})",
            "material": mat.upper(),
            "color": color,
            "spool": full_name,
            "unit_price": unit_price,
        }
    return slots


def render_slot_options(slots):
    priced = [(info["unit_price"], i) for i, info in slots.items() if info.get("unit_price") is not None]
    best_idx = min(priced)[1] if priced else 0
    out = []
    for i in range(4):
        info = slots[i]
        sel = " selected" if i == best_idx else ""
        out.append(
            f'<option value="{i}"{sel} data-material="{html.escape(info["material"])}" data-color="{html.escape(info["color"])}">'
            f'{html.escape(info["label"])}</option>'
        )
    return "\n".join(out)


def cleanup_old_jobs(max_age_sec: int = 86400):
    now = time.time()
    for p in JOBS_DIR.iterdir():
        try:
            if p.is_dir() and (now - p.stat().st_mtime) > max_age_sec:
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


def _nat_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name)]


def _cleanup_loop():
    while True:
        cleanup_old_jobs(86400)
        time.sleep(3600)

threading.Thread(target=_cleanup_loop, daemon=True).start()

def parse_gcode_stats(gcode_path: pathlib.Path):
    est_time = ""
    est_weight = ""
    try:
        size = gcode_path.stat().st_size
        with open(gcode_path, "rb") as f:
            head = f.read(65536).decode("utf-8", errors="ignore")
            tail = ""
            if size > 65536:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="ignore")
        text = head + "\n" + tail
        m_time = re.search(r";\s*estimated printing time \(normal mode\)\s*=\s*(.+)", text)
        if m_time:
            est_time = m_time.group(1).strip()
        m_weight = re.search(r";\s*total filament used \[g\]\s*=\s*([0-9.]+)", text)
        if m_weight:
            est_weight = f"{float(m_weight.group(1)):.1f} g"
    except Exception:
        pass
    return est_time, est_weight


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>orclipper</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 680px; margin: 20px auto; padding: 0 16px 40px; background: #111; color: #eee; }
    .topbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
    .topbar h2 { margin: 0; }
    .nav-links { display: flex; gap: 8px; }
    .nav-links a { text-decoration: none; font-size: 13px; font-weight: 600; color: #ddd; background: #222226; border: 1px solid #333; padding: 7px 12px; border-radius: 8px; }
    .nav-links a:hover { background: #2c2c30; color: #fff; }
    .slot-row { display: flex; align-items: center; gap: 10px; margin-top: 6px; }
    .slot-row select { margin-top: 0; flex: 1; }
    .color-swatch { width: 42px; height: 42px; border-radius: 10px; border: 2px solid #444; background: #2a2a2e; flex-shrink: 0; transition: background-color 0.15s ease; }
    label { display: block; margin-top: 14px; font-weight: 600; font-size: 14px; color: #ccc; }
    input, select, button, textarea { width: 100%; padding: 12px; margin-top: 6px; border-radius: 10px; border: 1px solid #333; background: #1d1d1f; color: #fff; font-size: 15px; }
    textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; min-height: 140px; resize: vertical; }
    .scad-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; margin-top: 6px; }
    .tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    .tab-btn { margin-top: 0; background: #1d1d1f; border: 1px solid #333; color: #aaa; padding: 10px; }
    .tab-btn.active { background: #0a84ff; border-color: #0a84ff; color: #fff; }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    .checks { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }
    .check-item { display: flex; align-items: center; gap: 8px; margin-top: 0; padding: 10px 12px; border-radius: 10px; border: 1px solid #333; background: #1d1d1f; font-weight: 500; font-size: 14px; color: #ddd; cursor: pointer; }
    .check-item input { width: auto; margin: 0; accent-color: #0a84ff; transform: scale(1.15); }
    .scale-box { margin-top: 14px; padding: 12px; border-radius: 10px; border: 1px solid #333; background: #18181b; }
    .scale-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
    .scale-head label { margin: 0; }
    .scale-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    .scale-field label { margin-top: 0; font-size: 12px; color: #999; }
    .scale-field input { padding: 8px 10px; font-size: 14px; margin-top: 4px; }
    .layer-bar { display: none; align-items: center; gap: 12px; padding: 10px 12px; background: #18181b; border-top: 1px solid #333; font-size: 13px; color: #ccc; }
    .layer-bar input[type='range'] { flex: 1; margin: 0; padding: 0; accent-color: #0a84ff; cursor: pointer; }
    .layer-bar strong { color: #fff; min-width: 72px; text-align: right; }
    .plate-tabs { display: none; position: relative; z-index: 20; flex-wrap: wrap; gap: 6px; padding: 8px 12px; background: #141417; border-bottom: 1px solid #2c2c2e; }
    .viewer-canvas { position: relative; overflow: hidden; }
    .plate-tab { width: auto; margin: 0; padding: 6px 12px; font-size: 13px; border-radius: 8px; background: #26262a; color: #ccc; border: 1px solid #3a3a3f; cursor: pointer; }
    .plate-tab.active { background: #0a84ff; color: #fff; border-color: #0a84ff; }
    .plate-tab.sent { border-color: #30d158; }
    .queue-box { margin: 12px 0; padding: 10px 12px; background: #1c1c1e; border: 1px solid #2c2c2e; border-radius: 10px; }
    .queue-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .queue-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
    .queue-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px; background: #2c2c2e; border-radius: 8px; font-size: 13px; }
    .queue-item-info { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .queue-item-meta { color: #aeaeb2; font-size: 12px; margin-left: 6px; }
    .queue-item-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
    .queue-item-actions input { width: 52px; height: 26px; margin: 0; padding: 2px 4px; font-size: 12px; text-align: center; }
    .queue-del { background: #3a3a3c; color: #ff453a; border: none; border-radius: 6px; width: 26px; height: 26px; padding: 0; margin: 0; display: inline-flex; align-items: center; justify-content: center; line-height: 1; font-size: 13px; cursor: pointer; font-weight: 700; }
    .brim-box { display: flex; align-items: center; flex-wrap: nowrap; gap: 8px; padding: 8px 12px; background: #1c1c1e; border: 1px solid #2c2c2e; border-radius: 10px; margin: 0; font-size: 14px; font-weight: 600; color: #f2f2f7; white-space: nowrap; }
    .brim-box select { width: auto; min-width: 0; height: 26px; margin: 0; padding: 2px 8px; font-size: 13px; line-height: 1.2; }
    .brim-width-wrap { display: none; align-items: center; gap: 4px; font-size: 13px; font-weight: 500; color: #aeaeb2; }
    .brim-width-wrap input { width: 56px; height: 26px; margin: 0; padding: 2px 6px; font-size: 13px; line-height: 1.2; text-align: center; }
    .brim-box .help-tip { margin-left: 0; }
    *:has(> .brim-box) { display: grid !important; grid-template-columns: 1fr !important; gap: 8px; }
    .brim-box { width: 100%; box-sizing: border-box; }
    .brim-box input[type="checkbox"] { width: 16px; height: 16px; margin: 0; flex-shrink: 0; }
    .brim-box select { flex-shrink: 0; }
    .help-tip { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; margin-left: 5px; border-radius: 50%; background: #2c2c32; border: 1px solid #48484f; color: #aeaeb2; font-size: 11px; font-weight: 700; cursor: help; vertical-align: middle; user-select: none; flex-shrink: 0; }
    .help-tip:hover, .help-tip.open { background: #0a84ff; border-color: #0a84ff; color: #fff; }
    .help-tip .help-pop { display: none; position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%); width: 240px; padding: 8px 10px; border-radius: 8px; background: #1c1c21; border: 1px solid #3a3a42; color: #e5e5ea; font-size: 12px; font-weight: 400; line-height: 1.4; text-align: left; white-space: normal; box-shadow: 0 8px 24px rgba(0,0,0,0.7); z-index: 200; pointer-events: none; }
    .help-tip:hover .help-pop, .help-tip.open .help-pop { display: block; }
    .overhang-hint { display: none; margin: 6px 0 4px; padding: 7px 10px; border-radius: 8px; background: rgba(255, 159, 10, 0.14); border: 1px solid rgba(255, 159, 10, 0.45); color: #ffd60a; font-size: 12px; line-height: 1.4; }
    .large-gcode-bar { display: none; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; background: #232329; border-bottom: 1px solid #3a3a3f; font-size: 13px; color: #e5e5ea; }
    .large-gcode-bar button { width: auto; margin: 0; padding: 6px 12px; font-size: 13px; }
    #statusBox { position: fixed; top: auto; bottom: 16px; left: 50%; transform: translateX(-50%); z-index: 150; width: fit-content; max-width: min(520px, calc(100vw - 32px)); height: auto; max-height: 30vh; overflow-y: auto; margin: 0; box-shadow: 0 8px 24px rgba(0,0,0,0.65); }
    body { padding-bottom: 84px; }
    .cam-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 200; display: none; align-items: center; justify-content: center; padding: 16px; }
    .cam-backdrop.open { display: flex; }
    .cam-modal { width: 100%; max-width: 560px; background: #18181b; border: 1px solid #333; border-radius: 14px; padding: 16px; box-shadow: 0 16px 40px rgba(0,0,0,0.6); }
    .cam-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .cam-head h3 { margin: 0; font-size: 16px; color: #fff; }
    .cam-frame { width: 100%; aspect-ratio: 16 / 9; background: #0f0f11; border: 1px solid #2c2c2e; border-radius: 10px; overflow: hidden; display: flex; align-items: center; justify-content: center; color: #888; font-size: 13px; }
    .cam-frame img { width: 100%; height: 100%; object-fit: contain; display: none; }
    .cam-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }
    .cam-actions button { margin-top: 0; }
    button { border: none; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-primary { background: #0a84ff; margin-top: 18px; }
    .btn-secondary { background: #2c2c2e; border: 1px solid #444; }
    .btn-danger { background: #30d158; color: #000; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }
    .viewer-wrap { margin-top: 16px; border: 1px solid #2c2c2e; border-radius: 12px; overflow: hidden; background: #18181b; display: none; }
    .viewer-head { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: #222226; font-size: 13px; color: #bbb; }
    .viewer-canvas { width: 100%; height: 320px; display: block; touch-action: none; }
    .status { position: fixed; top: 12px; left: 50%; transform: translateX(-50%); width: calc(100% - 32px); max-width: 648px; z-index: 100; padding: 12px 16px; border-radius: 10px; font-size: 14px; display: none; white-space: pre-wrap; word-break: break-word; box-shadow: 0 8px 24px rgba(0,0,0,0.55); cursor: pointer; }
    .status.info { background: #1c2a3a; color: #9cd0ff; display: block; }
    .status.ok { background: #163020; color: #7ef2a6; display: block; }
    .status.err { background: #3a1c1c; color: #ff9c9c; display: block; }
    .stats { display: flex; gap: 14px; font-size: 13px; color: #9cd0ff; }
    .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.75); display: none; align-items: center; justify-content: center; padding: 16px; z-index: 50; }
    .modal-bg.open { display: flex; }
    .modal { background: #1d1d1f; border: 1px solid #333; border-radius: 14px; padding: 20px; max-width: 420px; width: 100%; }
    .modal h3 { margin: 0 0 10px; }
    .modal p { margin: 6px 0; font-size: 14px; color: #ccc; }
  
    .header-bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
    .header-bar h1 { margin: 0; font-size: 1.5rem; }
    .lang-select { width: auto; margin: 0 0 0 10px; padding: 5px 10px; font-size: 13px; font-weight: 600; border-radius: 8px; background: #222226; color: #eee; border: 1px solid #333; cursor: pointer; }
  </style>
  <script type="importmap">
  {
    "imports": {
      "three": "https://esm.sh/three@0.169.0",
      "three/addons/": "https://esm.sh/three@0.169.0/examples/jsm/"
    }
  }
  </script>
  <script>window.BED_X = __BED_X__; window.BED_Y = __BED_Y__; window.BED_MAX = Math.max(window.BED_X, window.BED_Y); window.HAS_SPOOLMAN = __HAS_SPOOLMAN__; window.HAS_FLUIDD_LINK = __HAS_FLUIDD_LINK__; window.HAS_SPOOLMAN_LINK = __HAS_SPOOLMAN_LINK__;</script>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 64 64%27%3E%3Crect width=%2764%27 height=%2764%27 rx=%2714%27 fill=%27%23141417%27/%3E%3Cpath d=%27M14 52V14h36v38%27 fill=%27none%27 stroke=%27%23e5e5ea%27 stroke-width=%275%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27/%3E%3Cline x1=%2714%27 y1=%2726%27 x2=%2750%27 y2=%2726%27 stroke=%27%238e8e93%27 stroke-width=%274%27/%3E%3Crect x=%2726%27 y=%2721%27 width=%2712%27 height=%2710%27 rx=%272%27 fill=%27%230a84ff%27/%3E%3Cpolygon points=%2729,31 35,31 32,37%27 fill=%27%230a84ff%27/%3E%3Crect x=%2720%27 y=%2742%27 width=%2724%27 height=%276%27 rx=%271.5%27 fill=%27%2330d158%27/%3E%3Cline x1=%2710%27 y1=%2752%27 x2=%2754%27 y2=%2752%27 stroke=%27%23e5e5ea%27 stroke-width=%275%27 stroke-linecap=%27round%27/%3E%3C/svg%3E" />
</head>
<body>
  <div class="topbar">
    <div class="header-bar">
      <h1>orclipper</h1>
      <select id="langSwitch" class="lang-select" aria-label="Language"></select>
    </div>
    <div class="nav-links">
      <a href="__FLUIDD_URL__" target="_blank" rel="noopener">Fluidd</a>
      <a href="__SPOOLMAN_URL__" target="_blank" rel="noopener">Spoolman</a>
    </div>
  </div>

  <form id="sliceForm">
    <div class="tabs">
      <button type="button" id="tabFileBtn" class="tab-btn active" data-i18n="tab_file">Plik z modelem</button>
      <button type="button" id="tabScadBtn" class="tab-btn" data-i18n="tab_scad">Kod OpenSCAD</button>
    </div>

    <div id="paneFile" class="tab-pane active">
      <label data-i18n="label_model">Model (.stl / .3mf / .scad)</label>
      <input type="file" id="fileInput" name="file" accept=".stl,.3mf,.scad">
      <div id="modelViewerWrap" class="viewer-wrap">
        <div class="viewer-head">
          <span data-i18n="preview_file">Podgląd pliku (przeciągnij, aby obrócić)</span>
          <span id="modelName"></span>
        </div>
        <div id="modelCanvas" class="viewer-canvas"></div>
      </div>
    </div>

    <div id="paneScad" class="tab-pane">
      <label data-i18n="tab_scad">Kod OpenSCAD</label>
      <textarea id="scadCode" name="scad_code" placeholder="cube([20, 20, 10]);"></textarea>
      <label data-i18n="label_project_name">Nazwa projektu (pliku .gcode)</label>
      <div class="scad-row">
        <input type="text" id="scadName" name="scad_name" value="" placeholder="np. uchwyt_kabel" data-i18n-placeholder="placeholder_scad_name">
        <button type="button" id="previewScadBtn" class="btn-secondary" style="width:auto;margin-top:6px;" data-i18n="btn_preview_3d">Podgląd 3D</button>
      </div>
      <div id="scadViewerWrap" class="viewer-wrap">
        <div class="viewer-head">
          <span data-i18n="preview_scad">Podgląd OpenSCAD (przeciągnij, aby obrócić)</span>
          <span id="scadModelName"></span>
        </div>
        <div id="scadCanvas" class="viewer-canvas"></div>
      </div>
    </div>

    <div class="scale-box">
      <div class="scale-head">
        <label data-i18n="scale_section">Wymiary i skalowanie</label>
        <label class="check-item" style="padding:5px 10px;font-size:12px;">
          <input type="checkbox" id="uniformScale" checked>
          <span data-i18n="keep_ratio">Zachowaj proporcje</span>
        </label>
      </div>
      <div class="scale-grid">
        <div class="scale-field">
          <label data-i18n="scale_pct">Skala (%)</label>
          <input type="number" id="scalePct" value="100" min="1" max="2000" step="1">
        </div>
        <div class="scale-field">
          <label>X (mm)</label>
          <input type="number" id="dimX" placeholder="—" min="0.1" step="0.1">
        </div>
        <div class="scale-field">
          <label>Y (mm)</label>
          <input type="number" id="dimY" placeholder="—" min="0.1" step="0.1">
        </div>
        <div class="scale-field">
          <label>Z (mm)</label>
          <input type="number" id="dimZ" placeholder="—" min="0.1" step="0.1">
        </div>
      </div>
    </div>

    <label data-i18n="slot_mmu">Slot / MMU (Spoolman)</label>
    <div class="slot-row">
      <span id="slotColor" class="color-swatch" title="Kolor filamentu"></span>
      <select id="slotSelect" name="slot">__SLOT_OPTS__</select>
    </div>

    <label data-i18n="filament_profile">Profil materiału</label>
    <select id="filamentSelect" name="filament">__FIL_OPTS__</select>

    <label data-i18n="process_profile">Profil procesu</label>
    <select id="processSelect" name="process">__PROC_OPTS__</select>

    <label data-i18n="copies">Liczba kopii</label>
    <input type="number" id="copiesInput" name="copies" value="1" min="1" max="100" required>

    <div class="checks">
      <label class="check-item">
        <input type="checkbox" name="auto_orient" value="true" checked>
        <span data-i18n="auto_orient">Auto-orientacja</span>
      </label>
      <label class="check-item">
        <input type="checkbox" name="auto_arrange" value="true" checked>
        <span data-i18n="auto_arrange">Auto-rozłożenie</span>
      </label>
      <label class="check-item">
        <input type="checkbox" name="enable_support" value="true">
        <span data-i18n="supports">Podpory (Tree)</span>
      </label>
      <div id="overhangHint" class="overhang-hint"></div>
      <label for="brimEnable" class="brim-box"><input type="checkbox" id="brimEnable" checked /><span data-i18n="brim">Rondo (Brim)</span><select id="brimMode" name="brim_mode"><option value="auto" selected data-i18n="brim_auto">Auto</option><option value="on" data-i18n="brim_manual">Ręczny</option></select><input type="hidden" id="brimModeOff" name="brim_mode" value="off" disabled /><span id="brimWidthWrap" class="brim-width-wrap"><input type="number" id="brimWidth" name="brim_width" value="5" min="1" max="30" step="0.5" /><span>mm</span></span></label>
    </div>

    <div class="queue-box">
      <div class="queue-head">
        <span style="font-size:13px;font-weight:600;"><span data-i18n="build_plate">Stół roboczy</span> (<span id="queueCount">0</span>)</span>
        <button type="button" id="addToPlateBtn" class="btn-secondary" style="width:auto;margin:0;padding:6px 10px;font-size:12px;" data-i18n="btn_add_to_plate">+ Dodaj bieżący model</button>
      </div>
      <div id="plateQueueList" class="queue-list"></div>
    </div>
    <button type="submit" id="sliceBtn" class="btn-primary" data-i18n="btn_slice">Potnij</button>
  </form>

  <div id="statusBox" class="status"></div>

  <div id="gcodeViewerWrap" class="viewer-wrap">
    <div class="viewer-head">
      <span data-i18n="preview_gcode">Podgląd G-code</span>
      <div id="gcodeStats" class="stats"></div>
    </div>
    <div id="plateTabs" class="plate-tabs"></div>
      <div id="largeGcodeBar" class="large-gcode-bar"></div>
      <div id="gcodeCanvas" class="viewer-canvas"></div>
      <div id="layerBar" class="layer-bar">
        <span data-i18n="layer">Warstwa</span>
        <input type="range" id="layerSlider" min="1" max="1" value="1">
        <strong id="layerLabel">0 / 0</strong>
      </div>
  </div>

  <div id="postSliceActions" class="actions" style="display:none;">
    <button type="button" id="sendBtn" class="btn-secondary" data-i18n="btn_send">Wyślij</button>
    <button type="button" id="sendPrintBtn" class="btn-danger" data-i18n="btn_send_print">Wyślij i drukuj</button>
  </div>

  <div id="confirmModal" class="modal-bg">
    <div class="modal">
      <h3 data-i18n="modal_confirm_title">Potwierdź start druku</h3>
      <p id="modalSummary"></p>
      <div class="actions">
        <button type="button" id="cancelPrintBtn" class="btn-secondary" data-i18n="btn_cancel">Anuluj</button>
        <button type="button" id="confirmPrintBtn" class="btn-danger" data-i18n="btn_print_now">Drukuj teraz</button>
      </div>
    </div>
  </div>

  <script type="module">
    import * as THREE from 'three';
    import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
    import { STLLoader } from 'three/addons/loaders/STLLoader.js';
    import { ThreeMFLoader } from 'three/addons/loaders/3MFLoader.js';
    import { GCodeLoader } from 'three/addons/loaders/GCodeLoader.js';

    const slotSelect = document.getElementById('slotSelect');
    const slotColor = document.getElementById('slotColor');
    const filamentSelect = document.getElementById('filamentSelect');
    const processSelect = document.getElementById('processSelect');
    const savedProcess = localStorage.getItem('orca_process');
    if (savedProcess && [...processSelect.options].some(o => o.value === savedProcess)) {
      processSelect.value = savedProcess;
    }
    processSelect.addEventListener('change', () => {
      localStorage.setItem('orca_process', processSelect.value);
    });
    const fileInput = document.getElementById('fileInput');
    const scadCode = document.getElementById('scadCode');
    const scadName = document.getElementById('scadName');
    const previewScadBtn = document.getElementById('previewScadBtn');
    const tabFileBtn = document.getElementById('tabFileBtn');
    const tabScadBtn = document.getElementById('tabScadBtn');
    const paneFile = document.getElementById('paneFile');
    const paneScad = document.getElementById('paneScad');
    let activeTab = 'file';

    function setTab(mode) {
      activeTab = mode;
      const isFile = mode === 'file';
      tabFileBtn.classList.toggle('active', isFile);
      tabScadBtn.classList.toggle('active', !isFile);
      paneFile.classList.toggle('active', isFile);
      paneScad.classList.toggle('active', !isFile);
      fileInput.required = isFile;
      scadCode.required = !isFile;
      scadName.required = !isFile;
    }
    tabFileBtn.addEventListener('click', () => setTab('file'));
    tabScadBtn.addEventListener('click', () => setTab('scad'));
    setTab('file');
    const sliceForm = document.getElementById('sliceForm');
    const sliceBtn = document.getElementById('sliceBtn');
    const statusBox = document.getElementById('statusBox');
    const postSliceActions = document.getElementById('postSliceActions');
    const sendBtn = document.getElementById('sendBtn');
    const sendPrintBtn = document.getElementById('sendPrintBtn');
    const confirmModal = document.getElementById('confirmModal');
    const modalSummary = document.getElementById('modalSummary');
    const cancelPrintBtn = document.getElementById('cancelPrintBtn');
    const confirmPrintBtn = document.getElementById('confirmPrintBtn');

    const uniformScale = document.getElementById('uniformScale');
    const scalePct = document.getElementById('scalePct');
    const dimX = document.getElementById('dimX');
    const dimY = document.getElementById('dimY');
    const dimZ = document.getElementById('dimZ');
    let origDims = null;
    let scaleFactors = { x: 1, y: 1, z: 1 };

    function applyPreviewScale() {
      const v = activeTab === 'scad' ? scadViewer : modelViewer;
      if (v) v.setScale(scaleFactors.x, scaleFactors.y, scaleFactors.z);
    }

    function initDims(dims) {
      origDims = dims;
      scaleFactors = { x: 1, y: 1, z: 1 };
      scalePct.value = '100';
      if (dims) {
        dimX.value = dims.x.toFixed(1);
        dimY.value = dims.y.toFixed(1);
        dimZ.value = dims.z.toFixed(1);
      } else {
        dimX.value = dimY.value = dimZ.value = '';
      }
    }

    scalePct.addEventListener('input', () => {
      const pct = parseFloat(scalePct.value);
      if (!pct || pct <= 0) return;
      const f = pct / 100;
      scaleFactors = { x: f, y: f, z: f };
      if (origDims) {
        dimX.value = (origDims.x * f).toFixed(1);
        dimY.value = (origDims.y * f).toFixed(1);
        dimZ.value = (origDims.z * f).toFixed(1);
      }
      applyPreviewScale();
    });

    function onDimChange(axis, inputEl) {
      if (!origDims || !origDims[axis]) return;
      const val = parseFloat(inputEl.value);
      if (!val || val <= 0) return;
      const f = val / origDims[axis];
      if (uniformScale.checked) {
        scaleFactors = { x: f, y: f, z: f };
        scalePct.value = (f * 100).toFixed(1).replace(/\.0$/, '');
        if (axis !== 'x') dimX.value = (origDims.x * f).toFixed(1);
        if (axis !== 'y') dimY.value = (origDims.y * f).toFixed(1);
        if (axis !== 'z') dimZ.value = (origDims.z * f).toFixed(1);
      } else {
        scaleFactors[axis] = f;
        scalePct.value = '';
      }
      applyPreviewScale();
    }
    dimX.addEventListener('input', () => onDimChange('x', dimX));
    dimY.addEventListener('input', () => onDimChange('y', dimY));
    dimZ.addEventListener('input', () => onDimChange('z', dimZ));

    let currentJob = null;

    function setStatus(type, msg) {
      statusBox.className = 'status ' + type;
      statusBox.textContent = msg;
    }
    statusBox.addEventListener('click', () => { statusBox.className = 'status'; });

    function syncFilamentToSlot() {
      const opt = slotSelect.options[slotSelect.selectedIndex];
      const color = opt?.dataset?.color || '';
      slotColor.style.backgroundColor = color || '#2a2a2e';
      slotColor.title = color ? `Kolor: ${color}` : 'Brak koloru w Spoolmanie';
      const mat = (opt?.dataset?.material || '').toUpperCase().trim();
      if (!mat) return;
      const candidates = [mat];
      for (const base of ['PETG', 'PLA', 'ABS', 'ASA', 'TPU', 'PA', 'PC']) {
        if (mat.includes(base) && !candidates.includes(base)) candidates.push(base);
      }
      for (const needle of candidates) {
        for (const fOpt of filamentSelect.options) {
          const txt = (fOpt.textContent + ' ' + fOpt.value).toUpperCase();
          if (txt.includes(needle)) {
            filamentSelect.value = fOpt.value;
            return;
          }
        }
      }
    }
    slotSelect.addEventListener('change', syncFilamentToSlot);
    syncFilamentToSlot();

    function createViewer(container) {
      container.innerHTML = '';
      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0x18181b);

      const camera = new THREE.PerspectiveCamera(45, container.clientWidth / container.clientHeight, 0.1, 2000);
      camera.position.set(160, 140, 160);

      const renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.setSize(container.clientWidth, container.clientHeight);
      container.appendChild(renderer.domElement);

      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;

      scene.add(new THREE.AmbientLight(0xffffff, 0.7));
      const dir = new THREE.DirectionalLight(0xffffff, 0.9);
      dir.position.set(100, 200, 150);
      scene.add(dir);

      const grid = new THREE.GridHelper(window.BED_MAX, Math.round(window.BED_MAX / 10), 0x444444, 0x2a2a2e);
      grid.scale.set(window.BED_X / window.BED_MAX, 1, window.BED_Y / window.BED_MAX);
      scene.add(grid);

      function makeRuler(lengthMm, axisLabel, flipDir) {
        const pxPerMm = 4;
        const hMm = 14;
        const wPx = Math.max(64, Math.round(lengthMm * pxPerMm));
        const hPx = Math.round(hMm * pxPerMm);
        const cv = document.createElement('canvas');
        cv.width = wPx;
        cv.height = hPx;
        const ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, wPx, hPx);

        ctx.strokeStyle = '#636366';
        ctx.fillStyle = '#aeaeb2';
        ctx.font = 'bold 20px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';

        ctx.beginPath();
        ctx.moveTo(0, 2);
        ctx.lineTo(wPx, 2);
        ctx.stroke();

        for (let mm = 0; mm <= lengthMm; mm += 10) {
          const x = flipDir ? (lengthMm - mm) * pxPerMm : mm * pxPerMm;
          const isMajor = (mm % 50 === 0);
          const tickH = isMajor ? 16 : 9;
          ctx.beginPath();
          ctx.moveTo(x, 2);
          ctx.lineTo(x, 2 + tickH);
          ctx.stroke();
          if (isMajor && mm > 0 && mm < lengthMm) {
            ctx.fillText(String(mm), x, 22);
          }
        }
        ctx.fillStyle = '#0a84ff';
        ctx.textAlign = flipDir ? 'right' : 'left';
        ctx.fillText(axisLabel + ' (mm)', flipDir ? wPx - 6 : 6, 22);

        const tex = new THREE.CanvasTexture(cv);
        tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
        const mat = new THREE.MeshBasicMaterial({
          map: tex,
          transparent: true,
          side: THREE.DoubleSide,
          depthWrite: false,
        });
        const mesh = new THREE.Mesh(new THREE.PlaneGeometry(lengthMm, hMm), mat);
        mesh.rotation.x = -Math.PI / 2;
        return { mesh, hMm };
      }

      const bedX = Number(window.BED_X || 270);
      const bedY = Number(window.BED_Y || 270);
      const rX = makeRuler(bedX, 'X', false);
      rX.mesh.position.set(0, 0.08, bedY / 2 + rX.hMm / 2);
      scene.add(rX.mesh);

      const rY = makeRuler(bedY, 'Y', true);
      rY.mesh.rotation.z = -Math.PI / 2;
      rY.mesh.position.set(-bedX / 2 - rY.hMm / 2, 0.08, 0);
      scene.add(rY.mesh);

      function makeVerticalRuler(heightMm) {
        const pxPerMm = 4;
        const wMm = 18;
        const wPx = Math.round(wMm * pxPerMm);
        const hPx = Math.max(64, Math.round(heightMm * pxPerMm));
        const cv = document.createElement('canvas');
        cv.width = wPx;
        cv.height = hPx;
        const ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, wPx, hPx);

        ctx.strokeStyle = '#636366';
        ctx.fillStyle = '#aeaeb2';
        ctx.font = 'bold 20px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';

        ctx.beginPath();
        ctx.moveTo(wPx - 2, 0);
        ctx.lineTo(wPx - 2, hPx);
        ctx.stroke();

        for (let mm = 0; mm <= heightMm; mm += 10) {
          const y = hPx - mm * pxPerMm;
          const isMajor = (mm % 50 === 0);
          const tickW = isMajor ? 16 : 9;
          ctx.beginPath();
          ctx.moveTo(wPx - 2, y);
          ctx.lineTo(wPx - 2 - tickW, y);
          ctx.stroke();
          if (isMajor && mm > 0 && mm < heightMm) {
            ctx.fillText(String(mm), wPx - 22, y);
          }
        }
        ctx.fillStyle = '#0a84ff';
        ctx.textBaseline = 'top';
        ctx.fillText('Z (mm)', wPx - 6, 4);

        const tex = new THREE.CanvasTexture(cv);
        tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
        const mat = new THREE.MeshBasicMaterial({
          map: tex,
          transparent: true,
          side: THREE.DoubleSide,
          depthWrite: false,
        });
        const mesh = new THREE.Mesh(new THREE.PlaneGeometry(wMm, heightMm), mat);
        return { mesh, wMm };
      }

      const bedZ = Number(window.BED_Z || 260);
      const rZ = makeVerticalRuler(bedZ);
      rZ.mesh.position.set(-bedX / 2 - rZ.wMm / 2, bedZ / 2, -bedY / 2);
      scene.add(rZ.mesh);

      let objGroup = new THREE.Group();
      scene.add(objGroup);

      function fitObject(obj) {
        objGroup.scale.set(1, 1, 1);
        objGroup.clear();
        objGroup.add(obj);
        const box = new THREE.Box3().setFromObject(obj);
        if (box.isEmpty()) return null;
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        obj.position.x -= center.x;
        obj.position.z -= center.z;
        obj.position.y -= box.min.y;

        const maxDim = Math.max(size.x, size.y, size.z, 20);
        const dist = maxDim * 1.8;
        camera.position.set(dist, dist * 0.85, dist);
        controls.target.set(0, size.y / 2, 0);
        controls.update();
        return { x: size.x, y: size.z, z: size.y };
      }

      const ro = new ResizeObserver(() => {
        if (!container.clientWidth || !container.clientHeight) return;
        camera.aspect = container.clientWidth / container.clientHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(container.clientWidth, container.clientHeight);
      });
      ro.observe(container);

      function animate() {
        requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
      }
      animate();

      return { fitObject, clear: () => { objGroup.scale.set(1, 1, 1); objGroup.clear(); }, setScale: (sx, sy, sz) => objGroup.scale.set(sx, sz, sy) };
    }

    let modelViewer = null;
    let scadViewer = null;
    const overhangHint = document.getElementById('overhangHint');
    if (overhangHint && overhangHint.parentElement) overhangHint.parentElement.insertAdjacentElement('afterend', overhangHint);
    function checkOverhangs(geom) {
      if (!overhangHint || !geom || !geom.attributes || !geom.attributes.position) return;
      const pos = geom.attributes.position;
      const idx = geom.index;
      let minZ = Infinity;
      for (let i = 0; i < pos.count; i++) {
        const z = pos.getZ(i);
        if (z < minZ) minZ = z;
      }
      const triCount = idx ? idx.count / 3 : pos.count / 3;
      let overhangArea = 0;
      for (let t = 0; t < triCount; t++) {
        const i0 = idx ? idx.getX(t * 3) : t * 3;
        const i1 = idx ? idx.getX(t * 3 + 1) : t * 3 + 1;
        const i2 = idx ? idx.getX(t * 3 + 2) : t * 3 + 2;
        const ax = pos.getX(i0), ay = pos.getY(i0), az = pos.getZ(i0);
        const bx = pos.getX(i1), by = pos.getY(i1), bz = pos.getZ(i1);
        const cx = pos.getX(i2), cy = pos.getY(i2), cz = pos.getZ(i2);
        const midZ = (az + bz + cz) / 3;
        if (midZ <= minZ + 0.6) continue;
        const ux = bx - ax, uy = by - ay, uz = bz - az;
        const vx = cx - ax, vy = cy - ay, vz = cz - az;
        const nx = uy * vz - uz * vy;
        const ny = uz * vx - ux * vz;
        const nz = ux * vy - uy * vx;
        const len = Math.hypot(nx, ny, nz);
        if (len === 0) continue;
        if (nz / len < -0.76) overhangArea += 0.5 * len;
      }
      if (overhangArea >= 25) {
        overhangHint.textContent = `⚠ Wykryto duże nawisy (~${Math.round(overhangArea)} mm²) — rozważ włączenie podpór (Tree).`;
        overhangHint.style.display = 'block';
      } else {
        overhangHint.style.display = 'none';
      }
    }
    let gcodeViewer = null;
    const plateTabs = document.getElementById('plateTabs');
    let activePlateIndex = 1;
    let sentPlates = new Set();
    let plateCache = new Map();
    let platePromises = new Map();

    function renderPlateTabs() {
      const plates = (currentJob && currentJob.plates) || [];
      if (plates.length <= 1) {
        plateTabs.style.display = 'none';
      if (overhangHint) overhangHint.style.display = 'none';
        plateTabs.innerHTML = '';
        return;
      }
      plateTabs.style.display = 'flex';
      plateTabs.innerHTML = '';
      for (const pl of plates) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'plate-tab' + (pl.index === activePlateIndex ? ' active' : '') + (sentPlates.has(pl.index) ? ' sent' : '');
        const meta = [pl.est_time, pl.est_weight].filter(Boolean).join(' · ');
        btn.textContent = `Stół ${pl.index}` + (meta ? ` (${meta})` : '') + (sentPlates.has(pl.index) ? ' ✓' : '');
        btn.addEventListener('click', () => loadPlate(pl.index));
        plateTabs.appendChild(btn);
      }
    }

    const largeGcodeBar = document.getElementById('largeGcodeBar');
    async function loadPlate(plateIdx, forcePreview = false) {
      if (!currentJob || !currentJob.plates) return;
      const pl = currentJob.plates.find(x => x.index === plateIdx) || currentJob.plates[0];
      activePlateIndex = pl.index;
      currentJob.file = pl.file;
      currentJob.est_time = pl.est_time;
      currentJob.est_weight = pl.est_weight;
      const sp = [];
      if (pl.est_time) sp.push('Czas: ' + pl.est_time);
      if (pl.est_weight) sp.push('Waga: ' + pl.est_weight);
      document.getElementById('gcodeStats').textContent = sp.join(' | ');
      renderPlateTabs();
      const meta = [pl.est_time, pl.est_weight].filter(Boolean).join(' · ');
      const prefix = currentJob.plates.length > 1 ? `Stół ${pl.index}/${currentJob.plates.length}: ` : '';
      if (plateCache.has(pl.index)) {
        largeGcodeBar.style.display = 'none';
        const cachedObj = plateCache.get(pl.index);
        gcodeViewer.clear();
        gcodeViewer.fitObject(cachedObj);
        setupLayerSlider(cachedObj);
        renderPlateTabs();
        setStatus('ok', `${prefix}${pl.file}${meta ? ' (' + meta + ')' : ''}`);
        return;
      }
      const sizeMb = Number(pl.size_mb || 0);
      const inFlight = platePromises.has(pl.index);
      if (!inFlight && !forcePreview && sizeMb > 30) {
        if (gcodeViewer) gcodeViewer.clear();
        layerBar.style.display = 'none';
        largeGcodeBar.style.display = 'flex';
        if (sizeMb > 200) {
          largeGcodeBar.innerHTML = `<span>Plik G-code ma <strong>${sizeMb} MB</strong> (powyżej limitu podglądu 200 MB). Możesz go od razu wysłać na drukarkę.</span>`;
        } else {
          largeGcodeBar.innerHTML = `<span>Plik G-code jest duży (<strong>${sizeMb} MB</strong>). Wygenerować podgląd 3D?</span><button type="button" id="forcePreviewBtn" class="btn-secondary">Wygeneruj podgląd 3D</button>`;
          document.getElementById('forcePreviewBtn').addEventListener('click', () => loadPlate(pl.index, true));
        }
        setStatus('ok', `${prefix}${pl.file}${meta ? ' (' + meta + ')' : ''}`);
        return;
      }
      largeGcodeBar.style.display = 'none';
      if (gcodeViewer) gcodeViewer.clear();
      layerBar.style.display = 'none';
      setStatus('info', `Ładowanie podglądu G-code (${sizeMb || '?'} MB)...`);
      const jobRef = currentJob;
      let req = platePromises.get(pl.index);
      if (!req) {
        req = fetch(pl.gcode_url)
          .then(r => r.text())
          .then(gcodeText => {
            const gObj = new GCodeLoader().parse(gcodeText);
            gObj.rotation.x = -Math.PI / 2;
            if (currentJob === jobRef) plateCache.set(pl.index, gObj);
            return gObj;
          })
          .finally(() => {
            if (platePromises.get(pl.index) === req) platePromises.delete(pl.index);
          });
        platePromises.set(pl.index, req);
      }
      const gObj = await req;
      if (currentJob !== jobRef || activePlateIndex !== pl.index) return;
      gcodeViewer.clear();
      gcodeViewer.fitObject(gObj);
      setupLayerSlider(gObj);
      renderPlateTabs();
      setStatus('ok', `${prefix}${pl.file}${meta ? ' (' + meta + ')' : ''}`);
    }
    const layerBar = document.getElementById('layerBar');
    const layerSlider = document.getElementById('layerSlider');
    const layerLabel = document.getElementById('layerLabel');
    let gcodeLayers = [];

    function updateGcodeLayers(maxIdx) {
      for (let i = 0; i < gcodeLayers.length; i++) {
        const visible = i < maxIdx;
        for (const obj of gcodeLayers[i]) obj.visible = visible;
      }
      layerLabel.textContent = `${maxIdx} / ${gcodeLayers.length}`;
    }

    function setupLayerSlider(group) {
      const map = new Map();
      let fallback = 0;
      group.traverse((child) => {
        if (!child.isLine && !child.isLineSegments && !child.isMesh) return;
        const m = /^layer(\d+)$/i.exec(child.name || '');
        const key = m ? parseInt(m[1], 10) : fallback++;
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(child);
      });
      gcodeLayers = [...map.entries()].sort((a, b) => a[0] - b[0]).map(e => e[1]);
      if (gcodeLayers.length > 1) {
        layerSlider.min = '1';
        layerSlider.max = String(gcodeLayers.length);
        layerSlider.value = String(gcodeLayers.length);
        layerBar.style.display = 'flex';
        updateGcodeLayers(gcodeLayers.length);
      } else {
        layerBar.style.display = 'none';
      plateTabs.style.display = 'none';
      }
    }

    layerSlider.addEventListener('input', () => {
      updateGcodeLayers(parseInt(layerSlider.value, 10));
    });

    fileInput.addEventListener('change', async () => {
      initDims(null);
      if (modelViewer) modelViewer.clear();
      document.getElementById('modelViewerWrap').style.display = 'none';
      document.getElementById('modelName').textContent = '';
      currentJob = null;
      postSliceActions.style.display = 'none';
      if (gcodeViewer) gcodeViewer.clear();
      document.getElementById('gcodeViewerWrap').style.display = 'none';
      layerBar.style.display = 'none';
      const file = fileInput.files[0];
      if (!file) return;
      const ext = file.name.toLowerCase().split('.').pop();
      if (ext === 'scad') {
        setStatus('info', 'Wybrano plik .scad — kliknij Potnij, aby skompilować i pociąć.');
        return;
      }
      setStatus('info', `Ładowanie podglądu 3D: ${file.name}...`);
      await new Promise(r => setTimeout(r, 30));
      document.getElementById('modelName').textContent = file.name;
      const wrap = document.getElementById('modelViewerWrap');
      wrap.style.display = 'block';
      if (!modelViewer) modelViewer = createViewer(document.getElementById('modelCanvas'));

      const buf = await file.arrayBuffer();
      try {
        const defaultMat = new THREE.MeshStandardMaterial({ color: 0x0a84ff, roughness: 0.45, metalness: 0.1 });
        if (ext === 'stl') {
          const geom = new STLLoader().parse(buf);
          geom.computeVertexNormals();
              checkOverhangs(geom);
          const mesh = new THREE.Mesh(geom, defaultMat);
          mesh.rotation.x = -Math.PI / 2;
          initDims(modelViewer.fitObject(mesh));
          setStatus('ok', `Załadowano podgląd: ${file.name}`);
        } else if (ext === '3mf') {
          let rendered = false;
          try {
            const group = new ThreeMFLoader().parse(buf);
            group.traverse((child) => {
              if (child.isMesh) {
                if (child.geometry) child.geometry.computeVertexNormals();
                if (!child.material) child.material = defaultMat;
              }
            });
            group.rotation.x = -Math.PI / 2;
            if (!new THREE.Box3().setFromObject(group).isEmpty()) {
              initDims(modelViewer.fitObject(group));
              setStatus('ok', `Załadowano podgląd: ${file.name}`);
              rendered = true;
            }
          } catch (_) {}
          if (!rendered) {
            setStatus('info', `Konwertowanie .3mf na serwerze: ${file.name}...`);
            const fd = new FormData();
            fd.append('file', file);
            const res = await fetch('/3mf-preview', { method: 'POST', body: fd });
            if (!res.ok) {
              wrap.style.display = 'none';
              setStatus('info', 'Nie udało się wygenerować podglądu .3mf, ale możesz go normalnie pociąć.');
            } else {
              const stlBuf = await res.arrayBuffer();
              const geom = new STLLoader().parse(stlBuf);
              geom.computeVertexNormals();
              checkOverhangs(geom);
              const mesh = new THREE.Mesh(geom, defaultMat);
              mesh.rotation.x = -Math.PI / 2;
              initDims(modelViewer.fitObject(mesh));
              setStatus('ok', `Załadowano podgląd .3mf: ${file.name}`);
            }
          }
        }
      } catch (err) {
        setStatus('err', 'Nie udało się wyrenderować podglądu modelu: ' + err.message);
      }
    });

    previewScadBtn.addEventListener('click', async () => {
      const code = scadCode.value.trim();
      if (!code) { setStatus('err', 'Wklej najpierw kod OpenSCAD.'); return; }
      previewScadBtn.disabled = true;
      setStatus('info', 'Kompilowanie OpenSCAD do STL...');
      try {
        const fd = new FormData();
        fd.append('scad_code', code);
        const res = await fetch('/scad-preview', { method: 'POST', body: fd });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          setStatus('err', 'Błąd OpenSCAD:\n' + (data.stderr || res.statusText));
          return;
        }
        const buf = await res.arrayBuffer();
        document.getElementById('scadModelName').textContent = (scadName.value.trim() || 'openscad_model') + '.stl';
        document.getElementById('scadViewerWrap').style.display = 'block';
        if (!scadViewer) scadViewer = createViewer(document.getElementById('scadCanvas'));
        const geom = new STLLoader().parse(buf);
        geom.computeVertexNormals();
              checkOverhangs(geom);
        const mesh = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({ color: 0x0a84ff, roughness: 0.45, metalness: 0.1 }));
        mesh.rotation.x = -Math.PI / 2;
        initDims(scadViewer.fitObject(mesh));
        setStatus('ok', 'Wyrenderowano podgląd z OpenSCAD.');
      } catch (err) {
        setStatus('err', 'Błąd podglądu OpenSCAD: ' + err.message);
      } finally {
        previewScadBtn.disabled = false;
      }
    });


    (function initHelpTips() {
      const tips = {
        process: 'Profil druku (wysokość warstwy i prędkość). Wyższa warstwa (np. 0.28 mm) drukuje szybciej i taniej, niższa (np. 0.12 mm) daje gładsze detale.',
        filament: 'Profil materiału (temperatury dyszy/stołu, chłodzenie, przepływ). Dopasowuje się automatycznie do wybranego slotu ze Spoolmana.',
        slot: 'Slot w Qidi Box (T0–T3) z przypisaną szpulą ze Spoolmana. Domyślnie wybierany jest najtańszy filament w przeliczeniu na gram.',
        copies: 'Liczba sztuk tego samego modelu. Jeśli kopie nie zmieszczą się na jednym stole, slicer automatycznie rozbije je na kolejne stoły.',
        auto_orient: 'Automatycznie obraca model tak, aby miał największą płaską podstawę i wymagał jak najmniej podpór.',
        auto_arrange: 'Automatycznie układa obiekty na stole z bezpiecznym odstępem (włącza się zawsze przy więcej niż 1 kopii).',
        enable_support: 'Podpory drzewiaste (Tree) — tymczasowe gałęzie podtrzymujące nawisy w powietrzu. Łatwe do oderwania po wydruku.',
        brim_mode: 'Rondo (Brim) — cienki kołnierz wokół pierwszej warstwy poprawiający przyczepność wąskich lub wysokich modeli. Tryb Auto dodaje go tylko tam, gdzie model jest niestabilny.'
      };
      for (const [name, text] of Object.entries(tips)) {
        const field = document.querySelector(`[name="${name}"]`);
        if (!field) continue;
        let host = field.closest('label')
          || (field.id && document.querySelector(`label[for="${field.id}"]`))
          || (field.previousElementSibling && field.previousElementSibling.tagName === 'LABEL' ? field.previousElementSibling : null)
          || (field.parentElement && field.parentElement.querySelector('label'));
        if (!host) continue;
        const tip = document.createElement('span');
        tip.className = 'help-tip';
        tip.textContent = '?';
        tip.setAttribute('role', 'button');
        tip.setAttribute('tabindex', '0');
        const pop = document.createElement('span');
        pop.className = 'help-pop';
        pop.dataset.i18n = 'tip_' + name;
        pop.textContent = window.t ? window.t('tip_' + name) : text;
        tip.appendChild(pop);
        tip.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          const wasOpen = tip.classList.contains('open');
          document.querySelectorAll('.help-tip.open').forEach(el => el.classList.remove('open'));
          if (!wasOpen) tip.classList.add('open');
        });
        if (name === 'brim_mode') field.before(tip);
        else host.appendChild(tip);
      }
      document.addEventListener('click', () => {
        document.querySelectorAll('.help-tip.open').forEach(el => el.classList.remove('open'));
      });
    })();

    const brimEnableEl = document.getElementById('brimEnable');
    const brimModeEl = document.getElementById('brimMode');
    const brimModeOffEl = document.getElementById('brimModeOff');
    const brimWidthWrap = document.getElementById('brimWidthWrap');
    if (brimEnableEl && brimModeEl && brimModeOffEl && brimWidthWrap) {
      const syncBrim = () => {
        const on = brimEnableEl.checked;
        brimModeEl.style.display = on ? '' : 'none';
        brimModeEl.disabled = !on;
        brimModeOffEl.disabled = on;
        brimWidthWrap.style.display = (on && brimModeEl.value === 'on') ? 'inline-flex' : 'none';
      };
      brimEnableEl.addEventListener('change', syncBrim);
      brimModeEl.addEventListener('change', syncBrim);
      syncBrim();
    }

    const plateQueue = [];
    const addToPlateBtn = document.getElementById('addToPlateBtn');
    const plateQueueList = document.getElementById('plateQueueList');
    const queueCountEl = document.getElementById('queueCount');

    function renderPlateQueue() {
      if (!plateQueueList || !queueCountEl) return;
      queueCountEl.textContent = String(plateQueue.length);
      plateQueueList.innerHTML = '';
      plateQueue.forEach((item, idx) => {
        const row = document.createElement('div');
        row.className = 'queue-item';
        const sx = Math.round(item.scale_x * 100);
        const sy = Math.round(item.scale_y * 100);
        const sz = Math.round(item.scale_z * 100);
        const fallbackScale = (sx === 100 && sy === 100 && sz === 100) ? '' : (sx === sy && sy === sz ? ` · ${sx}%` : ` · ${sx}/${sy}/${sz}%`);
        const scaleTxt = item.dims ? ` · ${item.dims}` : fallbackScale;
        row.innerHTML =
          `<div class="queue-item-info"><strong>${item.name}</strong><span class="queue-item-meta">${item.type === 'scad' ? 'SCAD' : 'Plik'}${scaleTxt}</span></div>` +
          `<div class="queue-item-actions"><span style="font-size:12px;color:#aeaeb2;">×</span><input type="number" min="1" max="100" value="${item.copies}" data-qidx="${idx}"><button type="button" class="queue-del" data-qdel="${idx}" title="Usuń">✕</button></div>`;
        plateQueueList.appendChild(row);
      });
      plateQueueList.querySelectorAll('input[data-qidx]').forEach(inp => {
        inp.addEventListener('change', () => {
          const i = Number(inp.dataset.qidx);
          plateQueue[i].copies = Math.max(1, Math.min(100, parseInt(inp.value, 10) || 1));
          inp.value = String(plateQueue[i].copies);
        });
      });
      plateQueueList.querySelectorAll('button[data-qdel]').forEach(btn => {
        btn.addEventListener('click', () => {
          plateQueue.splice(Number(btn.dataset.qdel), 1);
          renderPlateQueue();
        });
      });
      sliceBtn.textContent = plateQueue.length > 0 ? (window.t ? window.t('btn_slice_plate', { n: plateQueue.length }) : `Potnij stół (${plateQueue.length} proj.)`) : (window.t ? window.t('btn_slice') : 'Potnij');
    }

    if (addToPlateBtn) {
      addToPlateBtn.addEventListener('click', () => {
        const copies = Math.max(1, Math.min(100, parseInt(document.getElementById('copiesInput').value, 10) || 1));
        const fmtDim = (id) => {
          const v = parseFloat(document.getElementById(id)?.value);
          return Number.isFinite(v) && v > 0 ? String(Math.round(v * 10) / 10) : null;
        };
        const dx = fmtDim('dimX'), dy = fmtDim('dimY'), dz = fmtDim('dimZ');
        const dims = (dx && dy && dz) ? `${dx}×${dy}×${dz} mm` : '';
        if (activeTab === 'file') {
          if (!fileInput.files.length) {
            setStatus('err', 'Najpierw wybierz plik (.stl/.3mf/.scad).');
            return;
          }
          const f = fileInput.files[0];
          plateQueue.push({ type: 'file', name: f.name, file: f, copies, dims, scale_x: scaleFactors.x, scale_y: scaleFactors.y, scale_z: scaleFactors.z });
        } else {
          if (!scadCode.value.trim() || !scadName.value.trim()) {
            setStatus('err', 'Wklej kod OpenSCAD i podaj nazwę projektu.');
            return;
          }
          plateQueue.push({ type: 'scad', name: scadName.value.trim(), scad_code: scadCode.value, copies, dims, scale_x: scaleFactors.x, scale_y: scaleFactors.y, scale_z: scaleFactors.z });
        }
        renderPlateQueue();
        setStatus('info', `Dodano do stołu (${plateQueue.length} projektów).`);
      });
    }

    sliceForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (plateQueue.length === 0) {
        if (activeTab === 'file' && !fileInput.files.length) {
          setStatus('err', 'Wybierz plik (.stl/.3mf/.scad).');
          return;
        }
        if (activeTab === 'scad' && (!scadCode.value.trim() || !scadName.value.trim())) {
          setStatus('err', 'Wklej kod OpenSCAD i podaj nazwę projektu.');
          return;
        }
      }
      currentJob = null;
      postSliceActions.style.display = 'none';
      document.getElementById('gcodeViewerWrap').style.display = 'none';

      sliceBtn.disabled = true;
      setStatus('info', 'Trwa cięcie w OrcaSlicerze...');
      try {
        const fd = new FormData(sliceForm);
        if (plateQueue.length > 0) {
          fd.delete('file');
          fd.delete('scad_code');
          fd.delete('scad_name');
          const meta = [];
          let fileIdx = 0;
          for (const item of plateQueue) {
            if (item.type === 'file') {
              fd.append('files', item.file, item.file.name);
              meta.push({ type: 'file', name: item.name, file_index: fileIdx++, copies: item.copies, scale_x: item.scale_x, scale_y: item.scale_y, scale_z: item.scale_z });
            } else {
              meta.push({ type: 'scad', name: item.name, scad_code: item.scad_code, copies: item.copies, scale_x: item.scale_x, scale_y: item.scale_y, scale_z: item.scale_z });
            }
          }
          fd.append('items_meta', JSON.stringify(meta));
        } else {
          if (activeTab === 'scad') fd.delete('file');
          else { fd.delete('scad_code'); fd.delete('scad_name'); }
          fd.append('scale_x', String(scaleFactors.x));
          fd.append('scale_y', String(scaleFactors.y));
          fd.append('scale_z', String(scaleFactors.z));
        }
        const res = await fetch('/slice', { method: 'POST', body: fd });
        const data = await res.json();
        if (!res.ok) {
          setStatus('err', 'Błąd cięcia:\n' + (data.stderr || data.status || res.statusText));
          return;
        }
        currentJob = data;
        setStatus('ok', `Pocięto: ${data.file} (Slot ${data.slot})`);

        const statsParts = [];
        if (data.est_time) statsParts.push('Czas: ' + data.est_time);
        if (data.est_weight) statsParts.push('Waga: ' + data.est_weight);
        document.getElementById('gcodeStats').textContent = statsParts.join(' | ');

        const gWrap = document.getElementById('gcodeViewerWrap');
        gWrap.style.display = 'block';
        if (!gcodeViewer) gcodeViewer = createViewer(document.getElementById('gcodeCanvas'));

        sentPlates = new Set();
        plateCache.clear();
        platePromises.clear();
        await loadPlate(1);

        postSliceActions.style.display = 'grid';
      } catch (err) {
        setStatus('err', 'Błąd połączenia: ' + err.message);
      } finally {
        sliceBtn.disabled = false;
      }
    });

    async function uploadJob(startPrint) {
      if (!currentJob) return;
      sendBtn.disabled = true;
      sendPrintBtn.disabled = true;
      setStatus('info', startPrint ? 'Wysyłanie i uruchamianie druku...' : 'Wysyłanie na drukarkę...');
      try {
        const fd = new FormData();
        fd.append('job_id', currentJob.job_id);
        fd.append('start_print', startPrint ? 'true' : 'false');
        fd.append('plate', String(activePlateIndex));
        fd.append('filename', currentJob.file || '');
        const res = await fetch('/upload', { method: 'POST', body: fd });
        const data = await res.json();
        if (res.ok) { sentPlates.add(activePlateIndex); renderPlateTabs(); }
        if (!res.ok) {
          setStatus('err', 'Błąd wysyłki: ' + (data.detail || data.status || res.statusText));
          return;
        }
        setStatus('ok', startPrint
          ? `Wysłano i uruchomiono druk: ${data.file}`
          : `Wysłano na drukarkę: ${data.file}`);
      } catch (err) {
        setStatus('err', 'Błąd wysyłki: ' + err.message);
      } finally {
        sendBtn.disabled = false;
        sendPrintBtn.disabled = false;
      }
    }

    sendBtn.addEventListener('click', () => uploadJob(false));

    sendPrintBtn.addEventListener('click', () => {
      if (!currentJob) return;
      const slotTxt = slotSelect.options[slotSelect.selectedIndex].textContent;
      const filTxt = filamentSelect.options[filamentSelect.selectedIndex].textContent;
      const procTxt = processSelect.options[processSelect.selectedIndex].textContent;
      modalSummary.innerHTML =
        `<b>Plik:</b> ${currentJob.file}<br>` +
        `<b>Slot:</b> ${slotTxt}<br>` +
        `<b>Filament:</b> ${filTxt}<br>` +
        `<b>Proces:</b> ${procTxt}` +
        (currentJob.est_time ? `<br><b>Czas:</b> ${currentJob.est_time}` : '');
      confirmModal.classList.add('open');
    });

    cancelPrintBtn.addEventListener('click', () => confirmModal.classList.remove('open'));
    confirmPrintBtn.addEventListener('click', () => {
      confirmModal.classList.remove('open');
      uploadJob(true);
    });
  </script>
  <div id="camBackdrop" class="cam-backdrop">
    <div class="cam-modal">
      <div class="cam-head">
        <h3 data-i18n="cam_title">Sprawdź stół przed drukiem</h3>
        <button type="button" id="camRefreshBtn" class="btn-secondary" style="width:auto;margin:0;padding:6px 12px;font-size:13px;" data-i18n="cam_refresh">Odśwież kadr</button>
      </div>
      <div class="cam-frame">
        <span id="camMsg" data-i18n="cam_loading">Ładowanie podglądu z kamery...</span>
        <img id="camImg" alt="Podgląd stołu">
      </div>
      <div class="cam-actions">
        <button type="button" id="camCancelBtn" class="btn-secondary" data-i18n="btn_cancel">Anuluj</button>
        <button type="button" id="camConfirmBtn" class="btn-primary" data-i18n="cam_confirm">Stół czysty — drukuj</button>
      </div>
    </div>
  </div>
  <script>
    (() => {
      const backdrop = document.getElementById("camBackdrop");
      const camImg = document.getElementById("camImg");
      const camMsg = document.getElementById("camMsg");
      const refreshBtn = document.getElementById("camRefreshBtn");
      const cancelBtn = document.getElementById("camCancelBtn");
      const confirmBtn = document.getElementById("camConfirmBtn");
      let pendingBtn = null;
      let bypass = false;

      function loadSnapshot() {
        camImg.style.display = "none";
        camMsg.style.display = "block";
        camMsg.textContent = window.t ? window.t("cam_loading") : "Ładowanie podglądu z kamery...";
        camImg.onload = () => { camMsg.style.display = "none"; camImg.style.display = "block"; };
        camImg.onerror = () => { camImg.style.display = "none"; camMsg.style.display = "block"; camMsg.textContent = window.t ? window.t("cam_no_image") : "Brak obrazu z kamery (możesz mimo to uruchomić druk)."; };
        camImg.src = "/camera-snapshot?t=" + Date.now();
      }

      document.addEventListener("click", (e) => {
        if (bypass) return;
        const btn = e.target.closest("#postSliceActions button");
        if (!btn) return;
        const txt = (btn.textContent || "").toLowerCase();
        if (!txt.includes("drukuj") && !btn.id.toLowerCase().includes("print")) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        pendingBtn = btn;
        backdrop.classList.add("open");
        loadSnapshot();
      }, true);

      refreshBtn.addEventListener("click", loadSnapshot);
      cancelBtn.addEventListener("click", () => { backdrop.classList.remove("open"); pendingBtn = null; });
      backdrop.addEventListener("click", (e) => { if (e.target === backdrop) { backdrop.classList.remove("open"); pendingBtn = null; } });
      confirmBtn.addEventListener("click", () => {
        backdrop.classList.remove("open");
        if (!pendingBtn) return;
        const target = pendingBtn;
        pendingBtn = null;
        bypass = true;
        target.click();
        bypass = false;
      });
    })();
  </script>
  <script>
    (() => {
      document.querySelectorAll("a").forEach((a) => {
        const txt = (a.textContent || "").toLowerCase();
        const href = (a.getAttribute("href") || "").trim();
        if (!window.HAS_FLUIDD_LINK && (txt.includes("fluidd") || (href === "" && txt.includes("printer")))) {
          a.style.display = "none";
        }
        if (!window.HAS_SPOOLMAN_LINK && txt.includes("spoolman")) {
          a.style.display = "none";
        }
      });
      if (!window.HAS_SPOOLMAN) {
        document.querySelectorAll("label").forEach((lbl) => {
          if ((lbl.textContent || "").includes("Slot / MMU (Spoolman)")) {
            lbl.style.display = "none";
            if (lbl.nextElementSibling) lbl.nextElementSibling.style.display = "none";
          }
        });
        const slotEl = document.getElementById("slotSelect") || document.getElementById("slots");
        if (slotEl) slotEl.style.display = "none";
      }
    })();
  
    let supportedLangs = [];
    let currentDict = {};

    function detectLang() {
      const saved = localStorage.getItem('orclipper_lang');
      if (saved && supportedLangs.includes(saved)) return saved;
      const langs = navigator.languages?.length ? navigator.languages : [navigator.language || 'en'];
      for (const l of langs) {
        const code = l.toLowerCase().slice(0, 2);
        if (supportedLangs.includes(code)) return code;
      }
      return supportedLangs.includes('en') ? 'en' : (supportedLangs[0] || 'en');
    }

    function t(key, vars) {
      let val = currentDict[key] ?? key;
      if (vars) for (const [k, v] of Object.entries(vars)) val = val.replaceAll('{' + k + '}', String(v));
      return val;
    }
    window.t = t;

    async function setLang(lang) {
      if (!supportedLangs.includes(lang)) {
        lang = supportedLangs.includes('en') ? 'en' : supportedLangs[0];
      }
      if (!lang) return;
      const res = await fetch(`/locales/${lang}.json`);
      if (!res.ok) return;
      currentDict = await res.json();
      localStorage.setItem('orclipper_lang', lang);
      document.documentElement.lang = lang;
      document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
      });
      document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = t(el.dataset.i18nPlaceholder);
      });
      const sw = document.getElementById('langSwitch');
      if (sw) sw.value = lang;
    }

    async function initI18n() {
      const res = await fetch('/locales');
      if (!res.ok) return;
      supportedLangs = await res.json();
      const sw = document.getElementById('langSwitch');
      const FLAGS = { pl: '🇵🇱', en: '🇬🇧', de: '🇩🇪', fr: '🇫🇷', es: '🇪🇸', cs: '🇨🇿', uk: '🇺🇦', it: '🇮🇹', nl: '🇳🇱', pt: '🇵🇹' };
      if (sw) {
        sw.innerHTML = '';
        for (const code of supportedLangs) {
          const opt = document.createElement('option');
          opt.value = code;
          opt.textContent = `${FLAGS[code] || '🌐'} ${code.toUpperCase()}`;
          sw.appendChild(opt);
        }
        sw.onchange = () => setLang(sw.value);
      }
      await setLang(detectLang());
    }

    initI18n();

  </script>
</body>
</html>
"""


def _available_locales() -> list[str]:
    locales_dir = Path(__file__).parent / "locales"
    if not locales_dir.is_dir():
        return []
    return sorted(f.stem for f in locales_dir.glob("*.json") if f.is_file())


@app.get("/locales")
def list_locales():
    return JSONResponse(_available_locales())


@app.get("/locales/{lang}.json")
def get_locale(lang: str):
    available = _available_locales()
    if not available:
        raise HTTPException(status_code=404, detail="No locales found")
    if lang not in available:
        lang = "en" if "en" in available else available[0]
    locale_file = Path(__file__).parent / "locales" / f"{lang}.json"
    return JSONResponse(json.loads(locale_file.read_text(encoding="utf-8")))

@app.get("/", response_class=HTMLResponse)
def index():
    return _inject_ui_config(_orig_index())


def _orig_index():
    proc_opts = render_options(list_profiles(PROCESS_DIR, "process"))
    fil_opts = render_options(list_profiles(FILAMENT_DIR, "filament"))
    slot_opts = render_slot_options(fetch_slots_info())
    page = (
        HTML_TEMPLATE
        .replace("__PROC_OPTS__", proc_opts)
        .replace("__FIL_OPTS__", fil_opts)
        .replace("__SLOT_OPTS__", slot_opts)
        .replace("__FLUIDD_URL__", html.escape(FLUIDD_PUBLIC_URL))
        .replace("__SPOOLMAN_URL__", html.escape(SPOOLMAN_PUBLIC_URL))
    )
    return page


def scale_stl_file(in_path: str, out_path: str, sx: float, sy: float, sz: float):
    raw = pathlib.Path(in_path).read_bytes()
    is_bin = False
    if len(raw) >= 84:
        tri_count = struct.unpack_from('<I', raw, 80)[0]
        if len(raw) == 84 + tri_count * 50:
            is_bin = True
            buf = bytearray(raw[:84])
            unpack_tri = struct.Struct('<12fH').unpack_from
            pack_tri = struct.Struct('<12fH').pack
            for i in range(tri_count):
                vals = unpack_tri(raw, 84 + i * 50)
                buf.extend(pack_tri(
                    0.0, 0.0, 0.0,
                    vals[3] * sx, vals[4] * sy, vals[5] * sz,
                    vals[6] * sx, vals[7] * sy, vals[8] * sz,
                    vals[9] * sx, vals[10] * sy, vals[11] * sz,
                    vals[12],
                ))
            pathlib.Path(out_path).write_bytes(bytes(buf))
    if not is_bin:
        text = raw.decode('utf-8', errors='ignore')
        def _repl(m):
            x, y, z = float(m.group(1)) * sx, float(m.group(2)) * sy, float(m.group(3)) * sz
            return f'vertex {x:.6g} {y:.6g} {z:.6g}'
        scaled = re.sub(r'vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)', _repl, text)
        pathlib.Path(out_path).write_text(scaled, encoding='utf-8')


def compile_scad_to_stl(scad_path: str, stl_path: str):
    return subprocess.run(
        ["openscad", "-o", stl_path, scad_path],
        capture_output=True,
        text=True,
        timeout=120,
    )


IDENT_MAT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

def _parse_mat(attr: str):
    if not attr:
        return IDENT_MAT
    vals = [float(x) for x in attr.split()]
    return tuple(vals) if len(vals) == 12 else IDENT_MAT

def _mul_mat(a, b):
    return (
        a[0]*b[0] + a[1]*b[3] + a[2]*b[6],
        a[0]*b[1] + a[1]*b[4] + a[2]*b[7],
        a[0]*b[2] + a[1]*b[5] + a[2]*b[8],
        a[3]*b[0] + a[4]*b[3] + a[5]*b[6],
        a[3]*b[1] + a[4]*b[4] + a[5]*b[7],
        a[3]*b[2] + a[4]*b[5] + a[5]*b[8],
        a[6]*b[0] + a[7]*b[3] + a[8]*b[6],
        a[6]*b[1] + a[7]*b[4] + a[8]*b[7],
        a[6]*b[2] + a[7]*b[5] + a[8]*b[8],
        a[9]*b[0] + a[10]*b[3] + a[11]*b[6] + b[9],
        a[9]*b[1] + a[10]*b[4] + a[11]*b[7] + b[10],
        a[9]*b[2] + a[10]*b[5] + a[11]*b[8] + b[11],
    )

def _apply_mat(v, m):
    x, y, z = v
    return (
        x*m[0] + y*m[3] + z*m[6] + m[9],
        x*m[1] + y*m[4] + z*m[7] + m[10],
        x*m[2] + y*m[5] + z*m[8] + m[11],
    )

def _tag(el):
    return el.tag.rsplit("}", 1)[-1]

def convert_3mf_to_stl_bytes(raw_bytes: bytes) -> bytes:
    models = {}
    build_items = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        model_names = [n for n in zf.namelist() if n.lower().endswith(".model")]
        for name in model_names:
            norm = "/" + name.lstrip("/")
            root = ET.fromstring(zf.read(name))
            objs = {}
            for el in root.iter():
                if _tag(el) == "object":
                    oid = el.attrib.get("id")
                    if not oid:
                        continue
                    verts, tris, comps = [], [], []
                    for child in el:
                        t = _tag(child)
                        if t == "mesh":
                            for sub in child:
                                st = _tag(sub)
                                if st == "vertices":
                                    verts = [(float(v.attrib["x"]), float(v.attrib["y"]), float(v.attrib["z"])) for v in sub if _tag(v) == "vertex"]
                                elif st == "triangles":
                                    tris = [(int(tr.attrib["v1"]), int(tr.attrib["v2"]), int(tr.attrib["v3"])) for tr in sub if _tag(tr) == "triangle"]
                        elif t == "components":
                            for c in child:
                                if _tag(c) == "component":
                                    cpath = None
                                    for k, v in c.attrib.items():
                                        if k.endswith("}path") or k == "path":
                                            cpath = "/" + v.lstrip("/")
                                    comps.append((cpath or norm, c.attrib.get("objectid"), _parse_mat(c.attrib.get("transform", ""))))
                    objs[oid] = (verts, tris, comps)
                elif _tag(el) == "build" and norm.lower().endswith("/3dmodel.model"):
                    for item in el:
                        if _tag(item) == "item":
                            ipath = None
                            for k, v in item.attrib.items():
                                if k.endswith("}path") or k == "path":
                                    ipath = "/" + v.lstrip("/")
                            build_items.append((ipath or norm, item.attrib.get("objectid"), _parse_mat(item.attrib.get("transform", ""))))
            models[norm] = objs

    out_tris = []
    def emit(mpath, oid, mat, depth=0):
        if depth > 16:
            return
        obj = models.get(mpath, {}).get(oid)
        if not obj:
            return
        verts, tris, comps = obj
        if verts and tris:
            tverts = [_apply_mat(v, mat) for v in verts]
            for i1, i2, i3 in tris:
                out_tris.append((tverts[i1], tverts[i2], tverts[i3]))
        for cpath, coid, cmat in comps:
            emit(cpath, coid, _mul_mat(cmat, mat), depth + 1)

    for mpath, oid, mat in build_items:
        emit(mpath, oid, mat)

    if not out_tris:
        for mpath, objs in models.items():
            for oid, (verts, tris, _) in objs.items():
                if verts and tris:
                    for i1, i2, i3 in tris:
                        out_tris.append((verts[i1], verts[i2], verts[i3]))

    buf = bytearray(struct.pack("<80sI", b"orclipper 3MF Preview", len(out_tris)))
    pack_tri = struct.Struct("<12fH").pack
    for v1, v2, v3 in out_tris:
        buf.extend(pack_tri(0.0, 0.0, 0.0, v1[0], v1[1], v1[2], v2[0], v2[1], v2[2], v3[0], v3[1], v3[2], 0))
    return bytes(buf)


@app.get("/camera-snapshot")
def camera_snapshot():
    from urllib.parse import urlparse
    u = urlparse(MOONRAKER_URL)
    base_host = f"{u.scheme}://{u.hostname}"
    candidates = []
    try:
        r = requests.get(f"{MOONRAKER_URL}/server/webcams/list", timeout=3)
        if r.ok:
            for cam in r.json().get("result", {}).get("webcams", []):
                snap = (cam.get("snapshot_url") or "").strip()
                if not snap:
                    continue
                if snap.startswith("http"):
                    candidates.append(snap)
                elif snap.startswith("/"):
                    candidates.extend([f"{base_host}{snap}", f"{base_host}:10088{snap}", f"{base_host}:8080{snap}"])
    except Exception:
        pass
    candidates.extend([
        f"{base_host}/webcam/?action=snapshot",
        f"{base_host}:10088/webcam/?action=snapshot",
        f"{base_host}:8080/?action=snapshot",
    ])
    for url in dict.fromkeys(candidates):
        try:
            img = requests.get(url, timeout=4)
            ctype = img.headers.get("Content-Type", "")
            if img.ok and ("image/" in ctype or img.content.startswith(b"\xff\xd8")):
                return Response(
                    content=img.content,
                    media_type=ctype or "image/jpeg",
                    headers={"Cache-Control": "no-store"},
                )
        except Exception:
            continue
    return JSONResponse({"status": "camera_unavailable"}, status_code=502)


@app.post("/3mf-preview")
async def threemf_preview(file: UploadFile = File(...)):
    try:
        stl_bytes = convert_3mf_to_stl_bytes(await file.read())
        if len(stl_bytes) <= 84:
            return JSONResponse({"status": "empty_3mf"}, status_code=400)
        return Response(content=stl_bytes, media_type="application/octet-stream")
    except Exception as e:
        return JSONResponse({"status": "3mf_error", "detail": str(e)}, status_code=400)


@app.post("/scad-preview")
async def scad_preview(scad_code: str = Form(...)):
    workdir = tempfile.mkdtemp()
    try:
        scad_path = os.path.join(workdir, "model.scad")
        stl_path = os.path.join(workdir, "model.stl")
        pathlib.Path(scad_path).write_text(scad_code, encoding="utf-8")
        res = compile_scad_to_stl(scad_path, stl_path)
        if res.returncode != 0 or not os.path.isfile(stl_path):
            return JSONResponse({"status": "scad_error", "stderr": (res.stderr or res.stdout)[-1500:]}, status_code=400)
        return Response(content=pathlib.Path(stl_path).read_bytes(), media_type="application/octet-stream")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/slice")
async def slice_model(
    file: UploadFile = File(None),
    scad_code: str = Form(""),
    scad_name: str = Form("openscad_model"),
    process: str = Form(...),
    filament: str = Form(...),
    slot: int = Form(0),
    auto_orient: bool = Form(False),
    auto_arrange: bool = Form(False),
    copies: int = Form(1),
    scale_x: float = Form(1.0),
    scale_y: float = Form(1.0),
    scale_z: float = Form(1.0),
    enable_support: bool = Form(False),
    brim_mode: str = Form("auto"),
    brim_width: float = Form(5.0),
    files: list[UploadFile] = File(default=[]),
    items_meta: str = Form(""),
):
    cleanup_old_jobs()
    proc_path = PROCESS_DIR / os.path.basename(process)
    fil_path = FILAMENT_DIR / os.path.basename(filament)
    if not proc_path.is_file() or not fil_path.is_file():
        return JSONResponse({"status": "invalid_profile"}, status_code=400)

    workdir = tempfile.mkdtemp()
    try:
        out_dir = os.path.join(workdir, "out")
        os.makedirs(out_dir, exist_ok=True)
        all_inputs = []
        input_groups = []

        def _maybe_scale(in_path, sx, sy, sz):
            sx = max(0.01, min(float(sx), 20.0))
            sy = max(0.01, min(float(sy), 20.0))
            sz = max(0.01, min(float(sz), 20.0))
            if abs(sx - 1.0) > 1e-4 or abs(sy - 1.0) > 1e-4 or abs(sz - 1.0) > 1e-4:
                if in_path.lower().endswith('.3mf'):
                    tmp_stl = os.path.join(workdir, pathlib.Path(in_path).stem + '_conv.stl')
                    pathlib.Path(tmp_stl).write_bytes(convert_3mf_to_stl_bytes(pathlib.Path(in_path).read_bytes()))
                    in_path = tmp_stl
                scaled_stl = os.path.join(workdir, pathlib.Path(in_path).stem + '_scaled.stl')
                scale_stl_file(in_path, scaled_stl, sx, sy, sz)
                return scaled_stl
            return in_path

        if items_meta.strip():
            meta_list = json.loads(items_meta)
            if not meta_list:
                return JSONResponse({"status": "missing_input"}, status_code=400)
            first_stem = "multi_plate"
            for idx, item in enumerate(meta_list):
                itype = item.get("type")
                icopies = max(1, min(int(item.get("copies", 1)), 100))
                if itype == "file":
                    fobj = files[int(item["file_index"])]
                    orig_name = os.path.basename(fobj.filename or f"model_{idx}.stl")
                    if orig_name.lower().endswith(".gcode.3mf"):
                        return JSONResponse({"status": "gcode_3mf_not_supported", "stderr": "Plik .gcode.3mf zawiera już gotowy G-code bez modelu 3D. Pobierz czysty .3mf lub .stl."}, status_code=400)
                    if idx == 0:
                        first_stem = pathlib.Path(orig_name).stem
                    in_path = os.path.join(workdir, f"p{idx}_{orig_name}")
                    with open(in_path, "wb") as f:
                        shutil.copyfileobj(fobj.file, f)
                    if orig_name.lower().endswith(".scad"):
                        stl_path = os.path.join(workdir, f"p{idx}_{pathlib.Path(orig_name).stem}.stl")
                        scad_res = compile_scad_to_stl(in_path, stl_path)
                        if scad_res.returncode != 0 or not os.path.isfile(stl_path):
                            return JSONResponse({"status": "scad_error", "stderr": (scad_res.stderr or scad_res.stdout)[-1500:]}, status_code=400)
                        in_path = stl_path
                elif itype == "scad":
                    base = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(item.get("name") or f"scad_{idx}")).strip("._") or f"scad_{idx}"
                    if idx == 0:
                        first_stem = base
                    scad_path = os.path.join(workdir, f"p{idx}_{base}.scad")
                    in_path = os.path.join(workdir, f"p{idx}_{base}.stl")
                    pathlib.Path(scad_path).write_text(str(item.get("scad_code", "")), encoding="utf-8")
                    scad_res = compile_scad_to_stl(scad_path, in_path)
                    if scad_res.returncode != 0 or not os.path.isfile(in_path):
                        return JSONResponse({"status": "scad_error", "stderr": (scad_res.stderr or scad_res.stdout)[-1500:]}, status_code=400)
                else:
                    continue
                in_path = _maybe_scale(in_path, item.get("scale_x", 1.0), item.get("scale_y", 1.0), item.get("scale_z", 1.0))
                all_inputs.extend([in_path] * icopies)
                input_groups.append([in_path] * icopies)
            if not all_inputs:
                return JSONResponse({"status": "missing_input"}, status_code=400)
            safe_name = f"{first_stem}_multi{len(meta_list)}.stl" if len(meta_list) > 1 else f"{first_stem}.stl"
            copies = len(all_inputs)
        else:
            has_file = file is not None and bool(file.filename)
            if has_file:
                safe_name = os.path.basename(file.filename)
                if safe_name.lower().endswith(".gcode.3mf"):
                    return JSONResponse({"status": "gcode_3mf_not_supported", "stderr": "Plik .gcode.3mf zawiera już gotowy G-code bez modelu 3D. Pobierz czysty .3mf lub .stl."}, status_code=400)
                input_path = os.path.join(workdir, safe_name)
                with open(input_path, "wb") as f:
                    shutil.copyfileobj(file.file, f)
                if safe_name.lower().endswith(".scad"):
                    stl_path = os.path.join(workdir, pathlib.Path(safe_name).stem + ".stl")
                    scad_res = compile_scad_to_stl(input_path, stl_path)
                    if scad_res.returncode != 0 or not os.path.isfile(stl_path):
                        return JSONResponse({"status": "scad_error", "stderr": (scad_res.stderr or scad_res.stdout)[-1500:]}, status_code=400)
                    input_path = stl_path
            elif scad_code.strip():
                base = re.sub(r"[^a-zA-Z0-9._-]+", "_", scad_name.strip() or "openscad_model").strip("._") or "openscad_model"
                safe_name = base + ".stl"
                scad_path = os.path.join(workdir, base + ".scad")
                input_path = os.path.join(workdir, safe_name)
                pathlib.Path(scad_path).write_text(scad_code, encoding="utf-8")
                scad_res = compile_scad_to_stl(scad_path, input_path)
                if scad_res.returncode != 0 or not os.path.isfile(input_path):
                    return JSONResponse({"status": "scad_error", "stderr": (scad_res.stderr or scad_res.stdout)[-1500:]}, status_code=400)
            else:
                return JSONResponse({"status": "missing_input"}, status_code=400)

            input_path = _maybe_scale(input_path, scale_x, scale_y, scale_z)
            copies = max(1, min(copies, 100))
            all_inputs = [input_path] * copies
            input_groups = [all_inputs]

        cmd = ["nice", "-n", "15", "xvfb-run", "-a", "/opt/orcaslicer/bin/orca-slicer"]
        if auto_orient:
            cmd += ["--orient", "1"]
        if auto_arrange or len(all_inputs) > 1:
            cmd += ["--arrange", "1"]
        proc_cfg = json.loads(pathlib.Path(proc_path).read_text(encoding="utf-8"))
        proc_cfg["exclude_object"] = "1"
        proc_cfg["enable_support"] = "1" if enable_support else "0"
        if enable_support:
            proc_cfg["support_type"] = "tree(auto)"
        if brim_mode == "on":
            proc_cfg["brim_type"] = "outer_only"
            proc_cfg["brim_width"] = str(max(1.0, min(float(brim_width), 30.0)))
        elif brim_mode == "off":
            proc_cfg["brim_type"] = "no_brim"
        else:
            proc_cfg["brim_type"] = "auto_brim"
        eff_proc_path = os.path.join(workdir, "process_override.json")
        pathlib.Path(eff_proc_path).write_text(json.dumps(proc_cfg), encoding="utf-8")
        base_cmd = cmd + [
            "--slice", "0",
            "--load-settings", f"{MACHINE_PROFILE};{eff_proc_path}",
            "--load-filaments", str(fil_path),
        ]

        def _run_orca(inp_list, target_dir):
            os.makedirs(target_dir, exist_ok=True)
            run_cmd = base_cmd + ["--outputdir", target_dir] + inp_list
            return subprocess.run(run_cmd, capture_output=True, text=True, cwd="/root/.config/OrcaSlicer"), run_cmd

        res, last_cmd = _run_orca(all_inputs, out_dir)
        gcode_paths = []
        if res.returncode == 0:
            gcode_paths = [
                pathlib.Path(out_dir) / f
                for f in sorted([f for f in os.listdir(out_dir) if f.endswith(".gcode")], key=_nat_key)
            ]
        elif len(all_inputs) > 1:
            print(f"[SLICE_FALLBACK] rc={res.returncode} — dzielenie na osobne stoły ({len(input_groups)} grup)", flush=True)
            batches = input_groups if len(input_groups) > 1 else [[x] for x in all_inputs]
            for b_idx, batch in enumerate(batches, 1):
                sub_dir = os.path.join(out_dir, f"batch_{b_idx}")
                sub_res, last_cmd = _run_orca(batch, sub_dir)
                if sub_res.returncode != 0 and len(batch) > 1:
                    for s_idx, single in enumerate(batch, 1):
                        s_dir = os.path.join(out_dir, f"batch_{b_idx}_{s_idx}")
                        s_res, last_cmd = _run_orca([single], s_dir)
                        if s_res.returncode != 0:
                            full_out = ((s_res.stderr or "") + "\n" + (s_res.stdout or "")).strip()
                            print(f"[SLICE_ERROR] rc={s_res.returncode} cmd={last_cmd}\n{full_out[-2000:]}", flush=True)
                            return JSONResponse({"status": "slice_error", "stderr": full_out[-1500:]}, status_code=500)
                        gcode_paths.extend([
                            pathlib.Path(s_dir) / f
                            for f in sorted([f for f in os.listdir(s_dir) if f.endswith(".gcode")], key=_nat_key)
                        ])
                elif sub_res.returncode != 0:
                    full_out = ((sub_res.stderr or "") + "\n" + (sub_res.stdout or "")).strip()
                    print(f"[SLICE_ERROR] rc={sub_res.returncode} cmd={last_cmd}\n{full_out[-2000:]}", flush=True)
                    return JSONResponse({"status": "slice_error", "stderr": full_out[-1500:]}, status_code=500)
                else:
                    gcode_paths.extend([
                        pathlib.Path(sub_dir) / f
                        for f in sorted([f for f in os.listdir(sub_dir) if f.endswith(".gcode")], key=_nat_key)
                    ])
        else:
            full_out = ((res.stderr or "") + "\n" + (res.stdout or "")).strip()
            print(f"[SLICE_ERROR] rc={res.returncode} cmd={last_cmd}\n{full_out[-2000:]}", flush=True)
            return JSONResponse({"status": "slice_error", "stderr": full_out[-1500:]}, status_code=500)

        if not gcode_paths:
            return JSONResponse({"status": "no_gcode_generated"}, status_code=500)

        job_id = uuid.uuid4().hex[:12]
        ts = time.strftime("%Y%m%d_%H%M%S")
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_x{copies}" if copies > 1 else ""
        plates = []
        gcodes = gcode_paths
        for idx, src_gcode in enumerate(gcode_paths, 1):
            plate_suffix = f"_plate{idx}" if len(gcodes) > 1 else ""
            upload_name = f"{pathlib.Path(safe_name).stem}{suffix}{plate_suffix}_{ts}_{job_id[:4]}.gcode"
            gcode = src_gcode.read_text(encoding="utf-8", errors="ignore")
            gcode = re.sub(r"EXTRUDER=\d+\b", f"EXTRUDER={slot}", gcode)
            gcode = re.sub(r"(?m)^T\d+(?=\s*(?:;.*)?$)", f"T{slot}", gcode)
            src_gcode.write_text(gcode, encoding="utf-8")
            saved_gcode = job_dir / upload_name
            shutil.move(str(src_gcode), str(saved_gcode))
            est_time, est_weight = parse_gcode_stats(saved_gcode)
            plates.append({
                "index": idx,
                "file": upload_name,
                "est_time": est_time,
                "est_weight": est_weight,
                "size_mb": round(saved_gcode.stat().st_size / (1024 * 1024), 1),
                "gcode_url": f"/gcode/{job_id}?plate={idx}",
            })

        return {
            "status": "ok",
            "job_id": job_id,
            "file": plates[0]["file"],
            "slot": slot + 1,
            "est_time": plates[0]["est_time"],
            "est_weight": plates[0]["est_weight"],
            "gcode_url": plates[0]["gcode_url"],
            "plates": plates,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.get("/gcode/{job_id}")
def get_gcode(job_id: str, plate: int = 1):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return JSONResponse({"status": "invalid_job"}, status_code=400)
    job_dir = JOBS_DIR / job_id
    files = sorted(job_dir.glob("*.gcode"), key=lambda x: _nat_key(x.name)) if job_dir.is_dir() else []
    if not files:
        return JSONResponse({"status": "not_found"}, status_code=404)
    idx = max(0, min(plate - 1, len(files) - 1))
    return FileResponse(files[idx], media_type="text/plain; charset=utf-8")


@app.post("/upload")
def upload_gcode(
    job_id: str = Form(...),
    start_print: bool = Form(False),
    plate: int = Form(1),
    filename: str = Form(""),
):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return JSONResponse({"status": "invalid_job"}, status_code=400)
    job_dir = JOBS_DIR / job_id
    files = sorted(job_dir.glob("*.gcode"), key=lambda x: _nat_key(x.name)) if job_dir.is_dir() else []
    if not files:
        return JSONResponse({"status": "job_expired"}, status_code=404)

    gcode_path = None
    if filename:
        cand = job_dir / os.path.basename(filename)
        if cand.is_file():
            gcode_path = cand
    if gcode_path is None:
        idx = max(0, min(plate - 1, len(files) - 1))
        gcode_path = files[idx]

    uploaded_name = gcode_path.name
    with open(gcode_path, "rb") as f:
        r = requests.post(
            f"{MOONRAKER_URL}/server/files/upload",
            files={"file": (uploaded_name, f)},
            data={"print": "true" if start_print else "false"},
            timeout=60,
        )
    r.raise_for_status()
    try:
        gcode_path.unlink(missing_ok=True)
        if not any(job_dir.glob("*.gcode")):
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        pass
    return {"status": "ok", "file": uploaded_name, "printing": start_print}
