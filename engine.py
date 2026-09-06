import csv
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from openai import OpenAI
from google import genai

OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "https://ai.aliawdeh.com/api")
LANGCC_API_BASE = os.getenv("LANGCC_API_BASE", "https://langcc.maidstech.ai/v1")
MAX_REQUEST_WORKERS_CAP = max(1, int(os.getenv("MAX_REQUEST_WORKERS_CAP", "64")))

# A placeholder is {Column Name}. Real spreadsheet headers contain spaces, dots,
# accents and Arabic, so the name cannot be restricted to Python identifiers --
# doing that made {Client Id} invisible to the mapping check while still being
# substituted at run time, which produced silent blank fields.
# Characters that only ever appear in a literal JSON example ("  :  ,) are
# excluded so the JSON schema an eval prompt asks the model to return is never
# mistaken for a placeholder. Newlines are excluded for the same reason.
PLACEHOLDER_RE = re.compile(r"\{([^{}\n\"':,]+)\}")


def _placeholder_names(text: str) -> List[str]:
    out = []
    for raw in PLACEHOLDER_RE.findall(text or ""):
        name = raw.strip()
        if name:
            out.append(name)
    return out


def required_placeholders(prompt_template: str, input_template: str = "") -> List[str]:
    names = _placeholder_names(prompt_template) + _placeholder_names(input_template)
    out = []
    for name in names:
        if name == "row_json" or name in out:
            continue
        out.append(name)
    return out


