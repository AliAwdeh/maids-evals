import difflib
import json
import re
from typing import Any, Dict, List, Optional

from company_context import compose_helper_prompt
from engine import call_provider, try_parse_json

STRUCTURE_RULES = """
Composition (read-only facts about how the runner works):
- Instructions (the prompt you edit) and Input data (input_template) are SEPARATE.
- When input_template is set, the model sees: instructions, then a line ===== INPUT =====, then the substituted input last.
- Placeholders like {ColumnName} are replaced with the RAW cell. There is no automatic label.
- Never use {row_json} (it duplicates columns already listed). Never put the same column in both parts.
- Large conversation/content belongs only in input_template, never mid-instructions.
- Short condition columns (nationality, status, skill) MAY stay in the instructions as {ColumnName} next to IF/THEN rules.
- JSON output must stay strictly flat (top-level keys, scalar values). No nested objects.
- Prefer boolean fields for yes/no checks, flags, violations, compliance findings, and "did this happen?" answers.
- Every boolean output key must have a sibling string key named exactly <boolean_key>_justification that explains the evidence for the true/false value in one sentence. Do not leave bare booleans without justification.
- For non-boolean categorical output keys, keep or add a closed "Allowed values:" rule when the values are known. Do not invent categories that are not supported by the prompt, notes, or user-provided context.
- Do not ask the model to echo input identifiers (Id, row_id, conversation id). Results are joined to the input row on export.
- You edit INSTRUCTIONS only. Treat input_template as read-only context. Do not pull big data into the instructions.
"""

ANALYST_INSTRUCTIONS = """You are the Analyst in a prompt-repair pipeline for an evaluation prompt.

You receive:
1. The current evaluation prompt / instructions (may be long).
2. The input_template if one exists (read-only; this is how row data is appended).
3. Reviewer notes on specific rows, each with input excerpt, model output, verdict, and note.
4. prior_changes: summaries of what this prompt already is and what was already fixed. Do not undo those fixes. Do not re-introduce those issues.
5. purpose: a short description of what the prompt is for. Keep that intent.

Your job:
- Cluster the notes into a small number of failure patterns (usually 2–8).
- Quote the prompt section that likely caused each pattern.
- Say what rule is missing, too weak, too strong, or contradictory.
- Ignore one-off nits unless they repeat.
- List every row that evidences a pattern in evidence_rows. Be honest: a pattern
  seen in one row gets one row. Do not pad the list to make a change look safer.
- A pattern backed by a single row must be written as a GENERAL rule that would
  also catch cases you have not seen. Never propose a change that names the
  specific customer, maid, agent, id, product or exact wording from that one
  row -- that fixes one row and leaves the rest of the sheet worse.
- If the notes conflict with each other, say so in the problem field instead of
  siding with whichever row you read last.
- Do not rewrite the prompt.
- Treat missing boolean justifications as a real repair target: if an answer contains a boolean with no explanation field, identify that as weakening later review/fixing.
- For categorical fields, check whether the prompt defines allowed values. If reviewer notes show confusion caused by open-ended categories, identify the missing or unclear value set.
- Keep maids.cc vocabulary (CC, MV, PTC, Enchanters, Resolvers, prospect vs client vs maid).
- Never add prices, salaries, counts, ratings, or nationality availability from company notes.

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
""" + STRUCTURE_RULES

