"""Gift assignment: every TC style x customer selection x claim decision."""
import pytest

from app.gifts import find_option, not_eligible, resolve_gift
from app.schemas import Gift, WebhookPayload

R500 = {"name": "R500 MTN voucher", "value": "R500", "type": "voucher"}
BUDS = {"name": "Samsung Galaxy Buds3", "value": None, "type": "gift"}
R750 = {"name": "R750 Takealot voucher", "value": "R750", "type": "voucher"}
CHARGER = {"name": "Samsung 25W Travel Adapter", "value": None, "type": "gift"}


def fixed(match=None):
    return {"gift_type": "fixed", "options": [R500], "eligible_gift": R500,
            "selected_gift_match": match, "tc_reference": "Gifts", "reason": "Every claim gets R500."}


def product(gift=BUDS, match=None):
    return {"gift_type": "product_based", "options": [gift] if gift else [],
            "eligible_gift": gift, "selected_gift_match": match, "tc_reference": "Gifts",
            "reason": "From the TC gift table."}


def choice(match=None):
    return {"gift_type": "customer_choice", "options": [BUDS, R750, CHARGER], "eligible_gift": None,
            "selected_gift_match": match, "tc_reference": "Gifts", "reason": "Customer chooses."}


# --------------------------------------------------------------- 1. fixed gift
def test_fixed_gift_without_selection():
    g = resolve_gift(fixed(), None, "approve")
    assert g.status == "assigned" and g.gift_type == "fixed"
    assert g.gift.name == "R500 MTN voucher" and g.gift.value == "R500" and g.gift.type == "voucher"
    assert g.selection_valid is None          # nothing to validate


def test_fixed_gift_with_matching_selection():
    g = resolve_gift(fixed(), "r500 mtn voucher", "approve")
    assert g.status == "assigned" and g.selection_valid is True


def test_fixed_gift_with_other_selection_still_gets_tc_gift():
    g = resolve_gift(fixed({"matched": False, "option": None}), "Galaxy Watch7", "approve")
    assert g.status == "assigned" and g.gift.name == "R500 MTN voucher"
    assert g.selection_valid is False and "TC assigns" in g.reason


# ------------------------------------------------------------ 2. product table
def test_product_table_gift():
    g = resolve_gift(product(BUDS), None, "approve")
    assert g.status == "assigned" and g.gift_type == "product_based" and g.gift.name == BUDS["name"]


def test_product_table_short_selection_matched_by_llm():
    g = resolve_gift(product(BUDS, {"matched": True, "option": "Samsung Galaxy Buds3"}), "Buds3", "approve")
    assert g.status == "assigned" and g.selection_valid is True


def test_product_not_in_table():
    g = resolve_gift(product(None), None, "approve")
    assert g.status == "not_found" and g.gift is None


# ------------------------------------------------------------ 3. customer choice
def test_choice_exact_selection():
    g = resolve_gift(choice(), "R750 Takealot Voucher", "approve")
    assert g.status == "assigned" and g.gift.name == "R750 Takealot voucher" and g.selection_valid


def test_choice_informal_selection_matched_by_llm():
    g = resolve_gift(choice({"matched": True, "option": "Samsung Galaxy Buds3"}), "the buds", "approve")
    assert g.status == "assigned" and g.gift.name == BUDS["name"]


def test_choice_invalid_selection():
    g = resolve_gift(choice({"matched": False, "option": None}), "Apple AirPods", "approve")
    assert g.status == "selection_invalid" and g.gift is None and g.selection_valid is False
    assert len(g.options) == 3


def test_choice_without_selection():
    g = resolve_gift(choice(), None, "approve")
    assert g.status == "selection_required" and g.gift is None and len(g.options) == 3


def test_empty_string_selection_counts_as_none():
    assert resolve_gift(choice(), "   ", "approve").status == "selection_required"
    assert resolve_gift(fixed(), "", "approve").selection_valid is None


# ---------------------------------------------------------------- guard rails
def test_llm_cannot_assign_a_gift_not_in_the_tc_options():
    out = fixed() | {"eligible_gift": {"name": "iPad Pro", "type": "gift"}}
    assert resolve_gift(out, None, "approve").status == "not_found"


def test_llm_match_to_unknown_option_is_ignored():
    g = resolve_gift(choice({"matched": True, "option": "Apple AirPods"}), "AirPods", "approve")
    assert g.status == "selection_invalid"


def test_no_gift_rule_in_tc():
    g = resolve_gift({"gift_type": "none", "options": []}, None, "approve")
    assert g.status == "not_found"


def test_bad_llm_values_are_tolerated():
    out = {"gift_type": "lottery", "options": ["R500 voucher", {"no_name": 1}, None]}
    assert resolve_gift(out, None, "approve").status == "not_found"


# ----------------------------------------------------------- claim decision
def test_review_claim_gets_provisional_gift():
    g = resolve_gift(fixed(), None, "review")
    assert g.status == "provisional" and g.gift.name == "R500 MTN voucher"


@pytest.mark.parametrize("decision", ["reject", "manual_review"])
def test_rejected_claim_gets_no_gift(decision):
    g = not_eligible(decision, "Buds3")
    assert g.status == "not_eligible" and g.gift is None and g.selected_gift == "Buds3"


def test_find_option_threshold():
    opts = [Gift(name="Samsung Galaxy Buds3"), Gift(name="R750 Takealot voucher")]
    assert find_option("samsung galaxy buds 3", opts).name == "Samsung Galaxy Buds3"
    assert find_option("R500 voucher", opts) is None


def test_selected_gift_in_payload_and_not_compared_as_field(payload_dict):
    from app.matching import build_pairs
    payload_dict["user"]["selected_gift"] = "Buds3"
    p = WebhookPayload.model_validate(payload_dict)
    assert p.user.selected_gift == "Buds3"
    assert "selected_gift" not in [x.field for x in build_pairs(p.ocr, p.user, [])]
