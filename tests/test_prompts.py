"""The prompts file loads, and every prompt still asks for the JSON the code reads."""
import pytest

from app.config import PROJECT_ROOT
from app.prompts import PromptsError, load_prompts

PROMPTS_FILE = PROJECT_ROOT / "prompts" / "prompts.yaml"

# keys pipeline.py / gifts.py read from each step's JSON answer
REQUIRED_KEYS = {
    "tc_validation": ["passed", "compliance_score", "checks", "issues", "severity"],
    "field_match": ["results", "field", "matching_score", "reason"],
    "history": ["check_claim_history", "risk_level", "flags", "explanation"],
    "gift_assignment": ["gift_type", "options", "eligible_gift", "selected_gift_match",
                        "tc_reference", "fixed", "product_based", "customer_choice"],
    "summary": ["ai_summary"],
}


def test_prompts_file_loads():
    p = load_prompts(PROMPTS_FILE)
    assert p.version
    assert not p.prompts.summary.endswith("\n")       # block scalars are stripped


@pytest.mark.parametrize("step,keys", REQUIRED_KEYS.items())
def test_prompt_keeps_json_contract(step, keys):
    text = getattr(load_prompts(PROMPTS_FILE).prompts, step)
    assert "Respond ONLY with JSON" in text
    missing = [k for k in keys if k not in text]
    assert not missing, f"{step} prompt no longer mentions {missing}"


def test_settings_expose_prompts(settings):
    assert settings.prompts.prompts.history.startswith("You are a fraud analyst")


def test_missing_file(tmp_path):
    with pytest.raises(PromptsError, match="not found"):
        load_prompts(tmp_path / "nope.yaml")


def test_missing_prompt_is_rejected(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("version: '1'\nprompts:\n  tc_validation: |\n    " + "x" * 30 + "\n")
    with pytest.raises(PromptsError):
        load_prompts(f)


def test_empty_prompt_is_rejected(tmp_path):
    body = "\n".join(f"  {k}: 'long enough prompt text here....'" for k in REQUIRED_KEYS)
    f = tmp_path / "p.yaml"
    f.write_text("version: '1'\nprompts:\n" + body.replace("summary: 'long enough prompt text here....'", "summary: ''"))
    with pytest.raises(PromptsError):
        load_prompts(f)


def test_unknown_prompt_key_is_rejected(tmp_path):
    body = "\n".join(f"  {k}: 'long enough prompt text here....'" for k in [*REQUIRED_KEYS, "typo_step"])
    f = tmp_path / "p.yaml"
    f.write_text("version: 2\nprompts:\n" + body)
    with pytest.raises(PromptsError):
        load_prompts(f)
