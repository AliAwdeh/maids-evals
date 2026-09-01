import io
import json
import os
import secrets
import threading
import time
import csv
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer
import pandas as pd

import auth
import storage
from engine import (
    LANGCC_API_BASE,
    MAX_REQUEST_WORKERS_CAP,
    _coerce_bool,
    _parse_date,
    _row_is_empty_dict,
    _row_is_empty_series,
    _normalize_value,
    _prompt_has_placeholder,
    _build_output_dataframe,
    call_provider,
    estimate_tokens,
    fetch_ollama_models,
    fetch_openai_compatible_models,
    filter_results_by_json,
    parse_model_params,
    process_row,
    provider_requires_key,
    render_prompt,
    result_to_base_row,
    try_parse_json,
)
from prompt_fixer import build_example, build_examples, improve_prompt, unified_diff

load_dotenv()

BASE_DIR = os.path.dirname(__file__)
PUBLIC_EXACT = {"/login", "/logout"}
PUBLIC_PREFIXES = ("/static",)
DATA_EXACT = {
    "/progress",
    "/usage",
    "/prompts/get",
    "/prompts/save",
    "/session/save",
    "/provider/models",
    "/ollama/models",
    "/credentials",
    "/credentials/forget",
    "/review/notes",
    "/review/prompt",
    "/review/improve",
    "/review/improve/status",
    "/review/accept",
    "/review/discard",
    "/test/improve",
    "/test/improve/status",
    "/test/accept",
    "/test/discard",
    "/prompts/raw",
}


def _session_secret() -> str:
    secret = (os.getenv("SESSION_SECRET") or "").strip()
    if secret:
        return secret
    generated = secrets.token_hex(32)
    print(
        "WARNING: SESSION_SECRET is not set. Using a one-off secret for this process. "
        "Logins will not survive a restart. Copy .env.example to .env and set SESSION_SECRET.",
        flush=True,
    )
    return generated


SESSION_SECRET = _session_secret()
if not (os.getenv("SESSION_SECRET") or "").strip():
    os.environ["SESSION_SECRET"] = SESSION_SECRET
work_serializer = URLSafeSerializer(SESSION_SECRET, salt="maids-evals-work")
sessions: Dict[str, Dict[str, Any]] = {}
WORK_COOKIE = "me_sid"

app = FastAPI(title="Maids Evals")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def render(request: Request, name: str, context: Dict[str, Any], status_code: int = 200):
    ctx = dict(context)
    if "is_admin" not in ctx:
        user = getattr(request.state, "user", None) or ctx.get("user")
        ctx["is_admin"] = bool(user and auth.is_admin(user))
    try:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)
    except TypeError:
        return templates.TemplateResponse(name, {"request": request, **ctx}, status_code=status_code)


def current_user(request: Request) -> Optional[str]:
    return auth.load_auth(SESSION_SECRET, request.cookies.get(auth.AUTH_COOKIE, ""))


def set_auth_cookie(resp, username: str) -> None:
    resp.set_cookie(
        auth.AUTH_COOKIE,
        auth.dump_auth(SESSION_SECRET, username),
        httponly=True,
        samesite="lax",
        max_age=auth.AUTH_MAX_AGE,
    )


def clear_auth_cookies(resp) -> None:
    resp.delete_cookie(auth.AUTH_COOKIE)
    resp.delete_cookie(WORK_COOKIE)


def get_work_session(request: Request) -> tuple[str, Dict[str, Any]]:
    user = request.state.user
    cookie = request.cookies.get(WORK_COOKIE)
    if cookie:
        try:
            sid = work_serializer.loads(cookie)
            sess = sessions.get(sid)
            if sess and sess.get("owner") == user:
                return sid, sess
        except Exception:
            pass
    sid = secrets.token_urlsafe(16)
    sessions[sid] = {"owner": user}
    return sid, sessions[sid]


def set_work_cookie(resp, sid: str) -> None:
    resp.set_cookie(WORK_COOKIE, work_serializer.dumps(sid), httponly=True, samesite="lax")


def attach_session(resp, request: Request, sid: Optional[str] = None):
    if sid is None:
        sid, _ = get_work_session(request)
    set_work_cookie(resp, sid)
    return resp


