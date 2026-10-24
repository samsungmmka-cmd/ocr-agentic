"""Pipeline tests with in-memory fakes (no network, no Redis, no LLM)."""
import asyncio

from app.pipeline import Pipeline
from app.schemas import WebhookPayload


class FakeLLM:
    def __init__(self, call_tool: bool = True):
        self.call_tool = call_tool
        self.steps: list[str] = []
        self.payloads: dict[str, dict] = {}
        self.gift_out = {"gift_type": "fixed", "options": [{"name": "R500 MTN voucher", "value": "R500",
                                                            "type": "voucher"}],
                         "eligible_gift": {"name": "R500 MTN voucher"}, "tc_reference": "Gifts"}

    async def chat_json(self, step, system, payload):
        self.steps.append(step)
        self.payloads[step] = payload
        if step == "tc_validation":
            return {"passed": True, "compliance_score": 100, "checks": [], "issues": []}
        if step == "field_match":
            return {"results": [{"field": p["field"], "matching_score": 91, "reason": "same entity"}
                                for p in payload["pairs"]]}
        if step == "gift_assignment":
            return self.gift_out
        if step == "summary":
            return {"ai_summary": "All good."}
        raise AssertionError(step)

    async def run_with_tools(self, step, system, payload, tools, executor):
        self.steps.append(step)
        assert tools[0]["function"]["name"] == "check_claim_history"
        calls = []
        if self.call_tool:
            claim = payload["claim"]
            res = await executor("check_claim_history", {
                k: claim[k] for k in ("campaign_id", "imei_or_serial", "id_number", "invoice_no")})
            calls.append(res)
        return {"risk_level": "low", "flags": [], "explanation": "none"}, calls


class FakeRag:
    def __init__(self, chunks=True):
        self.chunks = chunks

    async def get_rules(self, campaign_id):
        return {"tc_version": 3, "chunks": [{"text": "rule"}] if self.chunks else []}


class FakeHistory:
    def __init__(self, records):
        self.records, self.bodies = records, []

    async def check(self, body):
        self.bodies.append(body)
        return self.records


class FakeResults:
    def __init__(self):
        self.submitted = []

    async def submit(self, result):
        self.submitted.append(result)
        return {"status": "stored"}


class FakeStore:
    def __init__(self):
        self.steps = []

    async def step(self, task_id, step):
        self.steps.append(step.value)


def make(settings, records=None, llm=None, rag=None):
    history, results, store = FakeHistory(records or []), FakeResults(), FakeStore()
    p = Pipeline(settings, llm or FakeLLM(), rag or FakeRag(), history, results, store)
    return p, history, results, store


def test_happy_path(settings, payload_dict):
    llm = FakeLLM()
    p, history, results, store = make(settings, [{"field_name": "imei", "num_of_claims": 0}], llm=llm)
    result = asyncio.run(p.run("t1", WebhookPayload.model_validate(payload_dict)))

    assert result.decision == "approve"
    assert store.steps == ["field_match", "tc_validation", "history_check", "gift_assignment",
                           "reasoning", "submitting"]
    assert history.bodies[0]["feedback_id"] == 123          # current claim excluded
    assert history.bodies[0]["imei"] == "356789104512345"   # IMEI of the claimed line

    sent = results.submitted[0]
    rows = {r["field"]: r for r in sent["ocr_vs_user"]}
    assert rows["store_name"]["method"] == "ai" and rows["store_name"]["matching_score"] == 91
    assert rows["purchase_date"]["user"] is None
    assert rows["model"]["ocr"] == "Samsung SM-A556E" and rows["model"]["item_index"] == 0

    assert sent["claimed_item_index"] == 0
    assert [i["claimed"] for i in sent["invoice_items"]] == [True, False]
    # only the IMEI-matched line's model went to the AI, not the pouch
    ai_fields = [x["field"] for x in llm.payloads["field_match"]["pairs"]]
    assert "items[0].model" in ai_fields and "items[1].model" not in ai_fields
    # TC is validated against the claimed line
    assert llm.payloads["tc_validation"]["claimed_item"]["model"] == "Samsung SM-A556E"
    # gift step gets the TC chunks (no second RAG call) and the claimed line
    assert llm.payloads["gift_assignment"]["tc_rules"] and llm.payloads["gift_assignment"]["claimed_item"]
    assert sent["eligible_gift"]["status"] == "assigned"
    assert sent["eligible_gift"]["gift"]["name"] == "R500 MTN voucher"


