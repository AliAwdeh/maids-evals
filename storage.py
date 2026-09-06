import base64
import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

import pandas as pd

from engine import _build_output_dataframe, _json_default, _normalize_value, _write_csv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USERS_DATA_DIR = DATA_DIR / "users"
PROMPTS_DIR = BASE_DIR / "prompts"


def _safe_username(username: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (username or "").strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise ValueError("Invalid username.")
    return cleaned


def _safe_run_id(run_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "", run_id or "")
    if not cleaned:
        raise ValueError("Invalid run id.")
    return cleaned


def _safe_prompt_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise ValueError("Invalid prompt name.")
    return cleaned


_ensured_dirs: set = set()


def user_dir(username: str) -> Path:
    path = USERS_DATA_DIR / _safe_username(username)
    # log_usage runs once per row, and this used to mkdir on every one of them.
    key = str(path)
    if key not in _ensured_dirs:
        path.mkdir(parents=True, exist_ok=True)
        _ensured_dirs.add(key)
    return path


def user_runs_dir(username: str) -> Path:
    path = user_dir(username) / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir(username: str, run_id: str) -> Path:
    return user_runs_dir(username) / _safe_run_id(run_id)


def usage_path(username: str) -> Path:
    return user_dir(username) / "usage.jsonl"


KEY_PROVIDERS = ("openai", "langcc", "gemini")
_SECRETS_SALT = "maids-evals-secrets-v1"


def secrets_path(username: str, create: bool = False) -> Path:
    if create:
        return user_dir(username) / "secrets.json"
    return USERS_DATA_DIR / _safe_username(username) / "secrets.json"


def _fernet() -> "Fernet":
    if Fernet is None:
        raise RuntimeError("cryptography is not installed. Run pip install -r requirements.txt.")
    explicit = (os.getenv("SECRET_FERNET") or "").strip()
    if explicit:
        try:
            return Fernet(explicit.encode("ascii") if isinstance(explicit, str) else explicit)
        except Exception:
            digest = hashlib.sha256(explicit.encode("utf-8")).digest()
            return Fernet(base64.urlsafe_b64encode(digest))
    secret = (os.getenv("SESSION_SECRET") or "").strip()
    if not secret:
        raise RuntimeError("SESSION_SECRET or SECRET_FERNET is required to store API keys.")
    digest = hashlib.sha256((_SECRETS_SALT + ":" + secret).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _normalize_key_provider(provider: str) -> str:
    name = (provider or "").strip().lower()
    if name not in KEY_PROVIDERS:
        raise ValueError("API keys are only stored for openai, langcc, or gemini.")
    return name


def _read_secrets_blob(username: str) -> Dict[str, str]:
    return _read_secrets_full(username).get("keys") or {}


def _read_secrets_full(username: str) -> Dict[str, Any]:
    path = secrets_path(username)
    if not path.is_file():
        return {"v": 2, "keys": {}, "named": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"v": 2, "keys": {}, "named": {}}
    if not isinstance(data, dict):
        return {"v": 2, "keys": {}, "named": {}}
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    named = data.get("named") if isinstance(data.get("named"), dict) else {}
    return {
        "v": 2,
        "keys": {str(k): str(v) for k, v in keys.items() if v},
        "named": {str(k): v for k, v in named.items() if isinstance(v, dict)},
    }


def _write_secrets_full(username: str, blob: Dict[str, Any]) -> None:
    path = secrets_path(username, create=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "v": 2,
        "keys": blob.get("keys") or {},
        "named": blob.get("named") or {},
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _write_secrets_blob(username: str, keys: Dict[str, str]) -> None:
    blob = _read_secrets_full(username)
    blob["keys"] = keys
    _write_secrets_full(username, blob)


def load_user_secret(username: str, provider: str) -> Optional[str]:
    name = (provider or "").strip().lower()
    if name not in KEY_PROVIDERS:
        return None
    token = _read_secrets_blob(username).get(name)
    if not token:
        return None
    try:
        raw = _fernet().decrypt(token.encode("utf-8"))
    except (InvalidToken, Exception):
        return None
    value = raw.decode("utf-8")
    return value or None


def save_user_secret(username: str, provider: str, api_key: str) -> str:
    name = _normalize_key_provider(provider)
    key = (api_key or "").strip()
    if not key:
        raise ValueError("API key is empty.")
    blob = _read_secrets_blob(username)
    blob[name] = _fernet().encrypt(key.encode("utf-8")).decode("ascii")
    _write_secrets_blob(username, blob)
    return name


def delete_user_secret(username: str, provider: str) -> bool:
    name = _normalize_key_provider(provider)
    blob = _read_secrets_full(username)
    keys = blob.get("keys") or {}
    if name not in keys:
        return False
    del keys[name]
    blob["keys"] = keys
    _write_secrets_full(username, blob)
    return True


AI_TOOLS = ("runner", "builder", "fixer", "catalogue", "finder")


def _encrypt_value(value: str) -> str:
    return _fernet().encrypt((value or "").encode("utf-8")).decode("ascii")


def _decrypt_value(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        raw = _fernet().decrypt(token.encode("utf-8"))
    except (InvalidToken, Exception):
        return None
    value = raw.decode("utf-8")
    return value or None


def save_named_key(username: str, label: str, provider: str, api_key: str, key_id: str = "") -> Dict[str, str]:
    name = _normalize_key_provider(provider)
    key = (api_key or "").strip()
    if not key:
        raise ValueError("API key is empty.")
    display = (label or "").strip() or name
    blob = _read_secrets_full(username)
    named = blob.get("named") or {}
    kid = (key_id or "").strip() or secrets.token_hex(4)
    named[kid] = {
        "id": kid,
        "label": display,
        "provider": name,
        "token": _encrypt_value(key),
        "updated_at": datetime.utcnow().isoformat(),
    }
    blob["named"] = named
    keys = blob.get("keys") or {}
    keys[name] = named[kid]["token"]
    blob["keys"] = keys
    _write_secrets_full(username, blob)
    return {"id": kid, "label": display, "provider": name}


def list_named_keys(username: str) -> List[Dict[str, str]]:
    blob = _read_secrets_full(username)
    out: List[Dict[str, str]] = []
    seen_providers = set()
    for item in (blob.get("named") or {}).values():
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "")
        out.append({
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or provider),
            "provider": provider,
        })
        seen_providers.add(provider)
    for provider, token in (blob.get("keys") or {}).items():
        if provider in seen_providers or not token:
            continue
        out.append({"id": provider, "label": provider, "provider": provider, "legacy": "1"})
    out.sort(key=lambda x: (x.get("provider") or "", x.get("label") or ""))
    return out


def load_named_key(username: str, key_id: str) -> Optional[str]:
    kid = (key_id or "").strip()
    if not kid:
        return None
    blob = _read_secrets_full(username)
    item = (blob.get("named") or {}).get(kid)
    if isinstance(item, dict):
        return _decrypt_value(str(item.get("token") or ""))
    if kid in (blob.get("keys") or {}):
        return _decrypt_value(str(blob["keys"][kid]))
    return None


def delete_named_key(username: str, key_id: str) -> bool:
    kid = (key_id or "").strip()
    blob = _read_secrets_full(username)
    named = blob.get("named") or {}
    if kid in named:
        provider = str((named.get(kid) or {}).get("provider") or "")
        del named[kid]
        blob["named"] = named
        keys = blob.get("keys") or {}
        still = any(str((v or {}).get("provider") or "") == provider for v in named.values())
        if not still and provider in keys:
            del keys[provider]
            blob["keys"] = keys
        _write_secrets_full(username, blob)
        return True
    keys = blob.get("keys") or {}
    if kid in keys:
        del keys[kid]
        blob["keys"] = keys
        _write_secrets_full(username, blob)
        return True
    return False


def resolve_user_key(username: str, provider: str = "", key_id: str = "") -> str:
    if key_id:
        found = load_named_key(username, key_id)
        if found:
            return found
    if provider:
        found = load_user_secret(username, provider)
        if found:
            return found
        for item in list_named_keys(username):
            if item.get("provider") == (provider or "").strip().lower():
                val = load_named_key(username, item.get("id") or "")
                if val:
                    return val
    return ""


def settings_path(username: str) -> Path:
    return user_dir(username) / "settings.json"


def default_settings() -> Dict[str, Any]:
    empty_tool = {"provider": "", "model": "", "key_id": ""}
    return {
        "default_provider": "langcc",
        "default_model": "gpt-5-mini",
        "default_key_id": "",
        "last_prompt_id": "",
        "last_prompt_name": "",
        "tools": {name: dict(empty_tool) for name in AI_TOOLS},
    }


def load_user_settings(username: str) -> Dict[str, Any]:
    path = settings_path(username)
    data = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    base = default_settings()
    base["default_provider"] = str(data.get("default_provider") or base["default_provider"]).strip().lower() or "langcc"
    base["default_model"] = str(data.get("default_model") or base["default_model"]).strip() or "gpt-5-mini"
    base["default_key_id"] = str(data.get("default_key_id") or "").strip()
    base["last_prompt_id"] = str(data.get("last_prompt_id") or "").strip()
    base["last_prompt_name"] = str(data.get("last_prompt_name") or "").strip()
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    for name in AI_TOOLS:
        raw = tools.get(name) if isinstance(tools.get(name), dict) else {}
        base["tools"][name] = {
            "provider": str(raw.get("provider") or "").strip().lower(),
            "model": str(raw.get("model") or "").strip(),
            "key_id": str(raw.get("key_id") or "").strip(),
        }
    return base


def save_user_settings(username: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    current = load_user_settings(username)
    if "default_provider" in payload:
        current["default_provider"] = str(payload.get("default_provider") or "langcc").strip().lower() or "langcc"
    if "default_model" in payload:
        current["default_model"] = str(payload.get("default_model") or "").strip() or current["default_model"]
    if "default_key_id" in payload:
        current["default_key_id"] = str(payload.get("default_key_id") or "").strip()
    if "last_prompt_id" in payload:
        current["last_prompt_id"] = str(payload.get("last_prompt_id") or "").strip()
    if "last_prompt_name" in payload:
        current["last_prompt_name"] = str(payload.get("last_prompt_name") or "").strip()
    tools = payload.get("tools") if isinstance(payload.get("tools"), dict) else {}
    for name in AI_TOOLS:
        raw = tools.get(name) if isinstance(tools.get(name), dict) else None
        if not raw:
            continue
        current["tools"][name] = {
            "provider": str(raw.get("provider") or "").strip().lower(),
            "model": str(raw.get("model") or "").strip(),
            "key_id": str(raw.get("key_id") or "").strip(),
        }
    path = settings_path(username)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return current


def remember_last_prompt(username: str, prompt_id: str, prompt_name: str) -> None:
    if not prompt_id:
        return
    save_user_settings(username, {"last_prompt_id": prompt_id, "last_prompt_name": prompt_name or ""})


def tool_defaults(username: str, tool: str) -> Dict[str, str]:
    settings = load_user_settings(username)
    cfg = (settings.get("tools") or {}).get(tool) or {}
    provider = (cfg.get("provider") or settings.get("default_provider") or "langcc").strip().lower()
    model = (cfg.get("model") or settings.get("default_model") or "gpt-5-mini").strip()
    key_id = (cfg.get("key_id") or settings.get("default_key_id") or "").strip()
    return {"provider": provider, "model": model, "key_id": key_id}


def prompt_path(name: str) -> Path:
    safe_name = _safe_prompt_name(name)
    if not safe_name.endswith(".txt"):
        safe_name += ".txt"
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    return PROMPTS_DIR / safe_name


def list_prompts() -> List[str]:
    if not PROMPTS_DIR.is_dir():
        return []
    names = [fname[:-4] for fname in os.listdir(PROMPTS_DIR) if fname.endswith(".txt")]
    return sorted(names)


def load_prompt(name: str) -> str:
    path = prompt_path(name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_prompt(name: str, content: str) -> str:
    safe_name = _safe_prompt_name(name)
    path = prompt_path(safe_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return safe_name


def unique_prompt_name(name: str) -> str:
    safe = _safe_prompt_name(name)
    existing = set(list_prompts())
    if safe not in existing:
        return safe
    return next_prompt_version(safe)


def next_prompt_version(name: str) -> str:
    base = _safe_prompt_name(name)
    base = re.sub(r"\.v\d+$", "", base)
    version = 2
    candidate = f"{base}.v{version}"
    existing = set(list_prompts())
    while candidate in existing:
        version += 1
        candidate = f"{base}.v{version}"
    return candidate


def prompt_family(name: str) -> str:
    """Collapse a versioned prompt name (agent_eval.v3) to its family (agent_eval)."""
    base = _safe_prompt_name(name)
    return re.sub(r"\.v\d+$", "", base)


def list_prompt_versions(name: str) -> List[Dict[str, Any]]:
    """Every saved file in a prompt family, oldest first, with size + mtime."""
    base = prompt_family(name)
    versions: List[Dict[str, Any]] = []
    for existing in list_prompts():
        if existing == base or re.match(re.escape(base) + r"\.v\d+$", existing):
            path = prompt_path(existing)
            try:
                stat = path.stat()
                modified = datetime.utcfromtimestamp(stat.st_mtime).isoformat()
                size = stat.st_size
            except Exception:
                modified, size = "", 0
            versions.append({"name": existing, "modified": modified, "size": size})

    def _vnum(item: Dict[str, Any]) -> int:
        match = re.search(r"\.v(\d+)$", item["name"])
        return int(match.group(1)) if match else 1

    return sorted(versions, key=_vnum)


# --- Prompt-fix history -----------------------------------------------------
# Shared, team-visible record of every prompt-repair run (Analyst/Editor/Critic).
# One JSONL file per prompt family under prompts/history/. Records are updated
# in place (accept / discard) under a lock. Old version texts are never removed.

PROMPT_HISTORY_DIR = PROMPTS_DIR / "history"
_HISTORY_LOCK = threading.Lock()


def _history_path(name: str) -> Path:
    return PROMPT_HISTORY_DIR / f"{prompt_family(name)}.jsonl"


def _read_history(name: str) -> List[Dict[str, Any]]:
    path = _history_path(name)
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def _write_history(name: str, records: List[Dict[str, Any]]) -> None:
    path = _history_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=_json_default) + "\n")
    os.replace(tmp, path)


def _summarize_patterns(analysis: Any) -> List[Dict[str, str]]:
    patterns: List[Dict[str, str]] = []
    if isinstance(analysis, dict):
        for item in analysis.get("patterns") or []:
            if isinstance(item, dict):
                patterns.append(
                    {
                        "title": str(item.get("title", "") or ""),
                        "problem": str(item.get("problem", "") or ""),
                        "needed_change": str(item.get("needed_change", "") or ""),
                    }
                )
    return patterns


def record_prompt_fix(
    *,
    source_name: str,
    user: str,
    run_id: str,
    flow: str,
    examples: List[Dict[str, Any]],
    analysis: Any,
    critic: Any,
    change_summary: List[str],
    diff: List[str],
    old_prompt: str,
    new_prompt: str,
) -> str:
    """Persist one prompt-fix run. Returns the fix_id used to update it later."""
    fix_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    notes_summary = [
        {
            "row": ex.get("row"),
            "verdict": ex.get("verdict", ""),
            "note": (ex.get("note", "") or "")[:280],
        }
        for ex in (examples or [])
    ]
    critic_summary = ""
    if isinstance(critic, dict):
        rejected = critic.get("rejected_edits") or []
        risks = critic.get("risks") or []
        bits = []
        if rejected:
            bits.append("rejected: " + "; ".join(str(x) for x in rejected))
        if risks:
            bits.append("risks: " + "; ".join(str(x) for x in risks))
        critic_summary = " | ".join(bits)
    elif critic:
        critic_summary = str(critic)
    record = {
        "fix_id": fix_id,
        "ts": datetime.utcnow().isoformat(),
        "user": user,
        "source_name": source_name,
        "new_version": "",
        "status": "proposed",
        "flow": flow,
        "run_id": run_id or "",
        "example_count": len(notes_summary),
        "notes_summary": notes_summary,
        "patterns": _summarize_patterns(analysis),
        "change_summary": [str(x) for x in (change_summary or [])],
        "critic_summary": critic_summary,
        "diff": diff or [],
        "old_prompt": old_prompt or "",
        "new_prompt": new_prompt or "",
    }
    with _HISTORY_LOCK:
        records = _read_history(source_name)
        records.append(record)
        _write_history(source_name, records)
    return fix_id


def update_prompt_fix(
    source_name: str,
    fix_id: str,
    *,
    status: Optional[str] = None,
    new_version: Optional[str] = None,
) -> bool:
    if not fix_id:
        return False
    with _HISTORY_LOCK:
        records = _read_history(source_name)
        changed = False
        for rec in records:
            if rec.get("fix_id") == fix_id:
                if status is not None:
                    rec["status"] = status
                if new_version is not None:
                    rec["new_version"] = new_version
                rec["updated_ts"] = datetime.utcnow().isoformat()
                changed = True
        if changed:
            _write_history(source_name, records)
        return changed


def load_prompt_history(name: str) -> List[Dict[str, Any]]:
    return sorted(_read_history(name), key=lambda r: r.get("ts", ""), reverse=True)


def list_prompt_history() -> List[Dict[str, Any]]:
    """One summary row per prompt family that has any recorded fixes."""
    if not PROMPT_HISTORY_DIR.is_dir():
        return []
    summaries: List[Dict[str, Any]] = []
    for fname in os.listdir(PROMPT_HISTORY_DIR):
        if not fname.endswith(".jsonl"):
            continue
        base = fname[:-6]
        records = _read_history(base)
        if not records:
            continue
        latest = max(records, key=lambda r: r.get("ts", ""))
        summaries.append(
            {
                "prompt": base,
                "fix_count": len(records),
                "accepted_count": sum(1 for r in records if r.get("status") == "accepted"),
                "last_ts": latest.get("ts", ""),
                "last_user": latest.get("user", ""),
            }
        )
    return sorted(summaries, key=lambda r: r.get("last_ts", ""), reverse=True)


def log_usage(username: str, provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    path = usage_path(username)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_usage(username: str, since: Optional[datetime] = None) -> Dict[str, Any]:
    totals = {"input_tokens": 0, "output_tokens": 0}
    by_model: Dict[str, Dict[str, int]] = {}
    path = usage_path(username)
    if not path.is_file():
        return {"totals": totals, "by_model": by_model}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            ts = entry.get("ts")
            if since and ts:
                try:
                    if datetime.fromisoformat(ts) < since:
                        continue
                except Exception:
                    pass
            model = entry.get("model") or "unknown"
            inp = int(entry.get("input_tokens") or 0)
            outp = int(entry.get("output_tokens") or 0)
            totals["input_tokens"] += inp
            totals["output_tokens"] += outp
            if model not in by_model:
                by_model[model] = {"input_tokens": 0, "output_tokens": 0}
            by_model[model]["input_tokens"] += inp
            by_model[model]["output_tokens"] += outp
    return {"totals": totals, "by_model": by_model}


def _empty_review() -> Dict[str, Any]:
    return {
        "prompt_draft": "",
        "input_template": "",
        "notes": {},
        "fixer": {
            "status": "idle",
            "proposed_prompt": "",
            "diff": [],
            "analysis": "",
            "critic": "",
            "accepted_name": "",
            "error": "",
        },
        # Re-running the reviewed rows against a proposed prompt, so a fix is
        # judged on what it does to the rows rather than on how the diff reads.
        "recheck": {"status": "idle", "rows": [], "summary": {}, "error": "", "prompt": ""},
    }


def load_review(username: str, run_id: str) -> Dict[str, Any]:
    path = run_dir(username, run_id) / "review.json"
    if not path.is_file():
        return _empty_review()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _empty_review()
    base = _empty_review()
    if isinstance(data, dict):
        base["prompt_draft"] = data.get("prompt_draft") or ""
        base["notes"] = data.get("notes") if isinstance(data.get("notes"), dict) else {}
        if isinstance(data.get("fixer"), dict):
            base["fixer"].update(data["fixer"])
        if isinstance(data.get("recheck"), dict):
            base["recheck"].update(data["recheck"])
    return base


def save_review(username: str, run_id: str, review: Dict[str, Any]) -> None:
    path = run_dir(username, run_id) / "review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)


def save_run_archive(
    username: str,
    sess: Dict[str, Any],
    rows: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    detected_keys: List[str],
    provider: str,
    model: str,
    run_name: str,
    worker_count: int,
    model_params: Dict[str, Any],
    prompt_template: str,
    input_template: str = "",
) -> str:
    run_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
    path = run_dir(username, run_id)
    path.mkdir(parents=True, exist_ok=True)

    csv_cols = sess.get("csv_cols") or []
    input_df = pd.DataFrame(rows).reindex(columns=csv_cols)
    output_df = _build_output_dataframe(results, csv_cols, out_json_keys=detected_keys)

    _write_csv(str(path / "input.csv"), input_df)
    _write_csv(str(path / "output.csv"), output_df)
    with open(path / "results.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "csv_cols": csv_cols,
                "rows": rows,
                "results": results,
                "detected_json_keys": detected_keys,
            },
            f,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )

    with open(path / "prompt.txt", "w", encoding="utf-8") as f:
        f.write(prompt_template or "")
    with open(path / "input_template.txt", "w", encoding="utf-8") as f:
        f.write(input_template or "")

    failed_count = sum(1 for r in results if r.get("llm_api_failed"))
    valid_json_count = sum(1 for r in results if r.get("llm_json_valid"))
    metadata = {
        "run_id": run_id,
        "owner": username,
        "created_at": datetime.utcnow().isoformat(),
        "provider": provider,
        "model": model,
        "run_name": (run_name or "").strip(),
        "row_count": len(results),
        "input_row_count": len(rows),
        "failed_count": failed_count,
        "valid_json_count": valid_json_count,
        "json_key_count": len(detected_keys),
        "json_keys": detected_keys,
        "worker_count": worker_count,
        "json_mode": bool(sess.get("json_mode")),
        "model_params": model_params,
        "prompt_name": sess.get("prompt_name", ""),
        "input_template": input_template or "",
    }
    with open(path / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2, default=_json_default)

    review = _empty_review()
    review["prompt_draft"] = prompt_template or ""
    review["input_template"] = input_template or ""
    save_review(username, run_id, review)
    return run_id


def list_saved_runs(username: str) -> List[Dict[str, Any]]:
    runs_path = user_runs_dir(username)
    runs = []
    try:
        names = os.listdir(runs_path)
    except FileNotFoundError:
        return []
    for name in names:
        meta_path = runs_path / name / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        if meta.get("owner") and meta.get("owner") != username:
            continue
        meta["owner"] = username
        runs.append(meta)
    return sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)


def load_run_archive(username: str, run_id: str) -> Dict[str, Any]:
    path = run_dir(username, run_id) / "results.json"
    if not path.is_file():
        raise FileNotFoundError("Run not found.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_run_metadata(username: str, run_id: str) -> Dict[str, Any]:
    path = run_dir(username, run_id) / "metadata.json"
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return {}
    if meta.get("owner") and meta.get("owner") != username:
        raise PermissionError("Run is not owned by this user.")
    return meta


def load_run_prompt(username: str, run_id: str) -> str:
    path = run_dir(username, run_id) / "prompt.txt"
    if not path.is_file():
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_run_into_session(username: str, run_id: str, sess: Dict[str, Any]) -> None:
    data = load_run_archive(username, run_id)
    meta = load_run_metadata(username, run_id)
    csv_cols = data.get("csv_cols") or []
    rows = data.get("rows") or []
    sess["csv_cols"] = csv_cols
    sess["rows"] = rows
    sess["csv_df"] = pd.DataFrame(rows).reindex(columns=csv_cols)
    sess["results"] = data.get("results") or []
    sess["detected_json_keys"] = data.get("detected_json_keys") or []
    sess["current_run_id"] = _safe_run_id(run_id)
    sess["run_name"] = meta.get("run_name", "")
    sess["prompt_name"] = meta.get("prompt_name", "")
    prompt = load_run_prompt(username, run_id)
    if prompt:
        sess["prompt_template"] = prompt
    input_t = meta.get("input_template")
    if input_t is None:
        extra = run_dir(username, run_id) / "input_template.txt"
        input_t = extra.read_text(encoding="utf-8") if extra.is_file() else ""
    sess["input_template"] = input_t or ""
    sess.pop("progress", None)


def run_file(username: str, run_id: str, kind: str) -> Path:
    file_map = {
        "input": "input.csv",
        "output": "output.csv",
        "results": "results.json",
        "metadata": "metadata.json",
        "prompt": "prompt.txt",
        "input_template": "input_template.txt",
        "review": "review.json",
    }
    if kind not in file_map:
        raise ValueError("Unknown run file.")
    load_run_metadata(username, run_id)
    path = run_dir(username, run_id) / file_map[kind]
    if not path.is_file():
        raise FileNotFoundError("Run file not found.")
    return path


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _normalize_value(v) for k, v in row.items()}