def apply_column_map(row: Dict[str, Any], mapping: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """mapping: prompt placeholder -> spreadsheet column. Adds alias keys on a copy."""
    out = dict(row or {})
    for placeholder, column in (mapping or {}).items():
        ph = str(placeholder or "").strip()
        col = str(column or "").strip()
        if not ph or not col:
            continue
        if col in out:
            out[ph] = out[col]
    return out


def mapping_gaps(
    placeholders: List[str],
    columns: List[str],
    mapping: Optional[Dict[str, str]] = None,
) -> List[str]:
    cols = {str(c) for c in (columns or [])}
    mapping = mapping or {}
    missing = []
    for name in placeholders:
        mapped = (mapping.get(name) or "").strip()
        if name in cols or (mapped and mapped in cols):
            continue
        missing.append(name)
    return missing


def safe_format(template: str, row: Dict[str, Any]) -> str:
    """Substitute {Column} with the raw cell value.

    Deliberately does not use str.format_map: format() treats a literal JSON
    example in the prompt as a field spec, so `{"topic": ""}` could be replaced
    with nothing, silently deleting the output schema the prompt asks for. It
    also raises IndexError on `{0}` and eats `{a, b}`. A single regex pass over
    PLACEHOLDER_RE is predictable and matches exactly what required_placeholders
    reports to the mapping UI.
    """

    def replace(match):
        name = match.group(1).strip()
        if name in row:
            return str(row[name])
        # Unknown field: leave the placeholder visible instead of blanking it,
        # so a mismatch shows up in the Test preview rather than producing a
        # clean-looking run over empty inputs.
        return match.group(0)

    return PLACEHOLDER_RE.sub(replace, template or "")


def try_parse_json(text: str) -> Tuple[Optional[Any], Optional[str]]:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()
    try:
        return json.loads(t), None
    except Exception as e:
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
        if obj and all(isinstance(x, dict) for x in obj):
            for i, v in enumerate(obj):
                nk = f"{parent_key}{sep}{i}" if parent_key else str(i)
                out.update(flatten_json(v, nk, sep=sep))
        else:
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
    return "" if _is_empty_value(v) else v


def _row_is_empty_dict(row: Dict[str, Any]) -> bool:
    return all(_is_empty_value(v) for v in row.values())


def _row_is_empty_series(row) -> bool:
    return all(_is_empty_value(v) for v in row)


def _prompt_has_placeholder(prompt: str, names: List[str]) -> bool:
    for name in names:
        if f"{{{name}}}" in prompt or f"{{{{{name}}}}}" in prompt:
            return True
    return False


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _parse_date(value: str):
    if not value:
        return None
    try:
        from datetime import datetime

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


def _clean_model_params(raw: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or value == "":
            continue
        try:
            if key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                params[key] = float(value)
            elif key in ("max_output_tokens", "seed"):
                params[key] = int(value)
            elif key == "reasoning_effort":
                params[key] = str(value)
        except Exception:
            continue
    return params


def parse_model_params(
    send_model_params: Optional[str],
    enabled_names: Optional[List[str]],
    temperature: Optional[str],
    top_p: Optional[str],
    max_output_tokens: Optional[str],
    presence_penalty: Optional[str],
    frequency_penalty: Optional[str],
    seed: Optional[str],
    reasoning_effort: Optional[str],
) -> Dict[str, Any]:
    if send_model_params != "1":
        return {}
    enabled = set(enabled_names or [])
    raw = {
        "temperature": temperature if "temperature" in enabled else None,
        "top_p": top_p if "top_p" in enabled else None,
        "max_output_tokens": max_output_tokens if "max_output_tokens" in enabled else None,
        "presence_penalty": presence_penalty if "presence_penalty" in enabled else None,
        "frequency_penalty": frequency_penalty if "frequency_penalty" in enabled else None,
        "seed": seed if "seed" in enabled else None,
        "reasoning_effort": reasoning_effort if "reasoning_effort" in enabled else None,
    }
    return _clean_model_params(raw)


def provider_requires_key(provider: str) -> bool:
    return provider.lower().strip() in ("openai", "langcc", "gemini")


def result_to_base_row(row: Dict[str, Any], csv_cols: List[str]) -> Dict[str, Any]:
    return {c: _normalize_value(row.get(c, "")) for c in csv_cols}


def filter_results_by_json(
    results: List[Dict[str, Any]],
    filter_keys: Optional[List[str]],
    filter_vals: Optional[List[str]],
) -> List[int]:
    keys = filter_keys or []
    vals = filter_vals or []
    filters = []
    for key, val in zip(keys, vals):
        key = (key or "").strip()
        val = (val or "").strip().lower()
        if key and val in ("true", "false"):
            filters.append((key, val == "true"))
    if not filters:
        return list(range(len(results)))

    matched = []
    for i, row in enumerate(results):
        flat = row.get("_llm_json_flat", {}) or {}
        ok = True
        for key, target in filters:
            if _coerce_bool(flat.get(key)) is not target:
                ok = False
                break
        if ok:
            matched.append(i)
    return matched


def _json_default(obj: Any):
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)


def _build_output_dataframe(
    results: List[Dict[str, Any]],
    csv_cols: List[str],
    out_csv_cols: Optional[List[str]] = None,
    out_json_keys: Optional[List[str]] = None,
    json_export_mode: str = "flatten",
    flatten_sep: str = ".",
    out_prefix: str = "out_",
) -> pd.DataFrame:
    out_csv_cols = [c for c in (out_csv_cols if out_csv_cols is not None else csv_cols) if c in csv_cols]
    exported_rows: List[Dict[str, Any]] = []
    column_order: List[str] = []

    def remember_cols(cols):
        for k in cols:
            if k not in column_order:
                column_order.append(k)

    remember_cols(out_csv_cols)
    remember_cols(["llm_output", "llm_api_failed", "llm_api_error", "llm_error", "llm_latency_s"])

    for r in results:
        row_out: Dict[str, Any] = {}
        for c in out_csv_cols:
            row_out[c] = r.get(c, "")

        row_out["llm_output"] = r.get("llm_output", "")
        row_out["llm_api_failed"] = r.get("llm_api_failed", False)
        row_out["llm_api_error"] = r.get("llm_api_error", "")
        row_out["llm_error"] = r.get("llm_error", "")
        row_out["llm_latency_s"] = r.get("llm_latency_s", "")

        if json_export_mode == "raw_json":
            row_out["llm_json_raw"] = r.get("llm_json_raw", "")
            row_out["llm_json_valid"] = r.get("llm_json_valid", False)
            row_out["llm_json_error"] = r.get("llm_json_error", "")
        else:
            flat = r.get("_llm_json_flat", {}) or {}
            keys = out_json_keys if out_json_keys else list(flat.keys())
            for k in keys:
                kk = k.replace(".", flatten_sep) if flatten_sep != "." else k
                row_out[f"{out_prefix}{kk}"] = flat.get(k, "")

        exported_rows.append(row_out)
        remember_cols(row_out.keys())

    out_df = pd.DataFrame(exported_rows)
    if column_order:
        out_df = out_df.reindex(columns=column_order)
    return out_df


def _write_csv(path: str, df: pd.DataFrame):
    df.to_csv(path, index=False, quoting=csv.QUOTE_ALL, lineterminator="\n")


# Instructions first, then this header, then substituted input data. Keep in sync with the UI copy.
INPUT_SECTION_HEADER = "===== INPUT ====="


def render_prompt(prompt_template: str, row: Dict[str, Any]) -> str:
    row_json = json.dumps(row, ensure_ascii=False)
    row_for_template = dict(row)
    row_for_template["row_json"] = row_json
    tmp = (prompt_template or "").replace("{{row_json}}", "{row_json}")
    return safe_format(tmp, row_for_template)


def compose_model_input(
    prompt_template: str,
    row: Dict[str, Any],
    input_template: str = "",
    column_map: Optional[Dict[str, str]] = None,
) -> str:
    """Build the text sent to the model.

    Placeholders are replaced with the raw cell value (no automatic label).
    If input_template is empty, this is exactly render_prompt(prompt_template, row).
    Otherwise: substituted instructions, a blank line, ===== INPUT =====, then the data last.
    """
    used = apply_column_map(row, column_map)
    instructions = render_prompt(prompt_template or "", used)
    extra = input_template or ""
    if not extra.strip():
        return instructions
    data = render_prompt(extra, used)
    return f"{instructions.rstrip()}\n\n{INPUT_SECTION_HEADER}\n{data}"


def templates_have_placeholder(
    prompt_template: str,
    input_template: str,
    names: List[str],
    column_map: Optional[Dict[str, str]] = None,
) -> bool:
    allowed = list(names or [])
    colset = {str(n) for n in allowed}
    for placeholder, column in (column_map or {}).items():
        ph = str(placeholder or "").strip()
        col = str(column or "").strip()
        if ph and col in colset:
            allowed.append(ph)
    if _prompt_has_placeholder(prompt_template or "", allowed):
        return True
    if (input_template or "").strip() and _prompt_has_placeholder(input_template, allowed):
        return True
    placeholders = required_placeholders(prompt_template, input_template)
    if not placeholders:
        return False
    return bool(
        [p for p in placeholders if p in colset or ((column_map or {}).get(p) or "").strip() in colset]
    )


def row_input_error(
    prompt_template: str,
    input_template: str,
    columns: List[str],
    column_map: Optional[Dict[str, str]] = None,
) -> str:
    """Empty if every placeholder resolves and the prompt actually gets row data.

    Every unresolved placeholder is reported, not just the case where nothing
    resolves at all. One good placeholder used to be enough to pass this gate,
    so a prompt written against `{Client Id}` would run to completion against a
    sheet that calls it `client_id` -- substituting nothing, reporting no error,
    and billing the full batch for answers built on missing fields.
    """
    cols = list(columns or [])
    gaps = mapping_gaps(required_placeholders(prompt_template, input_template), cols, column_map)
    if gaps:
        names = ", ".join("{" + g + "}" for g in gaps)
        lead = "This prompt field is not" if len(gaps) == 1 else "These prompt fields are not"
        return f"{lead} in your sheet: {names}. Map each one to a column, or fix the spelling."
    if templates_have_placeholder(prompt_template, input_template, ["row_json"] + cols, column_map):
        return ""
    return "Add a {column} in Instructions or Input data, or map a prompt field to a sheet column."


def process_row(
    row: Dict[str, Any],
    csv_cols: List[str],
    prompt_template: str,
    provider: str,
    api_key: str,
    model: str,
    is_json_mode: bool,
    model_params: Optional[Dict[str, Any]],
    input_template: str = "",
    column_map: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], set, str, str]:
    import time

    prompt = compose_model_input(prompt_template, row, input_template, column_map=column_map)
    t0 = time.time()
    try:
        text = call_provider(
            provider,
            api_key.strip(),
            model.strip(),
            prompt,
            json_mode=is_json_mode,
            model_params=model_params,
        )
        err = ""
        api_failed = False
    except Exception as e:
        text = ""
        err = str(e)
        api_failed = True
    latency = round(time.time() - t0, 3)

    out: Dict[str, Any] = {c: row.get(c) for c in csv_cols}
    out["llm_output"] = text
    out["llm_api_failed"] = api_failed
    out["llm_api_error"] = err
    out["llm_error"] = ""
    out["llm_latency_s"] = latency

    row_keys: set = set()
    if is_json_mode and text:
        parsed, perr = try_parse_json(text)
        out["llm_json_valid"] = parsed is not None
        out["llm_json_error"] = perr or ""
        out["llm_error"] = perr or ""
        out["llm_json_raw"] = text
        if parsed is not None:
            flat = flatten_json(parsed, sep=".")
            out["_llm_json_flat"] = flat
            row_keys = set(flat.keys())

    return out, row_keys, prompt, text


