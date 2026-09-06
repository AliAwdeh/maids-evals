"""Shared maids.cc company notes. One file, loaded at call time so UI edits apply immediately."""

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONTEXT_DIR = BASE_DIR / "context"
CONTEXT_FILE = CONTEXT_DIR / "maids_cc.md"
MAX_CONTEXT_CHARS = 80_000

# Always attached to helper/fixer calls, even if someone edits the file.
VOLATILE_RULE = """CRITICAL BEHAVIOR RULE:
Treat the "Volatile facts" section as NON-authoritative.
You must NEVER quote or invent prices, salaries, benefit amounts, employee/client counts, ratings, branch counts, or nationality availability from this company context.
When such a figure is needed, the generated or updated prompt (and any AI answer) must say the figure must be pulled from the owning source, not stated here.
Do not bake specific volatile numbers into generated prompts.
"""


def _ensure_dir() -> None:
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)


def load_company_context() -> str:
    """Return the shared company notes. Empty string if the file is missing or unreadable."""
    try:
        if not CONTEXT_FILE.is_file():
            return ""
        return CONTEXT_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def save_company_context(text: str) -> str:
    """Write the shared company notes. Returns the text that was stored."""
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > MAX_CONTEXT_CHARS:
        raise ValueError(f"Company info is too long (max {MAX_CONTEXT_CHARS:,} characters).")
    stored = text.replace("\r\n", "\n").replace("\r", "\n")
    if stored and not stored.endswith("\n"):
        stored += "\n"
    _ensure_dir()
    tmp = CONTEXT_FILE.with_suffix(".md.tmp")
    tmp.write_text(stored, encoding="utf-8")
    os.replace(tmp, CONTEXT_FILE)
    return stored


def context_preamble() -> str:
    """Company notes plus the volatile-facts rule, loaded fresh each call."""
    body = (load_company_context() or "").strip()
    if body:
        return (
            "Company domain context (shared team notes about maids.cc). "
            "Use this vocabulary and service model. Do not copy volatile numbers.\n\n"
            f"{body}\n\n{VOLATILE_RULE.strip()}"
        )
    return VOLATILE_RULE.strip()


def with_company_context(instructions: str) -> str:
    """Prepend company notes + volatile rule once, ahead of the task instructions."""
    return f"{context_preamble()}\n\n---\n\n{instructions}"


def compose_helper_prompt(instructions: str, payload: Any) -> str:
    """Single preamble + task + payload string sent to the model (one shot, not per row)."""
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)
    return f"{with_company_context(instructions)}\n\n---\n\n{payload}"
