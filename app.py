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
    _build_output_dataframe,
    compose_model_input,
    call_provider,
    mapping_gaps,
    required_placeholders,
    estimate_tokens,
    fetch_ollama_models,
    fetch_openai_compatible_models,
    filter_results_by_json,
    load_tabular_file,
    parse_model_params,
    process_row,
    provider_requires_key,
    result_to_base_row,
    row_input_error,
    try_parse_json,
)
import catalogue
import company_context
from prompt_fixer import build_example, build_examples, improve_prompt, unified_diff
from prompt_builder import (
    answers_from_form,
    sample_report,
    catalogue_chat_turn,
    check_availability,
    discover_plan,
    find_prompts_turn,
    generate_prompt,
    sample_rows,
    MAX_DEEP_TOTAL_QUESTIONS,
)

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
    "/upload",
    "/provider/models",
    "/ollama/models",
    "/credentials",
    "/credentials/forget",
    "/review/notes",
    "/review/prompt",
    "/review/improve",
    "/review/improve/status",
    "/review/recheck",
    "/review/recheck/status",
    "/review/accept",
    "/review/discard",
    "/test/improve",
    "/test/improve/status",
    "/test/accept",
    "/test/discard",
    "/prompts/raw",
    "/builder/discover",
    "/builder/deep-next",
    "/builder/availability",
    "/builder/generate",
    "/builder/test",
    "/builder/fix",
    "/builder/save",
    "/context/save",
    "/context/text",
    "/users/search",
    "/catalogue/create",
    "/catalogue/search",
    "/catalogue/find",
    "/mapping/save",
    "/settings/save",
    "/settings/keys",
    "/settings/keys/forget",
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


_STATIC_DIR = os.path.join(BASE_DIR, "static")


def asset_url(path: str) -> str:
    """Stamp a static file with its mtime.

    Without this, a browser keeps serving the stylesheet it cached before the
    last change, so a fix lands on the server and the page still looks broken
    until someone thinks to hard-refresh.
    """
    try:
        stamp = int(os.path.getmtime(os.path.join(_STATIC_DIR, path)))
    except OSError:
        stamp = 0
    return f"/static/{path}?v={stamp}"


def render(request: Request, name: str, context: Dict[str, Any], status_code: int = 200):
    ctx = dict(context)
    ctx.setdefault("asset", asset_url)
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
    if path.startswith("/catalogue/") and path != "/catalogue":
        return True
    if "/download" in path:
        return True
    if path == "/run" or path == "/run/cancel":
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


def saved_key_for(user: str, provider: str, key_id: str = "") -> str:
    if not provider_requires_key(provider):
        return ""
    return storage.resolve_user_key(user, provider, key_id) or ""


def resolve_ai(
    request: Request,
    tool: str,
    provider: str = "",
    model: str = "",
    api_key: str = "",
    key_id: str = "",
) -> tuple[str, str, str]:
    defaults = storage.tool_defaults(request.state.user, tool)
    name = (provider or defaults.get("provider") or "langcc").strip().lower()
    chosen_model = (model or defaults.get("model") or "").strip()
    key = (api_key or "").strip()
    if not key:
        key = saved_key_for(request.state.user, name, key_id or defaults.get("key_id") or "")
    return name, chosen_model, key


