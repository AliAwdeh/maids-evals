"""Guided analysis-prompt builder. Thin two-call orchestration on the user's LLM."""

import json
import random
import re
from typing import Any, Dict, List, Optional

from company_context import compose_helper_prompt
from engine import call_provider, try_parse_json

SAMPLE_ROWS = 5
CELL_LIMIT = 280
MAX_QUESTIONS = 3
MAX_DEEP_QUESTIONS_PER_ROUND = 5
MAX_DEEP_TOTAL_QUESTIONS = 20
DEEP_READY_COVERAGE = 85

TOOL_LIMITS = """
You can ONLY write or update an analysis prompt (instructions + input template). You cannot edit the spreadsheet, add columns, join rows, run the batch, or use any other tool. If needed data is missing, say the user should add a column to the file — do not invent a join or a lookup.
"""

ANSWER_SHAPE_RULES = """
Answer-shape rules:
- Prefer boolean fields for yes/no checks, flags, violations, compliance findings, and "did this happen?" answers.
- Every boolean field MUST have a sibling explanation field named exactly <boolean_key>_justification. The justification must be a short evidence-based sentence explaining why the boolean is true or false.
- Do not output bare booleans without their justification fields. The prompt fixer relies on those explanations later to understand why a row passed or failed.
- For non-boolean categorical fields, use a closed set of predefined values whenever the values are known (for example low/medium/high/unclear, or relevant/irrelevant/unclear). State the allowed values in the instructions and in the JSON example.
- If a useful categorical field does not have known allowed values from the goal, sample, column meanings, or user answers, ask what values it should use before writing the prompt.
- Use free-text fields only for explanations, evidence, and short summaries, not for values that should be counted or filtered later.
"""

DISCOVER_INSTRUCTIONS = TOOL_LIMITS + """You help a non-technical person plan an analysis of spreadsheet rows.

You receive:
- what they want to find out
- the column they said holds the main content (usually a conversation)
- optional notes about what other columns mean
- a SMALL sample of rows (not the full sheet)

Return JSON only:
{
  "column_guesses": [{"column": "name", "likely": "plain-language meaning"}],
  "condition_columns": ["ExactColumnName", "..."],
  "approach": "2-5 sentences describing how to analyze each row",
  "suggested_fields": ["short_json_key", "..."],
  "questions": [{"id": "q1", "text": "a simple yes/no or short question"}]
}

Rules:
- Guess column meanings from the sample, even if the user already labeled some.
- condition_columns: short scalar columns used as IF/THEN rules in the instructions (nationality, status, skill, channel, plan type). Exact spreadsheet names only. Never the main content column.
- If a condition column is already clear from its name or sample values (e.g. a column of Filipino/Kenyan/Indian), do NOT ask about it.
- Ask only when a column is unclear, a rule the user mentioned cannot be mapped to a column, OR a categorical output field's allowed values are not clear. AT MOST 3 questions. Prefer 0.
- suggested_fields should be 3–8 short snake_case keys for NEW analysis answers only.
- Prefer boolean suggested_fields for yes/no checks, and include the matching <field>_justification field in suggested_fields.
- If you suggest a non-boolean categorical field, only do it when its allowed values are obvious from the user's goal, answers, or sample; otherwise ask what values it should use.
- Never suggest echoing a column that already exists (no id, row_id, conversation id, or any input column). Those stay on the row at export.
- Each spreadsheet row is analyzed ON ITS OWN. The runner cannot group, join, or stitch rows that share a Client Id, Contract Id, Conversation Id, or any other key. Do not ask whether to combine rows into a journey. If the user wants journey-level scoring, tell them in the approach that they must pre-merge those rows in the spreadsheet; the tool cannot do it.
- Do not write the analysis prompt yet.
- Do not mention tokens, schemas, or APIs.
- Use maids.cc vocabulary from the company notes (CC, MV, PTC, Enchanters, Resolvers, prospect vs client vs maid).
- Never treat prices, salaries, counts, ratings, or nationality availability from those notes as facts.
""" + ANSWER_SHAPE_RULES

