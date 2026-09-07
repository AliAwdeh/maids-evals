import json

from prompt_fixer import ANALYST_INSTRUCTIONS, CRITIC_INSTRUCTIONS, EDITOR_INSTRUCTIONS


def _with_stub_provider(module, replies, fn):
    """Run fn with module.call_provider answering from `replies`, then restore."""
    original = module.call_provider
    queue = list(replies)

    def stub(provider, api_key, model, prompt, json_mode, model_params=None):
        return queue.pop(0) if queue else "{}"

    module.call_provider = stub
    try:
        return fn()
    finally:
        module.call_provider = original


def test_fixer_contract_preserves_boolean_justifications():
    for instructions in (ANALYST_INSTRUCTIONS, EDITOR_INSTRUCTIONS, CRITIC_INSTRUCTIONS):
        assert "<boolean_key>_justification" in instructions
        assert "Allowed values" in instructions


# --- the safety pass has to cover every JSON block, not just the last one ----

def test_sanitiser_fixes_the_schema_even_when_an_example_follows_it():
    """A prompt usually carries the output schema plus an illustrative row.
    Walking only as far as the last parseable object left the schema bare."""
    from prompt_builder import _ensure_boolean_justifications

    out = _ensure_boolean_justifications(
        "Classify the chat.\n\n"
        "Return ONLY this JSON object:\n"
        '{"is_frustrated": true, "wants_to_cancel": false}\n\n'
        "For reference, a row looks like:\n"
        '{"channel": "whatsapp"}'
    )
    assert '"is_frustrated_justification"' in out
    assert '"wants_to_cancel_justification"' in out
    # The illustrative row is data, not an answer schema, and is left alone.
    assert '{"channel": "whatsapp"}' in out


def test_sanitiser_fixes_more_than_one_schema():
    from prompt_builder import _ensure_boolean_justifications

    out = _ensure_boolean_justifications(
        'Good answer:\n{"is_relevant": true}\n\nBad answer:\n{"is_relevant": false}'
    )
    assert out.count('"is_relevant_justification"') == 2


def test_the_rule_sentence_is_added_once_and_only_when_missing():
    from prompt_builder import _ensure_boolean_justifications

    added = _ensure_boolean_justifications('Return:\n{"ok": true}')
    assert added.count("For every boolean field") == 1

    # A prompt that already spells out the requirement should not be padded.
    already = _ensure_boolean_justifications(
        "Give a justification for each flag.\n" 'Return:\n{"ok": true}'
    )
    assert "For every boolean field" not in already
    assert '"ok_justification"' in already


# --- the fixer must not quietly break the shape it depends on ---------------

def test_fixer_flags_a_boolean_added_without_a_justification():
    from prompt_fixer import contract_warnings

    old = 'Return:\n{"is_frustrated": true, "is_frustrated_justification": "why"}'
    new = 'Return:\n{"is_frustrated": true, "is_frustrated_justification": "why", "wants_to_cancel": false}'
    warnings = contract_warnings(old, new)
    assert any("wants_to_cancel" in w for w in warnings)


def test_fixer_flags_a_dropped_justification():
    from prompt_fixer import contract_warnings

    old = 'Return:\n{"is_frustrated": true, "is_frustrated_justification": "why"}'
    new = 'Return:\n{"is_frustrated": true}'
    warnings = contract_warnings(old, new)
    assert any("removed" in w for w in warnings)


def test_fixer_is_quiet_when_the_shape_is_kept():
    from prompt_fixer import contract_warnings

    old = 'Return:\n{"ok": true, "ok_justification": "why", "urgency": "low"}'
    new = 'Return:\n{"ok": true, "ok_justification": "why", "urgency": "medium"}'
    assert contract_warnings(old, new) == []


# --- the contract has to survive a real generate call ----------------------

def test_generated_prompt_comes_out_with_justifications_even_if_the_model_forgets():
    """The instructions ask for justifications; this proves the pipeline still
    delivers them when the model ignores that."""
    import prompt_builder

    forgetful = json.dumps({
        "prompt_name": "frustration",
        "prompt": (
            "Read the conversation.\n"
            "Nationality: {nationality}\n\n"
            "Return ONLY this JSON object and nothing else:\n"
            '{"is_frustrated": true, "wants_to_cancel": false, "urgency": "low"}'
        ),
        "input_template": "Conversation:\n{Messages}",
        "json_mode": True,
        "summary": "one line",
    })
    made = _with_stub_provider(prompt_builder, [forgetful], lambda: prompt_builder.generate_prompt(
        "openai", "k", "m", ["Messages", "nationality"],
        "find frustrated customers", "Messages", "", {}, [],
    ))
    text = made["prompt"]
    assert '"is_frustrated_justification"' in text
    assert '"wants_to_cancel_justification"' in text
    # A non-boolean is left as the model wrote it.
    assert '"urgency": "low"' in text
    # The conversation still belongs only to the input template.
    assert "{Messages}" not in text
    assert "{Messages}" in made["input_template"]


def test_generate_instructions_give_a_usable_fallback_when_values_are_unknown():
    """Generation is one shot and cannot hand back to discovery, so telling it
    to 'ask instead' at that point is unactionable."""
    from prompt_builder import GENERATE_INSTRUCTIONS

    assert "You cannot ask a question at this stage" in GENERATE_INSTRUCTIONS
    assert "Reframe it as one or more booleans" in GENERATE_INSTRUCTIONS
    assert "unclear" in GENERATE_INSTRUCTIONS


def test_discovery_is_allowed_to_ask_about_unknown_category_values():
    """One rule listed the only reasons to ask and left this out, while another
    demanded it — so 'Prefer 0' won and the question never got asked."""
    from prompt_builder import DISCOVER_INSTRUCTIONS

    ask_rule = next(
        line for line in DISCOVER_INSTRUCTIONS.splitlines() if line.startswith("- Ask only when")
    )
    assert "allowed values are not clear" in ask_rule