# Not every OpenAI-compatible gateway accepts the JSON response format, and a
# rejection only shows up as an API error at call time. Probe once per
# (base_url, model) and remember the answer so a gateway that cannot do it costs
# one extra request in total rather than one per row.
_json_mode_unsupported: set = set()
_json_mode_lock = threading.Lock()

_gemini_clients: Dict[str, Any] = {}


def _gemini_client(api_key: str):
    with _clients_lock:
        client = _gemini_clients.get(api_key)
        if client is None:
            client = genai.Client(api_key=api_key)
            _gemini_clients[api_key] = client
        return client


def _json_mode_ok(scope: str) -> bool:
    with _json_mode_lock:
        return scope not in _json_mode_unsupported


def _mark_json_mode_unsupported(scope: str) -> None:
    with _json_mode_lock:
        _json_mode_unsupported.add(scope)


# One client per (base_url, key), reused for the life of the process.
#
# A fresh OpenAI() per call means a fresh connection pool and a fresh TLS
# handshake for every single row -- on a 5,000-row batch that is 5,000
# handshakes, and it is the single largest avoidable cost in a run. The SDK's
# client is thread-safe, so the worker pool can share one.
REQUEST_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
_clients: Dict[str, "OpenAI"] = {}
_clients_lock = threading.Lock()