DEEP_DISCOVER_INSTRUCTIONS = TOOL_LIMITS + """You help a non-technical person plan an EXTENSIVE analysis prompt with multiple conditions.

You receive:
- what they want to find out
- the main content column (usually a conversation)
- optional column notes
- a SMALL sample of rows
- previous questions and answers, if any

Return JSON only:
{
  "column_guesses": [{"column": "name", "likely": "plain-language meaning"}],
  "condition_columns": ["ExactColumnName", "..."],
  "approach": "2-6 sentences on the analysis plan and the conditions you still need",
  "suggested_fields": ["short_json_key", "..."],
  "questions": [{"id": "q1", "text": "a simple question"}],
  "coverage": 70,
  "ready": false
}

Rules:
- Goal: gather enough to write a detailed prompt with several IF/THEN conditions (80–90% coverage).
- coverage is 0–100: how much you need to write that prompt. ready is true only when coverage >= 85 and leftover gaps are minor.
- Ask 1–5 NEW questions this round. Never repeat a previous question. Stop asking when ready.
- Each spreadsheet row is analyzed ON ITS OWN. The runner cannot group or stitch rows by Client Id, Contract Id, Conversation Id, or any other key. NEVER ask whether to combine related rows into a customer journey. If journey-level scoring is wanted, say in the approach that the sheet must already have one row per journey.
- If a scalar column is already clear (name + sample values), do not ask about it — put it in condition_columns.
- Ask about unclear columns, missing thresholds the user mentioned, edge cases, which outcomes to flag, and any categorical output whose allowed values are not already clear.
- Never ask the user to invent volatile company figures (prices, salaries, nationality availability). If a rule needs a number, ask them to type the rule they want, or say it must come from the owning source.
- suggested_fields: NEW analysis keys only. Prefer boolean checks with matching <field>_justification fields. Never echo input columns.
- Use maids.cc vocabulary. Do not write the analysis prompt yet.
""" + ANSWER_SHAPE_RULES

GENERATE_INSTRUCTIONS = TOOL_LIMITS + """You write TWO pieces for a spreadsheet analysis: instructions, and a separate input-data template.

The runner substitutes {ExactColumnName} with the RAW cell value. There is NO automatic label. If you want a label, write it in the text next to the placeholder.

The runner then sends:
  <substituted instructions>
  ===== INPUT =====
  <substituted input_template>
So the conversation is ALWAYS last.

Return JSON only:
{
  "prompt_name": "short_snake_name",
  "prompt": "instructions — task, IF/THEN rules using scalar {Column} placeholders, and an explicit flat JSON object",
  "input_template": "labeled placeholders; the large content/conversation lives here",
  "json_mode": true,
  "summary": "one sentence about what each row will return"
}

Hard rules:
- NEVER use {row_json}.
- The large content column (content_column) goes ONLY in input_template. Write a short label on the line before {ThatColumn}.
- Each row is independent. Do not instruct the model to look up other rows, combine conversations, or score a multi-row journey. If the user's goal implied that, score only what is in THIS row and say so.
- Scalar / condition columns (nationality, status, skill, channel, plan) go IN THE INSTRUCTIONS as placeholders, because the model needs them next to the rules. Example:
    The maid nationality is: {nationality}
    If Filipina: salary below 1500 is a violation.
    If Kenyan: salary below 700 is a violation.
  Use the exact spreadsheet column name inside the braces. Write the label yourself.
- NEVER put the same column in both prompt and input_template.
- NEVER put the conversation/content column in the instructions.
- Do not put identifier columns (Id, row_id, conversation id) in either part as something to output, and do not put them in the instructions unless a rule truly needs them.
- Output JSON must be EXPLICIT. Include a literal JSON object with every key and a sample value type, then say: Return ONLY this JSON object and nothing else.
- JSON must be strictly FLAT: top-level keys only, scalar values (string, number, or boolean). No nested objects. Avoid arrays.
- Prefer boolean fields for checks and flags. Every boolean key in the JSON object MUST be followed by a string key named <boolean_key>_justification that explains the evidence for that true/false value in one sentence.
- For categorical fields that are not boolean, write an "Allowed values:" rule with the complete closed set when those values are known.
- If the allowed values are NOT known from the goal, answers or sample, do not invent a set and do not leave the field open. You cannot ask a question at this stage. Do one of these instead, in order of preference:
    1. Reframe it as one or more booleans with their _justification fields.
    2. Use a closed set you can defend from the sample, always including "unclear", and add a line saying the values need confirming with the person who asked.
  An open-ended category produces a different wording on every row and cannot be counted or filtered afterwards.
- Do NOT ask the model to output any field that already exists in the input. Results are joined back to the input row on export. Output only NEW analysis fields.
- Use maids.cc vocabulary when it fits. Never write specific prices, salaries, counts, ratings, or nationality availability from company notes as if they were facts — if the USER gave a threshold in their goal or answers, you MAY write that user-supplied rule.
- Keep instructions specific. Deep / extensive plans may be longer (up to ~120 lines) when several conditions are needed.
""" + ANSWER_SHAPE_RULES