def is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def is_data_request(request: Request) -> bool:
    path = request.url.path
    if path in DATA_EXACT:
        return True
    if "/download" in path:
        return True
    if path == "/run":
        return True
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if is_public(request.url.path):
        return await call_next(request)
    user = current_user(request)
    if not user:
        if is_data_request(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    request.state.user = user
    if is_admin_path(request.url.path) and not auth.is_admin(user):
        if is_data_request(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return render(request, "forbidden.html", {"nav": "", "user": user}, status_code=403)
    return await call_next(request)


def filter_pairs(keys: Optional[List[str]], vals: Optional[List[str]], n: int = 3) -> List[Dict[str, str]]:
    keys = keys or []
    vals = vals or []
    pairs = []
    for i in range(n):
        pairs.append({"key": keys[i] if i < len(keys) else "", "val": (vals[i] if i < len(vals) else "").lower()})
    return pairs


def filter_qs(pairs: List[Dict[str, str]]) -> str:
    parts = []
    for pair in pairs:
        if pair["key"] and pair["val"] in ("true", "false"):
            parts.append(f"filter_key={quote(pair['key'])}")
            parts.append(f"filter_val={quote(pair['val'])}")
    return ("&" + "&".join(parts)) if parts else ""


def run_title(meta: Dict[str, Any]) -> str:
    name = (meta.get("run_name") or "").strip()
    created = meta.get("created_at", "")
    try:
        created = datetime.fromisoformat(created).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return name or created or meta.get("run_id", "run")


def pretty_created(meta: Dict[str, Any]) -> str:
    created = meta.get("created_at", "")
    try:
        return datetime.fromisoformat(created).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return created


def saved_key_for(user: str, provider: str) -> str:
    if not provider_requires_key(provider):
        return ""
    return storage.load_user_secret(user, provider) or ""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return render(request, "login.html", {"error": error})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = auth.authenticate(username, password)
    if not user:
        return render(
            request,
            "login.html",
            {"error": "Unknown user or token. Ask whoever runs this instance to add you."},
            status_code=401,
        )
    resp = RedirectResponse(url="/", status_code=303)
    set_auth_cookie(resp, user)
    sid, sess = get_work_session_for_user(user, request)
    sess["owner"] = user
    set_work_cookie(resp, sid)
    return resp


def get_work_session_for_user(user: str, request: Request) -> tuple[str, Dict[str, Any]]:
    cookie = request.cookies.get(WORK_COOKIE)
    if cookie:
        try:
            sid = work_serializer.loads(cookie)
            sess = sessions.get(sid)
            if sess and sess.get("owner") == user:
                return sid, sess
        except Exception:
            pass
    sid = secrets.token_urlsafe(16)
    sessions[sid] = {"owner": user}
    return sid, sessions[sid]


@app.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    clear_auth_cookies(resp)
    return resp


def _admin_users_context(
    request: Request,
    *,
    new_user: str = "",
    new_token: str = "",
    revoked: str = "",
    error: str = "",
) -> Dict[str, Any]:
    return {
        "nav": "admin",
        "user": request.state.user,
        "users": auth.list_users_detailed(),
        "env_admin": auth.env_admin_username(),
        "new_user": new_user,
        "new_token": new_token,
        "revoked": revoked,
        "error": error,
    }


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, revoked: str = "", error: str = ""):
    return render(
        request,
        "admin_users.html",
        _admin_users_context(request, revoked=revoked, error=error),
    )


@app.post("/admin/users", response_class=HTMLResponse)
async def admin_create_user(request: Request, username: str = Form(...)):
    try:
        token = auth.add_user(username)
    except ValueError as e:
        return render(
            request,
            "admin_users.html",
            _admin_users_context(request, error=str(e)),
            status_code=400,
        )
    return render(
        request,
        "admin_users.html",
        _admin_users_context(request, new_user=username.strip(), new_token=token),
    )


@app.post("/admin/users/revoke")
async def admin_revoke_user(request: Request, username: str = Form(...)):
    try:
        removed = auth.revoke_user(username)
    except ValueError as e:
        return RedirectResponse(url=f"/admin/users?error={quote(str(e))}", status_code=303)
    if not removed:
        return RedirectResponse(
            url=f"/admin/users?error={quote(f'No user named {username.strip()!r}.')}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/users?revoked={quote(username.strip())}", status_code=303
    )


@app.post("/admin/users/reset-token", response_class=HTMLResponse)
async def admin_reset_token(request: Request, username: str = Form(...)):
    try:
        token = auth.reset_user_token(username)
    except ValueError as e:
        return render(
            request,
            "admin_users.html",
            _admin_users_context(request, error=str(e)),
            status_code=400,
        )
    return render(
        request,
        "admin_users.html",
        _admin_users_context(request, new_user=username.strip(), new_token=token),
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    sid, sess = get_work_session(request)
    has_csv = "csv_cols" in sess
    total_rows = len(sess.get("rows", [])) if has_csv else 0
    last_test = sess.get("last_test_row_idx")
    if not (has_csv and isinstance(last_test, int) and 0 <= last_test < total_rows):
        last_test = None
    recent = []
    for meta in storage.list_saved_runs(request.state.user)[:5]:
        recent.append(
            {
                "run_id": meta.get("run_id", ""),
                "title": run_title(meta),
                "created": pretty_created(meta),
                "provider": meta.get("provider", ""),
                "model": meta.get("model", ""),
                "row_count": meta.get("row_count", 0),
            }
        )
    model_params = sess.get("model_params_form") or {
        "temperature": "",
        "top_p": "",
        "max_output_tokens": "",
        "presence_penalty": "",
        "frequency_penalty": "",
        "seed": "",
        "reasoning_effort": "",
    }
    current_provider = sess.get("provider", "openai")
    saved_key = saved_key_for(request.state.user, current_provider)
    resp = render(
        request,
        "home.html",
        {
            "nav": "run",
            "user": request.state.user,
            "has_csv": has_csv,
            "total_rows": total_rows,
            "csv_cols": sess.get("csv_cols") or [],
            "prompts": storage.list_prompts(),
            "prompt_name": sess.get("prompt_name", ""),
            "prompt_template": sess.get("prompt_template", "Given this row:\n{row_json}\n\nReturn JSON with keys: status, note."),
            "run_name": sess.get("run_name", ""),
            "provider": current_provider,
            "model": sess.get("model", "gpt-5-mini"),
            "max_workers": sess.get("max_workers", 16),
            "workers_cap": MAX_REQUEST_WORKERS_CAP,
            "json_mode": sess.get("json_mode", True),
            "send_model_params": sess.get("send_model_params", False),
            "enabled_params": sess.get("enabled_params") or [],
            "model_params": model_params,
            "test_cols": sess.get("test_cols") or [],
            "last_test_row_idx": last_test,
            "recent_runs": recent,
            "sid_token": work_serializer.dumps(sid),
            "divide_default": min(total_rows, 100) if total_rows else 1,
            "saved_api_key": saved_key,
            "saved_key_exists": bool(saved_key),
        },
    )
    return attach_session(resp, request, sid)


@app.get("/ollama/models")
def ollama_models():
    try:
        return {"models": fetch_ollama_models()}
    except Exception as e:
        return PlainTextResponse(f"Failed to fetch Ollama models: {e}", status_code=502)


@app.post("/provider/models")
async def provider_models(provider: str = Form(...), api_key: str = Form("")):
    p = provider.lower().strip()
    try:
        if p == "ollama":
            return {"models": fetch_ollama_models()}
        if p == "langcc":
            if not api_key.strip():
                return PlainTextResponse("API key is required for LangCC models.", status_code=400)
            return {"models": fetch_openai_compatible_models(api_key.strip(), base_url=LANGCC_API_BASE)}
        if p == "openai":
            if not api_key.strip():
                return PlainTextResponse("API key is required for OpenAI models.", status_code=400)
            return {"models": fetch_openai_compatible_models(api_key.strip())}
        if p == "gemini":
            return {"models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]}
        return PlainTextResponse("Unsupported provider.", status_code=400)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=502)


@app.get("/progress")
def progress_status(request: Request, sid_token: Optional[str] = None):
    sid, sess = resolve_work(request, sid_token)
    prog = sess.get("progress") or {"status": "idle", "done": 0, "sent": 0, "total": 0, "error": ""}
    resp = JSONResponse(prog)
    return attach_session(resp, request, sid)


def resolve_work(request: Request, sid_token: Optional[str] = None) -> tuple[str, Dict[str, Any]]:
    user = request.state.user
    if sid_token:
        try:
            sid = work_serializer.loads(sid_token)
            sess = sessions.get(sid)
            if sess and sess.get("owner") == user:
                return sid, sess
        except Exception:
            pass
    return get_work_session(request)


@app.get("/usage")
def usage(request: Request, days: str = "7"):
    try:
        days_int = int(days)
    except Exception:
        days_int = 7
    since = datetime.utcnow() - timedelta(days=days_int) if days_int > 0 else None
    data = storage.read_usage(request.state.user, since=since)
    resp = JSONResponse(data)
    return attach_session(resp, request)


@app.post("/session/save")
async def save_session_state(
    request: Request,
    sid_token: Optional[str] = Form(None),
    provider: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    prompt_template: Optional[str] = Form(None),
    json_mode: Optional[str] = Form(None),
    max_workers: Optional[str] = Form(None),
    prompt_name: Optional[str] = Form(None),
    run_name: Optional[str] = Form(None),
    send_model_params: Optional[str] = Form(None),
    enabled_params: Optional[List[str]] = Form(None),
    temperature: Optional[str] = Form(None),
    top_p: Optional[str] = Form(None),
    max_output_tokens: Optional[str] = Form(None),
    presence_penalty: Optional[str] = Form(None),
    frequency_penalty: Optional[str] = Form(None),
    seed: Optional[str] = Form(None),
    reasoning_effort: Optional[str] = Form(None),
):
    sid, sess = resolve_work(request, sid_token)
    if provider is not None:
        sess["provider"] = provider
    if model is not None:
        sess["model"] = model
    if prompt_template is not None:
        sess["prompt_template"] = prompt_template
    if json_mode is not None:
        sess["json_mode"] = json_mode == "1"
    if max_workers is not None:
        try:
            mw = int(max_workers)
            if mw >= 1:
                sess["max_workers"] = min(mw, MAX_REQUEST_WORKERS_CAP)
        except Exception:
            pass
    if prompt_name is not None:
        sess["prompt_name"] = prompt_name
    if run_name is not None:
        sess["run_name"] = run_name
    if send_model_params is not None:
        sess["send_model_params"] = send_model_params == "1"
        sess["enabled_params"] = enabled_params or []
        sess["model_params_form"] = {
            "temperature": temperature or "",
            "top_p": top_p or "",
            "max_output_tokens": max_output_tokens or "",
            "presence_penalty": presence_penalty or "",
            "frequency_penalty": frequency_penalty or "",
            "seed": seed or "",
            "reasoning_effort": reasoning_effort or "",
        }
    resp = JSONResponse({"ok": True})
    return attach_session(resp, request, sid)


@app.get("/credentials")
def get_credentials(request: Request, provider: str = ""):
    user = request.state.user
    name = (provider or "").strip().lower()
    if name == "ollama":
        return JSONResponse({"provider": "ollama", "api_key": "", "saved": False, "needed": False})
    if name not in storage.KEY_PROVIDERS:
        return JSONResponse({"error": "unsupported provider"}, status_code=400)
    key = storage.load_user_secret(user, name) or ""
    return JSONResponse({"provider": name, "api_key": key, "saved": bool(key), "needed": True})


@app.post("/credentials")
async def save_credentials(request: Request, provider: str = Form(...), api_key: str = Form(...)):
    user = request.state.user
    try:
        name = storage.save_user_secret(user, provider, api_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Could not save key."}, status_code=500)
    return JSONResponse({"ok": True, "provider": name, "saved": True})


@app.post("/credentials/forget")
async def forget_credentials(request: Request, provider: str = Form(...)):
    user = request.state.user
    try:
        deleted = storage.delete_user_secret(user, provider)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Could not delete key."}, status_code=500)
    return JSONResponse({"ok": True, "provider": (provider or "").strip().lower(), "saved": False, "deleted": deleted})


@app.get("/prompts/get")
def get_prompt(request: Request, name: str):
    sid, sess = get_work_session(request)
    try:
        content = storage.load_prompt(name)
        safe_name = storage._safe_prompt_name(name)
    except Exception as e:
        return PlainTextResponse(f"Failed to load prompt: {e}", status_code=400)
    sess["prompt_name"] = safe_name
    sess["prompt_template"] = content
    resp = JSONResponse({"name": safe_name, "content": content})
    return attach_session(resp, request, sid)


@app.post("/prompts/save")
async def save_prompt(request: Request, prompt_name: str = Form(...), prompt_content: str = Form(...)):
    sid, sess = get_work_session(request)
    try:
        safe_name = storage.save_prompt(prompt_name, prompt_content or "")
    except Exception as e:
        return PlainTextResponse(f"Failed to save prompt: {e}", status_code=400)
    sess["prompt_name"] = safe_name
    sess["prompt_template"] = prompt_content or ""
    resp = JSONResponse({"ok": True, "name": safe_name})
    return attach_session(resp, request, sid)


@app.post("/upload")
async def upload(request: Request, csv_file: UploadFile = File(...)):
    sid, sess = get_work_session(request)
    raw = await csv_file.read()
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        return PlainTextResponse(f"Failed to parse CSV: {e}", status_code=400)
    df = df.loc[~df.apply(_row_is_empty_series, axis=1)].reset_index(drop=True)
    df = df.map(_normalize_value) if hasattr(df, "map") else df.applymap(_normalize_value)
    if df.empty:
        return PlainTextResponse("Uploaded CSV has no non-empty rows.", status_code=400)
    sess["csv_df"] = df
    sess["csv_cols"] = list(df.columns)
    sess["rows"] = df.to_dict(orient="records")
    for key in ("results", "detected_json_keys", "progress", "current_run_id", "last_test_row_idx"):
        sess.pop(key, None)
    resp = RedirectResponse(url="/", status_code=303)
    return attach_session(resp, request, sid)


@app.post("/divide")
async def divide_rows(request: Request, sid_token: Optional[str] = Form(None), divide_rows: int = Form(...)):
    sid, sess = resolve_work(request, sid_token)
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
    sess["csv_cols"] = list(df.columns)
    for key in ("results", "detected_json_keys", "progress", "current_run_id", "last_test_row_idx"):
        sess.pop(key, None)
    resp = RedirectResponse(url="/", status_code=303)
    return attach_session(resp, request, sid)


@app.post("/run")
async def run(
    request: Request,
    sid_token: Optional[str] = Form(None),
    run_name: str = Form(""),
    provider: str = Form(...),
    model: Optional[str] = Form(None),
    api_key: str = Form(""),
    prompt_template: str = Form(...),
    json_mode: Optional[str] = Form(None),
    max_workers: str = Form("16"),
    send_model_params: Optional[str] = Form(None),
    enabled_params: Optional[List[str]] = Form(None),
    temperature: Optional[str] = Form(None),
    top_p: Optional[str] = Form(None),
    max_output_tokens: Optional[str] = Form(None),
    presence_penalty: Optional[str] = Form(None),
    frequency_penalty: Optional[str] = Form(None),
    seed: Optional[str] = Form(None),
    reasoning_effort: Optional[str] = Form(None),
):
    sid, sess = resolve_work(request, sid_token)
    user = request.state.user
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Model is required.", status_code=400)
    if "rows" not in sess:
        return PlainTextResponse("Upload a CSV first.", status_code=400)
    rows = [r for r in sess["rows"] if not _row_is_empty_dict(r)]
    rows = [{k: _normalize_value(v) for k, v in row.items()} for row in rows]
    if not rows:
        return PlainTextResponse("No usable rows in CSV after dropping empty rows.", status_code=400)
    csv_cols = sess["csv_cols"]
    if not _prompt_has_placeholder(prompt_template, ["row_json"] + csv_cols):
        return PlainTextResponse("Prompt must include at least one column placeholder or {row_json}.", status_code=400)
    try:
        requested_workers = int(max_workers)
    except Exception:
        return PlainTextResponse("max_workers must be an integer.", status_code=400)
    if requested_workers < 1:
        return PlainTextResponse("max_workers must be >= 1.", status_code=400)
    requested_workers = min(requested_workers, MAX_REQUEST_WORKERS_CAP)
    sess["provider"] = provider
    sess["run_name"] = run_name
    sess["model"] = model
    sess["prompt_template"] = prompt_template
    sess["json_mode"] = json_mode == "1"
    sess["max_workers"] = requested_workers
    sess["send_model_params"] = send_model_params == "1"
    sess["enabled_params"] = enabled_params or []
    sess["model_params_form"] = {
        "temperature": temperature or "",
        "top_p": top_p or "",
        "max_output_tokens": max_output_tokens or "",
        "presence_penalty": presence_penalty or "",
        "frequency_penalty": frequency_penalty or "",
        "seed": seed or "",
        "reasoning_effort": reasoning_effort or "",
    }
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse("API key is required for OpenAI/LangCC/Gemini.", status_code=400)

    is_json_mode = json_mode == "1"
    model_params = parse_model_params(
        send_model_params, enabled_params, temperature, top_p, max_output_tokens,
        presence_penalty, frequency_penalty, seed, reasoning_effort,
    )
    sess["progress"] = {"status": "running", "done": 0, "sent": 0, "total": len(rows), "error": ""}
    key_once = api_key.strip()

    def work_one(idx: int, row: Dict[str, Any]):
        out, row_keys, prompt, text = process_row(
            row, csv_cols, prompt_template, provider, key_once, model, is_json_mode, model_params
        )
        storage.log_usage(user, provider, model, estimate_tokens(prompt), estimate_tokens(text))
        return idx, out, row_keys

    def run_job():
        worker_count = min(requested_workers, len(rows)) if rows else 1
        results: List[Optional[Dict[str, Any]]] = [None] * len(rows)
        detected_keys: set = set()
        done_count = 0
        sent_count = 0

        def submit_next(exec_obj, q, fset, mapping):
            nonlocal sent_count
            if not q:
                return
            idx, r = q.popleft()
            fut = exec_obj.submit(work_one, idx, r)
            fset.add(fut)
            mapping[fut] = idx
            sent_count += 1
            sess["progress"]["sent"] = sent_count

        task_queue = deque(list(enumerate(rows)))
        futures_set = set()
        future_to_idx = {}
        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                for _ in range(min(worker_count, len(rows))):
                    submit_next(executor, task_queue, futures_set, future_to_idx)
                while futures_set:
                    for future in as_completed(list(futures_set)):
                        futures_set.discard(future)
                        idx = future_to_idx.pop(future, None)
                        base_row = rows[idx] if idx is not None else {}
                        try:
                            _, out, row_keys = future.result()
                        except Exception as e:
                            out = {c: base_row.get(c) for c in csv_cols}
                            out.update({
                                "llm_output": "",
                                "llm_api_failed": True,
                                "llm_api_error": f"Failed: {e}",
                                "llm_error": "",
                                "llm_latency_s": 0,
                            })
                            row_keys = set()
                        if idx is not None:
                            results[idx] = out
                            detected_keys.update(row_keys)
                        done_count += 1
                        sess["progress"]["done"] = done_count
                        submit_next(executor, task_queue, futures_set, future_to_idx)
            for idx, res in enumerate(results):
                if res is None:
                    base_row = rows[idx]
                    results[idx] = {
                        **{c: base_row.get(c) for c in csv_cols},
                        "llm_output": "",
                        "llm_api_failed": True,
                        "llm_api_error": "Processing failed",
                        "llm_error": "",
                        "llm_latency_s": 0,
                    }
            sess["results"] = results
            sess["detected_json_keys"] = sorted(detected_keys)
            run_id = storage.save_run_archive(
                user, sess, rows, results, sorted(detected_keys),
                provider, model, run_name, worker_count, model_params, prompt_template,
            )
            sess["current_run_id"] = run_id
            sess["progress"] = {
                "status": "done",
                "done": len(results),
                "sent": len(results),
                "total": len(results),
                "error": "",
                "run_id": run_id,
            }
        except Exception as e:
            sess["progress"] = {
                "status": "error",
                "done": done_count,
                "sent": sent_count,
                "total": len(rows),
                "error": str(e),
            }

    threading.Thread(target=run_job, daemon=True).start()
    resp = JSONResponse({"ok": True, "started": True})
    return attach_session(resp, request, sid)


@app.post("/test", response_class=HTMLResponse)
async def test_one(
    request: Request,
    sid_token: Optional[str] = Form(None),
    provider: str = Form(...),
    model: Optional[str] = Form(None),
    api_key: str = Form(""),
    prompt_template: str = Form(...),
    json_mode: Optional[str] = Form(None),
    test_cols: Optional[List[str]] = Form(None),
    test_row_idx: Optional[int] = Form(None),
    send_model_params: Optional[str] = Form(None),
    enabled_params: Optional[List[str]] = Form(None),
    temperature: Optional[str] = Form(None),
    top_p: Optional[str] = Form(None),
    max_output_tokens: Optional[str] = Form(None),
    presence_penalty: Optional[str] = Form(None),
    frequency_penalty: Optional[str] = Form(None),
    seed: Optional[str] = Form(None),
    reasoning_effort: Optional[str] = Form(None),
):
    sid, sess = resolve_work(request, sid_token)
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Model is required.", status_code=400)
    if "rows" not in sess:
        return PlainTextResponse("Upload a CSV first.", status_code=400)
    rows = [r for r in sess["rows"] if not _row_is_empty_dict(r)]
    rows = [{k: _normalize_value(v) for k, v in row.items()} for row in rows]
    if not rows:
        return PlainTextResponse("No usable rows.", status_code=400)
    csv_cols = sess["csv_cols"]
    selected_cols = [c for c in (test_cols or []) if c in csv_cols]
    sess.update({
        "provider": provider,
        "model": model,
        "prompt_template": prompt_template,
        "json_mode": json_mode == "1",
        "test_cols": selected_cols,
        "send_model_params": send_model_params == "1",
        "enabled_params": enabled_params or [],
        "model_params_form": {
            "temperature": temperature or "",
            "top_p": top_p or "",
            "max_output_tokens": max_output_tokens or "",
            "presence_penalty": presence_penalty or "",
            "frequency_penalty": frequency_penalty or "",
            "seed": seed or "",
            "reasoning_effort": reasoning_effort or "",
        },
    })
    if not _prompt_has_placeholder(prompt_template, ["row_json"] + csv_cols):
        return PlainTextResponse("Prompt must include at least one column placeholder or {row_json}.", status_code=400)
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse("API key is required for OpenAI/LangCC/Gemini.", status_code=400)
    row_idx = test_row_idx if test_row_idx is not None and 0 <= test_row_idx < len(rows) else random.randrange(len(rows))
    sess["last_test_row_idx"] = row_idx
    row = rows[row_idx]
    model_params = parse_model_params(
        send_model_params, enabled_params, temperature, top_p, max_output_tokens,
        presence_penalty, frequency_penalty, seed, reasoning_effort,
    )
    err = ""
    raw_output = ""
    latency = 0
    try:
        t0 = time.time()
        text = call_provider(provider, api_key.strip(), model.strip(), render_prompt(prompt_template, row), json_mode == "1", model_params)
        latency = round(time.time() - t0, 3)
        storage.log_usage(request.state.user, provider, model, estimate_tokens(render_prompt(prompt_template, row)), estimate_tokens(text))
        raw_output = text or ""
    except Exception as e:
        err = str(e)
        raw_output = ""
    sess["last_test"] = {
        "row_idx": row_idx,
        "row": row,
        "output": raw_output,
        "prompt_template": prompt_template,
    }
    sess["test_fixer"] = _empty_test_fixer()
    test_saved_key = saved_key_for(request.state.user, provider)
    resp = render(
        request,
        "test.html",
        {
            "nav": "run",
            "user": request.state.user,
            "row_idx": row_idx,
            "total_rows": len(rows),
            "provider": provider,
            "model": model,
            "latency": latency,
            "err": err,
            "selected_cols": selected_cols,
            "row": row,
            "row_json": json.dumps(row, ensure_ascii=False, indent=2),
            "raw_output": raw_output,
            "prompt_template": prompt_template,
            "json_mode": json_mode == "1",
            "sid_token": work_serializer.dumps(sid),
            "saved_api_key": test_saved_key,
            "saved_key_exists": bool(test_saved_key),
            "accepted_name": sess.get("prompt_name") or "prompt",
        },
    )
    return attach_session(resp, request, sid)


@app.get("/export", response_class=HTMLResponse)
def export_page(
    request: Request,
    filter_key: Optional[List[str]] = Query(None),
    filter_val: Optional[List[str]] = Query(None),
):
    sid, sess = get_work_session(request)
    if "csv_cols" not in sess:
        resp = RedirectResponse(url="/", status_code=303)
        return attach_session(resp, request, sid)
    results = sess.get("results") or []
    detected_keys = sess.get("detected_json_keys") or []
    pairs = filter_pairs(filter_key, filter_val)
    filtered_indices = filter_results_by_json(results, [p["key"] for p in pairs], [p["val"] for p in pairs])
    choices = []
    csv_cols = sess["csv_cols"]
    for result_idx in filtered_indices[:200]:
        r = results[result_idx]
        label_parts = []
        for c in csv_cols[:3]:
            val = str(r.get(c, ""))[:60]
            if val:
                label_parts.append(f"{c}: {val}")
        choices.append({
            "idx": result_idx,
            "label": " | ".join(label_parts) or f"Row {result_idx + 1}",
            "failed": bool(r.get("llm_api_failed")),
            "output": r.get("llm_output", "") or "",
        })
    current_run_id = sess.get("current_run_id", "")
    current_run_name = (sess.get("run_name") or "").strip()
    current_run_text = f"Saved run: {current_run_name or current_run_id}" if current_run_id else "Unsaved current dataset"
    resp = render(
        request,
        "results.html",
        {
            "nav": "results",
            "user": request.state.user,
            "has_results": bool(results),
            "results": results,
            "csv_cols": csv_cols,
            "detected_keys": detected_keys,
            "filter_pairs": pairs,
            "filtered_count": len(filtered_indices),
            "result_choices": choices,
            "current_run_id": current_run_id,
            "current_run_text": current_run_text,
            "stats_keys": sess.get("stats_keys") or [],
        },
    )
    return attach_session(resp, request, sid)


@app.post("/download")
async def download(
    request: Request,
    out_csv_cols: Optional[List[str]] = Form(None),
    out_json_keys: Optional[List[str]] = Form(None),
    filter_key: Optional[List[str]] = Form(None),
    filter_val: Optional[List[str]] = Form(None),
    json_export_mode: str = Form("flatten"),
    flatten_sep: str = ".",
    out_prefix: str = Form("out_"),
):
    sid, sess = get_work_session(request)
    results = sess.get("results")
    if not results:
        return PlainTextResponse("No results to export. Run first.", status_code=400)
    if filter_key or filter_val:
        indices = filter_results_by_json(results, filter_key, filter_val)
        results = [results[i] for i in indices]
    csv_cols = sess.get("csv_cols", [])
    out_csv_cols = [c for c in (out_csv_cols or []) if c in csv_cols]
    out_df = _build_output_dataframe(
        results, csv_cols, out_csv_cols=out_csv_cols, out_json_keys=out_json_keys or [],
        json_export_mode=json_export_mode, flatten_sep=flatten_sep, out_prefix=out_prefix,
    )
    buf = io.StringIO()
    out_df.to_csv(buf, index=False, quoting=csv.QUOTE_ALL, lineterminator="\n")
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="output.csv"'},
    )


@app.post("/select-results")
async def select_results_for_new_set(request: Request, result_idx: Optional[List[int]] = Form(None)):
    sid, sess = get_work_session(request)
    results = sess.get("results") or []
    csv_cols = sess.get("csv_cols") or []
    if not results or not csv_cols:
        return PlainTextResponse("No results to select from.", status_code=400)
    selected = []
    for idx in result_idx or []:
        if 0 <= idx < len(results):
            selected.append(result_to_base_row(results[idx], csv_cols))
    if not selected:
        return PlainTextResponse("Choose at least one row.", status_code=400)
    df = pd.DataFrame(selected).reindex(columns=csv_cols)
    sess["csv_df"] = df
    sess["rows"] = df.to_dict(orient="records")
    for key in ("results", "detected_json_keys", "progress", "current_run_id", "last_test_row_idx"):
        sess.pop(key, None)
    resp = RedirectResponse(url="/", status_code=303)
    return attach_session(resp, request, sid)


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, q: str = ""):
    sid, _ = get_work_session(request)
    query = (q or "").strip().lower()
    rows = []
    for meta in storage.list_saved_runs(request.state.user):
        hay = " ".join([
            str(meta.get("run_name", "")),
            str(meta.get("run_id", "")),
            str(meta.get("prompt_name", "")),
            str(meta.get("provider", "")),
            str(meta.get("model", "")),
            str(meta.get("created_at", "")),
        ]).lower()
        if query and query not in hay:
            continue
        rows.append({
            **meta,
            "title": run_title(meta),
            "created": pretty_created(meta),
        })
    resp = render(
        request,
        "runs.html",
        {"nav": "history", "user": request.state.user, "runs": rows, "q": q},
    )
    return attach_session(resp, request, sid)


@app.post("/runs/{run_id}/open")
async def open_saved_run(request: Request, run_id: str):
    sid, sess = get_work_session(request)
    try:
        storage.load_run_into_session(request.state.user, run_id, sess)
    except PermissionError:
        return PlainTextResponse("Not your run.", status_code=403)
    except Exception as e:
        return PlainTextResponse(f"Failed to open run: {e}", status_code=404)
    resp = RedirectResponse(url="/export", status_code=303)
    return attach_session(resp, request, sid)


@app.get("/runs/{run_id}/download/{kind}")
def download_saved_run(request: Request, run_id: str, kind: str):
    try:
        path = storage.run_file(request.state.user, run_id, kind)
    except PermissionError:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    except ValueError:
        return JSONResponse({"error": "unknown file"}, status_code=404)
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    media = {
        "input": "text/csv",
        "output": "text/csv",
        "results": "application/json",
        "metadata": "application/json",
        "prompt": "text/plain",
        "review": "application/json",
    }.get(kind, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=f"{run_id}-{path.name}")


@app.post("/stats", response_class=HTMLResponse)
async def stats(
    request: Request,
    stats_keys: Optional[List[str]] = Form(None),
    stats_date_col: str = Form(""),
    stats_date_from: str = Form(""),
    stats_date_to: str = Form(""),
    stats_filter_key: str = Form(""),
    stats_filter_val: str = Form(""),
):
    sid, sess = get_work_session(request)
    results = sess.get("results") or []
    if not results:
        return PlainTextResponse("No results to compute stats. Run first.", status_code=400)
    selected_keys = stats_keys if stats_keys is not None else sess.get("stats_keys", [])
    sess["stats_keys"] = selected_keys
    csv_cols = sess.get("csv_cols") or []
    if stats_date_col not in csv_cols:
        stats_date_col = ""
    sess["stats_date_col"] = stats_date_col
    sess["stats_date_from"] = stats_date_from or ""
    sess["stats_date_to"] = stats_date_to or ""
    date_from = _parse_date(stats_date_from)
    date_to = _parse_date(stats_date_to)
    filtered_results = results
    filter_notes = []
    if stats_date_col and (date_from or date_to):
        tmp = []
        for r in results:
            raw_val = r.get(stats_date_col)
            if raw_val in (None, ""):
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
        filter_notes.append(f"Filtered by {stats_date_col}.")
    detected = sess.get("detected_json_keys") or []
    stats_filter_key = stats_filter_key if stats_filter_key in detected else ""
    stats_filter_val = (stats_filter_val or "").lower().strip()
    if stats_filter_key and stats_filter_val in ("true", "false"):
        target = stats_filter_val == "true"
        filtered_results = [
            r for r in filtered_results
            if _coerce_bool((r.get("_llm_json_flat", {}) or {}).get(stats_filter_key)) is target
        ]
        filter_notes.append(f"Filtered where {stats_filter_key} is {stats_filter_val}.")
    total_rows = len(filtered_results)
    stat_rows = []
    for key in selected_keys:
        true_count = sum(1 for r in filtered_results if _coerce_bool((r.get("_llm_json_flat", {}) or {}).get(key)) is True)
        false_count = total_rows - true_count
        stat_rows.append({
            "key": key,
            "true_count": true_count,
            "false_count": false_count,
            "true_pct": (true_count / total_rows * 100) if total_rows else 0,
            "false_pct": (false_count / total_rows * 100) if total_rows else 0,
        })
    resp = render(
        request,
        "stats.html",
        {
            "nav": "results",
            "user": request.state.user,
            "selected_keys": selected_keys,
            "csv_cols": csv_cols,
            "stats_date_col": stats_date_col,
            "stats_date_from": stats_date_from,
            "stats_date_to": stats_date_to,
            "stats_filter_key": stats_filter_key,
            "stat_rows": stat_rows,
            "total_rows": total_rows,
            "filter_note": " ".join(filter_notes) if filter_notes else "No filters applied.",
        },
    )
    return attach_session(resp, request, sid)


def _review_state(request: Request):
    sid, sess = get_work_session(request)
    run_id = sess.get("current_run_id")
    review = storage.load_review(request.state.user, run_id) if run_id else storage._empty_review()
    return sid, sess, run_id, review


@app.get("/review", response_class=HTMLResponse)
def review(
    request: Request,
    idx: int = 0,
    filter_key: Optional[List[str]] = Query(None),
    filter_val: Optional[List[str]] = Query(None),
):
    sid, sess, run_id, review_data = _review_state(request)
    results = sess.get("results") or []
    if not results:
        resp = RedirectResponse(url="/", status_code=303)
        return attach_session(resp, request, sid)
    csv_cols = sess.get("csv_cols") or []
    detected_keys = sess.get("detected_json_keys") or []
    pairs = filter_pairs(filter_key, filter_val)
    filtered_indices = filter_results_by_json(results, [p["key"] for p in pairs], [p["val"] for p in pairs])
    total = len(filtered_indices)
    row = None
    result_idx = None
    if total > 0:
        idx = max(0, min(idx, total - 1))
        result_idx = filtered_indices[idx]
        row = results[result_idx]
    pretty_text = ""
    err = ""
    if row is not None:
        text = row.get("llm_output", "") or ""
        parsed, _ = try_parse_json(text)
        pretty_text = json.dumps(parsed, ensure_ascii=False, indent=2) if parsed is not None else text
        err = row.get("llm_api_error", "") or row.get("llm_error", "") or ""
    note_rec = review_data.get("notes", {}).get(str(result_idx), {}) if result_idx is not None else {}
    noted_count = sum(
        1 for rec in (review_data.get("notes") or {}).values()
        if isinstance(rec, dict) and ((rec.get("verdict") or "").strip() or (rec.get("note") or "").strip())
    )
    fixer = review_data.get("fixer") or {}
    analysis = fixer.get("analysis")
    if isinstance(analysis, dict):
        analysis = json.dumps(analysis, ensure_ascii=False)
    critic = fixer.get("critic")
    if isinstance(critic, dict):
        critic = json.dumps(critic, ensure_ascii=False)
    qs = filter_qs(pairs)
    review_provider = sess.get("provider", "openai")
    review_saved_key = saved_key_for(request.state.user, review_provider)
    resp = render(
        request,
        "review.html",
        {
            "nav": "review",
            "user": request.state.user,
            "idx": idx,
            "prev_idx": max(0, idx - 1) if total else 0,
            "next_idx": min(total - 1, idx + 1) if total else 0,
            "display_index": idx + 1 if row is not None else 0,
            "total": total,
            "total_all": len(results),
            "filter_pairs": pairs,
            "filter_qs": qs,
            "detected_keys": detected_keys,
            "csv_cols": csv_cols,
            "row": row,
            "pretty_text": pretty_text,
            "err": err,
            "verdict": note_rec.get("verdict", ""),
            "note": note_rec.get("note", ""),
            "prompt_draft": review_data.get("prompt_draft") or sess.get("prompt_template") or "",
            "provider": review_provider,
            "model": sess.get("model", "gpt-5-mini"),
            "saved_api_key": review_saved_key,
            "saved_key_exists": bool(review_saved_key),
            "proposed_prompt": fixer.get("proposed_prompt") or "",
            "diff_lines": fixer.get("diff") or [],
            "analysis": analysis or "",
            "critic": critic or "",
            "fixer_status": fixer.get("error") or fixer.get("status") or "",
            "current_run_id": run_id or "",
            "result_idx": result_idx,
            "accepted_name": sess.get("prompt_name") or "prompt",
            "noted_count": noted_count,
            "fixer_fix_id": fixer.get("fix_id") or "",
            "prompt_name": sess.get("prompt_name") or "",
        },
    )
    return attach_session(resp, request, sid)


@app.post("/review/notes")
async def save_review_note(
    request: Request,
    row_idx: int = Form(...),
    verdict: str = Form(""),
    note: str = Form(""),
    prompt_draft: str = Form(""),
):
    sid, sess, run_id, review_data = _review_state(request)
    if not run_id:
        return PlainTextResponse("Open or finish a saved run first.", status_code=400)
    notes = review_data.setdefault("notes", {})
    notes[str(row_idx)] = {"verdict": verdict, "note": note}
    if prompt_draft:
        review_data["prompt_draft"] = prompt_draft
    storage.save_review(request.state.user, run_id, review_data)
    sess["prompt_template"] = review_data.get("prompt_draft") or sess.get("prompt_template", "")
    resp = JSONResponse({"ok": True})
    return attach_session(resp, request, sid)


@app.post("/review/improve")
async def start_prompt_improve(
    request: Request,
    provider: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    prompt_draft: str = Form(""),
):
    sid, sess, run_id, review_data = _review_state(request)
    if not run_id:
        return PlainTextResponse("Open or finish a saved run first.", status_code=400)
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse("API key is required to run the prompt fixer.", status_code=400)
    results = sess.get("results") or []
    examples = build_examples(results, sess.get("csv_cols") or [], review_data.get("notes") or {})
    if not examples:
        return PlainTextResponse("Add at least one note or verdict before improving the prompt.", status_code=400)
    prompt = prompt_draft or review_data.get("prompt_draft") or sess.get("prompt_template") or ""
    source_name = sess.get("prompt_name") or "prompt"
    prev_fixer = review_data.get("fixer") or {}
    if prev_fixer.get("status") == "done" and prev_fixer.get("fix_id"):
        storage.update_prompt_fix(prev_fixer.get("source_name") or source_name, prev_fixer.get("fix_id"), status="superseded")
    review_data["prompt_draft"] = prompt
    review_data["fixer"] = {
        "status": "running", "proposed_prompt": "", "diff": [], "analysis": "",
        "critic": "", "error": "", "fix_id": "", "source_name": source_name,
    }
    storage.save_review(request.state.user, run_id, review_data)
    user = request.state.user
    key_once = api_key.strip()

    def job():
        try:
            result = improve_prompt(provider, key_once, model, prompt, examples)
            fix_id = ""
            try:
                fix_id = storage.record_prompt_fix(
                    source_name=source_name,
                    user=user,
                    run_id=run_id,
                    flow="review",
                    examples=examples,
                    analysis=result["analysis"],
                    critic=result["critic"],
                    change_summary=(result.get("editor") or {}).get("change_summary") or [],
                    diff=result["diff"],
                    old_prompt=prompt,
                    new_prompt=result["proposed_prompt"],
                )
            except Exception:
                fix_id = ""
            current = storage.load_review(user, run_id)
            current["fixer"] = {
                "status": "done",
                "proposed_prompt": result["proposed_prompt"],
                "diff": result["diff"],
                "analysis": result["analysis"],
                "critic": result["critic"],
                "error": "",
                "fix_id": fix_id,
                "source_name": source_name,
            }
            storage.save_review(user, run_id, current)
        except Exception as e:
            current = storage.load_review(user, run_id)
            current["fixer"] = {
                "status": "error",
                "proposed_prompt": "",
                "diff": [],
                "analysis": "",
                "critic": "",
                "error": str(e),
                "fix_id": "",
                "source_name": source_name,
            }
            storage.save_review(user, run_id, current)

    threading.Thread(target=job, daemon=True).start()
    resp = JSONResponse({"ok": True, "status": "running"})
    return attach_session(resp, request, sid)


@app.get("/review/improve/status")
def prompt_improve_status(request: Request):
    sid, sess, run_id, review_data = _review_state(request)
    fixer = review_data.get("fixer") or {}
    status = fixer.get("status") or "idle"
    message = fixer.get("error") if status == "error" else {
        "idle": "Add notes, then run the fixer.",
        "running": "Analyst, editor, and critic are working…",
        "done": "Proposed update is ready.",
    }.get(status, status)
    resp = JSONResponse({"status": status, "message": message})
    return attach_session(resp, request, sid)


@app.post("/review/accept")
async def accept_prompt_version(request: Request, prompt_name: str = Form(...)):
    sid, sess, run_id, review_data = _review_state(request)
    fixer = review_data.get("fixer") or {}
    proposed = fixer.get("proposed_prompt") or ""
    if not proposed.strip():
        return PlainTextResponse("No proposed prompt to save. Run the fixer first.", status_code=400)
    try:
        version_name = storage.next_prompt_version(prompt_name)
        saved = storage.save_prompt(version_name, proposed)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    review_data["prompt_draft"] = proposed
    review_data["fixer"]["accepted_name"] = saved
    storage.save_review(request.state.user, run_id, review_data)
    if fixer.get("fix_id"):
        storage.update_prompt_fix(
            fixer.get("source_name") or prompt_name,
            fixer.get("fix_id"),
            status="accepted",
            new_version=saved,
        )
    sess["prompt_name"] = saved
    sess["prompt_template"] = proposed
    resp = JSONResponse({"ok": True, "name": saved})
    return attach_session(resp, request, sid)


@app.post("/review/discard")
async def discard_prompt_version(request: Request):
    sid, sess, run_id, review_data = _review_state(request)
    fixer = review_data.get("fixer") or {}
    if fixer.get("fix_id"):
        storage.update_prompt_fix(
            fixer.get("source_name") or sess.get("prompt_name") or "prompt",
            fixer.get("fix_id"),
            status="discarded",
        )
    review_data["fixer"] = storage._empty_review()["fixer"]
    if run_id:
        storage.save_review(request.state.user, run_id, review_data)
    resp = JSONResponse({"ok": True})
    return attach_session(resp, request, sid)


def _empty_test_fixer() -> Dict[str, Any]:
    return {
        "status": "idle", "proposed_prompt": "", "diff": [], "analysis": "",
        "critic": "", "error": "", "fix_id": "", "source_name": "",
    }


@app.post("/test/improve")
async def start_test_improve(
    request: Request,
    sid_token: Optional[str] = Form(None),
    provider: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    verdict: str = Form(""),
    note: str = Form(""),
    prompt_draft: str = Form(""),
):
    sid, sess = resolve_work(request, sid_token)
    last_test = sess.get("last_test")
    if not last_test:
        return PlainTextResponse("Run a test row first, then improve from it.", status_code=400)
    if not (note.strip() or verdict.strip()):
        return PlainTextResponse("Add a note or a verdict on this test row first.", status_code=400)
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse("API key is required to run the prompt fixer.", status_code=400)
    prompt = prompt_draft or last_test.get("prompt_template") or sess.get("prompt_template") or ""
    if not prompt.strip():
        return PlainTextResponse("There is no prompt to improve.", status_code=400)
    example = build_example(
        last_test.get("row") or {},
        sess.get("csv_cols") or [],
        last_test.get("output") or "",
        verdict,
        note,
        row_index=last_test.get("row_idx") or 0,
    )
    source_name = sess.get("prompt_name") or "prompt"
    prev = sess.get("test_fixer") or {}
    if prev.get("status") == "done" and prev.get("fix_id"):
        storage.update_prompt_fix(prev.get("source_name") or source_name, prev.get("fix_id"), status="superseded")
    sess["test_fixer"] = {**_empty_test_fixer(), "status": "running", "source_name": source_name}
    user = request.state.user
    key_once = api_key.strip()

    def job():
        try:
            result = improve_prompt(provider, key_once, model, prompt, [example])
            fix_id = ""
            try:
                fix_id = storage.record_prompt_fix(
                    source_name=source_name,
                    user=user,
                    run_id="",
                    flow="test",
                    examples=[example],
                    analysis=result["analysis"],
                    critic=result["critic"],
                    change_summary=(result.get("editor") or {}).get("change_summary") or [],
                    diff=result["diff"],
                    old_prompt=prompt,
                    new_prompt=result["proposed_prompt"],
                )
            except Exception:
                fix_id = ""
            sess["test_fixer"] = {
                "status": "done",
                "proposed_prompt": result["proposed_prompt"],
                "diff": result["diff"],
                "analysis": result["analysis"],
                "critic": result["critic"],
                "error": "",
                "fix_id": fix_id,
                "source_name": source_name,
            }
        except Exception as e:
            sess["test_fixer"] = {**_empty_test_fixer(), "status": "error", "error": str(e), "source_name": source_name}

    threading.Thread(target=job, daemon=True).start()
    resp = JSONResponse({"ok": True, "status": "running"})
    return attach_session(resp, request, sid)


def _fixer_diff_html(diff_lines: List[str]) -> str:
    parts = []
    for line in diff_lines or []:
        safe = (line or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if line.startswith("+") and not line.startswith("+++"):
            parts.append(f'<span class="add">{safe}</span>')
        elif line.startswith("-") and not line.startswith("---"):
            parts.append(f'<span class="del">{safe}</span>')
        else:
            parts.append(safe)
    return "\n".join(parts)


@app.get("/test/improve/status")
def test_improve_status(request: Request, sid_token: Optional[str] = None):
    sid, sess = resolve_work(request, sid_token)
    fixer = sess.get("test_fixer") or _empty_test_fixer()
    status = fixer.get("status") or "idle"
    analysis = fixer.get("analysis")
    if isinstance(analysis, dict):
        analysis = json.dumps(analysis, ensure_ascii=False)
    critic = fixer.get("critic")
    if isinstance(critic, dict):
        critic = json.dumps(critic, ensure_ascii=False)
    message = fixer.get("error") if status == "error" else {
        "idle": "Add a note, then run the fixer.",
        "running": "Analyst, editor, and critic are working…",
        "done": "Proposed update is ready.",
    }.get(status, status)
    payload = {
        "status": status,
        "message": message,
        "proposed": bool((fixer.get("proposed_prompt") or "").strip()),
        "diff_html": _fixer_diff_html(fixer.get("diff") or []) if status == "done" else "",
        "analysis": analysis or "",
        "critic": critic or "",
    }
    resp = JSONResponse(payload)
    return attach_session(resp, request, sid)


@app.post("/test/accept")
async def accept_test_version(request: Request, sid_token: Optional[str] = Form(None), prompt_name: str = Form(...)):
    sid, sess = resolve_work(request, sid_token)
    fixer = sess.get("test_fixer") or {}
    proposed = fixer.get("proposed_prompt") or ""
    if not proposed.strip():
        return PlainTextResponse("No proposed prompt to save. Run the fixer first.", status_code=400)
    try:
        version_name = storage.next_prompt_version(prompt_name)
        saved = storage.save_prompt(version_name, proposed)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    if fixer.get("fix_id"):
        storage.update_prompt_fix(
            fixer.get("source_name") or prompt_name,
            fixer.get("fix_id"),
            status="accepted",
            new_version=saved,
        )
    fixer["accepted_name"] = saved
    sess["prompt_name"] = saved
    sess["prompt_template"] = proposed
    resp = JSONResponse({"ok": True, "name": saved})
    return attach_session(resp, request, sid)


@app.post("/test/discard")
async def discard_test_version(request: Request, sid_token: Optional[str] = Form(None)):
    sid, sess = resolve_work(request, sid_token)
    fixer = sess.get("test_fixer") or {}
    if fixer.get("fix_id"):
        storage.update_prompt_fix(
            fixer.get("source_name") or sess.get("prompt_name") or "prompt",
            fixer.get("fix_id"),
            status="discarded",
        )
    sess["test_fixer"] = _empty_test_fixer()
    resp = JSONResponse({"ok": True})
    return attach_session(resp, request, sid)


@app.get("/prompts/raw")
def prompt_raw(request: Request, name: str):
    """Read-only view of a saved prompt version. Does not mutate the session."""
    try:
        content = storage.load_prompt(name)
        safe_name = storage._safe_prompt_name(name)
    except Exception as e:
        return PlainTextResponse(f"Failed to load prompt: {e}", status_code=400)
    return JSONResponse({"name": safe_name, "content": content})


@app.get("/prompts/history", response_class=HTMLResponse)
def prompts_history(request: Request, name: str = ""):
    sid, _ = get_work_session(request)
    name = (name or "").strip()
    if name:
        try:
            safe = storage._safe_prompt_name(name)
        except Exception:
            safe = ""
        fixes = storage.load_prompt_history(safe) if safe else []
        versions = storage.list_prompt_versions(safe) if safe else []
        for fix in fixes:
            fix["created"] = _iso_to_pretty(fix.get("ts", ""))
        resp = render(
            request,
            "prompt_history.html",
            {
                "nav": "prompts",
                "user": request.state.user,
                "detail": True,
                "prompt": storage.prompt_family(safe) if safe else name,
                "fixes": fixes,
                "versions": versions,
            },
        )
        return attach_session(resp, request, sid)
    summaries = storage.list_prompt_history()
    for item in summaries:
        item["last_pretty"] = _iso_to_pretty(item.get("last_ts", ""))
    resp = render(
        request,
        "prompt_history.html",
        {
            "nav": "prompts",
            "user": request.state.user,
            "detail": False,
            "summaries": summaries,
        },
    )
    return attach_session(resp, request, sid)


def _iso_to_pretty(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value or ""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
