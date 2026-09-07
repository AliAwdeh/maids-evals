from engine import json_key_profiles


def _rows(pairs):
    """pairs: list of dicts (flat JSON per row)."""
    return [{"_llm_json_flat": dict(p), "llm_output": ""} for p in pairs]


def test_boolean_key_is_selectable():
    rows = _rows([{"ok": True}, {"ok": False}, {"ok": True}, {"ok": "yes"}])
    profile = json_key_profiles(rows, ["ok"])[0]
    assert profile["selectable"] is True
    assert profile["boolish"] is True
    labels = {v["label"] for v in profile["values"]}
    assert labels <= {"true", "false"}


def test_small_category_set_is_selectable():
    rows = _rows([{"urgency": v} for v in ["low", "low", "medium", "high", "low", "medium", "low", "high"]])
    profile = json_key_profiles(rows, ["urgency"])[0]
    assert profile["selectable"] is True
    assert profile["unique"] == 3
    assert [v["label"] for v in profile["values"]][0] == "low"


def test_unique_free_text_is_not_selectable():
    rows = _rows([{"main_issue": f"Client asked about case {i} in a long unique sentence."} for i in range(20)])
    profile = json_key_profiles(rows, ["main_issue"])[0]
    assert profile["selectable"] is False
    assert profile["unique"] == 20


def test_mixed_batch_splits_closed_and_open_keys():
    rows = []
    for i in range(16):
        rows.append({
            "_llm_json_flat": {
                "sentiment": "neutral" if i % 2 == 0 else "frustrated",
                "note": f"completely different note number {i} with extra wording",
            },
            "llm_output": "",
        })
    profiles = {p["key"]: p for p in json_key_profiles(rows, ["sentiment", "note"])}
    assert profiles["sentiment"]["selectable"] is True
    assert profiles["note"]["selectable"] is False


def test_labelled_answers_with_commentary_are_grouped():
    """A model asked for low/medium/high that writes "Low - no issue raised"
    should still be countable. This is real: one 130-row batch produced 101
    spellings of three values."""
    spellings = (
        ["Low - no issue raised"] * 10
        + ["low"] * 9
        + ["Low - no dissatisfaction expressed"] * 4
        + ["Medium: client is chasing"] * 6
        + ["medium"] * 3
        + ["High — wants to cancel"] * 2
    )
    rows = _rows([{"escalation_risk": v} for v in spellings])
    profile = json_key_profiles(rows, ["escalation_risk"])[0]
    assert profile["selectable"] is True
    assert profile["grouped"] is True
    assert profile["raw_unique"] == 6
    assert {v["label"] for v in profile["values"]} == {"low", "medium", "high"}
    # Every original spelling is still reported, so nothing is hidden.
    assert len(profile["raw_values"]) == 6


def test_grouping_does_not_rescue_genuine_free_text():
    rows = _rows([
        {"main_issue": f"Client asked about invoice {i} - and then explained the whole story at length"}
        for i in range(30)
    ])
    profile = json_key_profiles(rows, ["main_issue"])[0]
    assert profile["selectable"] is False
    assert profile["grouped"] is False


def test_filtering_matches_the_bucket_a_value_was_counted_in():
    from engine import filter_results_by_json

    rows = _rows(
        [{"risk": "Low - nothing raised"}] * 3
        + [{"risk": "low"}] * 2
        + [{"risk": "High: wants out"}] * 1
    )
    assert len(filter_results_by_json(rows, ["risk"], ["low"])) == 5
    assert len(filter_results_by_json(rows, ["risk"], ["high"])) == 1


def test_boolean_filters_still_work_as_before():
    from engine import filter_results_by_json

    rows = _rows([{"ok": True}, {"ok": False}, {"ok": True}])
    assert len(filter_results_by_json(rows, ["ok"], ["true"])) == 2
    assert len(filter_results_by_json(rows, ["ok"], ["false"])) == 1
    assert len(filter_results_by_json(rows, [], [])) == 3