def _parse_agent_json(text: str) -> Dict[str, Any]:
    parsed, err = try_parse_json(text)
    if isinstance(parsed, dict):
        return parsed
    raise ValueError(f"The helper did not return a usable plan: {err or 'invalid payload'}")


def _clip(value: Any, limit: int = CELL_LIMIT) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def sample_rows(
    rows: List[Dict[str, Any]],
    columns: List[str],
    n: int = SAMPLE_ROWS,
    content_column: str = "",
    seed: Optional[int] = None,
) -> List[Dict[str, str]]:
    """A random handful of rows that actually have something to read.

    Two things matter here. It must be *random* across the whole sheet -- the
    first few rows of an export are usually the oldest or the smallest, and a
    plan built on those misses everything the tail looks like. And when a
    content column is named, rows where that column is blank are useless for
    studying: they were being sampled anyway, because the old filter accepted a
    row if *any* cell had text, so a sheet with sparse transcripts could hand
    the helper five empty conversations.

    Pass a seed to get the same sample twice, so the availability score
    describes the rows the plan was built on rather than a different draw.
    """
    content = (content_column or "").strip()
    if content:
        usable = [r for r in rows if str(r.get(content, "") or "").strip()]
        if not usable:
            # Nothing has content. Fall back rather than returning nothing, so
            # the helper can still say the sheet is not usable for this goal.
            usable = [r for r in rows if any(str(r.get(c, "")).strip() for c in columns)]
    else:
        usable = [r for r in rows if any(str(r.get(c, "")).strip() for c in columns)]
    if not usable:
        return []
    take = min(max(n, 3), 8, len(usable))
    if len(usable) <= take:
        chosen = list(usable)
    else:
        rng = random.Random(seed) if seed is not None else random
        chosen = rng.sample(usable, take)
    sample = []
    for row in chosen:
        sample.append({str(c): _clip(row.get(c, "")) for c in columns})
    return sample


def sample_report(rows: List[Dict[str, Any]], columns: List[str], content_column: str = "") -> Dict[str, Any]:
    """How much of the sheet is actually usable, counted rather than guessed.

    This runs in code, not in the model, so the coverage numbers are facts.
    """
    total = len(rows or [])
    content = (content_column or "").strip()
    filled = 0
    lengths: List[int] = []
    if content:
        for row in rows or []:
            text = str(row.get(content, "") or "").strip()
            if text:
                filled += 1
                lengths.append(len(text))
    blank_columns = []
    for col in columns or []:
        if not any(str(r.get(col, "") or "").strip() for r in (rows or [])):
            blank_columns.append(col)
    lengths.sort()
    median = lengths[len(lengths) // 2] if lengths else 0
    return {
        "total_rows": total,
        "content_column": content,
        "rows_with_content": filled,
        "content_coverage": round(100 * filled / total) if (total and content) else None,
        "median_content_chars": median,
        "shortest_content_chars": lengths[0] if lengths else 0,
        "empty_columns": blank_columns,
    }


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_PASSTHROUGH_KEY = re.compile(
    r"^(id|row_id|rowid|conversation_id|chat_id|ticket_id|message_id|uuid)$",
    re.I,
)
_BOOLEAN_JUSTIFICATION_RULE = (
    "For every boolean field, include the matching *_justification field and "
    "explain the evidence for the true/false decision in one sentence."
)


def _placeholders(text: str) -> List[str]:
    return _PLACEHOLDER_RE.findall(text or "")


def _strip_row_json(text: str) -> str:
    return re.sub(r"\{+row_json\}+", "", text or "")


def _remove_placeholder(text: str, name: str) -> str:
    return (text or "").replace("{" + name + "}", "")


def _is_id_field(name: str) -> bool:
    return bool(_PASSTHROUGH_KEY.match((name or "").strip()))


def _is_passthrough_field(name: str, columns: List[str]) -> bool:
    key = (name or "").strip()
    if not key:
        return True
    if _is_id_field(key):
        return True
    return key.lower() in {c.lower() for c in columns}


def _default_input_template(content_column: str, extra: Optional[List[str]] = None) -> str:
    lines = [f"{content_column}:", "{" + content_column + "}"]
    for col in extra or []:
        if col and col != content_column:
            lines.extend(["", f"{col}:", "{" + col + "}"])
    return "\n".join(lines)


def _json_object_spans(text: str) -> List[tuple[int, int]]:
    spans: List[tuple[int, int]] = []
    start = -1
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text or ""):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
                start = -1
    return spans