EDITOR_INSTRUCTIONS = """You are the Editor in a prompt-repair pipeline.

You receive the original instructions, a read-only input_template, and the Analyst JSON.

Rules:
- Make SURGICAL edits only. Do not rewrite the prompt.
- Keep structure, headings, and voice.
- Change the minimum number of paragraphs needed to address the patterns.
- Do not drop existing rules listed in do_not_change unless they directly contradict a needed fix.
- If a pattern can be fixed by adding one sentence under the existing heading, do that.
- Return the FULL updated prompt, not a patch.
- Keep maids.cc terms consistent. Do not bake volatile figures (prices, salaries, counts, ratings, nationality availability) into the prompt.
- Edit INSTRUCTIONS only. Do not move the conversation into the instructions, reintroduce {row_json}, or add id-echo fields.
- If JSON is requested, keep it an explicit, strictly flat object.
- If the prompt uses booleans, ensure every boolean key in the explicit JSON object has a matching <boolean_key>_justification string key. Add a short rule telling the model to justify both true and false decisions with row evidence.
- If the prompt uses categorical fields and the allowed values are known from the prompt or notes, state those values explicitly. If the allowed values are not known, leave a concise warning/risk instead of inventing them.

Return JSON only:
{
  "updated_prompt": "the full prompt text",
  "change_summary": ["one line per edit"],
  "purpose": "one or two sentences saying what this prompt is for now"
}
""" + STRUCTURE_RULES

CRITIC_INSTRUCTIONS = """You are the Critic in a prompt-repair pipeline.

You receive the original prompt, the editor's updated prompt, the analyst patterns, and the reviewer notes.

Reject or trim edits that:
- Contradict an existing core rule that still looks correct
- Overfit a single row. Treat these as overfitting and reject them:
    * a rule that repeats a name, id, phone number, date, product or verbatim
      phrase that appears in only ONE of the reviewer notes
    * a rule whose only support is one row, written narrowly enough that it
      would not catch a similar case worded differently
    * an example bolted into the instructions to make one row come out right
  A single-row pattern may still be fixed -- but only by a general rule.
- Rewrite large unrelated sections
- Weaken a rule that other notes still need
- Introduce prices, salaries, counts, ratings, or nationality availability from company notes
- Pull large input data into the instructions, add {row_json}, or ask the model to echo input identifiers
- Remove a boolean field's matching *_justification key, or add a boolean key without a justification sibling
- Invent categorical allowed values that the prompt, notes, or user-provided context do not support

For each pattern, weigh how many rows support it against how much of the prompt
the edit changes. A one-row pattern that rewrites a core rule is the most
dangerous edit you can approve.

If the update is mostly good, keep it and list remaining risks.
If an edit is harmful, revert that part by returning a safer prompt.

Return JSON only:
{
  "accepted": true,
  "final_prompt": "the full prompt to offer the user",
  "rejected_edits": ["what you rejected and why"],
  "risks": ["residual risks"]
}
""" + STRUCTURE_RULES


MAX_EXAMPLES = 40
MAX_NOTES_CHARS = 60_000


def _parse_agent_json(text: str) -> Dict[str, Any]:
    parsed, err = try_parse_json(text)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError(f"Agent did not return JSON: {err or 'invalid payload'}")


def select_examples(examples: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], str]:
    """Trim a review batch down to a payload one model call can actually hold.

    A run with 200 notes builds a ~330k-character request. Nothing rejects it
    up front -- it fails at the provider, after the user has waited. Failures
    the reviewer wrote about are what the Analyst needs, so keep those first and
    say plainly what was left out.
    """
    if not examples:
        return [], ""

    def rank(item: Dict[str, Any]) -> tuple:
        verdict = str(item.get("verdict") or "").lower()
        has_note = 1 if str(item.get("note") or "").strip() else 0
        weight = {"wrong": 0, "unclear": 1, "ok": 2}.get(verdict, 3)
        return (1 - has_note, weight, item.get("row", 0))

    ordered = sorted(examples, key=rank)
    kept: List[Dict[str, Any]] = []
    used = 0
    for item in ordered:
        size = len(json.dumps(item, ensure_ascii=False))
        if kept and (len(kept) >= MAX_EXAMPLES or used + size > MAX_NOTES_CHARS):
            continue
        kept.append(item)
        used += size
    kept.sort(key=lambda i: i.get("row", 0))
    dropped = len(examples) - len(kept)
    if not dropped:
        return kept, ""
    note = (
        f"Used {len(kept)} of {len(examples)} notes in this pass "
        "(wrong and unclear rows with a written note come first). "
        "Run the fixer again after accepting to work through the rest."
    )
    return kept, note


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


