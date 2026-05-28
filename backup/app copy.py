import io
import json
import os
import re
import secrets
import time
import csv
import random
from datetime import datetime
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Tuple

from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, PlainTextResponse, JSONResponse
from collections import deque
from itsdangerous import URLSafeSerializer

from openai import OpenAI
from google import genai

app = FastAPI(title="Prompt × CSV Runner")

# ----------------------------
# Minimal session + job store
# ----------------------------

SESSION_SECRET = "change-me-" + secrets.token_hex(16)  # set a constant in production
serializer = URLSafeSerializer(SESSION_SECRET, salt="csv-runner")

# In-memory store:
# sessions[session_id] = {
#   "csv_df": pd.DataFrame,
#   "csv_cols": [...],
#   "rows": [dict],
#   "results": [ {input..., llm_output, llm_json_valid, llm_json_parsed/flat...} ],
#   "detected_json_keys": [...],
# }
sessions: Dict[str, Dict[str, Any]] = {}

OLLAMA_API_BASE = "https://ai.aliawdeh.com/api"
# Cap concurrency to avoid overwhelming providers; frontend supplies default, backend enforces ceiling.
MAX_REQUEST_WORKERS_CAP = max(1, int(os.getenv("MAX_REQUEST_WORKERS_CAP", "64")))
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
os.makedirs(PROMPTS_DIR, exist_ok=True)

# Progress structure stored per session:
# sessions[sid]["progress"] = {
#   "status": "idle|running|done|error",
#   "done": int,
#   "sent": int,   # submitted to provider
#   "total": int,
#   "error": str,
# }

def get_or_create_session_id(request: Request) -> str:
    cookie = request.cookies.get("sid")
    if cookie:
        try:
            sid = serializer.loads(cookie)
            if sid in sessions:
                return sid
        except Exception:
            pass

    sid = secrets.token_urlsafe(16)
    sessions[sid] = {}
    return sid

def set_session_cookie(resp, sid: str):
    resp.set_cookie("sid", serializer.dumps(sid), httponly=True, samesite="lax")

def resolve_sid(request: Request, sid_token: Optional[str] = None) -> str:
    """Resolve session id from explicit token or cookie; create if missing."""
    if sid_token:
        try:
            sid = serializer.loads(sid_token)
            if sid in sessions:
                return sid
        except Exception:
            pass

    cookie = request.cookies.get("sid")
    if cookie:
        try:
            sid = serializer.loads(cookie)
            if sid in sessions:
                return sid
        except Exception:
            pass

    sid = get_or_create_session_id(request)
    return sid

# ----------------------------
# Helpers
# ----------------------------

def safe_format(template: str, row: Dict[str, Any]) -> str:
    class SafeDict(dict):
        def __missing__(self, key):
            return ""

    try:
        return template.format_map(SafeDict(row))
    except ValueError:
        # Fallback so literal braces (e.g., JSON examples) do not crash formatting
        pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
        return pattern.sub(lambda m: str(row.get(m.group(1), "")), template)

def try_parse_json(text: str) -> Tuple[Optional[Any], Optional[str]]:
    t = (text or "").strip()

    # Strip simple fenced blocks
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()

    # Try strict parse
    try:
        return json.loads(t), None
    except Exception as e:
        # Fallback: try extract from first { to last }
        try:
            i = t.find("{")
            j = t.rfind("}")
            if i != -1 and j != -1 and j > i:
                return json.loads(t[i : j + 1]), None
        except Exception:
            pass
        return None, str(e)

def flatten_json(obj: Any, parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            nk = f"{parent_key}{sep}{k}" if parent_key else str(k)
            out.update(flatten_json(v, nk, sep=sep))
    elif isinstance(obj, list):
        out[parent_key] = json.dumps(obj, ensure_ascii=False)
    else:
        out[parent_key] = obj
    return out

def _is_empty_value(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    try:
        return pd.isna(v)
    except Exception:
        return False

def _normalize_value(v: Any) -> Any:
    # Replace pandas NaN/None with empty string to avoid invalid JSON/CSV artifacts.
    return "" if _is_empty_value(v) else v

def _row_is_empty_dict(row: Dict[str, Any]) -> bool:
    return all(_is_empty_value(v) for v in row.values())

def _row_is_empty_series(row) -> bool:
    return all(_is_empty_value(v) for v in row)

def _normalize_json_root(obj: Any) -> Any:
    """If the JSON is a single-key dict with a dict value (e.g., {"summary": {...}}), unwrap it."""
    if isinstance(obj, dict) and len(obj) == 1:
        only_key, only_val = next(iter(obj.items()))
        if isinstance(only_val, dict):
            return only_val
    return obj

def _prompt_has_placeholder(prompt: str, names: List[str]) -> bool:
    for name in names:
        if f"{{{name}}}" in prompt or f"{{{{{name}}}}}" in prompt:
            return True
    return False

def _safe_prompt_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise ValueError("Invalid prompt name.")
    return cleaned

def _prompt_path(name: str) -> str:
    safe_name = _safe_prompt_name(name)
    if not safe_name.endswith(".txt"):
        safe_name += ".txt"
    return os.path.join(PROMPTS_DIR, safe_name)

def _list_prompts() -> List[str]:
    prompts = []
    try:
        for fname in os.listdir(PROMPTS_DIR):
            if fname.endswith(".txt"):
                prompts.append(fname[:-4])
    except FileNotFoundError:
        return []
    return sorted(prompts)

def _parse_date(value: str) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None

def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "t", "yes", "y", "1", "on"):
            return True
        if v in ("false", "f", "no", "n", "0", "off"):
            return False
    return None

# ----------------------------
# Provider calls
# ----------------------------

def call_openai(api_key: str, model: str, prompt: str) -> str:
    client = OpenAI(api_key=api_key)
    resp = client.responses.create(model=model, input=prompt)
    return resp.output_text  # SDK convenience for aggregated text :contentReference[oaicite:2]{index=2}

def call_gemini(api_key: str, model: str, prompt: str) -> str:
    # Gemini SDK can pick up env var, but here we pass api_key explicitly
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(model=model, contents=prompt)
    return resp.text or ""

def call_ollama(model: str, prompt: str, json_mode: bool) -> str:
    payload: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    # Ollama JSON mode uses "format": "json" (or a schema for structured outputs) :contentReference[oaicite:4]{index=4}
    if json_mode:
        payload["format"] = "json"

    r = requests.post(f"{OLLAMA_API_BASE}/generate", json=payload, timeout=600)
    r.raise_for_status()
    return r.json().get("response", "")

def fetch_ollama_models() -> List[str]:
    """Fetch available Ollama models from the API."""
    r = requests.get(f"{OLLAMA_API_BASE}/tags", timeout=10)
    r.raise_for_status()
    data = r.json() or {}

    models: List[str] = []
    for m in data.get("models", []):
        name = m.get("model") or m.get("name")
        if name:
            models.append(name)

    if not models:
        raise ValueError("No models returned from Ollama.")
    # Remove duplicates while preserving order
    seen = set()
    deduped = []
    for name in models:
        if name not in seen:
            deduped.append(name)
            seen.add(name)
    return deduped

def call_provider(provider: str, api_key: str, model: str, prompt: str, json_mode: bool) -> str:
    p = provider.lower().strip()
    if p == "openai":
        return call_openai(api_key, model, prompt)
    if p == "gemini":
        return call_gemini(api_key, model, prompt)
    if p == "ollama":
        # Ollama does not need an API key in this local setup
        return call_ollama(model, prompt, json_mode=json_mode)
    raise ValueError("Unsupported provider")

@app.get("/ollama/models")
def ollama_models():
    try:
        models = fetch_ollama_models()
        return {"models": models}
    except Exception as e:
        return PlainTextResponse(f"Failed to fetch Ollama models: {e}", status_code=502)

@app.get("/progress")
def progress_status(request: Request):
    sid_token = request.query_params.get("sid_token")
    sid = resolve_sid(request, sid_token)

    prog = sessions.get(sid, {}).get("progress") or {
        "status": "idle",
        "done": 0,
        "sent": 0,
        "total": 0,
        "error": "",
    }
    resp = JSONResponse(prog)
    set_session_cookie(resp, sid)
    return resp

@app.post("/session/save")
async def save_session_state(
    request: Request,
    sid_token: Optional[str] = Form(None),
    provider: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    api_key: Optional[str] = Form(None),
    prompt_template: Optional[str] = Form(None),
    json_mode: Optional[str] = Form(None),
    max_workers: Optional[str] = Form(None),
    prompt_name: Optional[str] = Form(None),
):
    sid = resolve_sid(request, sid_token)
    sess = sessions.get(sid, {})

    if provider is not None:
        sess["provider"] = provider
    if model is not None:
        sess["model"] = model
    if api_key is not None:
        sess["api_key"] = api_key
    if prompt_template is not None:
        sess["prompt_template"] = prompt_template
    if json_mode is not None:
        sess["json_mode"] = (json_mode == "1")
    if max_workers is not None:
        try:
            mw = int(max_workers)
            if mw >= 1:
                sess["max_workers"] = min(mw, MAX_REQUEST_WORKERS_CAP)
        except Exception:
            pass
    if prompt_name is not None:
        sess["prompt_name"] = prompt_name

    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, sid)
    return resp