def _ensure_boolean_justifications(prompt: str) -> str:
    """Add missing justification siblings to every flat JSON example in a draft.

    The model-facing instructions already ask for this; this is the final
    safety pass. It is scoped to spans that parse as a JSON object, so
    placeholders like {Messages} are never touched.

    Every object is visited, not just the last one. A prompt commonly carries
    more than one -- the output schema plus an illustrative row -- and stopping
    at the first parseable object meant the schema went unfixed whenever an
    example happened to sit after it.
    """
    text = prompt or ""
    # Whether the prompt explains the rule has to be judged before we start
    # inserting the word ourselves.
    already_explains = "justification" in text.lower()
    changed_any = False

    # Reverse order keeps the earlier spans' offsets valid as we splice.
    for start, end in reversed(_json_object_spans(text)):
        snippet = text[start:end]
        if '":' not in snippet:
            continue
        try:
            obj = json.loads(snippet)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        existing = {str(k).lower() for k in obj}
        out: Dict[str, Any] = {}
        changed = False
        for key, value in obj.items():
            out[key] = value
            if not isinstance(value, bool):
                continue
            justification_key = f"{key}_justification"
            if justification_key.lower() in existing:
                continue
            out[justification_key] = "Brief evidence-based justification."
            changed = True
        if not changed:
            continue
        text = text[:start] + json.dumps(out, ensure_ascii=False, indent=2) + text[end:]
        changed_any = True

    if changed_any and not already_explains:
        text = text.rstrip() + "\n\n" + _BOOLEAN_JUSTIFICATION_RULE
    return text


def _sanitize_generated(
    prompt: str,
    input_template: str,
    content_column: str,
    columns: List[str],
) -> tuple[str, str]:
    prompt = _strip_row_json(prompt).strip()
    input_template = _strip_row_json(input_template).strip()
    content_ph = "{" + content_column + "}"
    if content_ph in prompt:
        prompt = _remove_placeholder(prompt, content_column).strip()
    for name in list(_placeholders(prompt)):
        if _is_id_field(name):
            prompt = _remove_placeholder(prompt, name)
    if not input_template or content_ph not in input_template:
        extra = [c for c in _placeholders(input_template) if c != content_column and c in columns]
        base = _default_input_template(content_column, extra)
        if input_template and content_ph not in input_template:
            input_template = input_template.rstrip() + "\n\n" + base
        else:
            input_template = base
    instruction_names = set(_placeholders(prompt))
    extra = [
        c
        for c in _placeholders(input_template)
        if c != content_column and c in columns and c not in instruction_names and not _is_id_field(c)
    ]
    input_template = _default_input_template(content_column, extra)
    prompt = re.sub(r"[ \t]+\n", "\n", prompt)
    prompt = re.sub(r"\n{3,}", "\n\n", prompt).strip()
    prompt = _ensure_boolean_justifications(prompt)
    return prompt, input_template.strip()


