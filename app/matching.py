"""Field pairing (OCR vs user) and deterministic fuzzy scoring.

Fields that cannot be decided by string similarity are marked `needs_ai`
and resolved by the LLM in one batched call.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime

from rapidfuzz import fuzz

from app.schemas import FieldComparison, InvoiceItemMatch, OCRData, UserData

DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y",
    "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%Y%m%d",
)


@dataclass
class FieldPair:
    field: str
    user: str | None
    ocr: str | None


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# Compared per invoice item, not at invoice level.
ITEM_FIELDS = ("imei_or_serial", "model")
# Customer inputs that have no OCR counterpart to compare with (handled in other steps).
NON_COMPARED_USER_FIELDS = ("selected_gift",)


def build_pairs(ocr: OCRData, user: UserData, excluded: list[str]) -> list[FieldPair]:
    """Invoice-level fields: union of every field present in the OCR and user payloads.

    The user's first_name + last_name is compared with the OCR `name`.
    imei_or_serial / model are compared per invoice item (see `evaluate_items`).
    Order: OCR fields first (as sent), then user-only fields.
    """
    skip = set(excluded) | set(ITEM_FIELDS) | set(NON_COMPARED_USER_FIELDS) | {"items"}
    o = {k: _clean(v) for k, v in ocr.model_dump(exclude_unset=True).items() if k not in skip}
    u = {k: _clean(v) for k, v in user.model_dump(exclude_unset=True).items() if k not in skip}

    if "first_name" in u or "last_name" in u:
        full = " ".join(p for p in (u.pop("first_name", None), u.pop("last_name", None)) if p)
        u["name"] = full or None

    pairs = [FieldPair(k, u.get(k), v) for k, v in o.items()]
    pairs += [FieldPair(k, v, None) for k, v in u.items() if k not in o]
    return pairs


def normalize_text(v: str) -> str:
    v = re.sub(r"[^\w\s]", " ", v.lower())
    return re.sub(r"\s+", " ", v).strip()


def alnum(v: str) -> str:
    return re.sub(r"[^a-z0-9]", "", v.lower())


def parse_date(v: str) -> date | None:
    v = v.strip()
    try:
        return datetime.fromisoformat(v).date()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def is_date_field(field: str) -> bool:
    return "date" in field or field in {"dob"}


@dataclass
class Scored:
    comparison: FieldComparison
    needs_ai: bool = False


def score_pair(pair: FieldPair, strict_fields: list[str], auto_accept: int) -> Scored:
    u, o = pair.user, pair.ocr
    if u is None or o is None:
        return Scored(FieldComparison(field=pair.field, user=u, ocr=o,
                                      matching_score=None, method="single_side"))

    def result(score: int, method: str, needs_ai: bool = False) -> Scored:
        return Scored(FieldComparison(field=pair.field, user=u, ocr=o,
                                      matching_score=int(round(score)), method=method), needs_ai)

    if normalize_text(u) == normalize_text(o):
        return result(100, "exact")

    if pair.field in strict_fields:
        a, b = alnum(u), alnum(o)
        if a == b:
            return result(100, "exact")
        # Identifiers are never "reasoned": a one-digit difference is still a mismatch.
        return result(min(fuzz.ratio(a, b), 99), "strict")

    if is_date_field(pair.field):
        du, do = parse_date(u), parse_date(o)
        if du and do:
            return result(100 if du == do else 0, "strict")

    score = max(fuzz.ratio(normalize_text(u), normalize_text(o)),
                fuzz.token_sort_ratio(normalize_text(u), normalize_text(o)))
    if score >= auto_accept:
        return result(score, "fuzzy")
    return result(score, "ai", needs_ai=True)


def field_match_score(comparisons: list[FieldComparison]) -> int | None:
    """Average matching score over fields present on both sides."""
    scores = [c.matching_score for c in comparisons if c.matching_score is not None]
    return round(sum(scores) / len(scores)) if scores else None


# ------------------------------------------------------------- invoice items
def item_key(index: int, field: str) -> str:
    """Unique key used for an item field in the batched AI call, e.g. items[1].model."""
    return f"items[{index}].{field}"


@dataclass
class ItemEval:
    index: int
    imei: Scored
    model: Scored

    @property
    def imei_exact(self) -> bool:
        return self.imei.comparison.matching_score == 100

    @property
    def combined(self) -> float:
        """Ranking score when no IMEI matches exactly: IMEI weighs more than model."""
        i = self.imei.comparison.matching_score or 0
        m = self.model.comparison.matching_score or 0
        return 0.6 * i + 0.4 * m


def evaluate_items(ocr: OCRData, user: UserData, strict_fields: list[str],
                   auto_accept: int) -> list[ItemEval]:
    """Score every invoice line against the customer's IMEI/serial and model."""
    evals = []
    for i, item in enumerate(ocr.items):
        imei = score_pair(FieldPair("imei_or_serial", _clean(user.imei_or_serial),
                                    _clean(item.imei_or_serial)), strict_fields, auto_accept)
        model = score_pair(FieldPair("model", _clean(user.model), _clean(item.model)),
                           strict_fields, auto_accept)
        for sc in (imei, model):
            sc.comparison.item_index = i
        evals.append(ItemEval(i, imei, model))
    return evals


