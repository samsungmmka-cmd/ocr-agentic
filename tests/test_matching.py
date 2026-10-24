import pytest
from pydantic import ValidationError

from app.matching import (
    FieldPair,
    build_pairs,
    candidate_items,
    evaluate_items,
    field_match_score,
    item_flags,
    score_pair,
    select_claimed_item,
)
from app.schemas import WebhookPayload

STRICT = ["imei_or_serial", "id_number", "invoice_no"]


def payload(items, user_imei="356789104512345", user_model="Galaxy A55"):
    return WebhookPayload.model_validate({
        "feedback_id": 1, "campaign_id": 1,
        "ocr": {"name": "A", "items": items},
        "user": {"first_name": "A", "imei_or_serial": user_imei, "model": user_model},
    })


# ------------------------------------------------------------- payload contract
def test_snake_case_payload(payload_dict):
    p = WebhookPayload.model_validate(payload_dict)
    assert p.feedback_id == 123 and p.campaign_id == 221
    assert p.ocr.purchase_date == "2026-10-02"
    assert len(p.ocr.items) == 2 and p.ocr.items[1].model == "mobile Pouch"


@pytest.mark.parametrize("bad", [
    {"feedbackid": 1},                       # not snake_case
    {"ocr": {"purchase date": "2026-10-02"}},
    {"user": {"store name": "MTN"}},
    {"ocr": {"items": [{"imei": "1"}]}},
])
def test_non_snake_case_keys_are_rejected(payload_dict, bad):
    for k, v in bad.items():
        if isinstance(v, dict):
            payload_dict[k].update(v)
        else:
            payload_dict[k] = v
    with pytest.raises(ValidationError):
        WebhookPayload.model_validate(payload_dict)


def test_numeric_imei_accepted_as_string(payload_dict):
    payload_dict["ocr"]["items"][1]["imei_or_serial"] = 123
    assert WebhookPayload.model_validate(payload_dict).ocr.items[1].imei_or_serial == "123"


# ----------------------------------------------------------- invoice-level pairs
def test_invoice_pairs_exclude_item_fields(payload_dict):
    p = WebhookPayload.model_validate(payload_dict)
    pairs = {x.field: x for x in build_pairs(p.ocr, p.user, ["confidence"])}
    assert "items" not in pairs and "model" not in pairs and "imei_or_serial" not in pairs
    assert pairs["name"].user == "John Smith"
    assert pairs["purchase_date"].user is None
    assert pairs["submitted_date"].ocr is None


# ------------------------------------------------------------------ item loop
def test_exact_imei_selects_that_line_even_if_not_first():
    p = payload([{"imei_or_serial": "111", "model": "Galaxy S25"},
                 {"imei_or_serial": "356789104512345", "model": "Samsung SM-A556E"},
                 {"imei_or_serial": "123", "model": "mobile Pouch"}])
    evals = evaluate_items(p.ocr, p.user, STRICT, 90)
    assert [c.index for c in candidate_items(evals)] == [1]   # only that line goes to AI
    assert select_claimed_item(evals, 80).index == 1


def test_without_imei_match_every_line_is_a_candidate():
    p = payload([{"imei_or_serial": "999", "model": "mobile Pouch"},
                 {"imei_or_serial": "356789104512340", "model": "Galaxy A55"}])
    evals = evaluate_items(p.ocr, p.user, STRICT, 90)
    assert len(candidate_items(evals)) == 2
    claimed = select_claimed_item(evals, 80)
    assert claimed.index == 1
    assert "customer imei_or_serial not found on invoice" in item_flags(p.user, evals, claimed, 80)


def test_ai_model_score_decides_between_lines():
    p = payload([{"imei_or_serial": None, "model": "mobile Pouch"},
                 {"imei_or_serial": None, "model": "Samsung SM-A556E"}], user_imei=None)
    evals = evaluate_items(p.ocr, p.user, STRICT, 90)
    evals[0].model.comparison.matching_score = 5      # as the LLM would score them
    evals[1].model.comparison.matching_score = 92
    assert select_claimed_item(evals, 80).index == 1


def test_similar_imei_on_other_product_is_not_the_device():
    p = payload([{"imei_or_serial": "356789104512340", "model": "iPhone 16"}])
    evals = evaluate_items(p.ocr, p.user, STRICT, 90)
    evals[0].model.comparison.matching_score = 10     # LLM: not the same product
    assert select_claimed_item(evals, 80) is None
    assert item_flags(p.user, evals, None, 80) == ["customer's device not found on invoice"]


def test_no_items_flagged():
    p = payload([])
    evals = evaluate_items(p.ocr, p.user, STRICT, 90)
    assert select_claimed_item(evals, 80) is None
    assert item_flags(p.user, evals, None, 80) == ["no items found on invoice"]


def test_same_imei_on_two_lines_flagged():
    p = payload([{"imei_or_serial": "356789104512345", "model": "Galaxy A55"},
                 {"imei_or_serial": "356789104512345", "model": "Galaxy A55"}])
    evals = evaluate_items(p.ocr, p.user, STRICT, 90)
    flags = item_flags(p.user, evals, select_claimed_item(evals, 80), 80)
    assert "customer imei_or_serial appears on 2 invoice lines" in flags


# --------------------------------------------------------------- field scoring
def test_exact_and_case_insensitive():
    r = score_pair(FieldPair("name", "John Smith", "JOHN SMITH"), STRICT, 90)
    assert r.comparison.matching_score == 100 and r.comparison.method == "exact"


def test_strict_identifier_never_goes_to_ai():
    r = score_pair(FieldPair("imei_or_serial", "352099001761431", "352099001761481"), STRICT, 90)
    assert not r.needs_ai and r.comparison.method == "strict" and r.comparison.matching_score < 100


def test_strict_identifier_ignores_formatting():
    r = score_pair(FieldPair("imei_or_serial", "35-6789 1045 12345", "356789104512345"), STRICT, 90)
    assert r.comparison.matching_score == 100


def test_dates_in_different_formats():
    r = score_pair(FieldPair("purchase_date", "02/10/2026", "2026-10-02"), STRICT, 90)
    assert r.comparison.matching_score == 100


def test_semantic_field_goes_to_ai():
    r = score_pair(FieldPair("store_name", "MTN Stores", "MTN Southafrica"), STRICT, 90)
    assert r.needs_ai and r.comparison.method == "ai"


def test_field_match_score_ignores_single_side():
    comps = [score_pair(FieldPair("a", "x", "x"), [], 90).comparison,
             score_pair(FieldPair("b", None, "y"), [], 90).comparison]
    assert field_match_score(comps) == 100