def _client_for(api_key: str, base_url: Optional[str] = None) -> "OpenAI":
    cache_key = f"{base_url or 'default'}::{api_key}"
    with _clients_lock:
        client = _clients.get(cache_key)
        if client is None:
            kwargs: Dict[str, Any] = {
                "api_key": api_key,
                "timeout": REQUEST_TIMEOUT,
                "max_retries": 2,
            }
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
            _clients[cache_key] = client
        return client


def _is_unsupported_param_error(exc: Exception) -> bool:
    """True only when the endpoint rejected the JSON response format itself.

    A timeout or a 429 must not permanently disable JSON mode for a model that
    supports it perfectly well.
    """
    status = getattr(exc, "status_code", None)
    if status is not None and status not in (400, 404, 422):
        return False
    text = str(exc).lower()
    if status in (400, 404, 422):
        return True
    return any(
        hint in text
        for hint in ("unsupported", "unrecognized", "unknown parameter", "invalid_request", "not supported")
    )


def _responses_call(
    client: "OpenAI",
    model: str,
    prompt: str,
    json_mode: bool,
    model_params: Optional[Dict[str, Any]],
    scope: str,
) -> str:
    params = dict(model_params or {})
    if json_mode and _json_mode_ok(scope):
        try:
            resp = client.responses.create(
                model=model,
                input=prompt,
                text={"format": {"type": "json_object"}},
                **params,
            )
            return resp.output_text
        except Exception as e:
            # Only stop asking for JSON when the endpoint actually refused the
            # parameter. The prompt still asks for JSON and try_parse_json still
            # repairs it, so falling back costs accuracy, not correctness.
            if not _is_unsupported_param_error(e):
                raise
            _mark_json_mode_unsupported(scope)
    resp = client.responses.create(model=model, input=prompt, **params)
    return resp.output_text


def call_openai(
    api_key: str,
    model: str,
    prompt: str,
    json_mode: bool = False,
    model_params: Optional[Dict[str, Any]] = None,
) -> str:
    return _responses_call(
        _client_for(api_key), model, prompt, json_mode, model_params, f"openai::{model}"
    )


def call_langcc(
    api_key: str,
    model: str,
    prompt: str,
    json_mode: bool = False,
    model_params: Optional[Dict[str, Any]] = None,
) -> str:
    return _responses_call(
        _client_for(api_key, LANGCC_API_BASE), model, prompt, json_mode, model_params,
        f"{LANGCC_API_BASE}::{model}",
    )


def call_gemini(
    api_key: str,
    model: str,
    prompt: str,
    json_mode: bool = False,
    model_params: Optional[Dict[str, Any]] = None,
) -> str:
    client = _gemini_client(api_key)
    config = {}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("max_output_tokens", "max_output_tokens")):
        if model_params and src in model_params:
            config[dst] = model_params[src]
    scope = f"gemini::{model}"
    if json_mode and _json_mode_ok(scope):
        try:
            return (
                client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={**config, "response_mime_type": "application/json"},
                ).text
                or ""
            )
        except Exception:
            _mark_json_mode_unsupported(scope)
    resp = client.models.generate_content(model=model, contents=prompt, config=config or None)
    return resp.text or ""


