"""The validation pipeline: fixed order of steps, LLM reasoning inside each step.

    1. User vs OCR field match  (invoice fields + loop over invoice items to find the
                                 claimed product; fuzzy first, LLM for unclear fields)
    2. TC rules validation      (RAG API + LLM, device rules checked on the claimed item)
    3. History check            (LLM agent with check_claim_history tool) + risk rules
    4. Score and decision       (code)
    5. Gift assignment          (LLM reads the TC gift rule, code checks it; valid claims only)
    6. Summary                  (LLM)
    7. Submit                   (submit_result tool -> Result API)

Field match runs before TC because device rules (qualifying model, IMEI on the
invoice) can only be checked once we know which invoice line is being claimed.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from pydantic import ValidationError

from app.clients import HistoryClient, RagClient, ResultClient
from app.config import Settings
from app.gifts import BLOCKING_STATUSES, GIFT_DECISIONS, not_eligible, resolve_gift
from app.llm import LLMClient
from app.matching import (
    ItemEval,
    build_pairs,
    candidate_items,
    claimed_item_rows,
    evaluate_items,
    field_match_score,
    item_flags,
    item_key,
    item_matches,
    score_pair,
    select_claimed_item,
)
from app.schemas import (
    EligibleGift,
    FieldComparison,
    InvoiceItemMatch,
    HistoryRecord,
    RiskAssessment,
    TaskStep,
    TCCheck,
    TCIssue,
    TCValidation,
    ValidationResult,
    WebhookPayload,
)
from app.scoring import decide, merge_risk, rule_risk
from app.tasks import TaskStore
from app.tools import AgentTools

log = logging.getLogger(__name__)


def _items(model_cls, raw) -> list:
    out = []
    for item in raw or []:
        try:
            out.append(model_cls.model_validate(item))
        except ValidationError:
            log.warning("dropping malformed llm item", extra={"data": {"item": item}})
    return out


@dataclass
class MatchOutcome:
    comparisons: list[FieldComparison]           # ocr_vs_user rows
    items: list[InvoiceItemMatch]                # every invoice line, scored
    claimed: ItemEval | None                     # the invoice line being claimed
    claimed_item: dict | None                    # its OCR data (for TC / history)
    flags: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, s: Settings, llm: LLMClient, rag: RagClient, history: HistoryClient,
                 results: ResultClient, store: TaskStore):
        self.s, self.llm, self.rag, self.history, self.results, self.store = (
            s, llm, rag, history, results, store)

    # ------------------------------------------------------------ public
    async def run(self, task_id: str, payload: WebhookPayload) -> ValidationResult:
        tools = AgentTools(self.history, self.results, payload.feedback_id)
        flags: list[str] = []

        conf = payload.ocr.confidence
        if conf is not None:
            conf = conf / 100 if conf > 1 else conf
            if conf < self.s.ocr_min_confidence:
                flags.append(f"low OCR confidence ({conf:.2f})")

        await self.store.step(task_id, TaskStep.field_match)
        match = await self.match_fields(payload)
        flags += match.flags
        comparisons = match.comparisons

        await self.store.step(task_id, TaskStep.tc_validation)
        tc, tc_chunks = await self.validate_tc(payload, match.claimed_item)
        if tc.compliance_score is None:
            flags.append("no TC rules found for campaign")

        await self.store.step(task_id, TaskStep.history_check)
        risk = await self.check_history(payload, match.claimed_item, tools)

        for c in comparisons:  # invoice-level identifier differs (items are flagged above)
            if (c.item_index is None and c.field in self.s.strict_fields
                    and c.matching_score is not None and c.matching_score < 100):
                flags.append(f"{c.field} mismatch between customer and OCR")

        field_score = field_match_score(comparisons)
        decision = decide(field_score, tc, risk, flags, self.s)

        await self.store.step(task_id, TaskStep.gift_assignment)
        gift = await self.assign_gift(payload, match.claimed_item, tc_chunks, decision.decision)
        if decision.decision == "approve" and gift.status in BLOCKING_STATUSES:
            # a valid claim whose gift cannot be determined needs a person to decide
            decision.decision = "review"
            decision.reasons.append(f"gift {gift.status}")

        await self.store.step(task_id, TaskStep.reasoning)
        summary = await self.summarize(payload, match, tc, risk, decision.overall_score,
                                       decision.decision, decision.reasons, gift)

        result = ValidationResult(
            task_id=task_id,
            feedback_id=payload.feedback_id,
            campaign_id=payload.campaign_id,
            tc_version=tc.tc_version,
            prompt_version=self.s.prompts.version,
            ocr_vs_user=comparisons,
            claimed_item_index=match.claimed.index if match.claimed else None,
            invoice_items=match.items,
            tc_validation=tc,
            risk=risk,
            eligible_gift=gift,
            ai_summary=summary,
            overall_score=decision.overall_score,
            decision=decision.decision,
            flags=decision.reasons,
            processed_at=datetime.now(timezone.utc),
        )

        await self.store.step(task_id, TaskStep.submitting)
        await tools.submit_result(result.model_dump(mode="json"))
        return result

    async def submit_failure(self, task_id: str, payload: WebhookPayload, error: str) -> None:
        """A step failed after retries: post a manual_review result so no claim is lost."""
        match, _, _ = self._deterministic_match(payload)  # no LLM: it may be what failed
        result = ValidationResult(
            task_id=task_id,
            feedback_id=payload.feedback_id,
            campaign_id=payload.campaign_id,
            prompt_version=self.s.prompts.version,
            ocr_vs_user=match.comparisons,
            claimed_item_index=match.claimed.index if match.claimed else None,
            invoice_items=match.items,
            tc_validation=TCValidation(passed=False, compliance_score=None),
            risk=RiskAssessment(level="low", hard_flag=False, history_score=0,
                                explanation="not evaluated"),
            eligible_gift=EligibleGift(status="not_evaluated",
                                       selected_gift=payload.user.selected_gift,
                                       reason="Validation did not complete."),
            ai_summary="Automatic validation could not be completed. Manual review required.",
            overall_score=0,
            decision="manual_review",
            flags=["pipeline_error"],
            processed_at=datetime.now(timezone.utc),
            error=error,
        )
        await AgentTools(self.history, self.results, payload.feedback_id).submit_result(
            result.model_dump(mode="json"))

    # ------------------------------------------------------------ step 2
    async def validate_tc(self, payload: WebhookPayload,
                          claimed_item: dict | None) -> tuple[TCValidation, list[dict]]:
        """Returns the TC validation and the TC chunks (reused by the gift step)."""
        rules = await self.rag.get_rules(payload.campaign_id) or {}
        chunks = rules.get("chunks") or []
        version = rules.get("tc_version")
        if not chunks:
            log.warning("no TC rules for campaign")
            return TCValidation(
                passed=False, compliance_score=None, tc_version=version,
                issues=[TCIssue(rule="TC rules available", severity="high",
                                detail=f"No TC rules found for campaign {payload.campaign_id}")],
            ), []

        out = await self.llm.chat_json("tc_validation", self.s.prompts.prompts.tc_validation, {
            "today": date.today().isoformat(),
            "campaign_id": payload.campaign_id,
            "tc_rules": [{"section": c.get("section"), "text": c.get("text")} for c in chunks],
            "ocr_data": payload.ocr.model_dump(exclude_unset=True),
            "claimed_item": claimed_item,
            "customer_data": payload.user.model_dump(exclude_unset=True),
        })
        score = out.get("compliance_score")
        return TCValidation(
            passed=bool(out.get("passed", False)),
            compliance_score=max(0, min(100, int(score))) if score is not None else 0,
            tc_version=version,
            checks=_items(TCCheck, out.get("checks")),
            issues=_items(TCIssue, out.get("issues")),
        ), chunks

    # ------------------------------------------------------------ step 1
    def _deterministic_match(self, payload: WebhookPayload):
        """Invoice-level pairs + per-item scores, before any AI call."""
        s = self.s
        scored = [score_pair(p, s.strict_fields, s.fuzzy_auto_accept)
                  for p in build_pairs(payload.ocr, payload.user, s.excluded_fields)]
        evals = evaluate_items(payload.ocr, payload.user, s.strict_fields, s.fuzzy_auto_accept)
        claimed = select_claimed_item(evals, s.item_model_min_score)
        return self._outcome(payload, scored, evals, claimed), scored, evals

    def _outcome(self, payload, scored, evals, claimed) -> MatchOutcome:
        rows = [sc.comparison for sc in scored]
        # imei_or_serial / model rows come from the claimed invoice line
        rows += claimed_item_rows(payload.user, claimed)
        claimed_item = (payload.ocr.items[claimed.index].model_dump() | {"index": claimed.index}
                        if claimed else None)
        return MatchOutcome(
            comparisons=rows,
            items=item_matches(evals, claimed),
            claimed=claimed,
            claimed_item=claimed_item,
            flags=item_flags(payload.user, evals, claimed, self.s.item_model_min_score),
        )

    async def match_fields(self, payload: WebhookPayload) -> MatchOutcome:
        _, scored, evals = self._deterministic_match(payload)

        # One batched AI call: unclear invoice-level fields + unclear item models.
        # If an invoice line has the customer's exact IMEI only that line is checked,
        # otherwise every line is a candidate (e.g. IMEI typed wrongly by the customer).
        unclear: dict[str, FieldComparison] = {
            sc.comparison.field: sc.comparison for sc in scored if sc.needs_ai}
        for e in candidate_items(evals):
            if e.model.needs_ai:
                unclear[item_key(e.index, "model")] = e.model.comparison
        log.info("field match", extra={"data": {
            "fields": len(scored), "invoice_items": len(evals), "ai_fields": len(unclear)}})

        if unclear:
            out = await self.llm.chat_json("field_match", self.s.prompts.prompts.field_match, {
                "pairs": [{"field": key, "customer_value": c.user, "ocr_value": c.ocr}
                          for key, c in unclear.items()],
            })
            by_field = {r.get("field"): r for r in out.get("results") or [] if isinstance(r, dict)}
            for key, c in unclear.items():
                r = by_field.get(key)
                if r is None or r.get("matching_score") is None:
                    continue  # keep the fuzzy score
                c.matching_score = max(0, min(100, int(r["matching_score"])))
                c.ai_reason = r.get("reason")

        claimed = select_claimed_item(evals, self.s.item_model_min_score)  # with AI model scores
        outcome = self._outcome(payload, scored, evals, claimed)
        log.info("claimed item", extra={"data": {
            "index": claimed.index if claimed else None, "flags": outcome.flags}})
        return outcome

    # ------------------------------------------------------------ step 3
    async def check_history(self, payload: WebhookPayload, claimed_item: dict | None,
                            tools: AgentTools) -> RiskAssessment:
        o, u = payload.ocr, payload.user
        item_imei = (claimed_item or {}).get("imei_or_serial")
        claim = {
            "campaign_id": payload.campaign_id,
            "imei_or_serial": item_imei or u.imei_or_serial,
            "id_number": o.id_number,
            "invoice_no": o.invoice_no,
        }
        if u.imei_or_serial and item_imei and u.imei_or_serial != item_imei:
            claim["customer_imei_or_serial"] = u.imei_or_serial

        ai_out, calls = await self.llm.run_with_tools(
            "history_check", self.s.prompts.prompts.history, {"claim": claim}, tools.llm_tools, tools.execute)

        log.info("history agent finished", extra={"data": {"tool_calls": len(calls)}})
        if tools.history_records is None:
            # The model skipped the tool: history is mandatory, so call it ourselves.
            log.warning("llm did not call check_claim_history; calling directly")
            await tools.check_claim_history(**{k: v for k, v in claim.items()
                                                if k != "customer_imei_or_serial"})

        records = _items(HistoryRecord, tools.history_records)
        return merge_risk(rule_risk(records, self.s), ai_out, records)

    # ------------------------------------------------------------ step 5
    async def assign_gift(self, payload: WebhookPayload, claimed_item: dict | None,
                          tc_chunks: list[dict], decision: str) -> EligibleGift:
        selected = payload.user.selected_gift
        if decision not in GIFT_DECISIONS:
            return not_eligible(decision, selected)
        if not tc_chunks:
            return EligibleGift(status="not_found", selected_gift=selected,
                                reason="No TC rules found for the campaign.")
        out = await self.llm.chat_json("gift_assignment", self.s.prompts.prompts.gift_assignment, {
            "campaign_id": payload.campaign_id,
            "tc_rules": [{"section": c.get("section"), "text": c.get("text")} for c in tc_chunks],
            "claimed_item": claimed_item,
            "customer_model": payload.user.model,
            "selected_gift": selected,
            "purchase_date": payload.ocr.purchase_date,
            "store_name": payload.ocr.store_name,
        })
        gift = resolve_gift(out, selected, decision)
        log.info("gift assignment", extra={"data": {
            "status": gift.status, "gift_type": gift.gift_type,
            "gift": gift.gift.name if gift.gift else None, "selection_valid": gift.selection_valid}})
        return gift

    # ------------------------------------------------------------ step 6
    async def summarize(self, payload, match: MatchOutcome, tc, risk, score, decision,
                        reasons, gift: EligibleGift) -> str:
        comparisons = match.comparisons
        try:
            out = await self.llm.chat_json("summary", self.s.prompts.prompts.summary, {
                "feedback_id": payload.feedback_id,
                "ocr_vs_user": [c.model_dump() for c in comparisons],
                "invoice_items": [i.model_dump() for i in match.items],
                "tc_validation": tc.model_dump(),
                "risk": risk.model_dump(),
                "eligible_gift": gift.model_dump(),
                "overall_score": score,
                "decision": decision,
                "decision_reasons": reasons,
            })
            if out.get("ai_summary"):
                return str(out["ai_summary"])
        except Exception as e:  # summary is non-critical; never lose the result over it
            log.warning("summary failed, using fallback", extra={"data": {"error": str(e)}})
        mismatches = [c.field for c in comparisons
                      if c.matching_score is not None and c.matching_score < self.s.fuzzy_auto_accept]
        gift_text = gift.gift.name if gift.gift else gift.status
        return (f"Decision: {decision} (score {score}). TC passed: {tc.passed}. "
                f"History risk: {risk.level}. Mismatched fields: {', '.join(mismatches) or 'none'}. "
                f"Gift: {gift_text}.")