def candidate_items(evals: list[ItemEval]) -> list[ItemEval]:
    """Items worth an AI model check: the IMEI-matched line if there is one, else all lines."""
    exact = [e for e in evals if e.imei_exact]
    return exact[:1] or evals


def select_claimed_item(evals: list[ItemEval], model_min_score: int) -> ItemEval | None:
    """Find the invoice line the customer is claiming.

    1. A line with the customer's exact IMEI/serial wins.
    2. Otherwise the best IMEI + model combination, but only if that line's model
       really matches (>= model_min_score). A near-identical IMEI on a different
       product is not the customer's device.
    3. Otherwise None: the customer's device is not on the invoice.
    """
    if not evals:
        return None
    exact = [e for e in evals if e.imei_exact]
    if exact:
        return exact[0]
    matching_model = [e for e in evals
                      if (e.model.comparison.matching_score or 0) >= model_min_score]
    return max(matching_model, key=lambda e: e.combined) if matching_model else None


def item_matches(evals: list[ItemEval], claimed: ItemEval | None) -> list[InvoiceItemMatch]:
    return [
        InvoiceItemMatch(
            index=e.index,
            imei_or_serial=e.imei.comparison.ocr,
            model=e.model.comparison.ocr,
            imei_score=e.imei.comparison.matching_score,
            model_score=e.model.comparison.matching_score,
            model_reason=e.model.comparison.ai_reason,
            claimed=claimed is not None and e.index == claimed.index,
        )
        for e in evals
    ]


def claimed_item_rows(user: UserData, claimed: ItemEval | None) -> list[FieldComparison]:
    """The imei_or_serial / model rows for ocr_vs_user, taken from the claimed item."""
    if claimed is not None:
        return [claimed.imei.comparison, claimed.model.comparison]
    return [FieldComparison(field=f, user=_clean(getattr(user, f)), ocr=None,
                            matching_score=None, method="single_side") for f in ITEM_FIELDS]


def item_flags(user: UserData, evals: list[ItemEval], claimed: ItemEval | None,
               model_min_score: int) -> list[str]:
    flags = []
    if not evals:
        return ["no items found on invoice"]
    if claimed is None:
        return ["customer's device not found on invoice"]
    if user.imei_or_serial and not claimed.imei_exact:
        flags.append("customer imei_or_serial not found on invoice")
    n_same = sum(1 for e in evals if e.imei_exact)
    if n_same > 1:
        flags.append(f"customer imei_or_serial appears on {n_same} invoice lines")
    model_score = claimed.model.comparison.matching_score
    if model_score is not None and model_score < model_min_score:
        flags.append(f"customer model does not match invoice line {claimed.index}")
    return flags