def call_ollama(model: str, prompt: str, json_mode: bool, model_params: Optional[Dict[str, Any]] = None) -> str:
    payload: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    if json_mode:
        payload["format"] = "json"
    if model_params:
        options = {}
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "seed"):
            if key in model_params:
                options[key] = model_params[key]
        if "max_output_tokens" in model_params:
            options["num_predict"] = model_params["max_output_tokens"]
        if options:
            payload["options"] = options
    r = requests.post(f"{OLLAMA_API_BASE}/generate", json=payload, timeout=600)
    r.raise_for_status()
    return r.json().get("response", "")


def fetch_ollama_models() -> List[str]:
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
    seen = set()
    deduped = []
    for name in models:
        if name not in seen:
            deduped.append(name)
            seen.add(name)
    return deduped


def fetch_openai_compatible_models(api_key: str, base_url: Optional[str] = None) -> List[str]:
    client = _client_for(api_key, base_url)
    models_page = client.models.list()
    models = [m.id for m in models_page.data if getattr(m, "id", None)]
    return sorted(models)


def call_provider(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    json_mode: bool,
    model_params: Optional[Dict[str, Any]] = None,
) -> str:
    p = provider.lower().strip()
    if p == "openai":
        return call_openai(api_key, model, prompt, json_mode=json_mode, model_params=model_params)
    if p == "langcc":
        return call_langcc(api_key, model, prompt, json_mode=json_mode, model_params=model_params)
    if p == "gemini":
        return call_gemini(api_key, model, prompt, json_mode=json_mode, model_params=model_params)
    if p == "ollama":
        return call_ollama(model, prompt, json_mode=json_mode, model_params=model_params)
    raise ValueError("Unsupported provider")


def _stringify_headers(df: pd.DataFrame) -> pd.DataFrame:
    new_cols = []
    seen: Dict[str, int] = {}
    for i, c in enumerate(df.columns):
        name = "" if _is_empty_value(c) else str(c).strip()
        if not name or name.lower().startswith("unnamed"):
            name = f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        new_cols.append(name)
    df = df.copy()
    df.columns = new_cols
    return df


def _normalize_table(df: pd.DataFrame) -> pd.DataFrame:
    df = _stringify_headers(df)
    df = df.loc[~df.apply(_row_is_empty_series, axis=1)].reset_index(drop=True)
    df = df.map(_normalize_value) if hasattr(df, "map") else df.applymap(_normalize_value)
    return df


def _looks_like_xlsx(raw: bytes, filename: str) -> bool:
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return True
    return raw[:2] == b"PK"


def _looks_like_xls(raw: bytes, filename: str) -> bool:
    name = (filename or "").lower()
    if name.endswith(".xls"):
        return True
    return raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def load_tabular_file(raw: bytes, filename: str = "") -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Parse CSV or Excel into a normalized DataFrame. Always uses the first sheet."""
    import io

    name = (filename or "").lower()
    meta: Dict[str, Any] = {"source": "csv", "sheets": [], "used_sheet": "", "note": ""}

    if _looks_like_xlsx(raw, name) or _looks_like_xls(raw, name):
        is_legacy = _looks_like_xls(raw, name) and not _looks_like_xlsx(raw, name)
        engine = None if is_legacy else "openpyxl"
        try:
            xl = pd.ExcelFile(io.BytesIO(raw), engine=engine)
        except Exception as e:
            if is_legacy:
                raise ValueError("This older .xls file could not be read. Save it as .xlsx and try again.") from e
            raise ValueError(f"Could not read this Excel file: {e}") from e
        sheets = [str(s) for s in (xl.sheet_names or [])]
        if not sheets:
            raise ValueError("This Excel file has no sheets.")
        used = sheets[0]
        df = xl.parse(used)
        meta.update({"source": "xls" if is_legacy else "xlsx", "sheets": sheets, "used_sheet": used})
        if len(sheets) > 1:
            meta["note"] = f"This workbook has {len(sheets)} sheets. We used the first one ({used})."
        df = _normalize_table(df)
        if df.empty:
            raise ValueError("The first sheet has no non-empty rows.")
        return df, meta

    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        raise ValueError(f"Could not read this file as a spreadsheet: {e}") from e
    df = _normalize_table(df)
    if df.empty:
        raise ValueError("This file has no non-empty rows.")
    return df, meta
