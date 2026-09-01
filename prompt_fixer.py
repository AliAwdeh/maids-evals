import difflib
import json
import re
from typing import Any, Dict, List

from engine import call_provider, try_parse_json

ANALYST_INSTRUCTIONS = """You are the Analyst in a prompt-repair pipeline for an evaluation prompt.

You receive:
1. The current evaluation prompt (may be long).
2. Reviewer notes on specific rows, each with input excerpt, model output, verdict, and note.

Your job:
- Cluster the notes into a small number of failure patterns (usually 2–8).
- Quote the prompt section that likely caused each pattern.
- Say what rule is missing, too weak, too strong, or contradictory.
- Ignore one-off nits unless they repeat.
- Do not rewrite the prompt.

Return JSON only:
{
  "patterns": [
    {
      "title": "short name",
      "evidence_rows": [0, 3],
      "prompt_section": "short quote from the prompt",
      "problem": "what is wrong",
      "needed_change": "surgical change to make"
    }
  ],
  "do_not_change": ["existing rules that must stay"]
}
"""

EDITOR_INSTRUCTIONS = """You are the Editor in a prompt-repair pipeline.

You receive the original prompt and the Analyst JSON.

Rules:
- Make SURGICAL edits only. Do not rewrite the prompt.
- Keep structure, headings, and voice.
- Change the minimum number of paragraphs needed to address the patterns.
- Do not drop existing rules listed in do_not_change unless they directly contradict a needed fix.
- If a pattern can be fixed by adding one sentence under the existing heading, do that.
- Return the FULL updated prompt, not a patch.

Return JSON only:
{
  "updated_prompt": "the full prompt text",
  "change_summary": ["one line per edit"]
}
"""

CRITIC_INSTRUCTIONS = """You are the Critic in a prompt-repair pipeline.

You receive the original prompt, the editor's updated prompt, the analyst patterns, and the reviewer notes.

Reject or trim edits that:
- Contradict an existing core rule that still looks correct
- Overfit a single row
- Rewrite large unrelated sections
- Weaken a rule that other notes still need

If the update is mostly good, keep it and list remaining risks.
If an edit is harmful, revert that part by returning a safer prompt.

Return JSON only:
{
  "accepted": true,
  "final_prompt": "the full prompt to offer the user",
  "rejected_edits": ["what you rejected and why"],
  "risks": ["residual risks"]
}
"""


def _parse_agent_json(text: str) -> Dict[str, Any]:
    parsed, err = try_parse_json(text)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError(f"Agent did not return JSON: {err or 'invalid payload'}")


def _excerpt(value: Any, limit: int = 1200) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_examples(results: List[Dict[str, Any]], csv_cols: List[str], notes: Dict[str, Any]) -> List[Dict[str, Any]]:
    examples = []
    for key, note in notes.items():
        try:
            idx = int(key)
        except Exception:
            continue
        if idx < 0 or idx >= len(results):
            continue
        verdict = (note or {}).get("verdict") or ""
        text = ((note or {}).get("note") or "").strip()
        if not verdict and not text:
            continue
        row = results[idx]
        input_bits = []
        for col in csv_cols[:8]:
            val = row.get(col, "")
            if val:
                input_bits.append(f"{col}: {_excerpt(val, 400)}")
        examples.append(
            {
                "row": idx,
                "verdict": verdict,
                "note": text,
                "input": " | ".join(input_bits),
                "output": _excerpt(row.get("llm_output", ""), 1600),
            }
        )
    return examples


def build_example(
    row: Dict[str, Any],
    csv_cols: List[str],
    output: Any,
    verdict: str,
    note: str,
    row_index: int = 0,
) -> Dict[str, Any]:
    """Shape a single row (e.g. a Test row) like build_examples() entries."""
    input_bits = []
    for col in csv_cols[:8]:
        val = (row or {}).get(col, "")
        if val:
            input_bits.append(f"{col}: {_excerpt(val, 400)}")
    return {
        "row": row_index,
        "verdict": verdict or "",
        "note": (note or "").strip(),
        "input": " | ".join(input_bits),
        "output": _excerpt(output, 1600),
    }


def unified_diff(original: str, updated: str) -> List[str]:
    left = (original or "").splitlines()
    right = (updated or "").splitlines()
    return list(
        difflib.unified_diff(
            left,
            right,
            fromfile="current prompt",
            tofile="proposed prompt",
            lineterm="",
        )
    )


def _ask(provider: str, api_key: str, model: str, system: str, payload: str) -> Dict[str, Any]:
    prompt = f"{system}\n\n---\n\n{payload}"
    raw = call_provider(provider, api_key, model, prompt, json_mode=True)
    return _parse_agent_json(raw)


def improve_prompt(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    examples: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not examples:
        raise ValueError("Add at least one review note before improving the prompt.")
    if not (prompt or "").strip():
        raise ValueError("There is no prompt to improve.")

    analyst_payload = json.dumps(
        {"prompt": prompt, "notes": examples},
        ensure_ascii=False,
    )
    analysis = _ask(provider, api_key, model, ANALYST_INSTRUCTIONS, analyst_payload)

    editor_payload = json.dumps(
        {"prompt": prompt, "analysis": analysis},
        ensure_ascii=False,
    )
    edited = _ask(provider, api_key, model, EDITOR_INSTRUCTIONS, editor_payload)
    updated = edited.get("updated_prompt") or prompt

    critic_payload = json.dumps(
        {
            "original_prompt": prompt,
            "updated_prompt": updated,
            "analysis": analysis,
            "notes": examples,
            "change_summary": edited.get("change_summary") or [],
        },
        ensure_ascii=False,
    )
    critique = _ask(provider, api_key, model, CRITIC_INSTRUCTIONS, critic_payload)
    final_prompt = critique.get("final_prompt") or updated
    if not str(final_prompt).strip():
        final_prompt = updated

    return {
        "analysis": analysis,
        "editor": edited,
        "critic": critique,
        "proposed_prompt": final_prompt,
        "diff": unified_diff(prompt, final_prompt),
    }