_WORD_RE = re.compile(r"[\w'’]+")
SHINGLE = 6


def _shingles(text: str, n: int = SHINGLE) -> set:
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    if len(words) < n:
        return set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def overfit_warnings(
    original: str,
    proposed: str,
    examples: List[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Catch the two tells of a fix written for one row.

    The Critic is asked to reject overfitting, but it is the same kind of model
    that just wrote the edit. These checks do not depend on it:

    1. Wording lifted from exactly one reviewed row into the new prompt. A rule
       carrying a phrase that appears in one note and nowhere else is that row's
       patch, not a rule.
    2. A pattern the Analyst itself backed with one row out of many.
    """
    warnings: List[str] = []
    added = _shingles(proposed) - _shingles(original)
    if added and len(examples) > 1:
        culprits: Dict[int, List[str]] = {}
        for phrase in added:
            owners = [
                ex for ex in examples
                if phrase in _shingles(f"{ex.get('input', '')} {ex.get('output', '')} {ex.get('note', '')}")
            ]
            if len(owners) == 1:
                row = owners[0].get("row", 0)
                culprits.setdefault(row, []).append(phrase)
        for row, phrases in sorted(culprits.items())[:3]:
            sample = sorted(phrases, key=len, reverse=True)[0]
            warnings.append(
                f"The new prompt repeats wording that appears only in row {row}: "
                f"“{sample}”. Check this is a rule and not a patch for that one row."
            )

    total = len(examples)
    if total >= 4 and isinstance(analysis, dict):
        thin = [
            str(p.get("title") or "a pattern")
            for p in (analysis.get("patterns") or [])
            if isinstance(p, dict) and len(p.get("evidence_rows") or []) <= 1
        ]
        if thin:
            names = ", ".join(f"“{t}”" for t in thin[:3])
            warnings.append(
                f"{len(thin)} of {len(analysis.get('patterns') or [])} changes rest on a single row "
                f"out of {total} you reviewed ({names}). Re-run the noted rows before saving."
            )
    return warnings


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


RETRY_SUFFIX = (
    "\n\nYour previous reply was not valid JSON and was rejected. "
    "Reply again with the JSON object only: no prose before it, no prose after it, "
    "no markdown fence."
)


def _ask(provider: str, api_key: str, model: str, system: str, payload: str) -> Dict[str, Any]:
    """One agent turn. Retries once when the model answers in prose.

    Each stage of this pipeline costs a model call, so losing a good Editor
    result because the Critic opened with "Sure!" is expensive. One retry with
    an explicit correction recovers the common case.
    """
    prompt = compose_helper_prompt(system, payload)
    raw = call_provider(provider, api_key, model, prompt, json_mode=True)
    try:
        return _parse_agent_json(raw)
    except ValueError:
        retry = call_provider(provider, api_key, model, prompt + RETRY_SUFFIX, json_mode=True)
        return _parse_agent_json(retry)



_BOOL_KEY_RE = re.compile(r'"([A-Za-z0-9_.\- ]+)"\s*:\s*(true|false)\b', re.I)
_ANY_KEY_RE = re.compile(r'"([A-Za-z0-9_.\- ]+)"\s*:')


def _boolean_keys(text: str) -> set:
    return {m.group(1).strip() for m in _BOOL_KEY_RE.finditer(text or "")}


def _declared_keys(text: str) -> set:
    return {m.group(1).strip() for m in _ANY_KEY_RE.finditer(text or "")}


def contract_warnings(original: str, proposed: str) -> List[str]:
    """Check the edit kept the shape the whole loop depends on.

    Every boolean is supposed to carry a <key>_justification sibling, because
    that sentence is the evidence the Analyst reads on the next pass. The
    Critic is told to reject edits that break it, but the Critic is the same
    kind of model that just wrote the edit -- this check does not depend on it.
    """
    warnings: List[str] = []
    before_keys = _declared_keys(original)
    after_keys = _declared_keys(proposed)

    bare = sorted(
        key for key in _boolean_keys(proposed)
        if f"{key}_justification" not in after_keys
    )
    if bare:
        listed = ", ".join(f"{k}" for k in bare[:4])
        warnings.append(
            f"{len(bare)} boolean field(s) in the new prompt have no matching "
            f"_justification ({listed}). Without that sentence the next fix has no "
            "evidence for why a row passed or failed."
        )

    dropped = sorted(
        key for key in before_keys
        if key.endswith("_justification") and key not in after_keys
    )
    if dropped:
        listed = ", ".join(dropped[:4])
        warnings.append(
            f"The edit removed {len(dropped)} justification field(s) that the old "
            f"prompt asked for ({listed}). Put them back before saving."
        )
    return warnings


def improve_prompt(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    examples: List[Dict[str, Any]],
    input_template: str = "",
    prior_changes: Optional[List[Dict[str, Any]]] = None,
    purpose: str = "",
) -> Dict[str, Any]:
    if not examples:
        raise ValueError("Add at least one review note before improving the prompt.")
    if not (prompt or "").strip():
        raise ValueError("There is no prompt to improve.")
    examples, sampling_note = select_examples(examples)

    framing = {
        "input_template": input_template or "",
        "input_template_note": (
            "Read-only. Substituted and appended after ===== INPUT =====. "
            "Do not edit this; do not copy its placeholders into the instructions."
            if (input_template or "").strip()
            else "Empty — this run uses instructions-only (legacy)."
        ),
        "purpose": (purpose or "").strip(),
        "prior_changes": prior_changes or [],
        "prior_changes_note": (
            "These changes already happened. Do not reverse them. "
            "Do not re-introduce the same failure."
            if prior_changes else
            "No prior change log yet."
        ),
    }
    analyst_payload = json.dumps(
        {"prompt": prompt, "notes": examples, **framing},
        ensure_ascii=False,
    )
    analysis = _ask(provider, api_key, model, ANALYST_INSTRUCTIONS, analyst_payload)

    editor_payload = json.dumps(
        {"prompt": prompt, "analysis": analysis, **framing},
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
            **framing,
        },
        ensure_ascii=False,
    )
    warnings: List[str] = []
    if sampling_note:
        warnings.append(sampling_note)
    try:
        critique = _ask(provider, api_key, model, CRITIC_INSTRUCTIONS, critic_payload)
    except Exception as e:
        # The Editor already produced a usable prompt. Losing it because the
        # Critic misbehaved would throw away two paid calls and the reviewer's
        # whole batch of notes, so keep the edit and say it went unreviewed.
        critique = {"accepted": False, "final_prompt": "", "rejected_edits": [], "risks": []}
        warnings.append(
            f"The critic did not return a usable review ({e}). "
            "This proposal is the editor's version, unchecked. Read the diff closely before saving."
        )
    final_prompt = critique.get("final_prompt") or updated
    if not str(final_prompt).strip():
        final_prompt = updated

    purpose_out = str(edited.get("purpose") or purpose or "").strip()
    summary = [str(x) for x in (edited.get("change_summary") or []) if str(x).strip()]
    diff = unified_diff(prompt, final_prompt)
    unchanged = str(final_prompt).strip() == str(prompt).strip()
    if not unchanged:
        warnings.extend(overfit_warnings(prompt, final_prompt, examples, analysis))
        warnings.extend(contract_warnings(prompt, final_prompt))
    if unchanged:
        # A summary that claims edits next to an identical prompt is how a
        # no-op gets saved as agent_eval.v2 and trusted as a real improvement.
        warnings.append(
            "The pass ended with the prompt unchanged. There is nothing to save as a new version."
        )
    return {
        "analysis": analysis,
        "editor": edited,
        "critic": critique,
        "proposed_prompt": final_prompt,
        "diff": diff,
        "change_summary": [] if unchanged else summary,
        "purpose": purpose_out,
        "unchanged": unchanged,
        "warnings": warnings,
        "examples_used": len(examples),
    }
