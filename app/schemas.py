"""Request / response / result models.

The webhook contract is strict snake_case: unknown or misspelled keys
(e.g. "purchase date") are rejected with 422 instead of being silently lost.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    """Forbid unknown keys, accept numbers for string fields, trim strings."""

    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _strip(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: (v.strip() or None) if isinstance(v, str) else v for k, v in data.items()}
        return data


class OCRItem(_Strict):
    """One product line on the invoice."""

    imei_or_serial: str | None = None
    model: str | None = None


class OCRData(_Strict):
    name: str | None = None
    id_number: str | None = None
    gender: str | None = None
    dob: str | None = None
    country: str | None = None
    invoice_no: str | None = None
    purchase_date: str | None = None
    items: list[OCRItem] = []
    store_name: str | None = None
    store_address: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=100)


class UserData(_Strict):
    first_name: str | None = None
    last_name: str | None = None
    imei_or_serial: str | None = None
    store_name: str | None = None
    model: str | None = None
    submitted_date: str | None = None
    selected_gift: str | None = None  # gift the customer chose, if the campaign offers a choice


class WebhookPayload(_Strict):
    feedback_id: int
    campaign_id: int
    ocr: OCRData
    user: UserData


# ---------------------------------------------------------------- tasks
class TaskStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class TaskStep(str, Enum):
    received = "received"
    tc_validation = "tc_validation"
    field_match = "field_match"
    history_check = "history_check"
    gift_assignment = "gift_assignment"
    reasoning = "reasoning"
    submitting = "submitting"
    done = "done"


class TaskState(BaseModel):
    task_id: str
    feedback_id: int
    campaign_id: int
    status: TaskStatus
    current_step: TaskStep
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    decision: str | None = None
    overall_score: int | None = None


class WebhookAccepted(BaseModel):
    task_id: str
    status: TaskStatus
    duplicate: bool = False


# --------------------------------------------------------------- result
class FieldComparison(BaseModel):
    field: str
    user: str | None
    ocr: str | None
    matching_score: int | None
    method: Literal["exact", "fuzzy", "strict", "ai", "single_side"]
    ai_reason: str | None = None
    item_index: int | None = None  # set for imei_or_serial / model: the matched invoice line


class InvoiceItemMatch(BaseModel):
    """How one invoice line compares with the customer's IMEI/serial and model."""

    index: int
    imei_or_serial: str | None
    model: str | None
    imei_score: int | None
    model_score: int | None
    model_reason: str | None = None
    claimed: bool = False


class TCCheck(BaseModel):
    rule: str
    field: str | None = None
    status: Literal["pass", "fail", "not_verifiable"] = "not_verifiable"
    detail: str = ""


class TCIssue(BaseModel):
    rule: str
    field: str | None = None
    severity: Literal["low", "medium", "high"] = "medium"
    detail: str = ""


class TCValidation(BaseModel):
    passed: bool
    compliance_score: int | None
    tc_version: str | int | None = None
    checks: list[TCCheck] = []
    issues: list[TCIssue] = []


class HistoryRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field_name: str
    num_of_claims: int = 0


class RiskAssessment(BaseModel):
    level: Literal["low", "medium", "high"]
    hard_flag: bool
    flags: list[str] = []
    history: list[HistoryRecord] = []
    history_score: int
    explanation: str = ""


class Gift(BaseModel):
    name: str
    value: str | None = None                       # e.g. "R500"
    type: Literal["voucher", "gift"] | None = None


GiftStatus = Literal[
    "assigned",            # claim approved, gift determined
    "provisional",         # claim in review; gift applies if the reviewer approves
    "not_eligible",        # claim rejected
    "selection_required",  # campaign lets the customer choose, but no selected_gift
    "selection_invalid",   # selected_gift is not one of the TC options
    "not_found",           # TC has no gift rule for this claim / product
    "not_evaluated",       # pipeline error
]


class EligibleGift(BaseModel):
    status: GiftStatus
    gift_type: Literal["fixed", "product_based", "customer_choice", "none"] | None = None
    gift: Gift | None = None
    selected_gift: str | None = None              # what the customer asked for
    selection_valid: bool | None = None           # None = no selection to validate
    options: list[Gift] = []                      # gifts the TC offers for this claim
    tc_reference: str | None = None               # TC section the gift comes from
    reason: str = ""


class ValidationResult(BaseModel):
    task_id: str
    feedback_id: int
    campaign_id: int
    tc_version: str | int | None = None
    prompt_version: str | None = None
    ocr_vs_user: list[FieldComparison]
    claimed_item_index: int | None = None
    invoice_items: list[InvoiceItemMatch] = []
    tc_validation: TCValidation
    risk: RiskAssessment
    eligible_gift: EligibleGift
    ai_summary: str
    overall_score: int
    decision: Literal["approve", "review", "reject", "manual_review"]
    flags: list[str] = []
    processed_at: datetime
    error: str | None = None