def test_rejected_claim_skips_gift_llm(settings, payload_dict):
    llm = FakeLLM()
    p, *_ = make(settings, [{"field_name": "imei", "num_of_claims": 1}], llm=llm)
    result = asyncio.run(p.run("t1", WebhookPayload.model_validate(payload_dict)))
    assert result.decision == "reject"
    assert result.eligible_gift.status == "not_eligible" and "gift_assignment" not in llm.steps


def test_choice_without_selection_moves_approve_to_review(settings, payload_dict):
    llm = FakeLLM()
    llm.gift_out = {"gift_type": "customer_choice",
                    "options": [{"name": "Samsung Galaxy Buds3"}, {"name": "R750 Takealot voucher"}]}
    p, *_ = make(settings, llm=llm)
    result = asyncio.run(p.run("t1", WebhookPayload.model_validate(payload_dict)))
    assert result.eligible_gift.status == "selection_required"
    assert result.decision == "review" and "gift selection_required" in result.flags


def test_choice_with_valid_selection_stays_approved(settings, payload_dict):
    payload_dict["user"]["selected_gift"] = "R750 Takealot voucher"
    llm = FakeLLM()
    llm.gift_out = {"gift_type": "customer_choice",
                    "options": [{"name": "Samsung Galaxy Buds3"}, {"name": "R750 Takealot voucher"}]}
    p, *_ = make(settings, llm=llm)
    result = asyncio.run(p.run("t1", WebhookPayload.model_validate(payload_dict)))
    assert result.decision == "approve"
    assert result.eligible_gift.status == "assigned" and result.eligible_gift.selection_valid


def test_device_not_on_invoice(settings, payload_dict):
    payload_dict["ocr"]["items"] = [{"imei_or_serial": None, "model": "mobile Pouch"}]
    llm = FakeLLM()
    p, _, results, _ = make(settings, llm=llm)

    async def low_scores(step, system, payload):          # LLM says the pouch is not the phone
        if step == "field_match":
            return {"results": [{"field": x["field"], "matching_score": 5, "reason": "different"}
                                for x in payload["pairs"]]}
        return await FakeLLM.chat_json(llm, step, system, payload)
    llm.chat_json = low_scores

    result = asyncio.run(p.run("t1", WebhookPayload.model_validate(payload_dict)))
    assert "customer's device not found on invoice" in result.flags
    assert result.claimed_item_index is None
    assert llm.payloads["tc_validation"]["claimed_item"] is None
    assert result.decision != "approve"


def test_history_is_called_even_if_llm_skips_tool(settings, payload_dict):
    p, history, results, _ = make(settings, [{"field_name": "imei", "num_of_claims": 1}],
                                  llm=FakeLLM(call_tool=False))
    result = asyncio.run(p.run("t1", WebhookPayload.model_validate(payload_dict)))
    assert len(history.bodies) == 1
    assert result.decision == "reject" and result.risk.hard_flag


def test_missing_tc_rules_means_review(settings, payload_dict):
    p, *_ = make(settings, rag=FakeRag(chunks=False))
    result = asyncio.run(p.run("t1", WebhookPayload.model_validate(payload_dict)))
    assert result.decision == "review"
    assert "no TC rules found for campaign" in result.flags


def test_failure_result_is_manual_review(settings, payload_dict):
    p, _, results, _ = make(settings)
    asyncio.run(p.submit_failure("t1", WebhookPayload.model_validate(payload_dict), "LLMError: x"))
    sent = results.submitted[0]
    assert sent["decision"] == "manual_review" and sent["error"] == "LLMError: x"
    assert len(sent["ocr_vs_user"]) > 0
    assert sent["claimed_item_index"] == 0                  # deterministic, no LLM needed
    assert sent["eligible_gift"]["status"] == "not_evaluated"
