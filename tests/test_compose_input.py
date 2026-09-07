from engine import INPUT_SECTION_HEADER, compose_model_input, display_cell, row_input_error, templates_have_placeholder
from prompt_builder import (
    ANSWER_SHAPE_RULES,
    DISCOVER_INSTRUCTIONS,
    GENERATE_INSTRUCTIONS,
    _sanitize_generated,
)


def test_legacy_empty_input_is_instructions_only():
    row = {"Messages": "hello there", "Id": "9"}
    prompt = "Look at {Messages} and return JSON."
    assert compose_model_input(prompt, row, "") == "Look at hello there and return JSON."
    assert compose_model_input(prompt, row) == "Look at hello there and return JSON."


def test_input_section_is_last_with_delimiter():
    row = {"Messages": "long chat", "Id": "42", "Channel": "WhatsApp"}
    instructions = "Classify the conversation.\nReturn ONLY this JSON object and nothing else.\n{\"topic\": \"\"}"
    extra = "Conversation:\n{Messages}\n\nChannel:\n{Channel}"
    out = compose_model_input(instructions, row, extra)
    assert out.startswith(instructions)
    assert f"\n\n{INPUT_SECTION_HEADER}\n" in out
    assert out.endswith("Conversation:\nlong chat\n\nChannel:\nWhatsApp")
    assert out.index(INPUT_SECTION_HEADER) < out.index("long chat")
    assert "{Messages}" not in out
    assert "{row_json}" not in out


def test_placeholder_check_accepts_input_only():
    names = ["row_json", "Messages", "Id"]
    assert templates_have_placeholder("No placeholders here.", "Conversation:\n{Messages}", names)
    assert not templates_have_placeholder("No placeholders here.", "", names)
    assert templates_have_placeholder("Use {row_json}", "", names)


def test_placeholder_check_accepts_mapped_fields():
    prompt = "Classify the chat."
    extra = "Conversation:\n{transcript}"
    names = ["row_json", "Messages", "Id"]
    assert not templates_have_placeholder(prompt, extra, names)
    assert templates_have_placeholder(prompt, extra, names, {"transcript": "Messages"})
    assert compose_model_input(prompt, {"Messages": "hello"}, extra, column_map={"transcript": "Messages"}).endswith("hello")
    assert row_input_error(prompt, extra, ["Messages", "Id"], {}) == (
        "This prompt field is not in your sheet: {transcript}. "
        "Map each one to a column, or fix the spelling."
    )
    assert row_input_error(prompt, extra, ["Messages", "Id"], {"transcript": "Messages"}) == ""


def test_sanitize_drops_row_json_and_moves_content():
    prompt = (
        "Read {row_json} and {Messages}. Also {Id}.\n"
        "Return JSON with keys: row_id, topic."
    )
    extra = ""
    clean_prompt, clean_input = _sanitize_generated(prompt, extra, "Messages", ["Messages", "Id"])
    assert "{row_json}" not in clean_prompt
    assert "{Messages}" not in clean_prompt
    assert "{Id}" not in clean_prompt
    assert "{Messages}" in clean_input
    assert clean_input.startswith("Messages:")


def test_sanitize_keeps_condition_columns_in_instructions():
    prompt = (
        "The maid nationality is: {nationality}\n"
        "If Filipina, salary below 1500 is a violation.\n"
        "Read {Messages} and {row_json}.\n"
        "Return ONLY this JSON object and nothing else.\n"
        '{"violation": false}'
    )
    extra = "Messages:\n{Messages}\n\nnationality:\n{nationality}"
    clean_prompt, clean_input = _sanitize_generated(
        prompt, extra, "Messages", ["Messages", "Id", "nationality"]
    )
    assert "{nationality}" in clean_prompt
    assert "{Messages}" not in clean_prompt
    assert "{row_json}" not in clean_prompt
    assert "{nationality}" not in clean_input
    assert "{Messages}" in clean_input


def test_sanitize_adds_boolean_justification_to_generated_schema():
    prompt = (
        "Classify the conversation.\n"
        "Return ONLY this JSON object and nothing else.\n"
        '{"needs_follow_up": false, "risk_level": "low"}'
    )
    clean_prompt, clean_input = _sanitize_generated(prompt, "", "Messages", ["Messages"])
    assert '"needs_follow_up": false' in clean_prompt
    assert '"needs_follow_up_justification": "Brief evidence-based justification."' in clean_prompt
    assert '"risk_level": "low"' in clean_prompt
    assert "For every boolean field" in clean_prompt
    assert clean_input == "Messages:\n{Messages}"


def test_sanitize_does_not_duplicate_existing_boolean_justification():
    prompt = (
        "Return ONLY this JSON object and nothing else.\n"
        '{"is_relevant": true, "is_relevant_justification": "The message asks about maids.cc."}'
    )
    clean_prompt, _ = _sanitize_generated(prompt, "Messages:\n{Messages}", "Messages", ["Messages"])
    assert clean_prompt.count("is_relevant_justification") == 1


def test_builder_ai_contract_mentions_boolean_justifications_and_known_categories():
    assert "<boolean_key>_justification" in ANSWER_SHAPE_RULES
    assert "predefined values" in ANSWER_SHAPE_RULES
    assert "<field>_justification" in DISCOVER_INSTRUCTIONS
    assert "Allowed values" in GENERATE_INSTRUCTIONS


def test_display_cell_turns_escaped_breaks_into_real_ones():
    assert display_cell("hello\\nworld") == "hello\nworld"
    assert display_cell("a\\tb") == "a\tb"
    assert display_cell("already\nbroken") == "already\nbroken"
    assert display_cell(None) == ""
    assert display_cell("  ") == ""