@app.get("/prompts/get")
def get_prompt(request: Request, name: str, sid_token: Optional[str] = None):
    sid = resolve_sid(request, sid_token)
    sess = sessions.get(sid, {})
    try:
        path = _prompt_path(name)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return PlainTextResponse(f"Failed to load prompt: {e}", status_code=400)

    sess["prompt_name"] = _safe_prompt_name(name)
    sess["prompt_template"] = content
    resp = JSONResponse({"name": sess["prompt_name"], "content": content})
    set_session_cookie(resp, sid)
    return resp

@app.post("/prompts/save")
async def save_prompt(
    request: Request,
    prompt_name: str = Form(...),
    prompt_content: str = Form(...),
    sid_token: Optional[str] = Form(None),
):
    sid = resolve_sid(request, sid_token)
    sess = sessions.get(sid, {})
    try:
        safe_name = _safe_prompt_name(prompt_name)
        path = _prompt_path(safe_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(prompt_content or "")
    except Exception as e:
        return PlainTextResponse(f"Failed to save prompt: {e}", status_code=400)

    sess["prompt_name"] = safe_name
    sess["prompt_template"] = prompt_content or ""
    resp = JSONResponse({"ok": True, "name": safe_name})
    set_session_cookie(resp, sid)
    return resp

# ----------------------------
# HTML builders (simple)
# ----------------------------

def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def render_checkbox_list(name: str, options: List[str], selected: Optional[List[str]] = None) -> str:
    selected = set(selected or [])
    items = []
    for opt in options:
        checked = "checked" if opt in selected else ""
        items.append(
            f'<label style="display:block;margin:4px 0;">'
            f'<input type="checkbox" name="{name}" value="{html_escape(opt)}" {checked}/> '
            f'{html_escape(opt)}'
            f"</label>"
        )
    return "\n".join(items)

# ----------------------------
# Routes
# ----------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    sid = get_or_create_session_id(request)
    sess = sessions.get(sid, {})
    has_csv = "csv_cols" in sess

    toolbar_html = ""
    if has_csv:
        toolbar_html = "".join(
            [
                f'<button type="button" class="col-chip" data-insert="{{{{{html_escape(c)}}}}}">{html_escape(c)}</button>'
                for c in sess["csv_cols"]
            ]
        )
    toolbar_html += '<button type="button" class="col-chip" data-insert="{{row_json}}">row_json</button>'
    total_rows = len(sess.get("rows", [])) if has_csv else 0
    sid_token = serializer.dumps(sid)
    test_cols_selected = sess.get("test_cols", []) if has_csv else []
    test_cols_html = (
        render_checkbox_list("test_cols", sess["csv_cols"], selected=test_cols_selected)
        if has_csv
        else "<div class='small'>Upload a CSV first to pick test columns.</div>"
    )
    prompt_name = sess.get("prompt_name", "")
    prompt_options = ['<option value="">Select saved prompt</option>']
    for name in _list_prompts():
        sel = "selected" if name == prompt_name else ""
        prompt_options.append(f'<option value="{html_escape(name)}" {sel}>{html_escape(name)}</option>')
    prompt_options_html = "\n".join(prompt_options)
    saved_prompt = sess.get(
        "prompt_template",
        "Given this row:\n{{row_json}}\n\nReturn JSON with keys: status, note.",
    )

    display_index = idx + 1 if row is not None else 0
    total_all = len(results)
    key_options = ['<option value="">All attributes</option>'] + [
        f'<option value="{html_escape(k)}" {"selected" if k == filter_key else ""}>{html_escape(k)}</option>'
        for k in detected_keys
    ]
    val_options = [
        ('', 'Any value'),
        ('true', 'True'),
        ('false', 'False'),
    ]
    val_options_html = "\n".join(
        [
            f'<option value="{v}" {"selected" if v == filter_val else ""}>{label}</option>'
            for v, label in val_options
        ]
    )
    filter_qs = ""
    if filter_key and target_bool is not None:
        filter_qs = f"&key={quote(filter_key)}&val={quote(filter_val)}"

    page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Prompt × CSV Runner</title>
  <style>
    :root {{
      --card: #fff;
      --border: #e3e7ef;
      --accent: #0b74ff;
      --muted: #55616f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 32px;
      font-family: "Inter", "Segoe UI", Arial, sans-serif;
      background: radial-gradient(circle at 20% 20%, #eef3ff, #f9fbff 42%, #f6f7fa);
      color: #0f172a;
    }}
    h2 {{ margin: 0 0 18px; letter-spacing: -0.01em; }}
    h3 {{ margin: 0 0 10px; }}
    .row {{ display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px;
      flex: 1;
      min-width: 280px;
      box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
    }}
    label {{ display:block; margin-top:12px; font-weight:700; color:#111827; }}
    input[type="text"], input[type="password"], textarea, select {{
      width:100%;
      padding:10px 12px;
      margin-top:6px;
      border-radius:10px;
      border:1px solid var(--border);
      background:#fdfdff;
      font-size:14px;
    }}
    textarea {{ height:170px; }}
    .small {{ font-size:12px; color: var(--muted); margin-top:6px; }}
    .colsbox {{ max-height: 260px; overflow:auto; border:1px solid var(--border); padding:10px; border-radius:10px; background:#f7f9ff; }}
    button {{
      padding:10px 16px;
      border:none;
      background: var(--accent);
      color:white;
      border-radius:10px;
      cursor:pointer;
      font-weight:700;
    }}
    button[disabled] {{ opacity:0.6; cursor:not-allowed; }}
    .ok {{ color: #0a7; }}
    .warn {{ color: #b60; }}
    .overlay {{
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.5);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 999;
    }}
    .overlay .panel {{
      background: white;
      padding: 20px;
      border-radius: 12px;
      width: 320px;
      text-align: center;
      box-shadow: 0 20px 50px rgba(15, 23, 42, 0.3);
    }}
    .overlay h4 {{ margin: 0 0 10px; }}
    .progress-nums {{ font-size: 14px; color: #334155; margin-top: 8px; }}
    .error-msg {{ color: #b91c1c; margin-top: 10px; font-size: 13px; }}
    .modal-panel {{ width: 420px; max-height: 80vh; overflow: auto; }}
    .modal-actions {{ display:flex; justify-content:flex-end; gap:10px; margin-top:14px; }}
  </style>
</head>
<body>
  <h2>Prompt × CSV Runner (OpenAI / Gemini / Ollama)</h2>

  <div class="row">
    <div class="card">
      <h3>Step 1 — Upload CSV</h3>
      <form action="/upload" method="post" enctype="multipart/form-data">
        <label>CSV File</label>
        <input type="file" name="csv_file" accept=".csv" required />
        <div style="margin-top:12px;">
          <button type="submit">Upload</button>
        </div>
      </form>
      <div class="small">
        After upload, columns will show in Step 3 for export selection.
      </div>
      <div style="margin-top:10px;">
        {"<span class='ok'>CSV loaded.</span>" if has_csv else "<span class='warn'>No CSV loaded yet.</span>"}
      </div>
      <div style="margin-top:12px;">
        <button type="button" id="divide-button" {"disabled" if not has_csv else ""} style="background:#0f766e;">Divide</button>
        <div class="small">Randomly keep a subset of rows and drop the rest.</div>
      </div>
    </div>

    <div class="card">
      <h3>Step 2 — Configure & Run</h3>
      <form action="/run" method="post" id="run-form" data-total-rows="{total_rows}" data-sid-token="{sid_token}">
        <label>Provider</label>
        <select name="provider" id="provider-select">
          <option value="openai" {"selected" if sess.get("provider", "openai") == "openai" else ""}>OpenAI</option>
          <option value="gemini" {"selected" if sess.get("provider") == "gemini" else ""}>Gemini</option>
          <option value="ollama" {"selected" if sess.get("provider") == "ollama" else ""}>Ollama</option>
        </select>

        <div id="model-text-wrapper">
          <label>Model</label>
          <input type="text" name="model" id="model-input" value="{html_escape(sess.get('model', 'gpt-4o-mini'))}" />
          <div class="small">Examples: OpenAI gpt-4o-mini, Gemini gemini-2.5-flash.</div>
        </div>

        <div id="model-select-wrapper" style="display:none;">
          <label>Model</label>
          <select name="model" id="model-select">
            <option value="">Loading models...</option>
          </select>
          <div class="small" id="model-select-help">Pick an available Ollama model.</div>
        </div>

        <label>API Key (stored only in this browser session)</label>
        <input type="password" name="api_key" value="{html_escape(sess.get('api_key', ''))}" placeholder="Required for OpenAI/Gemini. Leave empty for Ollama." />

        <label>Max concurrent requests</label>
        <input type="number" name="max_workers" value="{html_escape(str(sess.get('max_workers', 16)))}" min="1" max="{MAX_REQUEST_WORKERS_CAP}" />
        <div class="small">Controls how many requests run in parallel. Capped by server policy @64.</div>

        <label>Saved prompts</label>
        <div style="display:flex; gap:8px; align-items:center;">
          <select id="prompt-select" style="flex:1;">
            {prompt_options_html}
          </select>
          <button type="button" id="prompt-load" style="background:#0f766e;">Load</button>
        </div>
        <label>Prompt name</label>
        <div style="display:flex; gap:8px; align-items:center;">
          <input type="text" id="prompt-name" value="{html_escape(prompt_name)}" placeholder="My prompt name" />
          <button type="button" id="prompt-save" style="background:#0f766e;">Save prompt</button>
        </div>
        <div class="small" id="prompt-status"></div>

        <label>Prompt template</label>
        <div id="prompt-toolbar" style="margin:6px 0; display:flex; gap:8px; flex-wrap:wrap;">
          {toolbar_html}
        </div>
        <textarea name="prompt_template" id="prompt-template">{html_escape(saved_prompt)}</textarea>
        <div class="small">Use placeholders like {{name}}, {{age}}, and/or {{row_json}}. Click a column chip to insert.</div>

        <label><input type="checkbox" name="json_mode" value="1" {"checked" if sess.get("json_mode", True) else ""} /> Output is JSON</label>

        <div style="margin-top:14px;">
          <button type="submit" {"disabled" if not has_csv else ""}>Run</button>
          <button type="button" id="test-button" {"disabled" if not has_csv else ""} style="margin-left:8px;background:#10b981;">Test (random row)</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h3>Step 3 — Choose Output Columns & Download</h3>
      <div class="small">
        After you run, this page will show discovered JSON keys so you can click-select which ones to flatten into the output CSV,
        along with any input columns you want to keep.
      </div>
      <div style="margin-top:12px;">
        <a href="/export"><button>{"Open export options" if has_csv else "Export (upload first)"}</button></a>
      </div>
    </div>
  </div>
  <div class="overlay" id="progress-overlay">
    <div class="panel">
      <h4>Processing rows…</h4>
      <div class="progress-nums">
        Processed: <span id="progress-done">0</span> /
        <span id="progress-total">0</span><br/>
        In progress: <span id="progress-running">0</span><br/>
        Pending: <span id="progress-pending">0</span>
      </div>
      <div class="error-msg" id="progress-error"></div>
    </div>
  </div>
  <div class="overlay" id="test-overlay">
    <div class="panel modal-panel">
      <h4>Select columns to show</h4>
      <div style="margin:6px 0; display:flex; gap:8px; flex-wrap:wrap;">
        <button type="button" class="toggle-btn" data-target="test_cols" data-action="all">Select all</button>
        <button type="button" class="toggle-btn" data-target="test_cols" data-action="none">Select none</button>
      </div>
      <div class="colsbox" style="margin-top:8px; max-height:320px; overflow:auto;">
        {test_cols_html}
      </div>
      <div class="small">Pick which input columns to display on the Test page.</div>
      <div class="modal-actions">
        <button type="button" id="test-cancel" style="background:#94a3b8;">Cancel</button>
        <button type="button" id="test-confirm" style="background:#10b981;">Run Test</button>
      </div>
    </div>
  </div>
  <div class="overlay" id="divide-overlay">
    <div class="panel modal-panel">
      <h4>Divide rows</h4>
      <form action="/divide" method="post" id="divide-form">
        <input type="hidden" name="sid_token" value="{html_escape(sid_token)}" />
        <label>Rows to keep (out of {total_rows})</label>
        <input type="number" name="divide_rows" min="1" max="{total_rows}" value="{min(total_rows, 100) if total_rows else 1}" />
        <div class="small">This will randomly keep the selected number of rows and discard the rest.</div>
        <div class="modal-actions">
          <button type="button" id="divide-cancel" style="background:#94a3b8;">Cancel</button>
          <button type="submit" id="divide-confirm" style="background:#0f766e;">Apply</button>
        </div>
      </form>
    </div>
  </div>
  <script>
    (() => {{
      const providerSel = document.getElementById("provider-select");
      const modelTextWrap = document.getElementById("model-text-wrapper");
      const modelSelectWrap = document.getElementById("model-select-wrapper");
      const modelInput = document.getElementById("model-input");
      const modelSelect = document.getElementById("model-select");
      const modelSelectHelp = document.getElementById("model-select-help");
      const promptBox = document.getElementById("prompt-template");
      const promptSelect = document.getElementById("prompt-select");
      const promptName = document.getElementById("prompt-name");
      const promptLoad = document.getElementById("prompt-load");
      const promptSave = document.getElementById("prompt-save");
      const promptStatus = document.getElementById("prompt-status");
      const chips = Array.from(document.querySelectorAll(".col-chip"));
      const runForm = document.getElementById("run-form");
      const apiKeyInput = runForm?.querySelector('input[name="api_key"]');
      const maxWorkersInput = runForm?.querySelector('input[name="max_workers"]');
      const jsonModeInput = runForm?.querySelector('input[name="json_mode"]');
      const testBtn = document.getElementById("test-button");
      const overlay = document.getElementById("progress-overlay");
      const progDone = document.getElementById("progress-done");
      const progTotal = document.getElementById("progress-total");
      const progRunning = document.getElementById("progress-running");
      const progError = document.getElementById("progress-error");
      const totalRows = Number(runForm?.dataset?.totalRows || 0);
      const progPending = document.getElementById("progress-pending");
      const sidToken = runForm?.dataset?.sidToken || "";
      const testOverlay = document.getElementById("test-overlay");
      const testConfirm = document.getElementById("test-confirm");
      const testCancel = document.getElementById("test-cancel");
      const divideBtn = document.getElementById("divide-button");
      const divideOverlay = document.getElementById("divide-overlay");
      const divideCancel = document.getElementById("divide-cancel");
      const fallbackOllamaModels = ["llama3", "llama3:70b", "gemma3", "mistral-small"];
      let ollamaModelsLoaded = false;
      let ollamaModelsLoading = false;
      let pollTimer = null;

      function syncSelectToInput() {{
        if (modelSelect && modelInput) {{
          modelInput.value = modelSelect.value;
        }}
      }}

      function setModelOptions(models, noteText) {{
        if (!modelSelect) return;
        modelSelect.innerHTML = "";
        models.forEach((m) => {{
          const opt = document.createElement("option");
          opt.value = m;
          opt.textContent = m;
          modelSelect.appendChild(opt);
        }});
        if (modelSelectHelp) {{
          modelSelectHelp.textContent = noteText || "Pick an available Ollama model.";
        }}
        if (models.length) {{
          modelSelect.value = modelInput.value || models[0];
          syncSelectToInput();
        }}
      }}

      async function loadOllamaModels() {{
        if (ollamaModelsLoading || ollamaModelsLoaded) return;
        ollamaModelsLoading = true;
        if (modelSelectHelp) modelSelectHelp.textContent = "Loading models from Ollama...";
        if (modelSelect) {{
          modelSelect.innerHTML = '<option value="">Loading...</option>';
        }}
        try {{
          const resp = await fetch("/ollama/models");
          if (!resp.ok) {{
            throw new Error("Failed to load models");
          }}
          const data = await resp.json();
          const models = Array.isArray(data?.models) ? data.models : [];
          if (!models.length) {{
            throw new Error("No models returned");
          }}
          ollamaModelsLoaded = true;
          setModelOptions(models, "Pick an available Ollama model.");
        }} catch (err) {{
          setModelOptions(fallbackOllamaModels, "Using fallback model list.");
        }} finally {{
          ollamaModelsLoading = false;
        }}
      }}

      function syncModelForProvider() {{
        const isOllama = (providerSel.value || "").toLowerCase() === "ollama";
        if (isOllama) {{
          modelTextWrap.style.display = "none";
          modelSelectWrap.style.display = "block";
          modelInput.disabled = true;
          modelSelect.disabled = false;
          loadOllamaModels();
        }} else {{
          modelTextWrap.style.display = "block";
          modelSelectWrap.style.display = "none";
          modelInput.disabled = false;
          modelSelect.disabled = true;
          if (!modelInput.value) {{
            modelInput.value = "gpt-4o-mini";
          }}
        }}
      }}

      providerSel.addEventListener("change", () => {{
        syncModelForProvider();
        syncSelectToInput();
        queueSaveState();
      }});
      modelSelect.addEventListener("change", () => {{
        syncSelectToInput();
        queueSaveState();
      }});
      syncModelForProvider();

      function insertAtCursor(textarea, text) {{
        if (!textarea) return;
        const start = textarea.selectionStart ?? textarea.value.length;
        const end = textarea.selectionEnd ?? textarea.value.length;
        const before = textarea.value.slice(0, start);
        const after = textarea.value.slice(end);
        const newPos = start + text.length;
        textarea.value = before + text + after;
        textarea.focus();
        textarea.setSelectionRange(newPos, newPos);
      }}

      chips.forEach((btn) => {{
        btn.addEventListener("click", () => {{
          const text = btn.getAttribute("data-insert") || "";
          insertAtCursor(promptBox, text);
          queueSaveState();
        }});
      }});

      if (modelInput) modelInput.addEventListener("input", () => queueSaveState());
      if (apiKeyInput) apiKeyInput.addEventListener("input", () => queueSaveState());
      if (maxWorkersInput) maxWorkersInput.addEventListener("input", () => queueSaveState());
      if (promptBox) promptBox.addEventListener("input", () => queueSaveState());
      if (jsonModeInput) jsonModeInput.addEventListener("change", () => queueSaveState());
      if (promptName) promptName.addEventListener("input", () => queueSaveState());
      if (promptSelect) promptSelect.addEventListener("change", () => queueSaveState());

      function setModalVisible(el, visible) {{
        if (!el) return;
        el.style.display = visible ? "flex" : "none";
      }}

      let saveTimer = null;
      function buildStateForm() {{
        if (!runForm) return null;
        const form = new FormData();
        if (sidToken) form.append("sid_token", sidToken);
        if (providerSel) form.append("provider", providerSel.value);
        if (modelInput) form.append("model", modelInput.value);
        if (apiKeyInput) form.append("api_key", apiKeyInput.value);
        if (promptBox) form.append("prompt_template", promptBox.value);
        if (jsonModeInput) form.append("json_mode", jsonModeInput.checked ? "1" : "0");
        if (maxWorkersInput) form.append("max_workers", maxWorkersInput.value);
        if (promptName) form.append("prompt_name", promptName.value);
        return form;
      }}
      function sendState(immediate = false) {{
        const form = buildStateForm();
        if (!form) return;
        if (immediate && navigator.sendBeacon) {{
          const params = new URLSearchParams();
          for (const [k, v] of form.entries()) params.append(k, v);
          navigator.sendBeacon("/session/save", params);
          return;
        }}
        fetch("/session/save", {{
          method: "POST",
          body: form,
          credentials: "same-origin",
          keepalive: immediate,
        }});
      }}
      function queueSaveState(immediate = false) {{
        if (saveTimer) clearTimeout(saveTimer);
        if (immediate) {{
          sendState(true);
          return;
        }}
        saveTimer = setTimeout(() => sendState(false), 400);
      }}

      function setPromptStatus(msg, isError = false) {{
        if (!promptStatus) return;
        promptStatus.textContent = msg;
        promptStatus.style.color = isError ? "#b91c1c" : "#0a7";
      }}

      if (promptLoad) {{
        promptLoad.addEventListener("click", async () => {{
          const name = promptSelect?.value || "";
          if (!name) {{
            setPromptStatus("Select a saved prompt to load.", true);
            return;
          }}
          try {{
            const qs = sidToken ? `&sid_token=${{encodeURIComponent(sidToken)}}` : "";
            const resp = await fetch(`/prompts/get?name=${{encodeURIComponent(name)}}${{qs}}`, {{ credentials: "same-origin" }});
            if (!resp.ok) {{
              throw new Error("Failed to load prompt");
            }}
            const data = await resp.json();
            if (promptBox) promptBox.value = data.content || "";
            if (promptName) promptName.value = data.name || name;
            setPromptStatus(`Loaded "${{data.name || name}}"`);
            queueSaveState();
          }} catch (e) {{
            setPromptStatus("Failed to load prompt.", true);
          }}
        }});
      }}

      if (promptSave) {{
        promptSave.addEventListener("click", async () => {{
          const name = (promptName?.value || "").trim();
          if (!name) {{
            setPromptStatus("Enter a prompt name to save.", true);
            return;
          }}
          const content = promptBox?.value || "";
          const form = new FormData();
          form.append("prompt_name", name);
          form.append("prompt_content", content);
          if (sidToken) form.append("sid_token", sidToken);
          try {{
            const resp = await fetch("/prompts/save", {{
              method: "POST",
              body: form,
              credentials: "same-origin",
            }});
            if (!resp.ok) {{
              throw new Error("Failed to save prompt");
            }}
            const data = await resp.json();
            if (promptSelect) {{
              let opt = Array.from(promptSelect.options).find((o) => o.value === data.name);
              if (!opt) {{
                opt = document.createElement("option");
                opt.value = data.name;
                opt.textContent = data.name;
                promptSelect.appendChild(opt);
              }}
              promptSelect.value = data.name;
            }}
            if (promptName) promptName.value = data.name;
            setPromptStatus(`Saved "${{data.name}}"`);
            queueSaveState();
          }} catch (e) {{
            setPromptStatus("Failed to save prompt.", true);
          }}
        }});
      }}

      window.addEventListener("visibilitychange", () => {{
        if (document.visibilityState === "hidden") {{
          queueSaveState(true);
        }}
      }});

      if (testBtn && runForm) {{
        testBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          setModalVisible(testOverlay, true);
        }});
      }}

      function submitTest() {{
        if (!runForm) return;
        // Sync selected test columns into hidden inputs on the form
        runForm.querySelectorAll('input[data-test-copy="1"]').forEach((el) => el.remove());
        const checkedCols = Array.from(document.querySelectorAll('#test-overlay input[name="test_cols"]:checked'));
        checkedCols.forEach((chk) => {{
          const hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = "test_cols";
          hidden.value = chk.value;
          hidden.setAttribute("data-test-copy", "1");
          runForm.appendChild(hidden);
        }});

        runForm.dataset.mode = "test";
        const originalAction = runForm.action;
        runForm.action = "/test";
        runForm.submit();
        setModalVisible(testOverlay, false);
        setTimeout(() => {{
          runForm.action = originalAction;
          runForm.dataset.mode = "";
        }}, 0);
      }}

      if (testConfirm) {{
        testConfirm.addEventListener("click", submitTest);
      }}
      if (testCancel) {{
        testCancel.addEventListener("click", () => setModalVisible(testOverlay, false));
      }}
      if (divideBtn) {{
        divideBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          setModalVisible(divideOverlay, true);
        }});
      }}
      if (divideCancel) {{
        divideCancel.addEventListener("click", () => setModalVisible(divideOverlay, false));
      }}

      async function pollProgress() {{
        try {{
          const qs = sidToken ? `?sid_token=${{encodeURIComponent(sidToken)}}` : "";
          const resp = await fetch(`/progress${{qs}}`, {{ credentials: "same-origin" }});
          const data = await resp.json();
          const done = data?.done ?? 0;
          const total = data?.total ?? 0;
          const sent = data?.sent ?? done;
          const status = data?.status ?? "idle";
          const err = data?.error ?? "";
          if (progDone) progDone.textContent = done;
          if (progTotal) progTotal.textContent = total;
          const running = Math.max(sent - done, 0);
          const pending = Math.max(total - sent, 0);
          if (progRunning) progRunning.textContent = running;
          if (progPending) progPending.textContent = pending;
          if (progError) progError.textContent = status === "error" ? err : "";
          return status;
        }} catch (e) {{
          if (progError) progError.textContent = "Failed to poll progress.";
          return "error";
        }}
      }}

      if (runForm) {{
        runForm.addEventListener("submit", async (e) => {{
          if (runForm.dataset.mode === "test") {{
            runForm.dataset.mode = "";
            return; // allow default submit for test
          }}
          e.preventDefault();
          if (overlay) overlay.style.display = "flex";
          if (progDone) progDone.textContent = "0";
          if (progTotal) progTotal.textContent = String(totalRows || 0);
          const maxWorkersInput = runForm.querySelector('input[name="max_workers"]');
          const reqWorkers = Number(maxWorkersInput?.value || totalRows || 0);
          const initialRunning = Math.max(Math.min(reqWorkers, totalRows || 0), 0);
          const initialPending = Math.max((totalRows || 0) - initialRunning, 0);
          if (progRunning) progRunning.textContent = String(initialRunning);
          if (progPending) progPending.textContent = String(initialPending);
          if (progError) progError.textContent = "";
          await pollProgress(); // initialize

          const formData = new FormData(runForm);
          const runPromise = fetch("/run", {{ method: "POST", body: formData, credentials: "same-origin" }});

          pollTimer = setInterval(pollProgress, 800);
          const resp = await runPromise.catch((err) => err);
          clearInterval(pollTimer);
          await pollProgress();

          if (!resp || !resp.ok) {{
            if (progError) progError.textContent = "Run failed. Please check inputs and try again.";
            return;
          }}
          window.location.href = "/export";
        }});
      }}
    }})();
  </script>
</body>
</html>
"""
    resp = HTMLResponse(page)
    set_session_cookie(resp, sid)
    return resp

@app.post("/upload")
async def upload(request: Request, csv_file: UploadFile = File(...)):
    sid = get_or_create_session_id(request)
    raw = await csv_file.read()
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        return PlainTextResponse(f"Failed to parse CSV: {e}", status_code=400)

    # Drop rows that are entirely empty/whitespace and normalize NaN -> "".
    df = df.loc[~df.apply(_row_is_empty_series, axis=1)].reset_index(drop=True)
    df = df.applymap(_normalize_value)
    if df.empty:
        return PlainTextResponse("Uploaded CSV has no non-empty rows.", status_code=400)

    sessions[sid]["csv_df"] = df
    sessions[sid]["csv_cols"] = list(df.columns)
    sessions[sid]["rows"] = df.to_dict(orient="records")
    sessions[sid].pop("results", None)
    sessions[sid].pop("detected_json_keys", None)
    sessions[sid].pop("progress", None)

    resp = RedirectResponse(url="/", status_code=303)
    set_session_cookie(resp, sid)
    return resp

@app.post("/divide")
async def divide_rows(
    request: Request,
    sid_token: Optional[str] = Form(None),
    divide_rows: int = Form(...),
):
    sid = resolve_sid(request, sid_token)
    sess = sessions.get(sid, {})
    df = sess.get("csv_df")
    if df is None or df.empty:
        return PlainTextResponse("Upload a CSV first.", status_code=400)

    total = len(df)
    if divide_rows < 1 or divide_rows > total:
        return PlainTextResponse(f"divide_rows must be between 1 and {total}.", status_code=400)

    if divide_rows < total:
        df = df.sample(n=divide_rows, replace=False).reset_index(drop=True)
        sess["csv_df"] = df
        sess["rows"] = df.to_dict(orient="records")
    # Always keep columns
    sess["csv_cols"] = list(df.columns)
    sess.pop("results", None)
    sess.pop("detected_json_keys", None)
    sess.pop("progress", None)

    resp = RedirectResponse(url="/", status_code=303)
    set_session_cookie(resp, sid)
    return resp

@app.post("/run")
async def run(
    request: Request,
    sid_token: Optional[str] = Form(None),
    provider: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    prompt_template: str = Form(...),
    json_mode: Optional[str] = Form(None),
    max_workers: str = Form("16"),
):
    sid = resolve_sid(request, sid_token)
    sess = sessions.get(sid, {})
    if "rows" not in sess:
        return PlainTextResponse("Upload a CSV first.", status_code=400)

    rows = [r for r in sess["rows"] if not _row_is_empty_dict(r)]
    rows = [{k: _normalize_value(v) for k, v in row.items()} for row in rows]
    if not rows:
        return PlainTextResponse("No usable rows in CSV after dropping empty rows.", status_code=400)
    csv_cols = sess["csv_cols"]
    if not _prompt_has_placeholder(prompt_template, ["row_json"] + csv_cols):
        return PlainTextResponse("Prompt must include at least one column placeholder or {{row_json}}.", status_code=400)

    try:
        requested_workers = int(max_workers)
    except Exception:
        return PlainTextResponse("max_workers must be an integer.", status_code=400)
    if requested_workers < 1:
        return PlainTextResponse("max_workers must be >= 1.", status_code=400)
    requested_workers = min(requested_workers, MAX_REQUEST_WORKERS_CAP)

    # Persist selections in session
    sess["provider"] = provider
    sess["model"] = model
    sess["api_key"] = api_key
    sess["prompt_template"] = prompt_template
    sess["json_mode"] = (json_mode == "1")
    sess["max_workers"] = requested_workers

    is_json_mode = (json_mode == "1")

    # Provider key rules
    if provider.lower() in ("openai", "gemini") and not api_key.strip():
        return PlainTextResponse("API key is required for OpenAI/Gemini.", status_code=400)

    # Initialize progress tracking
    sessions[sid]["progress"] = {"status": "running", "done": 0, "sent": 0, "total": len(rows), "error": ""}

    def process_row(idx: int, row: Dict[str, Any]) -> Tuple[int, Dict[str, Any], set[str]]:
        row_json = json.dumps(row, ensure_ascii=False)
        row_for_template = dict(row)
        row_for_template["row_json"] = row_json

        # allow both {row_json} and {{row_json}} habit
        tmp = prompt_template.replace("{{row_json}}", "{row_json}")
        prompt = safe_format(tmp, row_for_template)

        t0 = time.time()
        try:
            text = call_provider(provider, api_key.strip(), model.strip(), prompt, json_mode=is_json_mode)
            err = ""
        except Exception as e:
            text = ""
            err = str(e)
        latency = round(time.time() - t0, 3)

        out: Dict[str, Any] = {c: row.get(c) for c in csv_cols}
        out["llm_output"] = text
        out["llm_error"] = err
        out["llm_latency_s"] = latency

        row_keys: set[str] = set()
        if is_json_mode and text:
            parsed, perr = try_parse_json(text)
            out["llm_json_valid"] = parsed is not None
            out["llm_json_error"] = perr or ""
            out["llm_json_raw"] = text

            if parsed is not None:
                flat = flatten_json(parsed, sep=".")
                out["_llm_json_flat"] = flat  # internal
                row_keys = set(flat.keys())

        return idx, out, row_keys

    worker_count = min(requested_workers, len(rows)) if rows else 1
    results: List[Optional[Dict[str, Any]]] = [None] * len(rows)
    detected_keys: set[str] = set()
    done_count = 0
    sent_count = 0

    def submit_next(exec_obj, q: deque, fset: set, mapping: dict):
        nonlocal sent_count
        if not q:
            return
        idx, r = q.popleft()
        fut = exec_obj.submit(process_row, idx, r)
        fset.add(fut)
        mapping[fut] = idx
        sent_count += 1
        sessions[sid]["progress"]["sent"] = sent_count

    task_queue: deque[tuple[int, Dict[str, Any]]] = deque(list(enumerate(rows)))
    futures_set: set = set()
    future_to_idx: dict = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for _ in range(min(worker_count, len(rows))):
            submit_next(executor, task_queue, futures_set, future_to_idx)

        while futures_set:
            for future in as_completed(list(futures_set), timeout=None):
                futures_set.remove(future)
                idx = future_to_idx.pop(future, None)
                base_row = rows[idx] if idx is not None else {}
                try:
                    _, out, row_keys = future.result()
                except Exception as e:
                    # Preserve row slot even on failure to keep output aligned.
                    out = {c: base_row.get(c) for c in csv_cols}
                    out["llm_output"] = ""
                    out["llm_error"] = f"Failed: {e}"
                    out["llm_latency_s"] = 0
                    row_keys = set()
                results[idx] = out
                detected_keys.update(row_keys)
                done_count += 1
                sessions[sid]["progress"]["done"] = done_count

                submit_next(executor, task_queue, futures_set, future_to_idx)

    # Fill any missing slots to keep row order intact.
    for idx, res in enumerate(results):
        if res is None:
            base_row = rows[idx]
            fallback = {c: base_row.get(c) for c in csv_cols}
            fallback["llm_output"] = ""
            fallback["llm_error"] = "Processing failed"
            fallback["llm_latency_s"] = 0
            results[idx] = fallback

    sess["results"] = results
    sess["detected_json_keys"] = sorted(detected_keys)
    sessions[sid]["progress"] = {
        "status": "done",
        "done": len(results),
        "sent": len(results),
        "total": len(results),
        "error": "",
    }

    resp = RedirectResponse(url="/export", status_code=303)
    set_session_cookie(resp, sid)
    return resp

@app.post("/test", response_class=HTMLResponse)
async def test_one(
    request: Request,
    sid_token: Optional[str] = Form(None),
    provider: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    prompt_template: str = Form(...),
    json_mode: Optional[str] = Form(None),
    test_cols: Optional[List[str]] = Form(None),
):
    sid = resolve_sid(request, sid_token)
    sess = sessions.get(sid, {})
    if "rows" not in sess:
        return PlainTextResponse("Upload a CSV first.", status_code=400)

    rows = [r for r in sess["rows"] if not _row_is_empty_dict(r)]
    rows = [{k: _normalize_value(v) for k, v in row.items()} for row in rows]
    if not rows:
        return PlainTextResponse("No usable rows in CSV after dropping empty rows.", status_code=400)
    csv_cols = sess["csv_cols"]
    selected_cols = [c for c in (test_cols or []) if c in csv_cols]
    # Persist selections in session
    sess["provider"] = provider
    sess["model"] = model
    sess["api_key"] = api_key
    sess["prompt_template"] = prompt_template
    sess["json_mode"] = (json_mode == "1")
    sess["test_cols"] = selected_cols
    sid_token = serializer.dumps(sid)

    if not _prompt_has_placeholder(prompt_template, ["row_json"] + csv_cols):
        return PlainTextResponse("Prompt must include at least one column placeholder or {{row_json}}.", status_code=400)

    if provider.lower() in ("openai", "gemini") and not api_key.strip():
        return PlainTextResponse("API key is required for OpenAI/Gemini.", status_code=400)

    row_idx = random.randrange(len(rows))
    row = rows[row_idx]

    row_json = json.dumps(row, ensure_ascii=False)
    row_for_template = dict(row)
    row_for_template["row_json"] = row_json
    tmp = prompt_template.replace("{{row_json}}", "{row_json}")
    prompt = safe_format(tmp, row_for_template)

    err = ""
    text = ""
    latency = 0
    pretty_text = ""
    try:
        t0 = time.time()
        text = call_provider(provider, api_key.strip(), model.strip(), prompt, json_mode=(json_mode == "1"))
        latency = round(time.time() - t0, 3)
        parsed, _ = try_parse_json(text)
        if parsed is not None:
            pretty_text = json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception as e:
        err = str(e)
    if not pretty_text:
        pretty_text = text or ""

    def format_value(v: Any) -> str:
        s = "" if v is None else str(v)
        escaped = html_escape(s)
        return escaped.replace("\n", "<br><span class='mini-sep'></span><br>")

    cols_display = "".join(
        [
            f"<div class='col-block'><div class='col-name'>{html_escape(c)}</div><div class='col-val'>{format_value(row.get(c, ''))}</div></div>"
            for c in (selected_cols or [])
        ]
    ) or "<div class='small'>No columns selected for Test.</div>"

    page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Test Result</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 20px; background:#f5f7fb; color:#0f172a; }}
    .wrap {{ display:flex; gap:20px; flex-wrap:wrap; }}
    .pane {{ flex:1; min-width:280px; background:white; border:1px solid #e5e7eb; border-radius:12px; padding:16px; box-shadow:0 10px 30px rgba(15,23,42,0.08); max-height:70vh; overflow:auto; }}
    h3 {{ margin-top:0; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#0f172a; color:#e5e7eb; padding:12px; border-radius:10px; font-size:13px; max-height:60vh; overflow:auto; }}
    .small {{ font-size:12px; color:#6b7280; }}
    .meta {{ margin:8px 0; font-size:13px; color:#334155; }}
    .err {{ color:#b91c1c; }}
    .col-block {{ border-bottom:1px dashed #e5e7eb; padding:6px 0; }}
    .col-block:last-child {{ border-bottom:none; }}
    .col-name {{ font-weight:600; margin-bottom:4px; color:#0f172a; }}
    .col-val {{ white-space:pre-wrap; word-break:break-word; color:#1f2937; }}
    .mini-sep {{ display:block; height:1px; background:#e5e7eb; margin:4px 0; }}
  </style>
</head>
<body>
  <h2>Test (random row #{row_idx + 1} of {len(rows)})</h2>
  <div class="meta">Provider: {html_escape(provider)} — Model: {html_escape(model)} — Latency: {latency}s</div>
  {"<div class='meta err'>Error: " + html_escape(err) + "</div>" if err else ""}
  <div class="wrap">
    <div class="pane">
      <h3>Selected columns</h3>
      {cols_display}
      <div style="margin-top:12px;">
        <details>
          <summary>Full row JSON</summary>
          <pre>{html_escape(row_json)}</pre>
        </details>
      </div>
    </div>
    <div class="pane">
      <h3>Model response</h3>
      <pre>{html_escape(pretty_text)}</pre>
    </div>
  </div>
  <div style="margin-top:14px;">
    <form action="/test" method="post" style="display:inline;">
      <input type="hidden" name="sid_token" value="{html_escape(sid_token)}" />
      <input type="hidden" name="provider" value="{html_escape(provider)}" />
      <input type="hidden" name="model" value="{html_escape(model)}" />
      <input type="hidden" name="api_key" value="{html_escape(api_key)}" />
      <input type="hidden" name="prompt_template" value="{html_escape(prompt_template)}" />
      {"<input type='hidden' name='json_mode' value='1' />" if (json_mode == "1") else ""}
      {"".join([f'<input type="hidden" name="test_cols" value="{html_escape(c)}" />' for c in selected_cols])}
      <button type="submit" style="background:#10b981;">Again</button>
    </form>
    <a href="/" style="margin-left:8px;"><button type="button">Back</button></a>
  </div>
</body>
</html>
"""
    return HTMLResponse(page)

@app.get("/export", response_class=HTMLResponse)
def export_page(request: Request):
    sid = get_or_create_session_id(request)
    sess = sessions.get(sid, {})
    has_csv = "csv_cols" in sess
    has_results = "results" in sess and len(sess["results"]) > 0

    if not has_csv:
        resp = RedirectResponse(url="/", status_code=303)
        set_session_cookie(resp, sid)
        return resp

    csv_cols = sess["csv_cols"]
    detected_keys = sess.get("detected_json_keys", [])

    csv_cols_html = render_checkbox_list("out_csv_cols", csv_cols, selected=[])
    keys_html = (
        render_checkbox_list("out_json_keys", detected_keys, selected=[])
        if detected_keys
        else "<div class='small'>No JSON keys detected yet. Run the prompt with JSON mode enabled.</div>"
    )
    stats_keys_selected = sess.get("stats_keys", [])
    stats_keys_html = (
        render_checkbox_list("stats_keys", detected_keys, selected=stats_keys_selected)
        if detected_keys
        else "<div class='small'>No JSON keys detected yet. Run the prompt with JSON mode enabled.</div>"
    )

    page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Export</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    .row {{ display:flex; gap:24px; align-items:flex-start; }}
    .card {{ border:1px solid #ddd; border-radius:10px; padding:16px; flex:1; }}
    label {{ display:block; margin-top:10px; font-weight:600; }}
    select, input[type="text"] {{ width:100%; padding:8px; margin-top:6px; }}
    .small {{ font-size:12px; color:#555; margin-top:6px; }}
    .box {{ max-height: 340px; overflow:auto; border:1px solid #eee; padding:10px; border-radius:8px; }}
    button {{ padding:10px 14px; }}
    .ok {{ color:#0a7; }}
    .warn {{ color:#b60; }}
    .overlay {{
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.5);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 999;
    }}
    .overlay .panel {{
      background: white;
      padding: 20px;
      border-radius: 12px;
      width: 420px;
      box-shadow: 0 20px 50px rgba(15, 23, 42, 0.3);
      max-height: 80vh;
      overflow: auto;
    }}
    .modal-actions {{ display:flex; justify-content:flex-end; gap:10px; margin-top:14px; }}
  </style>
</head>
<body>
  <h2>Export Options</h2>
  <div class="small">
    Select which columns from the input CSV and which JSON keys (discovered from the last run) you want to include in the output CSV.
  </div>

  <div style="margin: 10px 0;">
    {"<span class='ok'>Run results loaded.</span>" if has_results else "<span class='warn'>No run results yet. Go back and run first.</span>"}
    <a href="/" style="margin-left:16px;">Back</a>
  </div>

  <form action="/download" method="post">
    <div class="row">
      <div class="card">
        <h3>Include input CSV columns</h3>
        <div style="margin:6px 0; display:flex; gap:8px; flex-wrap:wrap;">
          <button type="button" class="toggle-btn" data-target="out_csv_cols" data-action="all">Select all</button>
          <button type="button" class="toggle-btn" data-target="out_csv_cols" data-action="none">Select none</button>
        </div>
        <div class="box">{csv_cols_html}</div>
      </div>

      <div class="card">
        <h3>Include JSON output</h3>

        <label>JSON export mode</label>
        <select name="json_export_mode">
          <option value="raw_json">Raw JSON column (llm_json_raw)</option>
          <option value="flatten" selected>Flatten selected JSON keys into columns</option>
        </select>

        <label>Output prefix</label>
        <input type="text" name="out_prefix" value="out_" />

        <h4 style="margin-top:14px;">Discovered JSON keys</h4>
        <div style="margin:6px 0; display:flex; gap:8px; flex-wrap:wrap;">
          <button type="button" class="toggle-btn" data-target="out_json_keys" data-action="all">Select all</button>
          <button type="button" class="toggle-btn" data-target="out_json_keys" data-action="none">Select none</button>
        </div>
        <div class="box">{keys_html}</div>
        <div class="small">Keys appear after you run at least once with JSON mode enabled.</div>
      </div>
    </div>

    <div style="margin-top:14px;">
      <button type="submit" {"disabled" if not has_results else ""}>Download output.csv</button>
      <button type="button" id="stats-button" {"disabled" if not has_results else ""} style="margin-left:10px;background:#0f766e;">Stats</button>
      <a href="/review?idx=0" style="margin-left:10px;"><button type="button" {"disabled" if not has_results else ""}>Review rows</button></a>
    </div>
  </form>
  <div class="overlay" id="stats-overlay">
    <div class="panel">
      <h3>Stats on JSON keys</h3>
      <div style="margin:6px 0; display:flex; gap:8px; flex-wrap:wrap;">
        <button type="button" class="toggle-btn" data-target="stats_keys" data-action="all">Select all</button>
        <button type="button" class="toggle-btn" data-target="stats_keys" data-action="none">Select none</button>
      </div>
      <form action="/stats" method="post" id="stats-form">
        <div class="box" style="margin-top:8px; max-height:320px; overflow:auto;">
          {stats_keys_html}
        </div>
        <div class="small">Choose JSON output keys to compute true/false stats.</div>
        <div class="modal-actions">
          <button type="button" id="stats-cancel" style="background:#94a3b8;">Cancel</button>
          <button type="submit" id="stats-confirm" style="background:#0f766e;">Run stats</button>
        </div>
      </form>
    </div>
  </div>
  <script>
    (() => {{
      function toggleGroup(name, action) {{
        const boxes = Array.from(document.querySelectorAll(`input[type="checkbox"][name="${{name}}"]`));
        const shouldCheck = action === "all";
        boxes.forEach((b) => {{
          b.checked = shouldCheck;
        }});
      }}
      document.querySelectorAll(".toggle-btn").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          const target = btn.getAttribute("data-target");
          const action = btn.getAttribute("data-action");
          if (target && action) {{
            toggleGroup(target, action);
          }}
        }});
      }});

      const statsBtn = document.getElementById("stats-button");
      const statsOverlay = document.getElementById("stats-overlay");
      const statsCancel = document.getElementById("stats-cancel");
      function setModalVisible(el, visible) {{
        if (!el) return;
        el.style.display = visible ? "flex" : "none";
      }}
      if (statsBtn) {{
        statsBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          setModalVisible(statsOverlay, true);
        }});
      }}
      if (statsCancel) {{
        statsCancel.addEventListener("click", (e) => {{
          e.preventDefault();
          setModalVisible(statsOverlay, false);
        }});
      }}
    }})();
  </script>
</body>
</html>
"""
    resp = HTMLResponse(page)
    set_session_cookie(resp, sid)
    return resp

@app.post("/download")
async def download(
    request: Request,
    out_csv_cols: Optional[List[str]] = Form(None),
    out_json_keys: Optional[List[str]] = Form(None),
    json_export_mode: str = Form("flatten"),
    flatten_sep: str = ".",
    out_prefix: str = Form("out_"),
):
    sid = resolve_sid(request)
    sess = sessions.get(sid, {})
    results = sess.get("results")
    if not results:
        return PlainTextResponse("No results to export. Run first.", status_code=400)

    csv_cols = sess.get("csv_cols", [])
    out_csv_cols = out_csv_cols or []
    out_csv_cols = [c for c in out_csv_cols if c in csv_cols]

    out_json_keys = out_json_keys or []

    exported_rows: List[Dict[str, Any]] = []
    column_order: List[str] = []

    def remember_cols(cols):
        for k in cols:
            if k not in column_order:
                column_order.append(k)

    # Seed column order with input columns then debug columns; JSON columns added as encountered.
    remember_cols(out_csv_cols)
    remember_cols(["llm_output", "llm_error", "llm_latency_s"])

    for r in results:
        row_out: Dict[str, Any] = {}
        for c in out_csv_cols:
            row_out[c] = r.get(c, "")

        # Always keep these debugging columns
        row_out["llm_output"] = r.get("llm_output", "")
        row_out["llm_error"] = r.get("llm_error", "")
        row_out["llm_latency_s"] = r.get("llm_latency_s", "")

        if json_export_mode == "raw_json":
            row_out["llm_json_raw"] = r.get("llm_json_raw", "")
            row_out["llm_json_valid"] = r.get("llm_json_valid", False)
            row_out["llm_json_error"] = r.get("llm_json_error", "")
        else:
            # flatten
            flat = r.get("_llm_json_flat", {}) or {}
            # If no keys selected, export all detected keys for that row
            keys = out_json_keys if out_json_keys else list(flat.keys())
            for k in keys:
                # Support different separator if user changes it at export time
                if flatten_sep != ".":
                    # keys were detected with ".", convert if needed
                    kk = k.replace(".", flatten_sep)
                else:
                    kk = k
                row_out[f"{out_prefix}{kk}"] = flat.get(k, "")

        exported_rows.append(row_out)
        remember_cols(row_out.keys())

    out_df = pd.DataFrame(exported_rows)
    if column_order:
        out_df = out_df.reindex(columns=column_order)
    buf = io.StringIO()
    # Quote all fields so commas/newlines inside values don't break row boundaries when re-opened.
    out_df.to_csv(buf, index=False, quoting=csv.QUOTE_ALL, lineterminator="\n")
    out_bytes = buf.getvalue().encode("utf-8")

    return StreamingResponse(
        io.BytesIO(out_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="output.csv"'},
    )

@app.post("/stats", response_class=HTMLResponse)
async def stats(
    request: Request,
    stats_keys: Optional[List[str]] = Form(None),
    stats_date_col: str = Form(""),
    stats_date_from: str = Form(""),
    stats_date_to: str = Form(""),
):
    sid = resolve_sid(request)
    sess = sessions.get(sid, {})
    results = sess.get("results") or []
    if not results:
        return PlainTextResponse("No results to compute stats. Run first.", status_code=400)

    selected_keys = stats_keys if stats_keys is not None else sess.get("stats_keys", [])
    sess["stats_keys"] = selected_keys
    if stats_date_col and stats_date_col in (sess.get("csv_cols") or []):
        sess["stats_date_col"] = stats_date_col
    else:
        stats_date_col = ""
        sess["stats_date_col"] = ""
    sess["stats_date_from"] = stats_date_from or ""
    sess["stats_date_to"] = stats_date_to or ""

    date_from = _parse_date(stats_date_from)
    date_to = _parse_date(stats_date_to)

    filtered_results = results
    filter_note = "No date filter applied."
    if stats_date_col and (date_from or date_to):
        tmp = []
        for r in results:
            raw_val = r.get(stats_date_col)
            if _is_empty_value(raw_val):
                continue
            dt = pd.to_datetime(raw_val, errors="coerce")
            if pd.isna(dt):
                continue
            d = dt.date()
            if date_from and d < date_from:
                continue
            if date_to and d > date_to:
                continue
            tmp.append(r)
        filtered_results = tmp
        range_parts = []
        if date_from:
            range_parts.append(f"from {date_from.isoformat()}")
        if date_to:
            range_parts.append(f"to {date_to.isoformat()}")
        range_text = " ".join(range_parts) if range_parts else "selected range"
        filter_note = f"Filtered by {stats_date_col} ({range_text})."
    elif stats_date_col:
        filter_note = f"Date column selected ({stats_date_col}) but no date range provided."

    total_rows = len(filtered_results)
    rows_html = ""
    for key in selected_keys:
        true_count = 0
        for r in filtered_results:
            flat = r.get("_llm_json_flat", {}) or {}
            val = flat.get(key)
            if _coerce_bool(val) is True:
                true_count += 1
        false_count = total_rows - true_count
        true_pct = (true_count / total_rows * 100) if total_rows else 0
        false_pct = (false_count / total_rows * 100) if total_rows else 0
        rows_html += (
            "<tr>"
            f"<td>{html_escape(key)}</td>"
            f"<td>{total_rows}</td>"
            f"<td>{true_count}</td>"
            f"<td>{false_count}</td>"
            f"<td>{true_pct:.2f}%</td>"
            f"<td>{false_pct:.2f}%</td>"
            "</tr>"
        )

    if not rows_html:
        rows_html = "<tr><td colspan='6'>No JSON keys selected.</td></tr>"

    csv_cols = sess.get("csv_cols") or []
    date_options = ['<option value="">No date filter</option>'] + [
        f'<option value="{html_escape(c)}" {"selected" if c == stats_date_col else ""}>{html_escape(c)}</option>'
        for c in csv_cols
    ]
    stats_date_select = "\n".join(date_options)
    stats_keys_hidden = "".join(
        [f'<input type="hidden" name="stats_keys" value="{html_escape(k)}" />' for k in selected_keys]
    )

    page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Stats</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 20px; background:#f5f7fb; color:#0f172a; }}
    table {{ width:100%; border-collapse:collapse; background:white; border-radius:10px; overflow:hidden; }}
    th, td {{ padding:10px 12px; border-bottom:1px solid #e5e7eb; text-align:left; font-size:14px; }}
    th {{ background:#f1f5f9; }}
    .small {{ font-size:12px; color:#6b7280; margin-top:8px; }}
    .nav {{ display:flex; gap:10px; align-items:center; margin-bottom:12px; }}
    button {{ padding:8px 12px; border:none; border-radius:8px; background:#0b74ff; color:white; cursor:pointer; }}
    .filter {{ background:white; border:1px solid #e5e7eb; border-radius:10px; padding:12px; margin:12px 0; }}
    .filter-row {{ display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end; }}
    .filter-row > div {{ flex:1; min-width:160px; }}
    label {{ display:block; font-size:12px; color:#475569; margin-bottom:4px; }}
    input[type="date"], select {{ width:100%; padding:8px; border:1px solid #e5e7eb; border-radius:8px; }}
  </style>
</head>
<body>
  <div class="nav">
    <a href="/export"><button type="button">Back to export</button></a>
  </div>
  <h2>Stats</h2>
  <div class="filter">
    <form action="/stats" method="post">
      {stats_keys_hidden}
      <div class="filter-row">
        <div>
          <label>Date column (optional)</label>
          <select name="stats_date_col">
            {stats_date_select}
          </select>
        </div>
        <div>
          <label>From</label>
          <input type="date" name="stats_date_from" value="{html_escape(stats_date_from)}" />
        </div>
        <div>
          <label>To</label>
          <input type="date" name="stats_date_to" value="{html_escape(stats_date_to)}" />
        </div>
        <div style="flex:0 0 auto;">
          <button type="submit">Apply filter</button>
        </div>
      </div>
      <div class="small">If no date column is selected, no date filtering is applied.</div>
    </form>
  </div>
  <table>
    <thead>
      <tr>
        <th>JSON key</th>
        <th>Rows</th>
        <th>True</th>
        <th>False</th>
        <th>% True</th>
        <th>% False</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  <div class="small">Note: missing or non-boolean values are treated as false.</div>
  <div class="small">{html_escape(filter_note)} Total rows: {total_rows}.</div>
</body>
</html>
"""
    resp = HTMLResponse(page)
    set_session_cookie(resp, sid)
    return resp

@app.get("/review", response_class=HTMLResponse)
def review(request: Request, idx: int = 0, key: str = "", val: str = ""):
    sid = get_or_create_session_id(request)
    sess = sessions.get(sid, {})
    results = sess.get("results") or []
    csv_cols = sess.get("csv_cols") or []
    detected_keys = sess.get("detected_json_keys") or []
    if not results:
        resp = RedirectResponse(url="/", status_code=303)
        set_session_cookie(resp, sid)
        return resp

    filter_key = key if key in detected_keys else ""
    filter_val = val.lower().strip() if val else ""
    target_bool: Optional[bool] = None
    if filter_val in ("true", "false"):
        target_bool = (filter_val == "true")

    filtered_indices: List[int] = []
    if filter_key and target_bool is not None:
        for i, r in enumerate(results):
            flat = r.get("_llm_json_flat", {}) or {}
            if _coerce_bool(flat.get(filter_key)) is target_bool:
                filtered_indices.append(i)
    else:
        filtered_indices = list(range(len(results)))

    total = len(filtered_indices)
    row = None
    if total > 0:
        idx = max(0, min(idx, total - 1))
        row = results[filtered_indices[idx]]

    def format_value(v: Any) -> str:
        s = "" if v is None else str(v)
        escaped = html_escape(s)
        return escaped.replace("\n", "<br><span class='mini-sep'></span><br>")

    if row is not None:
        cols_display = "".join(
            [
                f"<div class='col-block'><div class='col-name'>{html_escape(c)}</div><div class='col-val'>{format_value(row.get(c, ''))}</div></div>"
                for c in csv_cols
            ]
        ) or "<div class='small'>No columns to display.</div>"
        text = row.get("llm_output", "") or ""
        pretty_text = text
        parsed, _ = try_parse_json(text)
        if parsed is not None:
            try:
                pretty_text = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                pretty_text = text
        err = row.get("llm_error", "") or ""
    else:
        cols_display = "<div class='small'>No rows match this filter.</div>"
        pretty_text = ""
        err = ""

    prev_idx = max(0, idx - 1) if total > 0 else 0
    next_idx = min(total - 1, idx + 1) if total > 0 else 0

    page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Review Rows</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 20px; background:#f5f7fb; color:#0f172a; }}
    .wrap {{ display:flex; gap:20px; flex-wrap:wrap; }}
    .pane {{ flex:1; min-width:280px; background:white; border:1px solid #e5e7eb; border-radius:12px; padding:16px; box-shadow:0 10px 30px rgba(15,23,42,0.08); max-height:70vh; overflow:auto; }}
    h3 {{ margin-top:0; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#0f172a; color:#e5e7eb; padding:12px; border-radius:10px; font-size:13px; max-height:60vh; overflow:auto; }}
    .small {{ font-size:12px; color:#6b7280; }}
    .meta {{ margin:8px 0; font-size:13px; color:#334155; }}
    .err {{ color:#b91c1c; }}
    .col-block {{ border-bottom:1px dashed #e5e7eb; padding:6px 0; }}
    .col-block:last-child {{ border-bottom:none; }}
    .col-name {{ font-weight:600; margin-bottom:4px; color:#0f172a; }}
    .col-val {{ white-space:pre-wrap; word-break:break-word; color:#1f2937; }}
    .mini-sep {{ display:block; height:1px; background:#e5e7eb; margin:4px 0; }}
    .nav {{ display:flex; gap:10px; align-items:center; margin-bottom:12px; }}
    button {{ padding:8px 12px; border:none; border-radius:8px; background:#0b74ff; color:white; cursor:pointer; }}
    button[disabled] {{ opacity:0.6; cursor:not-allowed; }}
    .filter {{ background:white; border:1px solid #e5e7eb; border-radius:10px; padding:12px; margin:12px 0; }}
    .filter-row {{ display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end; }}
    .filter-row > div {{ flex:1; min-width:160px; }}
    label {{ display:block; font-size:12px; color:#475569; margin-bottom:4px; }}
    select {{ width:100%; padding:8px; border:1px solid #e5e7eb; border-radius:8px; }}
  </style>
</head>
<body>
  <div class="nav">
    <a href="/export"><button type="button">Back to export</button></a>
    <span class="small">Row {display_index} of {total} (filtered from {total_all})</span>
    <a href="/review?idx={prev_idx}{filter_qs}"><button type="button" {"disabled" if total == 0 or idx == 0 else ""}>Previous</button></a>
    <a href="/review?idx={next_idx}{filter_qs}"><button type="button" {"disabled" if total == 0 or idx >= total - 1 else ""}>Next</button></a>
  </div>
  <div class="filter">
    <form action="/review" method="get">
      <input type="hidden" name="idx" value="0" />
      <div class="filter-row">
        <div>
          <label>JSON attribute</label>
          <select name="key">
            {"".join(key_options)}
          </select>
        </div>
        <div>
          <label>Value</label>
          <select name="val">
            {val_options_html}
          </select>
        </div>
        <div style="flex:0 0 auto;">
          <button type="submit">Apply filter</button>
          <a href="/review?idx=0"><button type="button" style="background:#94a3b8;margin-left:6px;">Clear</button></a>
        </div>
      </div>
      <div class="small">Filter uses JSON output keys (true/false). Leave empty for all rows.</div>
    </form>
  </div>
  <div class="wrap">
    <div class="pane">
      <h3>Input columns</h3>
      {cols_display}
    </div>
    <div class="pane">
      <h3>Model response</h3>
      {"<div class='meta err'>Error: " + html_escape(err) + "</div>" if err else ""}
      <pre>{html_escape(pretty_text)}</pre>
    </div>
  </div>
</body>
</html>
"""
    resp = HTMLResponse(page)
    set_session_cookie(resp, sid)
    return resp