def _ask(provider: str, api_key: str, model: str, system: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = call_provider(provider, api_key, model, compose_helper_prompt(system, payload), json_mode=True)
    return _parse_agent_json(raw)


def _normalize_questions(raw: Any, limit: int, used_ids: Optional[set] = None) -> List[Dict[str, str]]:
    used_ids = used_ids or set()
    questions = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        qid = str(item.get("id") or f"q{len(used_ids) + len(questions) + 1}")
        if qid in used_ids:
            qid = f"q{len(used_ids) + len(questions) + 1}"
        questions.append({"id": qid, "text": text})
        if len(questions) >= limit:
            break
    return questions


def _normalize_guesses(raw: Any) -> List[Dict[str, str]]:
    guesses = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        col = str(item.get("column") or "").strip()
        likely = str(item.get("likely") or "").strip()
        if col and likely:
            guesses.append({"column": col, "likely": likely})
    return guesses


def _normalize_condition_columns(raw: Any, columns: List[str], content_column: str) -> List[str]:
    known = {c.lower(): c for c in columns}
    out = []
    for item in raw or []:
        name = str(item or "").strip()
        match = known.get(name.lower())
        if not match or match == content_column or _is_id_field(match):
            continue
        if match not in out:
            out.append(match)
    return out


def _normalize_plan_fields(data: Dict[str, Any], columns: List[str], content_column: str, q_limit: int) -> Dict[str, Any]:
    fields = [
        str(x).strip()
        for x in (data.get("suggested_fields") or [])
        if str(x).strip() and not _is_passthrough_field(str(x), columns)
    ]
    coverage = data.get("coverage")
    try:
        coverage = max(0, min(100, int(coverage)))
    except Exception:
        coverage = None
    return {
        "column_guesses": _normalize_guesses(data.get("column_guesses")),
        "condition_columns": _normalize_condition_columns(data.get("condition_columns"), columns, content_column),
        "approach": str(data.get("approach") or "").strip(),
        "suggested_fields": fields[:8],
        "questions": _normalize_questions(data.get("questions"), q_limit),
        "coverage": coverage,
        "ready": bool(data.get("ready")),
    }


def discover_plan(
    provider: str,
    api_key: str,
    model: str,
    columns: List[str],
    sample: List[Dict[str, str]],
    goal: str,
    content_column: str,
    meanings: str = "",
    mode: str = "quick",
    prior_qa: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    if not sample:
        raise ValueError("Need a few example rows to look at the sheet.")
    if not (goal or "").strip():
        raise ValueError("Say what you want to find out about each row.")
    deep = (mode or "quick").strip().lower() == "deep"
    data = _ask(
        provider,
        api_key,
        model,
        DEEP_DISCOVER_INSTRUCTIONS if deep else DISCOVER_INSTRUCTIONS,
        {
            "goal": goal.strip(),
            "content_column": content_column,
            "column_meanings": meanings.strip(),
            "columns": columns,
            "sample_rows": sample,
            "previous_questions_and_answers": prior_qa or [],
            "mode": "deep" if deep else "quick",
        },
    )
    used = {str(q.get("id") or "") for q in (prior_qa or [])}
    plan = _normalize_plan_fields(
        data,
        columns,
        content_column,
        MAX_DEEP_QUESTIONS_PER_ROUND if deep else MAX_QUESTIONS,
    )
    if used:
        plan["questions"] = [q for q in plan["questions"] if q["id"] not in used]
    if not deep:
        plan["questions"] = plan["questions"][:MAX_QUESTIONS]
        plan["coverage"] = 100 if not plan["questions"] else plan.get("coverage")
        plan["ready"] = not plan["questions"]
    elif plan.get("coverage") is None:
        plan["coverage"] = 85 if plan.get("ready") else (55 if plan["questions"] else 80)
    if deep and (plan.get("coverage") or 0) >= DEEP_READY_COVERAGE:
        plan["ready"] = True
    return plan


def generate_prompt(
    provider: str,
    api_key: str,
    model: str,
    columns: List[str],
    goal: str,
    content_column: str,
    meanings: str,
    plan: Dict[str, Any],
    answers: List[Dict[str, str]],
) -> Dict[str, Any]:
    if not (goal or "").strip():
        raise ValueError("Say what you want to find out about each row.")
    if content_column not in columns:
        raise ValueError("Pick the column that holds the main text.")
    data = _ask(
        provider,
        api_key,
        model,
        GENERATE_INSTRUCTIONS,
        {
            "goal": goal.strip(),
            "content_column": content_column,
            "column_meanings": meanings.strip(),
            "columns": columns,
            "condition_columns": (plan or {}).get("condition_columns") or [],
            "plan": plan,
            "answers": answers,
        },
    )
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("The helper did not write a prompt. Try again.")
    input_template = str(data.get("input_template") or "").strip()
    prompt, input_template = _sanitize_generated(prompt, input_template, content_column, columns)
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(data.get("prompt_name") or "analysis").strip()) or "analysis"
    return {
        "prompt_name": name[:60],
        "prompt": prompt,
        "input_template": input_template,
        "json_mode": bool(data.get("json_mode", True)),
        "summary": str(data.get("summary") or "").strip(),
    }


def answers_from_form(questions: List[Dict[str, Any]], raw: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
    raw = raw or {}
    out = []
    for q in questions:
        qid = str(q.get("id") or "")
        text = str(q.get("text") or "")
        out.append({"id": qid, "question": text, "answer": str(raw.get(qid) or raw.get(text) or "").strip()})
    return out


AVAILABILITY_INSTRUCTIONS = TOOL_LIMITS + """Score how useful the CURRENT spreadsheet is for the user's analysis goal.

You receive the goal, the main content column, optional notes, a small sample, the plan so far, and measured_stats.

measured_stats was counted over the WHOLE sheet in code, not estimated from the sample. Trust it over your impression of the sample:
- content_coverage is the percentage of rows whose main content column has any text. Below 70 is a real problem: say so plainly and cap the score at 60, because most rows will produce an answer about nothing.
- median_content_chars near zero means the content is too short to analyse.
- empty_columns are entirely blank in every row. Never rely on one, and mention it if the user's goal needs it.

Return JSON only:
{
  "score": 78,
  "summary": "2-4 sentences a non-technical person can read",
  "have": ["what is already in the sheet that helps"],
  "helpful_missing": [{"field": "plain name", "why": "why it would help", "likely_column": "ExactColumnName or empty if not in the sheet"}],
  "questions": [{"id": "a1", "text": "Do you have X in another file you could add as a column?"}],
  "ready": true
}

Rules:
- score is 0–100: how useful THIS sheet is for the goal, not how good the future prompt will be.
- 80+ means everything important is already here. ready=true in that case.
- helpful_missing is columns the user could ADD to the CSV/Excel before writing the prompt. You cannot add them.
- Ask at most 4 short questions about missing fields. Prefer 0 if the sheet is enough.
- Do not ask about joining rows. Do not write the prompt yet.
"""


def check_availability(
    provider: str,
    api_key: str,
    model: str,
    columns: List[str],
    sample: List[Dict[str, str]],
    goal: str,
    content_column: str,
    meanings: str = "",
    plan: Optional[Dict[str, Any]] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not sample:
        raise ValueError("Need a few example rows to score the sheet.")
    stats = stats or {}
    data = _ask(
        provider,
        api_key,
        model,
        AVAILABILITY_INSTRUCTIONS,
        {
            "goal": goal.strip(),
            "content_column": content_column,
            "column_meanings": meanings.strip(),
            "columns": columns,
            "sample_rows": sample,
            "plan": plan or {},
            "measured_stats": stats,
        },
    )
    try:
        score = max(0, min(100, int(data.get("score"))))
    except Exception:
        score = 50
    # Coverage is a counted fact. Do not let an optimistic model score a sheet
    # highly when most of its rows have nothing to read.
    coverage = stats.get("content_coverage")
    if isinstance(coverage, int) and coverage < 70:
        score = min(score, 60 if coverage >= 40 else 35)
    missing = []
    for item in data.get("helpful_missing") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if field:
            missing.append({
                "field": field,
                "why": str(item.get("why") or "").strip(),
                "likely_column": str(item.get("likely_column") or "").strip(),
            })
    return {
        "score": score,
        "summary": str(data.get("summary") or "").strip(),
        "have": [str(x).strip() for x in (data.get("have") or []) if str(x).strip()],
        "helpful_missing": missing,
        "questions": _normalize_questions(data.get("questions"), 4),
        "ready": (bool(data.get("ready")) or score >= 80) and score >= 70,
        "stats": stats,
    }


CATALOGUE_CHAT_INSTRUCTIONS = TOOL_LIMITS + """You help someone talk about an existing analysis prompt.

You can:
- Explain what the prompt does, in plain language
- Suggest edge cases
- Propose adding, renaming, or removing required input placeholders (exact {Column} names)
- Propose an edited prompt and/or input_template when the user asks to change something

You cannot run rows, join data, or change access.
You receive the full recent chat history. Use it. If they asked for an edit earlier, keep that intent.
You also receive purpose and prior_changes. Do not undo those fixes.

If can_edit is false and the user asks to change the prompt, do not invent an edit. Set wants_edit true and tell them plainly they do not have edit access — they should clone the prompt or request edit access from the owner.

Return JSON only:
{
  "reply": "what to show the user",
  "wants_edit": false,
  "updated_prompt": "full instructions if you changed them, else empty",
  "updated_input": "full input template if you changed it, else empty",
  "required_inputs": ["ColumnName"],
  "change_summary": "one sentence describing the edit, empty if none",
  "purpose": "one or two sentences saying what this prompt is for"
}

If the user did not ask to change the prompt, leave updated_prompt empty.
Keep JSON output flat if you edit the prompt. Never use {row_json}. Put the conversation column only in input_template.
If you edit a prompt, prefer boolean output fields for yes/no checks and make every boolean key include a sibling <boolean_key>_justification string key. For categorical fields, define the allowed values when known; if the user asks for a category and the values are unclear, ask what values it should use instead of inventing them.
""" + ANSWER_SHAPE_RULES

NO_EDIT_ACCESS = (
    "You don't have edit access. Clone this prompt to keep the conversation and edit your copy, "
    "or request edit access from the owner."
)


def catalogue_chat_turn(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    input_template: str,
    history: List[Dict[str, str]],
    message: str,
    can_edit: bool = True,
    purpose: str = "",
    prior_changes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    data = _ask(
        provider,
        api_key,
        model,
        CATALOGUE_CHAT_INSTRUCTIONS,
        {
            "current_prompt": prompt,
            "current_input_template": input_template,
            "recent_chat": history[-20:],
            "user_message": message.strip(),
            "can_edit": can_edit,
            "purpose": purpose or "",
            "prior_changes": prior_changes or [],
        },
    )
    wants_edit = bool(data.get("wants_edit")) or bool(str(data.get("updated_prompt") or "").strip())
    updated_prompt = str(data.get("updated_prompt") or "").strip()
    updated_input = str(data.get("updated_input") or "").strip()
    reply = str(data.get("reply") or "").strip() or "I could not answer that."
    if wants_edit and not can_edit:
        updated_prompt = ""
        updated_input = ""
        if NO_EDIT_ACCESS.lower() not in reply.lower():
            reply = NO_EDIT_ACCESS
    return {
        "reply": reply,
        "wants_edit": wants_edit,
        "updated_prompt": updated_prompt,
        "updated_input": updated_input,
        "required_inputs": [str(x).strip() for x in (data.get("required_inputs") or []) if str(x).strip()],
        "change_summary": str(data.get("change_summary") or "").strip(),
        "purpose": str(data.get("purpose") or purpose or "").strip(),
        "can_edit": can_edit,
    }


FINDER_INSTRUCTIONS = """You help someone find an analysis prompt they are allowed to use.

You receive:
- their question
- optional owner filter
- the ONLY prompts they can see (id, name, owner, visibility, role, purpose, required_inputs)

Never mention a prompt that is not in that list.
If they ask for a specific user's prompts, filter by owner.
If nothing fits, say so and suggest they build a new one.

Return JSON only:
{
  "reply": "short plain-language answer",
  "matches": [{"id": "prompt-id", "why": "one short reason"}]
}
"""


def find_prompts_turn(
    provider: str,
    api_key: str,
    model: str,
    message: str,
    visible: List[Dict[str, Any]],
    owner: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    cards = []
    owner_q = (owner or "").strip().lower()
    for item in visible:
        if owner_q and owner_q not in str(item.get("owner") or "").lower():
            continue
        cards.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "owner": item.get("owner"),
            "visibility": item.get("visibility"),
            "role": item.get("role"),
            "purpose": item.get("purpose") or "",
            "required_inputs": item.get("required_inputs") or [],
        })
    data = _ask(
        provider,
        api_key,
        model,
        FINDER_INSTRUCTIONS,
        {
            "user_message": (message or "").strip(),
            "owner_filter": owner or "",
            "visible_prompts": cards[:80],
            "recent_chat": (history or [])[-8:],
        },
    )
    allowed = {str(c.get("id") or "") for c in cards}
    matches = []
    for raw in data.get("matches") or []:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "")
        if pid and pid in allowed:
            matches.append({"id": pid, "why": str(raw.get("why") or "").strip()})
    return {
        "reply": str(data.get("reply") or "").strip() or "I could not find a matching prompt.",
        "matches": matches,
    }
