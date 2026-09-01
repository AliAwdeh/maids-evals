import csv
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from openai import OpenAI
from google import genai

OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "https://ai.aliawdeh.com/api")
LANGCC_API_BASE = os.getenv("LANGCC_API_BASE", "https://langcc.maidstech.ai/v1")
MAX_REQUEST_WORKERS_CAP = max(1, int(os.getenv("MAX_REQUEST_WORKERS_CAP", "64")))


def safe_format(template: str, row: Dict[str, Any]) -> str:
    class SafeDict(dict):
        def __missing__(self, key):
            return ""

    try:
        return template.format_map(SafeDict(row))
    except ValueError:
        pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
        return pattern.sub(lambda m: str(row.get(m.group(1), "")), template)


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


def render_prompt(prompt_template: str, row: Dict[str, Any]) -> str:
    row_json = json.dumps(row, ensure_ascii=False)
    row_for_template = dict(row)
    row_for_template["row_json"] = row_json
    tmp = prompt_template.replace("{{row_json}}", "{row_json}")
    return safe_format(tmp, row_for_template)


def process_row(
    row: Dict[str, Any],
    csv_cols: List[str],
    prompt_template: str,
    provider: str,
    api_key: str,
    model: str,
    is_json_mode: bool,
    model_params: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], set, str, str]:
    import time

    prompt = render_prompt(prompt_template, row)
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


def call_openai(api_key: str, model: str, prompt: str, model_params: Optional[Dict[str, Any]] = None) -> str:
    client = OpenAI(api_key=api_key)
    resp = client.responses.create(model=model, input=prompt, **(model_params or {}))
    return resp.output_text


def call_langcc(api_key: str, model: str, prompt: str, model_params: Optional[Dict[str, Any]] = None) -> str:
    client = OpenAI(api_key=api_key, base_url=LANGCC_API_BASE)
    resp = client.responses.create(model=model, input=prompt, **(model_params or {}))
    return resp.output_text


def call_gemini(api_key: str, model: str, prompt: str, model_params: Optional[Dict[str, Any]] = None) -> str:
    client = genai.Client(api_key=api_key)
    config = {}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("max_output_tokens", "max_output_tokens")):
        if model_params and src in model_params:
            config[dst] = model_params[src]
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
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
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
        return call_openai(api_key, model, prompt, model_params=model_params)
    if p == "langcc":
        return call_langcc(api_key, model, prompt, model_params=model_params)
    if p == "gemini":
        return call_gemini(api_key, model, prompt, model_params=model_params)
    if p == "ollama":
        return call_ollama(model, prompt, json_mode=json_mode, model_params=model_params)
    raise ValueError("Unsupported provider")
