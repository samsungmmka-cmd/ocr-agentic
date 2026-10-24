"""Gift / voucher assignment from the campaign TC.

The LLM reads the TC and extracts the gift rule (fixed gift, product table or
customer choice) together with the gift options that apply to this claim.
The code then decides the outcome, so a gift can only ever be one that the
TC actually lists:

    fixed            every valid claim gets the same gift
    product_based    the gift for the claimed product, from the TC table
    customer_choice  the customer's selected_gift, if it is one of the TC options

`selected_gift` is only validated when the customer sent one.
"""
import logging
import re

from pydantic import ValidationError
from rapidfuzz import fuzz

from app.schemas import EligibleGift, Gift

log = logging.getLogger(__name__)

GIFT_TYPES = {"fixed", "product_based", "customer_choice", "none"}
# Decisions that get a gift. reject / manual_review never do.
GIFT_DECISIONS = {"approve", "review"}
# Gift statuses that stop a claim from being auto-approved.
BLOCKING_STATUSES = {"selection_required", "selection_invalid", "not_found"}


def _norm(v: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (v or "").lower())).strip()


def _gift(raw) -> Gift | None:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict) or not raw.get("name"):
        return None
    if raw.get("type") not in ("voucher", "gift"):
        raw = {**raw, "type": None}
    try:
        return Gift.model_validate({k: raw.get(k) for k in ("name", "value", "type")})
    except ValidationError:
        return None


def find_option(name: str | None, options: list[Gift], threshold: int = 90) -> Gift | None:
    """Return the TC option `name` refers to (case/punctuation-insensitive, fuzzy)."""
    if not name:
        return None
    target = _norm(name)
    best, best_score = None, 0
    for opt in options:
        score = max(fuzz.ratio(target, _norm(opt.name)), fuzz.token_sort_ratio(target, _norm(opt.name)))
        if score > best_score:
            best, best_score = opt, score
    return best if best_score >= threshold else None


def not_eligible(decision: str, selected_gift: str | None) -> EligibleGift:
    return EligibleGift(status="not_eligible", selected_gift=selected_gift,
                        reason=f"Claim decision is '{decision}': no gift is assigned.")


def resolve_gift(llm_out: dict, selected_gift: str | None, decision: str) -> EligibleGift:
    """Turn the LLM's reading of the TC into a checked gift assignment."""
    gift_type = str(llm_out.get("gift_type") or "none")
    gift_type = gift_type if gift_type in GIFT_TYPES else "none"
    options = [g for g in (_gift(o) for o in llm_out.get("options") or []) if g]
    ref = llm_out.get("tc_reference")
    llm_reason = str(llm_out.get("reason") or "")
    selected = (selected_gift or "").strip() or None

    def result(status, gift=None, selection_valid=None, reason=llm_reason) -> EligibleGift:
        if status == "assigned" and decision == "review":
            status = "provisional"
            reason = (reason + " Gift applies if the claim is approved in review.").strip()
        return EligibleGift(status=status, gift_type=gift_type, gift=gift, selected_gift=selected,
                            selection_valid=selection_valid, options=options, tc_reference=ref,
                            reason=reason)

    if gift_type == "none" or not options:
        return result("not_found", reason=llm_reason or "The TC does not define a gift for this claim.")

    # LLM's own match of the customer's selection, accepted only if it names a TC option
    llm_match = llm_out.get("selected_gift_match") or {}
    llm_pick = find_option(llm_match.get("option"), options) if llm_match.get("matched") else None

    if gift_type == "customer_choice":
        if not selected:
            return result("selection_required",
                          reason="The campaign lets the customer choose a gift, but no selected_gift was sent.")
        chosen = find_option(selected, options) or llm_pick
        if chosen is None:
            names = ", ".join(o.name for o in options)
            return result("selection_invalid", selection_valid=False,
                          reason=f"'{selected}' is not one of the TC gift options ({names}).")
        return result("assigned", gift=chosen, selection_valid=True,
                      reason=f"Customer selected '{selected}', which matches the TC option '{chosen.name}'.")

    # fixed / product_based: the TC decides the gift
    gift = find_option((_gift(llm_out.get("eligible_gift")) or Gift(name="")).name, options)
    if gift is None:
        return result("not_found", reason=llm_reason or "No TC gift matches the claimed product.")
    if not selected:
        return result("assigned", gift=gift)
    same = find_option(selected, [gift]) is not None or (llm_pick is not None and llm_pick.name == gift.name)
    if same:
        return result("assigned", gift=gift, selection_valid=True)
    return result("assigned", gift=gift, selection_valid=False,
                  reason=f"{llm_reason} Customer selected '{selected}', but the TC assigns "
                         f"'{gift.name}' for this claim; the TC gift is assigned.".strip())