def settings_payload(user: str) -> Dict[str, Any]:
    settings = storage.load_user_settings(user)
    keys = storage.list_named_keys(user)
    return {"settings": settings, "keys": keys}


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
    user_settings = storage.load_user_settings(request.state.user)
    runner = storage.tool_defaults(request.state.user, "runner")
    current_provider = sess.get("provider") or runner.get("provider") or "langcc"
    current_model = sess.get("model") or runner.get("model") or "gpt-5-mini"
    current_key_id = sess.get("key_id") or runner.get("key_id") or user_settings.get("default_key_id") or ""
    saved_key = saved_key_for(request.state.user, current_provider, current_key_id)
    named_keys = storage.list_named_keys(request.state.user)
    resp = render(
        request,
        "home.html",
        {
            "nav": "run",
            "user": request.state.user,
            "has_csv": has_csv,
            "total_rows": total_rows,
            "csv_cols": sess.get("csv_cols") or [],
            "prompts": catalogue.list_visible(request.state.user),
            "prompt_id": sess.get("prompt_id", ""),
            "prompt_name": sess.get("prompt_name", ""),
            "last_prompt_id": user_settings.get("last_prompt_id") or "",
            "last_prompt_name": user_settings.get("last_prompt_name") or "",
            "named_keys": named_keys,
            "key_id": current_key_id,
            "column_map": sess.get("column_map") or {},
            "mapping_needed": mapping_gaps(
                required_placeholders(sess.get("prompt_template") or "", sess.get("input_template") or ""),
                sess.get("csv_cols") or [],
                sess.get("column_map") or {},
            ),
            "prompt_template": sess.get("prompt_template", "Given this row:\n{row_json}\n\nReturn JSON with keys: status, note."),
            "input_template": sess.get("input_template", ""),
            "run_name": sess.get("run_name", ""),
            "provider": current_provider,
            "model": current_model,
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
            # One real row, so the prompt preview shows what the model will
            # actually receive rather than a description of it.
            "sample_row": {
                str(k): (str(v)[:600] if v is not None else "")
                for k, v in (sess.get("rows") or [{}])[0].items()
            } if has_csv else {},
            "saved_key_exists": bool(saved_key),
            "upload_note": sess.get("upload_note") or "",
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
async def provider_models(request: Request, provider: str = Form(...), api_key: str = Form("")):
    p = provider.lower().strip()
    if not api_key.strip() and provider_requires_key(p):
        # The page no longer holds the key, so fall back to the account's saved
        # one; otherwise the model picker would break for everyone.
        api_key = saved_key_for(request.state.user, p) or ""
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
    prog = dict(sess.get("progress") or {"status": "idle", "done": 0, "sent": 0, "total": 0, "error": ""})
    stamped = prog.pop("updated_at", None)
    # Seconds since a row last finished. A frozen number on the page means
    # nothing on its own -- this is what says whether the server is still
    # working or the run has actually wedged.
    prog["idle_seconds"] = round(time.time() - stamped) if stamped else None
    prog["run_id"] = prog.get("run_id") or sess.get("current_run_id") or ""
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
    input_template: Optional[str] = Form(None),
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
    if input_template is not None:
        sess["input_template"] = input_template
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
    # Only whether a key exists. The value used to be returned here and written
    # straight into a form field, which put the decrypted provider key in the
    # DOM of every page and in every request the page made afterwards. Routes
    # that need it read it server-side.
    return JSONResponse({"provider": name, "api_key": "", "saved": bool(key), "needed": True})


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



def _scope_column_map(sess: Dict[str, Any], prompt_key: str, prompt: str, input_template: str) -> Dict[str, str]:
    """Keep only the field matches the prompt now on screen actually uses.

    The map used to live on the session and never be cleared, so every prompt
    you had loaded that day left its fields behind: opening a prompt that needs
    only {FULL_CONVERSATION} still showed {CHAT_TRANSCRIPT} from an earlier one.
    A stale alias is worse than untidy -- apply_column_map writes it over the
    row, so a leftover entry naming a column the new sheet really has would
    silently replace that column's value.

    Each prompt's matches are remembered separately, so switching back to one
    you mapped earlier restores it instead of asking again.
    """
    store = sess.setdefault("column_maps", {})
    active = sess.get("column_map") or {}
    previous_key = sess.get("column_map_key") or ""
    if previous_key and active:
        store[previous_key] = dict(active)

    wanted = set(required_placeholders(prompt or "", input_template or ""))
    remembered = store.get(prompt_key) or {}
    # Anything the previous prompt shared with this one carries over, so a
    # re-load of the same prompt does not lose the matches you just made.
    merged = {**{k: v for k, v in active.items() if k in wanted}, **remembered}
    scoped = {k: v for k, v in merged.items() if k in wanted}

    sess["column_map"] = scoped
    sess["column_map_key"] = prompt_key
    if prompt_key:
        store[prompt_key] = dict(scoped)
    return scoped


@app.get("/prompts/get")
def get_prompt(request: Request, name: str = "", id: str = ""):
    sid, sess = get_work_session(request)
    user = request.state.user
    prompt_id = (id or name or "").strip()
    try:
        item = catalogue.get_visible(prompt_id, user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception:
        try:
            content = storage.load_prompt(name)
            item = {
                "id": "",
                "name": storage._safe_prompt_name(name),
                "prompt": content,
                "input_template": "",
                "required_inputs": required_placeholders(content, ""),
            }
        except Exception as e:
            return PlainTextResponse(f"Failed to load prompt: {e}", status_code=400)
    sess["prompt_id"] = item.get("id") or ""
    sess["prompt_name"] = item.get("name") or ""
    sess["prompt_template"] = item.get("prompt") or ""
    sess["input_template"] = item.get("input_template") or ""
    if item.get("id"):
        storage.remember_last_prompt(user, item.get("id") or "", item.get("name") or "")
    column_map = _scope_column_map(
        sess,
        item.get("id") or item.get("name") or "",
        item.get("prompt") or "",
        item.get("input_template") or "",
    )
    gaps = mapping_gaps(
        required_placeholders(item.get("prompt") or "", item.get("input_template") or ""),
        sess.get("csv_cols") or [],
        column_map,
    )
    resp = JSONResponse({
        "id": item.get("id") or "",
        "name": item.get("name") or "",
        "content": item.get("prompt") or "",
        "input_template": item.get("input_template") or "",
        "required_inputs": required_placeholders(item.get("prompt") or "", item.get("input_template") or ""),
        "mapping_needed": gaps,
        "column_map": column_map,
        "columns": sess.get("csv_cols") or [],
    })
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


def _store_uploaded_frame(sess: Dict[str, Any], df: pd.DataFrame, meta: Optional[Dict[str, Any]] = None) -> None:
    sess["csv_df"] = df
    sess["csv_cols"] = list(df.columns)
    sess["rows"] = df.to_dict(orient="records")
    sess["upload_note"] = (meta or {}).get("note") or ""
    sess["upload_source"] = (meta or {}).get("source") or "csv"
    sess["upload_sheet"] = (meta or {}).get("used_sheet") or ""
    for key in ("results", "detected_json_keys", "progress", "current_run_id", "last_test_row_idx"):
        sess.pop(key, None)


def _safe_next_path(next_path: Optional[str]) -> str:
    allowed = {"/", "/builder"}
    path = (next_path or "/").strip() or "/"
    return path if path in allowed else "/"


def _dataset_payload(sess: Dict[str, Any]) -> Dict[str, Any]:
    """What the page needs to show a newly loaded sheet without reloading."""
    rows = sess.get("rows") or []
    cols = sess.get("csv_cols") or []
    first = rows[0] if rows else {}
    return {
        "ok": True,
        "rows": len(rows),
        "columns": cols,
        "note": sess.get("upload_note") or "",
        "sheet": sess.get("upload_sheet") or "",
        "sample_row": {str(k): (str(v)[:600] if v is not None else "") for k, v in first.items()},
        "column_map": sess.get("column_map") or {},
        "mapping_needed": mapping_gaps(
            required_placeholders(sess.get("prompt_template") or "", sess.get("input_template") or ""),
            cols,
            sess.get("column_map") or {},
        ),
    }


@app.post("/upload")
async def upload(
    request: Request,
    csv_file: UploadFile = File(...),
    next: Optional[str] = Form(None),
    json_response: Optional[str] = Form(None),
):
    sid, sess = get_work_session(request)
    raw = await csv_file.read()
    filename = csv_file.filename or ""
    wants_json = json_response == "1"
    try:
        df, meta = load_tabular_file(raw, filename)
    except ValueError as e:
        if wants_json:
            return JSONResponse({"error": str(e)}, status_code=400)
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        message = f"Could not read this file: {e}"
        if wants_json:
            return JSONResponse({"error": message}, status_code=400)
        return PlainTextResponse(message, status_code=400)
    _store_uploaded_frame(sess, df, meta)

    # A new sheet can invalidate matches made against the old one.
    columns = set(sess.get("csv_cols") or [])
    kept = {k: v for k, v in (sess.get("column_map") or {}).items() if v in columns}
    sess["column_map"] = kept
    for key, saved in (sess.get("column_maps") or {}).items():
        sess["column_maps"][key] = {k: v for k, v in saved.items() if v in columns}

    if wants_json:
        # Returning data instead of a redirect is what lets the page keep the
        # prompt someone is halfway through typing.
        resp = JSONResponse(_dataset_payload(sess))
        return attach_session(resp, request, sid)
    resp = RedirectResponse(url=_safe_next_path(next), status_code=303)
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
    key_id: str = Form(""),
    prompt_template: str = Form(...),
    input_template: str = Form(""),
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
    missing_input = row_input_error(prompt_template, input_template, csv_cols, sess.get("column_map") or {})
    if missing_input:
        return PlainTextResponse(missing_input, status_code=400)
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
    sess["input_template"] = input_template or ""
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
    if not api_key.strip():
        _p, _m, api_key = resolve_ai(request, "runner", provider=provider, model=model or "", api_key=api_key, key_id=key_id)
        sess["key_id"] = key_id
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse("API key is required for OpenAI/LangCC/Gemini. Save one in Settings.", status_code=400)

    is_json_mode = json_mode == "1"
    model_params = parse_model_params(
        send_model_params, enabled_params, temperature, top_p, max_output_tokens,
        presence_penalty, frequency_penalty, seed, reasoning_effort,
    )
    sess["progress"] = {
        "status": "running", "done": 0, "sent": 0, "total": len(rows), "error": "",
        "updated_at": time.time(),
    }
    # A batch is the expensive thing this tool does. Without a stop, a prompt
    # aimed at the wrong column had to be ridden out or the server restarted.
    sess["cancel"] = False
    key_once = api_key.strip()

    def cancelled() -> bool:
        return bool(sess.get("cancel"))

    def work_one(idx: int, row: Dict[str, Any]):
        out, row_keys, prompt, text = process_row(
            row, csv_cols, prompt_template, provider, key_once, model, is_json_mode, model_params,
            input_template, sess.get("column_map") or {},
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
            if not q or cancelled():
                return
            idx, r = q.popleft()
            fut = exec_obj.submit(work_one, idx, r)
            fset.add(fut)
            mapping[fut] = idx
            sent_count += 1
            sess["progress"]["sent"] = sent_count
            sess["progress"]["updated_at"] = time.time()

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
                        sess["progress"]["updated_at"] = time.time()
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
            if cancelled():
                # Keep whatever finished; a partial pass is still worth reading.
                finished = [r for r in results if r is not None]
                sess["results"] = finished
                sess["detected_json_keys"] = sorted(detected_keys)
                sess["progress"] = {
                    "status": "cancelled",
                    "done": done_count,
                    "sent": sent_count,
                    "total": len(rows),
                    "error": "",
                    "message": f"Stopped after {len(finished)} of {len(rows)} rows. Nothing was archived.",
                    "updated_at": time.time(),
                }
                return
            sess["results"] = results
            sess["detected_json_keys"] = sorted(detected_keys)
            run_id = storage.save_run_archive(
                user, sess, rows, results, sorted(detected_keys),
                provider, model, run_name, worker_count, model_params, prompt_template,
                input_template,
            )
            sess["current_run_id"] = run_id
            sess["progress"] = {
                "status": "done",
                "done": len(results),
                "sent": len(results),
                "total": len(results),
                "error": "",
                "run_id": run_id,
                "updated_at": time.time(),
            }
        except Exception as e:
            sess["progress"] = {
                "status": "error",
                "done": done_count,
                "sent": sent_count,
                "total": len(rows),
                "error": str(e),
                "updated_at": time.time(),
            }

    threading.Thread(target=run_job, daemon=True).start()
    resp = JSONResponse({"ok": True, "started": True})
    return attach_session(resp, request, sid)


@app.post("/run/cancel")
async def cancel_run(request: Request, sid_token: Optional[str] = Form(None)):
    sid, sess = resolve_work(request, sid_token)
    progress = sess.get("progress") or {}
    if progress.get("status") != "running":
        return JSONResponse({"ok": True, "cancelled": False, "status": progress.get("status") or "idle"})
    sess["cancel"] = True
    # Rows already in flight finish; nothing further is dispatched.
    resp = JSONResponse({"ok": True, "cancelled": True})
    return attach_session(resp, request, sid)


@app.post("/test", response_class=HTMLResponse)
async def test_one(
    request: Request,
    sid_token: Optional[str] = Form(None),
    provider: str = Form(...),
    model: Optional[str] = Form(None),
    api_key: str = Form(""),
    key_id: str = Form(""),
    prompt_template: str = Form(...),
    input_template: str = Form(""),
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
        "input_template": input_template or "",
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
    missing_input = row_input_error(prompt_template, input_template, csv_cols, sess.get("column_map") or {})
    if missing_input:
        return PlainTextResponse(missing_input, status_code=400)
    if not api_key.strip():
        _p, _m, api_key = resolve_ai(request, "runner", provider=provider, model=model or "", api_key=api_key, key_id=key_id)
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse("API key is required for OpenAI/LangCC/Gemini. Save one in Settings.", status_code=400)
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
        model_text = compose_model_input(prompt_template, row, input_template, column_map=sess.get("column_map") or {})
        text = call_provider(provider, api_key.strip(), model.strip(), model_text, json_mode == "1", model_params)
        latency = round(time.time() - t0, 3)
        storage.log_usage(request.state.user, provider, model, estimate_tokens(model_text), estimate_tokens(text))
        raw_output = text or ""
    except Exception as e:
        err = str(e)
        raw_output = ""
    sess["last_test"] = {
        "row_idx": row_idx,
        "row": row,
        "output": raw_output,
        "prompt_template": prompt_template,
        "input_template": input_template or "",
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
            "input_template": input_template or "",
            "json_mode": json_mode == "1",
            "sid_token": work_serializer.dumps(sid),
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
        "input_template": "text/plain",
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

    # A sidebar of every row in the filter, so walking 50 rows is 50 clicks
    # rather than 50 guesses at what is left to check.
    notes_map = review_data.get("notes") or {}
    row_links = []
    for pos, ridx in enumerate(filtered_indices):
        rec = notes_map.get(str(ridx)) or {}
        label = ""
        for col in csv_cols:
            value = str((results[ridx] or {}).get(col, "") or "").strip()
            if value:
                label = value[:46]
                break
        row_links.append({
            "pos": pos,
            "idx": ridx,
            "verdict": (rec.get("verdict") or ""),
            "noted": bool((rec.get("note") or "").strip()),
            "label": label or f"Row {ridx + 1}",
            "current": pos == idx,
        })

    # The analyst JSON rendered as structured patterns instead of a raw dump.
    raw_analysis = fixer.get("analysis")
    patterns = []
    do_not_change = []
    if isinstance(raw_analysis, dict):
        for item in raw_analysis.get("patterns") or []:
            if not isinstance(item, dict):
                continue
            rows_cited = [r for r in (item.get("evidence_rows") or []) if isinstance(r, int)]
            patterns.append({
                "title": str(item.get("title") or "Change"),
                "problem": str(item.get("problem") or ""),
                "needed_change": str(item.get("needed_change") or ""),
                "prompt_section": str(item.get("prompt_section") or ""),
                "rows": rows_cited,
                "thin": len(rows_cited) <= 1 and noted_count >= 4,
            })
        do_not_change = [str(x) for x in (raw_analysis.get("do_not_change") or []) if str(x).strip()]

    raw_critic = fixer.get("critic")
    rejected, risks = [], []
    if isinstance(raw_critic, dict):
        rejected = [str(x) for x in (raw_critic.get("rejected_edits") or []) if str(x).strip()]
        risks = [str(x) for x in (raw_critic.get("risks") or []) if str(x).strip()]

    def _diff_rows(lines):
        out = []
        for line in lines or []:
            text = str(line)
            if text.startswith("+++") or text.startswith("---") or text.startswith("@@"):
                kind, gut = "meta", ""
            elif text.startswith("+"):
                kind, gut = "add", "+"
            elif text.startswith("-"):
                kind, gut = "del", "-"
            else:
                kind, gut = "same", ""
            out.append({"kind": kind, "gut": gut, "text": text[1:] if kind in ("add", "del") else text})
        return out

    recheck = review_data.get("recheck") or {}
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
            "input_template": review_data.get("input_template") or sess.get("input_template") or "",
            "provider": review_provider,
            "model": sess.get("model", "gpt-5-mini"),
            "saved_key_exists": bool(review_saved_key),
            "proposed_prompt": fixer.get("proposed_prompt") or "",
            "diff_lines": fixer.get("diff") or [],
            "analysis": analysis or "",
            "critic": critic or "",
            "fixer_status": fixer.get("error") or fixer.get("status") or "",
            "fixer_message": (
                fixer.get("error")
                or {
                    "running": "Working through your notes…",
                    "done": (
                        "The fixer ran but did not change anything."
                        if fixer.get("unchanged")
                        else "A change is proposed below."
                    ),
                }.get(fixer.get("status") or "", "")
            ),
            "current_run_id": run_id or "",
            "result_idx": result_idx,
            "accepted_name": sess.get("prompt_name") or "prompt",
            "noted_count": noted_count,
            "fixer_fix_id": fixer.get("fix_id") or "",
            "prompt_name": sess.get("prompt_name") or "",
            "row_links": row_links,
            "patterns": patterns,
            "do_not_change": do_not_change,
            "rejected_edits": rejected,
            "risks": risks,
            "diff_rows": _diff_rows(fixer.get("diff") or []),
            "fixer_warnings": fixer.get("warnings") or [],
            "fixer_unchanged": bool(fixer.get("unchanged")),
            "examples_used": fixer.get("examples_used") or 0,
            "recheck": recheck,
            "recheck_rows": recheck.get("rows") or [],
            "recheck_summary": recheck.get("summary") or {},
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
    if not api_key.strip():
        _p, _m, api_key = resolve_ai(request, "fixer", provider=provider, model=model, api_key=api_key)
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse(
            "No API key for this provider. Save one in Settings, or paste one here.", status_code=400
        )
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
    review_data["recheck"] = {"status": "idle", "rows": [], "summary": {}, "error": "", "prompt": ""}
    review_data["fixer"] = {
        "status": "running", "proposed_prompt": "", "diff": [], "analysis": "",
        "critic": "", "error": "", "fix_id": "", "source_name": source_name,
    }
    storage.save_review(request.state.user, run_id, review_data)
    user = request.state.user
    key_once = api_key.strip()

    def job():
        try:
            prior = []
            purpose = ""
            if sess.get("prompt_id"):
                ctx = catalogue.change_context(sess.get("prompt_id"))
                prior = ctx.get("change_log") or []
                purpose = ctx.get("purpose") or ""
            result = improve_prompt(
                provider, key_once, model, prompt, examples,
                input_template=sess.get("input_template") or review_data.get("input_template") or "",
                prior_changes=prior,
                purpose=purpose,
            )
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
                "unchanged": bool(result.get("unchanged")),
                "warnings": result.get("warnings") or [],
                "examples_used": result.get("examples_used") or 0,
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
    done_message = (
        "The fixer ran but did not change the prompt."
        if fixer.get("unchanged")
        else "Proposed update is ready."
    )
    message = fixer.get("error") if status == "error" else {
        "idle": "Add notes, then run the fixer.",
        "running": "Analyst, editor, and critic are working…",
        "done": done_message,
    }.get(status, status)
    resp = JSONResponse({
        "status": status,
        "message": message,
        "unchanged": bool(fixer.get("unchanged")),
        "warnings": fixer.get("warnings") or [],
    })
    return attach_session(resp, request, sid)


RECHECK_MAX_ROWS = 30


def _noted_row_indexes(notes: Dict[str, Any], total: int) -> List[int]:
    out = []
    for key, note in (notes or {}).items():
        try:
            idx = int(key)
        except Exception:
            continue
        if not (0 <= idx < total):
            continue
        if (note or {}).get("verdict") or ((note or {}).get("note") or "").strip():
            out.append(idx)
    return sorted(out)


@app.post("/review/recheck")
async def start_review_recheck(
    request: Request,
    provider: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
):
    """Run the proposed prompt over the rows you reviewed and show what moved.

    A diff says what the wording became. It cannot say whether the fix repaired
    the rows you called wrong, or quietly changed rows you had already called
    ok. Re-running both kinds is the only thing that answers that.
    """
    sid, sess, run_id, review_data = _review_state(request)
    if not run_id:
        return PlainTextResponse("Open or finish a saved run first.", status_code=400)
    fixer = review_data.get("fixer") or {}
    proposed = (fixer.get("proposed_prompt") or "").strip()
    if not proposed:
        return PlainTextResponse("Improve the prompt first, then re-check the rows.", status_code=400)
    if fixer.get("unchanged"):
        return PlainTextResponse("The prompt did not change, so there is nothing to re-check.", status_code=400)
    results = sess.get("results") or []
    picked = _noted_row_indexes(review_data.get("notes") or {}, len(results))
    if not picked:
        return PlainTextResponse("Mark a few rows first — those are the rows to re-check.", status_code=400)
    if not api_key.strip():
        _p, _m, api_key = resolve_ai(request, "fixer", provider=provider, model=model, api_key=api_key)
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse(
            "No API key for this provider. Save one in Settings, or paste one here.", status_code=400
        )

    trimmed = picked[:RECHECK_MAX_ROWS]
    csv_cols = sess.get("csv_cols") or []
    input_template = sess.get("input_template") or review_data.get("input_template") or ""
    column_map = sess.get("column_map") or {}
    json_mode = bool(sess.get("json_mode", True))
    notes = review_data.get("notes") or {}
    user = request.state.user
    key_once = api_key.strip()

    review_data["recheck"] = {
        "status": "running", "rows": [], "summary": {}, "error": "", "prompt": proposed,
    }
    storage.save_review(user, run_id, review_data)

    def job():
        rows_out: List[Dict[str, Any]] = []

        def one(idx: int) -> Dict[str, Any]:
            row = results[idx]
            note = notes.get(str(idx)) or {}
            base = {c: row.get(c, "") for c in csv_cols}
            text = ""
            err = ""
            try:
                composed = compose_model_input(proposed, base, input_template, column_map=column_map)
                text = call_provider(provider, key_once, model, composed, json_mode) or ""
                storage.log_usage(user, provider, model, estimate_tokens(composed), estimate_tokens(text))
            except Exception as e:
                err = str(e)
            before = str(row.get("llm_output", "") or "")
            return {
                "row": idx,
                "verdict": (note.get("verdict") or ""),
                "note": (note.get("note") or ""),
                "input": " | ".join(
                    f"{c}: {str(base.get(c, ''))[:160]}" for c in csv_cols[:4] if base.get(c)
                ),
                "before": before,
                "after": text,
                "error": err,
                "changed": bool(text) and text.strip() != before.strip(),
            }

        try:
            with ThreadPoolExecutor(max_workers=min(8, len(trimmed))) as pool:
                for res in pool.map(one, trimmed):
                    rows_out.append(res)
            rows_out.sort(key=lambda r: r["row"])
            fixed = [r for r in rows_out if r["verdict"] in ("wrong", "unclear") and r["changed"]]
            still = [r for r in rows_out if r["verdict"] in ("wrong", "unclear") and not r["changed"]]
            moved_ok = [r for r in rows_out if r["verdict"] == "ok" and r["changed"]]
            failed = [r for r in rows_out if r["error"]]
            current = storage.load_review(user, run_id)
            current["recheck"] = {
                "status": "done",
                "rows": rows_out,
                "prompt": proposed,
                "error": "",
                "summary": {
                    "checked": len(rows_out),
                    "skipped": max(0, len(picked) - len(trimmed)),
                    "targets": len(fixed) + len(still),
                    "moved": len(fixed),
                    "unmoved": len(still),
                    "regressions": len(moved_ok),
                    "failed": len(failed),
                },
            }
            storage.save_review(user, run_id, current)
        except Exception as e:
            current = storage.load_review(user, run_id)
            current["recheck"] = {
                "status": "error", "rows": [], "summary": {}, "error": str(e), "prompt": proposed,
            }
            storage.save_review(user, run_id, current)

    threading.Thread(target=job, daemon=True).start()
    resp = JSONResponse({"ok": True, "status": "running", "rows": len(trimmed), "skipped": max(0, len(picked) - len(trimmed))})
    return attach_session(resp, request, sid)


@app.get("/review/recheck/status")
def review_recheck_status(request: Request):
    sid, sess, run_id, review_data = _review_state(request)
    check = review_data.get("recheck") or {}
    status = check.get("status") or "idle"
    summary = check.get("summary") or {}
    if status == "error":
        message = check.get("error") or "The re-check failed."
    elif status == "running":
        message = "Running the proposed prompt on the rows you reviewed…"
    elif status == "done":
        bits = [f"{summary.get('moved', 0)} of {summary.get('targets', 0)} flagged rows answered differently"]
        if summary.get("regressions"):
            bits.append(f"{summary['regressions']} row(s) you marked ok also changed")
        else:
            bits.append("no rows you marked ok changed")
        if summary.get("failed"):
            bits.append(f"{summary['failed']} call(s) failed")
        if summary.get("skipped"):
            bits.append(f"{summary['skipped']} row(s) not checked (limit {RECHECK_MAX_ROWS})")
        message = " · ".join(bits)
    else:
        message = ""
    resp = JSONResponse({
        "status": status,
        "message": message,
        "summary": summary,
        "rows": check.get("rows") or [],
    })
    return attach_session(resp, request, sid)


def _publish_accepted_fix(
    user: str,
    sess: Dict[str, Any],
    fixer: Dict[str, Any],
    prompt_name: str,
    proposed: str,
    version_name: str,
    origin: str,
) -> Dict[str, Any]:
    """Write an accepted fix somewhere a colleague can actually load it.

    The .txt version keeps Fix history and the version list working; the
    catalogue entry is what the New-run picker and the Catalogue page read.
    Writing only the first is how agent_eval.v2 ended up on disk and nowhere
    in the product.
    """
    bits = (fixer.get("editor") or {}).get("change_summary") if isinstance(fixer.get("editor"), dict) else None
    summary = "; ".join(str(x) for x in (bits or []) if str(x).strip()) or f"Accepted a prompt fix from {origin}."
    result = {"catalogue_id": "", "catalogue_name": "", "catalogue_mode": "", "catalogue_error": ""}
    try:
        published = catalogue.publish_version(
            sess.get("prompt_id") or "",
            user,
            version_name,
            proposed,
            input_template=sess.get("input_template") or None,
            summary=summary,
        )
        result["catalogue_id"] = published.get("published_to") or ""
        result["catalogue_name"] = published.get("name") or version_name
        result["catalogue_mode"] = published.get("mode") or ""
        if result["catalogue_id"]:
            sess["prompt_id"] = result["catalogue_id"]
            storage.remember_last_prompt(user, result["catalogue_id"], result["catalogue_name"])
    except Exception as e:
        result["catalogue_error"] = str(e)
    return result


@app.post("/review/accept")
async def accept_prompt_version(request: Request, prompt_name: str = Form(...)):
    sid, sess, run_id, review_data = _review_state(request)
    fixer = review_data.get("fixer") or {}
    proposed = fixer.get("proposed_prompt") or ""
    if not proposed.strip():
        return PlainTextResponse("No proposed prompt to save. Run the fixer first.", status_code=400)
    if proposed.strip() == (review_data.get("prompt_draft") or sess.get("prompt_template") or "").strip():
        return PlainTextResponse(
            "The fixer did not change the prompt, so there is no new version to save.",
            status_code=400,
        )
    try:
        version_name = storage.next_prompt_version(prompt_name)
        saved = storage.save_prompt(version_name, proposed)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    published = _publish_accepted_fix(
        request.state.user, sess, fixer, prompt_name, proposed, saved, "review"
    )
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
    sess["prompt_name"] = published.get("catalogue_name") or saved
    sess["prompt_template"] = proposed
    resp = JSONResponse({"ok": True, "name": saved, **published})
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
        "unchanged": False, "warnings": [], "examples_used": 0,
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
    if not api_key.strip():
        _p, _m, api_key = resolve_ai(request, "fixer", provider=provider, model=model, api_key=api_key)
    if provider_requires_key(provider) and not api_key.strip():
        return PlainTextResponse(
            "No API key for this provider. Save one in Settings, or paste one here.", status_code=400
        )
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
            prior = []
            purpose = ""
            if sess.get("prompt_id"):
                ctx = catalogue.change_context(sess.get("prompt_id"))
                prior = ctx.get("change_log") or []
                purpose = ctx.get("purpose") or ""
            result = improve_prompt(
                provider, key_once, model, prompt, [example],
                input_template=last_test.get("input_template") or sess.get("input_template") or "",
                prior_changes=prior,
                purpose=purpose,
            )
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
                "unchanged": bool(result.get("unchanged")),
                "warnings": result.get("warnings") or [],
                "examples_used": result.get("examples_used") or 0,
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
        "done": (
            "The fixer ran but did not change the prompt."
            if fixer.get("unchanged")
            else "Proposed update is ready."
        ),
    }.get(status, status)
    payload = {
        "status": status,
        "message": message,
        "unchanged": bool(fixer.get("unchanged")),
        "warnings": fixer.get("warnings") or [],
        "proposed": bool((fixer.get("proposed_prompt") or "").strip()) and not fixer.get("unchanged"),
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
    if fixer.get("unchanged"):
        return PlainTextResponse(
            "The fixer did not change the prompt, so there is no new version to save.",
            status_code=400,
        )
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
    published = _publish_accepted_fix(
        request.state.user, sess, fixer, prompt_name, proposed, saved, "a test row"
    )
    fixer["accepted_name"] = saved
    sess["prompt_name"] = published.get("catalogue_name") or saved
    sess["prompt_template"] = proposed
    resp = JSONResponse({"ok": True, "name": saved, **published})
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


def _builder_state(sess: Dict[str, Any]) -> Dict[str, Any]:
    state = sess.get("builder")
    if not isinstance(state, dict):
        state = {}
        sess["builder"] = state
    state.setdefault("goal", "")
    state.setdefault("content_col", "")
    state.setdefault("meanings", "")
    state.setdefault("plan", {})
    state.setdefault("questions", [])
    state.setdefault("question_log", [])
    state.setdefault("discover_mode", "quick")
    state.setdefault("answers", {})
    state.setdefault("prompt_name", "")
    state.setdefault("prompt", "")
    state.setdefault("input_template", "")
    state.setdefault("json_mode", True)
    state.setdefault("summary", "")
    state.setdefault("change_log", [])
    return state


def _builder_key(request: Request, provider: str, api_key: str, key_id: str = "", tool: str = "builder") -> str:
    _provider, _model, key = resolve_ai(request, tool, provider=provider, api_key=api_key, key_id=key_id)
    return key


@app.get("/context", response_class=HTMLResponse)
def context_page(request: Request):
    sid, _ = get_work_session(request)
    text = company_context.load_company_context()
    resp = render(
        request,
        "company_context.html",
        {
            "nav": "context",
            "user": request.state.user,
            "context_text": text,
            "context_chars": len(text),
        },
    )
    return attach_session(resp, request, sid)


@app.get("/context/text")
def context_text(request: Request):
    text = company_context.load_company_context()
    return JSONResponse({"text": text, "chars": len(text)})


@app.post("/context/save")
async def context_save(request: Request, content: str = Form("")):
    try:
        stored = company_context.save_company_context(content)
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        return PlainTextResponse(f"Could not save company info: {e}", status_code=500)
    return JSONResponse({
        "ok": True,
        "chars": len(stored),
        "message": "Saved. The prompt helper and the fixer will use this on the next call.",
    })


@app.get("/builder", response_class=HTMLResponse)
def builder_page(request: Request):
    sid, sess = get_work_session(request)
    has_csv = "csv_cols" in sess
    total_rows = len(sess.get("rows", [])) if has_csv else 0
    last_test = sess.get("last_test_row_idx")
    if not (has_csv and isinstance(last_test, int) and 0 <= last_test < total_rows):
        last_test = None
    builder_defaults = storage.tool_defaults(request.state.user, "builder")
    current_provider = sess.get("provider") or builder_defaults.get("provider") or "langcc"
    current_model = sess.get("model") or builder_defaults.get("model") or "gpt-5-mini"
    saved_key = saved_key_for(request.state.user, current_provider, builder_defaults.get("key_id") or "")
    state = _builder_state(sess)
    last_builder_test = sess.get("builder_test") or {}
    resp = render(
        request,
        "builder.html",
        {
            "nav": "builder",
            "user": request.state.user,
            "has_csv": has_csv,
            "total_rows": total_rows,
            "csv_cols": sess.get("csv_cols") or [],
            "upload_note": sess.get("upload_note") or "",
            "provider": current_provider,
            "model": current_model,
            "saved_key_exists": bool(saved_key),
            "sid_token": work_serializer.dumps(sid),
            "builder": state,
            "last_test_row_idx": last_test,
            "builder_test": last_builder_test,
        },
    )
    return attach_session(resp, request, sid)


def _parse_answers_json(answers_json: str) -> Dict[str, str]:
    try:
        raw = json.loads(answers_json or "{}")
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        return {}


def _merge_question_log(existing: List[Dict[str, Any]], questions: List[Dict[str, Any]], answers: Dict[str, str]) -> List[Dict[str, Any]]:
    by_id = {str(item.get("id") or ""): dict(item) for item in existing if item.get("id")}
    for q in questions:
        qid = str(q.get("id") or "")
        if not qid:
            continue
        row = by_id.get(qid) or {"id": qid, "text": q.get("text") or "", "answer": ""}
        row["text"] = q.get("text") or row.get("text") or ""
        if qid in answers:
            row["answer"] = answers.get(qid) or ""
        by_id[qid] = row
    for qid, ans in answers.items():
        if qid in by_id:
            by_id[qid]["answer"] = ans
    return list(by_id.values())


@app.post("/builder/discover")
async def builder_discover(
    request: Request,
    provider: str = Form("langcc"),
    model: str = Form(""),
    api_key: str = Form(""),
    goal: str = Form(""),
    content_col: str = Form(""),
    meanings: str = Form(""),
    mode: str = Form("quick"),
):
    sid, sess = get_work_session(request)
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Pick a model first.", status_code=400)
    rows = sess.get("rows") or []
    cols = sess.get("csv_cols") or []
    if not rows or not cols:
        return PlainTextResponse("Upload a spreadsheet first.", status_code=400)
    if content_col not in cols:
        return PlainTextResponse("Click the column that holds the main text.", status_code=400)
    key = _builder_key(request, provider, api_key)
    if provider_requires_key(provider) and not key:
        return PlainTextResponse("An API key is required for this provider.", status_code=400)
    # One random draw, used by both passes, so the availability score describes
    # the same rows the plan was built on.
    seed = random.randrange(1 << 30)
    sample = sample_rows(rows, cols, content_column=content_col, seed=seed)
    stats = sample_report(rows, cols, content_col)
    mode = "deep" if (mode or "").strip().lower() == "deep" else "quick"

    # Studying the rows and scoring the sheet are independent, so run them at
    # the same time -- the wall clock is one call, not two.
    def _plan():
        return discover_plan(provider, key, model, cols, sample, goal, content_col, meanings, mode=mode)

    def _availability():
        return check_availability(
            provider, key, model, cols, sample, goal, content_col, meanings, stats=stats
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        plan_future = pool.submit(_plan)
        avail_future = pool.submit(_availability)
        try:
            plan = plan_future.result()
        except ValueError as e:
            return PlainTextResponse(str(e), status_code=400)
        except Exception as e:
            return PlainTextResponse(f"Could not study the sample: {e}", status_code=502)
        try:
            availability = avail_future.result()
        except Exception:
            # A missing score should never block the plan the user asked for.
            availability = None

    storage.log_usage(request.state.user, provider, model, estimate_tokens(goal) + 800, 700)
    state = _builder_state(sess)
    state.update({
        "goal": goal,
        "content_col": content_col,
        "meanings": meanings,
        "plan": plan,
        "questions": plan.get("questions") or [],
        "question_log": [],
        "discover_mode": mode,
        "answers": {},
        "availability": availability or {},
        "sample_seed": seed,
        "sample_stats": stats,
    })
    sess["provider"] = provider
    sess["model"] = model
    resp = JSONResponse({
        "ok": True,
        "plan": plan,
        "sample_size": len(sample),
        "mode": mode,
        "report": availability,
        "stats": stats,
    })
    return attach_session(resp, request, sid)


@app.post("/builder/deep-next")
async def builder_deep_next(
    request: Request,
    provider: str = Form("langcc"),
    model: str = Form(""),
    api_key: str = Form(""),
    goal: str = Form(""),
    content_col: str = Form(""),
    meanings: str = Form(""),
    answers_json: str = Form("{}"),
):
    sid, sess = get_work_session(request)
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Pick a model first.", status_code=400)
    rows = sess.get("rows") or []
    cols = sess.get("csv_cols") or []
    if not rows or not cols:
        return PlainTextResponse("Upload a spreadsheet first.", status_code=400)
    if content_col not in cols:
        return PlainTextResponse("Click the column that holds the main text.", status_code=400)
    key = _builder_key(request, provider, api_key)
    if provider_requires_key(provider) and not key:
        return PlainTextResponse("An API key is required for this provider.", status_code=400)
    state = _builder_state(sess)
    answers = _parse_answers_json(answers_json)
    state["question_log"] = _merge_question_log(state.get("question_log") or [], state.get("questions") or [], answers)
    if len(state["question_log"]) >= MAX_DEEP_TOTAL_QUESTIONS:
        plan = dict(state.get("plan") or {})
        plan["ready"] = True
        plan["coverage"] = max(int(plan.get("coverage") or 0), 90)
        plan["questions"] = []
        state["plan"] = plan
        state["questions"] = []
        resp = JSONResponse({"ok": True, "plan": plan, "mode": "deep", "stopped": "limit"})
        return attach_session(resp, request, sid)
    sample = sample_rows(rows, cols, content_column=content_col)
    prior = [
        {"id": q.get("id"), "question": q.get("text"), "answer": q.get("answer")}
        for q in state["question_log"]
    ]
    try:
        plan = discover_plan(
            provider, key, model, cols, sample, goal, content_col, meanings, mode="deep", prior_qa=prior
        )
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        return PlainTextResponse(f"Could not continue the deep pass: {e}", status_code=502)
    storage.log_usage(request.state.user, provider, model, estimate_tokens(goal) + 400, 400)
    if plan.get("ready"):
        plan["questions"] = []
    state.update({
        "goal": goal,
        "content_col": content_col,
        "meanings": meanings,
        "plan": plan,
        "questions": plan.get("questions") or [],
        "discover_mode": "deep",
        "answers": answers,
    })
    sess["provider"] = provider
    sess["model"] = model
    resp = JSONResponse({"ok": True, "plan": plan, "mode": "deep"})
    return attach_session(resp, request, sid)


@app.post("/builder/generate")
async def builder_generate(
    request: Request,
    provider: str = Form("langcc"),
    model: str = Form(""),
    api_key: str = Form(""),
    goal: str = Form(""),
    content_col: str = Form(""),
    meanings: str = Form(""),
    answers_json: str = Form("{}"),
):
    sid, sess = get_work_session(request)
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Pick a model first.", status_code=400)
    cols = sess.get("csv_cols") or []
    if content_col not in cols:
        return PlainTextResponse("Click the column that holds the main text.", status_code=400)
    key = _builder_key(request, provider, api_key)
    if provider_requires_key(provider) and not key:
        return PlainTextResponse("An API key is required for this provider.", status_code=400)
    state = _builder_state(sess)
    raw_answers = _parse_answers_json(answers_json)
    state["question_log"] = _merge_question_log(state.get("question_log") or [], state.get("questions") or [], raw_answers)
    logged = state.get("question_log") or []
    answers = answers_from_form(logged, {str(q.get("id")): str(q.get("answer") or "") for q in logged})
    try:
        made = generate_prompt(
            provider, key, model, cols, goal, content_col, meanings, state.get("plan") or {}, answers
        )
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        return PlainTextResponse(f"Could not write the prompt: {e}", status_code=502)
    storage.log_usage(request.state.user, provider, model, estimate_tokens(goal) + 600, estimate_tokens(made["prompt"]))
    state.update({
        "goal": goal,
        "content_col": content_col,
        "meanings": meanings,
        "answers": {str(q.get("id")): str(q.get("answer") or "") for q in (state.get("question_log") or [])},
        "prompt_name": made["prompt_name"],
        "prompt": made["prompt"],
        "input_template": made.get("input_template") or "",
        "json_mode": made.get("json_mode", True),
        "summary": made.get("summary") or "",
        "catalogue_id": "",
    })
    sess["prompt_name"] = made["prompt_name"]
    sess["prompt_id"] = ""
    sess["prompt_template"] = made["prompt"]
    sess["input_template"] = made.get("input_template") or ""
    _scope_column_map(sess, "", made["prompt"], made.get("input_template") or "")
    sess["json_mode"] = bool(made.get("json_mode", True))
    sess["provider"] = provider
    sess["model"] = model
    resp = JSONResponse({
        "ok": True,
        "name": made["prompt_name"],
        "prompt": made["prompt"],
        "input_template": made.get("input_template") or "",
        "json_mode": bool(made.get("json_mode", True)),
        "summary": made.get("summary") or "",
        "saved": False,
    })
    return attach_session(resp, request, sid)


@app.post("/builder/test")
async def builder_test(
    request: Request,
    provider: str = Form("langcc"),
    model: str = Form(""),
    api_key: str = Form(""),
    test_row_idx: Optional[int] = Form(None),
    input_template: Optional[str] = Form(None),
):
    sid, sess = get_work_session(request)
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Pick a model first.", status_code=400)
    rows = [r for r in (sess.get("rows") or []) if not _row_is_empty_dict(r)]
    if not rows:
        return PlainTextResponse("Upload a spreadsheet first.", status_code=400)
    state = _builder_state(sess)
    prompt_template = state.get("prompt") or sess.get("prompt_template") or ""
    used_input = input_template if input_template is not None else (state.get("input_template") or sess.get("input_template") or "")
    csv_cols = sess.get("csv_cols") or []
    missing_input = row_input_error(prompt_template, used_input, csv_cols, sess.get("column_map") or {})
    if missing_input:
        return PlainTextResponse(missing_input if prompt_template.strip() else "Generate a prompt first.", status_code=400)
    key = _builder_key(request, provider, api_key)
    if provider_requires_key(provider) and not key:
        return PlainTextResponse("An API key is required for this provider.", status_code=400)
    if test_row_idx is not None and 0 <= test_row_idx < len(rows):
        row_idx = test_row_idx
    else:
        row_idx = random.randrange(len(rows))
    row = rows[row_idx]
    json_mode = bool(state.get("json_mode", True))
    err = ""
    text = ""
    latency = 0.0
    try:
        t0 = time.time()
        model_text = compose_model_input(prompt_template, row, used_input, column_map=sess.get("column_map") or {})
        text = call_provider(provider, key, model, model_text, json_mode, None)
        latency = round(time.time() - t0, 3)
        storage.log_usage(request.state.user, provider, model, estimate_tokens(model_text), estimate_tokens(text))
    except Exception as e:
        err = str(e)
    sess["last_test_row_idx"] = row_idx
    preview = {c: row.get(c, "") for c in csv_cols[:6]}
    if state.get("content_col") and state["content_col"] in row:
        preview[state["content_col"]] = row.get(state["content_col"], "")
    result = {
        "ok": not bool(err),
        "row_idx": row_idx,
        "total": len(rows),
        "preview": preview,
        "output": text or "",
        "error": err,
        "latency": latency,
    }
    sess["builder_test"] = result
    sess["provider"] = provider
    sess["model"] = model
    resp = JSONResponse(result)
    return attach_session(resp, request, sid)


def _iso_to_pretty(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value or ""


@app.post("/builder/availability")
async def builder_availability(
    request: Request,
    provider: str = Form("langcc"),
    model: str = Form(""),
    api_key: str = Form(""),
    goal: str = Form(""),
    content_col: str = Form(""),
    meanings: str = Form(""),
):
    sid, sess = get_work_session(request)
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Pick a model first.", status_code=400)
    rows = sess.get("rows") or []
    cols = sess.get("csv_cols") or []
    if not rows or not cols:
        return PlainTextResponse("Upload a spreadsheet first.", status_code=400)
    if content_col not in cols:
        return PlainTextResponse("Click the column that holds the main text.", status_code=400)
    key = _builder_key(request, provider, api_key)
    if provider_requires_key(provider) and not key:
        return PlainTextResponse("An API key is required for this provider.", status_code=400)
    state = _builder_state(sess)
    sample = sample_rows(rows, cols, content_column=content_col, seed=state.get("sample_seed"))
    stats = sample_report(rows, cols, content_col)
    try:
        report = check_availability(
            provider, key, model, cols, sample, goal, content_col, meanings,
            state.get("plan") or {}, stats=stats,
        )
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        return PlainTextResponse(f"Could not score the sheet: {e}", status_code=502)
    storage.log_usage(request.state.user, provider, model, estimate_tokens(goal) + 300, 300)
    state["availability"] = report
    resp = JSONResponse({"ok": True, "report": report})
    return attach_session(resp, request, sid)


@app.post("/builder/save")
async def builder_save_catalogue(request: Request, name: str = Form("")):
    sid, sess = get_work_session(request)
    state = _builder_state(sess)
    prompt = state.get("prompt") or sess.get("prompt_template") or ""
    extra = state.get("input_template") or sess.get("input_template") or ""
    if not prompt.strip():
        return PlainTextResponse("Write a prompt first.", status_code=400)
    display = (name or state.get("prompt_name") or "analysis").strip()
    existing_id = state.get("catalogue_id") or ""
    try:
        # Save is idempotent. It used to create a fresh catalogue entry on every
        # click, so a normal build-test-tweak-save loop left four near-identical
        # prompts in the shared list and nobody could tell which was current.
        if existing_id and catalogue.load_meta(existing_id):
            card = catalogue.update_prompt(
                existing_id,
                request.state.user,
                prompt=prompt,
                input_template=extra,
                name=display,
                content_column=state.get("content_col") or "",
            )
            created = False
        else:
            card = catalogue.create_prompt(
                request.state.user,
                display,
                prompt,
                extra,
                visibility="personal",
                content_column=state.get("content_col") or "",
            )
            created = True
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    state["catalogue_id"] = card["id"]
    state["prompt_name"] = card["name"]
    sess["prompt_id"] = card["id"]
    sess["prompt_name"] = card["name"]
    resp = JSONResponse({"ok": True, "id": card["id"], "name": card["name"], "created": created})
    return attach_session(resp, request, sid)


@app.post("/builder/fix")
async def builder_fix(
    request: Request,
    provider: str = Form("langcc"),
    model: str = Form(""),
    api_key: str = Form(""),
    note: str = Form(""),
):
    sid, sess = get_work_session(request)
    model = (model or "").strip()
    if not model:
        return PlainTextResponse("Pick a model first.", status_code=400)
    if not (note or "").strip():
        return PlainTextResponse("Write what you want changed.", status_code=400)
    state = _builder_state(sess)
    prompt = state.get("prompt") or sess.get("prompt_template") or ""
    extra = state.get("input_template") or sess.get("input_template") or ""
    if not prompt.strip():
        return PlainTextResponse("Write a prompt first.", status_code=400)
    key = _builder_key(request, provider, api_key)
    if provider_requires_key(provider) and not key:
        return PlainTextResponse("An API key is required for this provider.", status_code=400)
    example = {
        "row_index": 0,
        "verdict": "wrong",
        "note": note.strip(),
        "input_excerpt": extra or "(input template)",
        "output": "",
    }
    prior = []
    purpose = ""
    prompt_id = state.get("catalogue_id") or sess.get("prompt_id") or ""
    if prompt_id:
        ctx = catalogue.change_context(prompt_id)
        prior = ctx.get("change_log") or []
        purpose = ctx.get("purpose") or ""
    prior.extend(state.get("change_log") or [])
    try:
        result = improve_prompt(
            provider, key, model, prompt, [example], input_template=extra,
            prior_changes=prior, purpose=purpose,
        )
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        return PlainTextResponse(f"Could not fix the prompt: {e}", status_code=502)
    proposed = result.get("proposed_prompt") or prompt
    state["prompt"] = proposed
    sess["prompt_template"] = proposed
    summary_bits = result.get("change_summary") or (result.get("editor") or {}).get("change_summary") or []
    summary = "; ".join(str(x) for x in summary_bits if str(x).strip())
    log = list(state.get("change_log") or [])
    if summary:
        log.append({"at": datetime.utcnow().isoformat(), "by": request.state.user, "summary": summary, "purpose": result.get("purpose") or purpose})
        state["change_log"] = log[-20:]
    saved_to_catalogue = False
    if prompt_id and catalogue.load_meta(prompt_id):
        # The builder edited its own draft but never wrote it back, so a prompt
        # already in the catalogue kept its old text while its change log
        # recorded edits that had not happened to it.
        try:
            catalogue.update_prompt(prompt_id, request.state.user, prompt=proposed)
            saved_to_catalogue = True
            if summary:
                catalogue.append_change(prompt_id, request.state.user, summary, result.get("purpose") or "")
        except PermissionError:
            saved_to_catalogue = False
        except Exception:
            saved_to_catalogue = False
    resp = JSONResponse({
        "ok": True,
        "prompt": proposed,
        "diff": result.get("diff") or [],
        "summary": summary_bits,
        "purpose": result.get("purpose") or purpose,
        "unchanged": bool(result.get("unchanged")),
        "warnings": result.get("warnings") or [],
        "saved": saved_to_catalogue,
    })
    return attach_session(resp, request, sid)


@app.post("/mapping/save")
async def save_mapping(request: Request):
    sid, sess = get_work_session(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = body.get("mapping") if isinstance(body, dict) else {}
    if isinstance(body, dict):
        if "prompt_template" in body:
            sess["prompt_template"] = str(body.get("prompt_template") or "")
        if "input_template" in body:
            sess["input_template"] = str(body.get("input_template") or "")
    mapping = {}
    cols = set(sess.get("csv_cols") or [])
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k or "").strip()
            val = str(v or "").strip()
            if key and val in cols:
                mapping[key] = val
    sess["column_map"] = mapping
    placeholders = required_placeholders(sess.get("prompt_template") or "", sess.get("input_template") or "")
    resp = JSONResponse({"ok": True, "mapping": mapping, "required_inputs": placeholders, "mapping_needed": mapping_gaps(
        placeholders,
        sess.get("csv_cols") or [],
        mapping,
    )})
    return attach_session(resp, request, sid)


@app.get("/users/search")
def users_search(request: Request, q: str = ""):
    needle = (q or "").strip().lower()
    names = [n for n in auth.list_users() if n != request.state.user]
    if needle:
        names = [n for n in names if needle in n.lower()]
    return JSONResponse({"users": names[:20]})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    sid, sess = get_work_session(request)
    payload = settings_payload(request.state.user)
    resp = render(
        request,
        "settings.html",
        {
            "nav": "settings",
            "user": request.state.user,
            **payload,
            "providers": ["langcc", "openai", "gemini", "ollama"],
            "tools": storage.AI_TOOLS,
        },
    )
    return attach_session(resp, request, sid)


@app.post("/settings/save")
async def settings_save(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    saved = storage.save_user_settings(request.state.user, body)
    return JSONResponse({"ok": True, "settings": saved})


@app.post("/settings/keys")
async def settings_add_key(
    request: Request,
    label: str = Form(""),
    provider: str = Form(...),
    api_key: str = Form(...),
    key_id: str = Form(""),
):
    try:
        card = storage.save_named_key(request.state.user, label, provider, api_key, key_id)
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception:
        return PlainTextResponse("Could not save key.", status_code=500)
    return JSONResponse({"ok": True, **card, "keys": storage.list_named_keys(request.state.user)})


@app.post("/settings/keys/forget")
async def settings_forget_key(request: Request, key_id: str = Form(...)):
    deleted = storage.delete_named_key(request.state.user, key_id)
    return JSONResponse({"ok": True, "deleted": deleted, "keys": storage.list_named_keys(request.state.user)})


@app.get("/catalogue", response_class=HTMLResponse)
def catalogue_page(request: Request):
    sid, sess = get_work_session(request)
    helper = storage.tool_defaults(request.state.user, "catalogue")
    finder = storage.tool_defaults(request.state.user, "finder")
    resp = render(
        request,
        "catalogue.html",
        {
            "nav": "catalogue",
            "user": request.state.user,
            "provider": helper.get("provider") or sess.get("provider", "langcc"),
            "model": helper.get("model") or sess.get("model", "gpt-5-mini"),
            "saved_key_exists": bool(
                saved_key_for(
                    request.state.user,
                    helper.get("provider") or sess.get("provider", "langcc"),
                    helper.get("key_id") or "",
                )
            ),
            "finder_provider": finder.get("provider") or "langcc",
            "finder_model": finder.get("model") or "",
        },
    )
    return attach_session(resp, request, sid)


@app.get("/catalogue/search")
def catalogue_search(
    request: Request,
    q: str = "",
    owner: str = "",
    access: str = "",
    page: int = 1,
):
    data = catalogue.search_visible(request.state.user, q=q, owner=owner, access=access, page=page)
    return JSONResponse(data)


@app.post("/catalogue/find")
async def catalogue_find(
    request: Request,
    message: str = Form(""),
    owner: str = Form(""),
    provider: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    key_id: str = Form(""),
):
    if not (message or "").strip():
        return PlainTextResponse("Write what you are looking for.", status_code=400)
    name, chosen, key = resolve_ai(request, "finder", provider, model, api_key, key_id)
    if not chosen:
        return PlainTextResponse("Set a model in Settings first.", status_code=400)
    if provider_requires_key(name) and not key:
        return PlainTextResponse("An API key is required for this provider.", status_code=400)
    visible = catalogue.list_visible(request.state.user)
    try:
        turn = find_prompts_turn(name, key, chosen, message, visible, owner=owner)
    except Exception as e:
        return PlainTextResponse(f"The helper did not answer: {e}", status_code=502)
    cards = []
    by_id = {i.get("id"): i for i in visible}
    for match in turn.get("matches") or []:
        card = by_id.get(match.get("id"))
        if card:
            cards.append({**card, "why": match.get("why") or ""})
    return JSONResponse({"reply": turn.get("reply") or "", "matches": cards})


@app.get("/catalogue/{prompt_id}")
def catalogue_get(request: Request, prompt_id: str):
    try:
        item = catalogue.get_visible(prompt_id, request.state.user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=404)
    item["chat"] = catalogue.load_chat(prompt_id, request.state.user)
    ctx = catalogue.change_context(prompt_id)
    item["purpose"] = ctx.get("purpose") or item.get("purpose") or ""
    item["change_log"] = ctx.get("change_log") or item.get("change_log") or []
    return JSONResponse(item)


@app.get("/catalogue/{prompt_id}/versions")
def catalogue_versions(request: Request, prompt_id: str):
    try:
        history = catalogue.list_versions(prompt_id, request.state.user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=404)
    # Index is the position in the stored list, which is what /restore expects.
    cards = [
        {
            "index": i,
            "at": _iso_to_pretty(str(item.get("at") or "")),
            "by": item.get("by") or "",
            "chars": len(item.get("prompt") or ""),
            "prompt": item.get("prompt") or "",
            "input_template": item.get("input_template") or "",
        }
        for i, item in enumerate(history)
    ]
    cards.reverse()
    return JSONResponse({"versions": cards})


@app.post("/catalogue/{prompt_id}/restore")
async def catalogue_restore(request: Request, prompt_id: str, index: int = Form(...)):
    try:
        item = catalogue.restore_version(prompt_id, request.state.user, index)
    except PermissionError:
        return PlainTextResponse(
            "You don't have edit access. Clone this prompt or request edit access from the owner.",
            status_code=403,
        )
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    try:
        catalogue.append_change(prompt_id, request.state.user, "Restored an earlier version of this prompt.")
    except Exception:
        pass
    return JSONResponse({"ok": True, **item})


@app.post("/catalogue/{prompt_id}/acl")
async def catalogue_acl(
    request: Request,
    prompt_id: str,
    visibility: str = Form("personal"),
    everyone_role: str = Form("viewer"),
    viewers: str = Form("[]"),
    editors: str = Form("[]"),
):
    try:
        view_list = json.loads(viewers or "[]")
        edit_list = json.loads(editors or "[]")
        if not isinstance(view_list, list):
            view_list = []
        if not isinstance(edit_list, list):
            edit_list = []
        card = catalogue.update_acl(
            prompt_id,
            request.state.user,
            visibility=visibility,
            everyone_role=everyone_role,
            viewers=[str(x) for x in view_list],
            editors=[str(x) for x in edit_list],
        )
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    return JSONResponse({"ok": True, **card})


@app.post("/catalogue/{prompt_id}/delete")
async def catalogue_delete(request: Request, prompt_id: str):
    try:
        catalogue.delete_prompt(prompt_id, request.state.user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    return JSONResponse({"ok": True})


@app.post("/catalogue/{prompt_id}/clone")
async def catalogue_clone(request: Request, prompt_id: str):
    try:
        card = catalogue.clone_prompt(prompt_id, request.state.user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    return JSONResponse({"ok": True, **card})


@app.post("/catalogue/{prompt_id}/use")
async def catalogue_use(request: Request, prompt_id: str):
    sid, sess = get_work_session(request)
    try:
        item = catalogue.get_visible(prompt_id, request.state.user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=404)
    sess["prompt_id"] = item["id"]
    sess["prompt_name"] = item["name"]
    sess["prompt_template"] = item.get("prompt") or ""
    sess["input_template"] = item.get("input_template") or ""
    storage.remember_last_prompt(request.state.user, item["id"], item["name"])
    column_map = _scope_column_map(
        sess, item["id"], item.get("prompt") or "", item.get("input_template") or ""
    )
    gaps = mapping_gaps(
        required_placeholders(item.get("prompt") or "", item.get("input_template") or ""),
        sess.get("csv_cols") or [],
        column_map,
    )
    resp = JSONResponse({
        "ok": True,
        "id": item["id"],
        "name": item["name"],
        "mapping_needed": gaps,
        "column_map": column_map,
        "columns": sess.get("csv_cols") or [],
        "required_inputs": required_placeholders(item.get("prompt") or "", item.get("input_template") or ""),
    })
    return attach_session(resp, request, sid)


@app.post("/catalogue/{prompt_id}/request-edit")
async def catalogue_request_edit(request: Request, prompt_id: str):
    try:
        card = catalogue.request_edit(prompt_id, request.state.user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    return JSONResponse({"ok": True, **card})


@app.post("/catalogue/{prompt_id}/grant-edit")
async def catalogue_grant_edit(
    request: Request,
    prompt_id: str,
    username: str = Form(...),
    grant: str = Form("1"),
):
    try:
        card = catalogue.resolve_edit_request(prompt_id, request.state.user, username, grant=grant != "0")
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    return JSONResponse({"ok": True, **card})


@app.post("/catalogue/{prompt_id}/chat")
async def catalogue_chat(
    request: Request,
    prompt_id: str,
    message: str = Form(""),
    provider: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    key_id: str = Form(""),
    apply: str = Form("0"),
):
    user = request.state.user
    try:
        item = catalogue.get_visible(prompt_id, user)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    if not (message or "").strip():
        return PlainTextResponse("Write a message.", status_code=400)
    name, chosen, key = resolve_ai(request, "catalogue", provider, model, api_key, key_id)
    if provider_requires_key(name) and not key:
        return PlainTextResponse("An API key is required for this provider. Save one in Settings.", status_code=400)
    if not chosen:
        return PlainTextResponse("Set a model in Settings first.", status_code=400)
    history = catalogue.load_chat(prompt_id, user)
    ctx = catalogue.change_context(prompt_id)
    can_edit = catalogue.can_edit(item, user)
    try:
        turn = catalogue_chat_turn(
            name, key, chosen, item.get("prompt") or "", item.get("input_template") or "",
            history, message, can_edit=can_edit,
            purpose=ctx.get("purpose") or "",
            prior_changes=ctx.get("change_log") or [],
        )
    except Exception as e:
        return PlainTextResponse(f"The helper did not answer: {e}", status_code=502)
    catalogue.append_chat(prompt_id, user, "user", message.strip())
    catalogue.append_chat(prompt_id, user, "assistant", turn["reply"])
    applied = False
    if apply == "1" and (turn.get("updated_prompt") or turn.get("updated_input")):
        if not can_edit:
            turn["updated_prompt"] = ""
            turn["updated_input"] = ""
            turn["reply"] = (
                "You don't have edit access. Clone this prompt or request edit access from the owner."
            )
        else:
            catalogue.update_prompt(
                prompt_id,
                user,
                prompt=turn.get("updated_prompt") or None,
                input_template=turn.get("updated_input") or None,
            )
            if turn.get("change_summary"):
                catalogue.append_change(prompt_id, user, turn["change_summary"], turn.get("purpose") or "")
            applied = True
    return JSONResponse({
        **turn,
        "applied": applied,
        "can_edit": can_edit,
        "owner": item.get("owner") or "",
        "chat": catalogue.load_chat(prompt_id, user),
    })


@app.post("/catalogue/{prompt_id}/apply")
async def catalogue_apply(
    request: Request,
    prompt_id: str,
    prompt: str = Form(""),
    input_template: str = Form(""),
    summary: str = Form(""),
    purpose: str = Form(""),
):
    user = request.state.user
    try:
        item = catalogue.get_visible(prompt_id, user)
        if not catalogue.can_edit(item, user):
            return PlainTextResponse(
                "You don't have edit access. Clone this prompt or request edit access from the owner.",
                status_code=403,
            )
        item = catalogue.update_prompt(
            prompt_id, user, prompt=prompt, input_template=input_template
        )
        if summary.strip():
            catalogue.append_change(prompt_id, user, summary.strip(), purpose)
    except PermissionError as e:
        return PlainTextResponse(
            "You don't have edit access. Clone this prompt or request edit access from the owner.",
            status_code=403,
        )
    except Exception as e:
        return PlainTextResponse(str(e), status_code=400)
    return JSONResponse({"ok": True, **item, "chat": catalogue.load_chat(prompt_id, user)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
