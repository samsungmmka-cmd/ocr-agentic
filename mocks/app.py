"""Mock external services for local development and demos.

    RAG API      GET  /tc/{campaign_id}/rules, POST /tc/search
    History API  POST /history/check
    Result API   POST /results (Bearer), GET /results, GET /results/{feedback_id}
    Status API   POST /status, GET /status/{task_id}
    Mock LLM     POST /v1/chat/completions  (OpenAI-compatible, tool calling)

Replace each with the real service by changing URLs in .env.
The mock LLM uses simple heuristics so the whole pipeline can run without a
real model. It is NOT meant to be accurate.

Run:  uvicorn mocks.app:app --port 9000
"""
import json
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import date, datetime

from fastapi import FastAPI, Header, HTTPException, Request
from rapidfuzz import fuzz

app = FastAPI(title="Mock services")

RESULT_TOKEN = os.getenv("MOCK_RESULT_TOKEN", os.getenv("RESULT_API_TOKEN", "change-me-result-token"))

# --------------------------------------------------------------- RAG API
_BASE_RULES = [
    {"section": "Promotion period",
     "text": "The promotion is valid for qualifying purchases made between 1 September 2026 and 30 November 2026 (inclusive)."},
    {"section": "Qualifying devices",
     "text": "Qualifying devices are the Samsung Galaxy A55, Galaxy S25 and Galaxy S25 Ultra only."},
    {"section": "Participating retailers",
     "text": "Purchases must be made at MTN Stores, Vodacom Shops or Takealot. Purchases from other retailers do not qualify."},
    {"section": "Eligibility",
     "text": "Open to South African residents with a valid South African ID number."},
    {"section": "Claim window",
     "text": "Claims must be submitted within 30 days of the purchase date."},
    {"section": "Limits",
     "text": "One claim per IMEI/serial number and one claim per ID number per campaign."},
    {"section": "Proof of purchase",
     "text": "The invoice must show the invoice number, store name, purchase date and device IMEI or serial number."},
]

# One campaign per gift scenario
GIFT_RULES = {
    # gift depends on the product (table)
    221: "Each valid claim receives the gift listed for the purchased device:\n"
         "| Device | Gift |\n|---|---|\n"
         "| Galaxy A55 | R500 Takealot voucher |\n"
         "| Galaxy S25 | Samsung Galaxy Buds3 |\n"
         "| Galaxy S25 Ultra | Samsung Galaxy Watch7 |",
    # same gift for every valid claim
    222: "Campaign 222 is eligible for an R500 voucher: every successful claim receives an R500 MTN voucher.",
    # customer chooses
    223: "When submitting the claim, the customer may choose one of the following gifts: "
         "Samsung Galaxy Buds3, R750 Takealot voucher, Samsung 25W Travel Adapter.",
}


def _campaign(cid: int) -> dict:
    chunks = [{"id": f"{cid}-{i + 1}", **c} for i, c in enumerate(_BASE_RULES)]
    chunks.append({"id": f"{cid}-gift", "section": "Gifts", "text": GIFT_RULES[cid]})
    return {"tc_version": 3, "chunks": chunks}


TC_DB = {cid: _campaign(cid) for cid in GIFT_RULES}


@app.get("/tc/{campaign_id}/rules")
async def tc_rules(campaign_id: int):
    tc = TC_DB.get(campaign_id, {"tc_version": None, "chunks": []})
    return {"campaign_id": campaign_id, **tc}


@app.post("/tc/search")
async def tc_search(body: dict):
    tc = TC_DB.get(int(body.get("campaign_id", 0)), {"tc_version": None, "chunks": []})
    q = str(body.get("query", ""))
    ranked = sorted(tc["chunks"], key=lambda c: fuzz.token_set_ratio(q, c["text"]), reverse=True)
    return {"campaign_id": body.get("campaign_id"), "tc_version": tc["tc_version"],
            "chunks": ranked[: int(body.get("top_k", 5))]}


# ----------------------------------------------------------- History API
PRIOR_CLAIMS = {
    "imei": {"356789104599999": 1},          # already claimed device
    "id_number": {"8501015800088": 2},       # same person, several claims
    "invoice_no": {"INV-DUP-0001": 1},
}


@app.post("/history/check")
async def history_check(body: dict):
    out = []
    for field in ("imei", "id_number", "invoice_no"):
        value = body.get(field)
        if value is None:
            continue
        out.append({"field_name": field,
                    "num_of_claims": PRIOR_CLAIMS[field].get(str(value).replace(" ", ""), 0)})
    return out


# ------------------------------------------------------------ Result API
RESULTS: dict[int, list[dict]] = defaultdict(list)


@app.post("/results", status_code=201)
async def submit_result(request: Request, authorization: str = Header(default=""),
                        idempotency_key: str = Header(default="")):
    if authorization != f"Bearer {RESULT_TOKEN}":
        raise HTTPException(401, "invalid token")
    body = await request.json()
    for existing in RESULTS[body["feedback_id"]]:
        if existing["_idempotency_key"] == idempotency_key:
            return {"status": "duplicate_ignored", "feedback_id": body["feedback_id"]}
    RESULTS[body["feedback_id"]].append({**body, "_idempotency_key": idempotency_key,
                                         "_received_at": datetime.utcnow().isoformat()})
    return {"status": "stored", "feedback_id": body["feedback_id"]}


@app.get("/results")
async def list_results():
    return {fid: items[-1] for fid, items in RESULTS.items()}


@app.get("/results/{feedback_id}")
async def get_result(feedback_id: int):
    if feedback_id not in RESULTS:
        raise HTTPException(404, "no result")
    return RESULTS[feedback_id][-1]


# ------------------------------------------------------------ Status API
STATUS_EVENTS: dict[str, list[dict]] = defaultdict(list)


@app.post("/status")
async def status_push(body: dict):
    STATUS_EVENTS[body["task_id"]].append(body)
    return {"ok": True}


@app.get("/status/{task_id}")
async def status_events(task_id: str):
    return STATUS_EVENTS.get(task_id, [])


# -------------------------------------------------------------- Mock LLM
ELIGIBLE_RETAILERS = {"mtn", "vodacom", "takealot"}
MODEL_CODES = {"sm a556": "galaxy a55", "sm s931": "galaxy s25", "sm s938": "galaxy s25 ultra"}
ELIGIBLE_MODELS = {"galaxy a55", "galaxy s25", "galaxy s25 ultra"}


def _norm(v) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(v or "").lower())).strip()


def _model_name(v) -> str:
    n = _norm(v)
    for code, name in MODEL_CODES.items():
        if code in n:
            return name
    n = n.replace("samsung ", "")
    for name in sorted(ELIGIBLE_MODELS, key=len, reverse=True):
        if name in n:
            return name
    return n


def _date(v):
    try:
        return datetime.fromisoformat(str(v)).date()
    except (TypeError, ValueError):
        return None


def _mock_tc(data: dict) -> dict:
    ocr, user = data.get("ocr_data", {}), data.get("customer_data", {})
    item = data.get("claimed_item")  # invoice line being claimed; None = not on invoice
    checks, issues = [], []

    def add(rule, field, ok, detail, severity="high"):
        status = "not_verifiable" if ok is None else ("pass" if ok else "fail")
        checks.append({"rule": rule, "field": field, "status": status, "detail": detail})
        if ok is False:
            issues.append({"rule": rule, "field": field, "severity": severity, "detail": detail})

    pd = _date(ocr.get("purchase_date"))
    add("Promotion period", "purchase_date",
        None if not pd else date(2026, 9, 1) <= pd <= date(2026, 11, 30),
        f"Purchase date {pd} vs window 2026-09-01..2026-11-30")

    if item is None:
        add("Qualifying devices", "items", False, "Claimed device not found on the invoice")
    else:
        m = _model_name(item.get("model"))
        add("Qualifying devices", f"items[{item['index']}].model",
            None if not m else m in ELIGIBLE_MODELS,
            f"Invoice line {item['index']} model '{item.get('model')}' interpreted as '{m}'")

    store = _norm(ocr.get("store_name"))
    brand = store.split(" ")[0] if store else ""
    add("Participating retailers", "store_name", None if not store else brand in ELIGIBLE_RETAILERS,
        f"'{ocr.get('store_name')}' belongs to retailer brand '{brand.upper()}'")

    country = _norm(ocr.get("country"))
    add("Eligibility", "country", None if not country else country in {"south africa", "za", "rsa"},
        f"Country '{ocr.get('country')}'", "medium")

    sd = _date(user.get("submitted_date"))
    add("Claim window", "submitted_date", None if not (pd and sd) else 0 <= (sd - pd).days <= 30,
        f"Submitted {sd}, {(sd - pd).days if pd and sd else '?'} days after purchase", "medium")

    missing = [f for f in ("invoice_no", "store_name", "purchase_date") if not ocr.get(f)]
    if not (item or {}).get("imei_or_serial"):
        missing.append("imei_or_serial of the claimed item")
    add("Proof of purchase", None, not missing,
        "All required invoice fields present" if not missing else f"Missing: {', '.join(missing)}", "medium")

    verifiable = [c for c in checks if c["status"] != "not_verifiable"]
    passed_n = sum(c["status"] == "pass" for c in verifiable)
    score = round(100 * passed_n / len(verifiable)) if verifiable else 0
    return {"passed": not issues, "compliance_score": score, "checks": checks, "issues": issues}


def _mock_field_match(data: dict) -> dict:
    out = []
    for p in data.get("pairs", []):
        f, u, o = p["field"], p.get("customer_value"), p.get("ocr_value")
        if f == "model" or f.endswith(".model"):   # "model" or "items[1].model"
            same = _model_name(u) == _model_name(o)
            out.append({"field": f, "matching_score": 92 if same else 10,
                        "reason": f"'{u}' and '{o}' {'are' if same else 'are not'} the same product"
                                  + (f" ({_model_name(o)})" if same else "")})
        elif f in ("store_name", "store_address"):
            bu, bo = _norm(u).split(" ")[0], _norm(o).split(" ")[0]
            if bu and bu == bo:
                out.append({"field": f, "matching_score": 90,
                            "reason": f"Same retailer brand '{bu.upper()}'; branch/region naming differs"})
            else:
                out.append({"field": f, "matching_score": fuzz.token_set_ratio(_norm(u), _norm(o)),
                            "reason": "Different retailer names"})
        else:
            s = fuzz.token_set_ratio(_norm(u), _norm(o))
            out.append({"field": f, "matching_score": s, "reason": "Text similarity after normalisation"})
    return {"results": out}


def _mock_history(messages: list[dict]) -> dict:
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if not tool_msgs:
        claim = json.loads(messages[-1]["content"]).get("claim", {})
        args = {k: claim.get(k) for k in ("campaign_id", "imei_or_serial", "id_number", "invoice_no")}
        return {"tool_call": {"name": "check_claim_history", "arguments": args}}
    records = json.loads(tool_msgs[-1]["content"])
    flags, level = [], "low"
    for r in records:
        n = r.get("num_of_claims", 0)
        if n > 0:
            flags.append(f"{r['field_name']} has {n} previous claim(s)")
            lvl = "high" if r["field_name"] in ("imei", "invoice_no") else "medium"
            level = "high" if "high" in (level, lvl) else lvl
    expl = ("No previous claims for this device, invoice or ID number." if not flags else
            "Previous claims found: " + "; ".join(flags) + ". " +
            ("Device or invoice reuse is a strong fraud signal." if level == "high"
             else "Same person claiming again; check against the one-claim-per-ID rule."))
    return {"risk_level": level, "flags": flags, "explanation": expl}


def _mock_summary(data: dict) -> dict:
    rows = data.get("ocr_vs_user", [])
    mism = [r for r in rows if r.get("matching_score") is not None and r["matching_score"] < 90]
    ai_rows = [r for r in rows if r.get("method") == "ai"]
    tc, risk = data.get("tc_validation", {}), data.get("risk", {})
    items = data.get("invoice_items", [])
    claimed = next((i for i in items if i.get("claimed")), None)
    parts = [f"Claim {data.get('feedback_id')} is recommended for '{data.get('decision')}' "
             f"with an overall score of {data.get('overall_score')}."]
    if items:
        parts.append(f"The invoice lists {len(items)} item(s); " + (
            f"the claimed product is line {claimed['index']} ({claimed['model']}, {claimed['imei_or_serial']})."
            if claimed else "the customer's device was not found on it."))
    if ai_rows:
        parts.append("Fields matched by reasoning: " + "; ".join(
            f"{r['field']} ('{r['user']}' vs '{r['ocr']}': {r.get('ai_reason')})" for r in ai_rows) + ".")
    if mism:
        parts.append("Mismatched fields: " + ", ".join(f"{r['field']} ({r['matching_score']})" for r in mism) + ".")
    parts.append("All verifiable TC rules passed." if tc.get("passed") else
                 "TC issues: " + "; ".join(i["detail"] for i in tc.get("issues", [])) + ".")
    parts.append(f"History risk is {risk.get('level')}. {risk.get('explanation', '')}".strip())
    gift = data.get("eligible_gift") or {}
    if gift.get("gift"):
        parts.append(f"Gift ({gift.get('status')}): {gift['gift']['name']}. {gift.get('reason', '')}".strip())
    elif gift:
        parts.append(f"No gift assigned ({gift.get('status')}): {gift.get('reason', '')}".strip())
    return {"ai_summary": " ".join(parts)}


def _gift_obj(name: str) -> dict:
    name = name.strip().rstrip(".")
    value = re.search(r"R\d[\d,]*", name)
    return {"name": name, "value": value.group(0) if value else None,
            "type": "voucher" if "voucher" in name.lower() else "gift"}


def _mock_gift(data: dict) -> dict:
    chunk = next((c for c in data.get("tc_rules", []) if (c.get("section") or "").lower() == "gifts"), None)
    if chunk is None:
        return {"gift_type": "none", "options": [], "eligible_gift": None,
                "selected_gift_match": None, "tc_reference": None, "reason": "The TC defines no gift."}
    text = chunk["text"]
    item = data.get("claimed_item") or {}
    model = _model_name(item.get("model") or data.get("customer_model"))

    if "|" in text:                                              # product table
        table = {}
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0].lower() not in ("device", "") and not set(cells[0]) <= set("-"):
                table[_model_name(cells[0])] = cells[1]
        gift = table.get(model)
        gift_type, options = "product_based", ([_gift_obj(gift)] if gift else [])
        eligible = options[0] if options else None
        reason = (f"TC gift table assigns '{gift}' to {model}." if gift
                  else f"'{model}' is not in the TC gift table.")
    elif "choose" in text.lower():                               # customer choice
        names = text.split(":", 1)[1].split(",")
        gift_type, options, eligible = "customer_choice", [_gift_obj(n) for n in names], None
        reason = "The TC lets the customer choose one gift from the list."
    else:                                                        # fixed gift
        m = re.search(r"receives an? (.+?)\.?$", text)
        gift_type, options = "fixed", [_gift_obj(m.group(1) if m else text)]
        eligible = options[0]
        reason = f"Every successful claim receives {options[0]['name']}."

    match = None
    sel = _norm(data.get("selected_gift"))
    if sel:
        hit = next((o for o in options if sel in _norm(o["name"]) or _norm(o["name"]) in sel), None)
        match = {"matched": hit is not None, "option": hit["name"] if hit else None,
                 "reason": "Selection refers to a TC option." if hit else "Selection is not a TC option."}
    return {"gift_type": gift_type, "options": options, "eligible_gift": eligible,
            "selected_gift_match": match, "tc_reference": chunk["section"], "reason": reason}


def _completion(message: dict, finish: str) -> dict:
    return {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": "mock-llm",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: dict, x_agent_step: str = Header(default="")):
    messages = body.get("messages", [])
    data = {}
    try:
        data = json.loads(messages[1]["content"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        pass

    if x_agent_step == "history_check":
        out = _mock_history(messages)
        if "tool_call" in out:
            tc = out["tool_call"]
            return _completion({"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}]},
                "tool_calls")
    elif x_agent_step == "tc_validation":
        out = _mock_tc(data)
    elif x_agent_step == "field_match":
        out = _mock_field_match(data)
    elif x_agent_step == "gift_assignment":
        out = _mock_gift(data)
    elif x_agent_step == "summary":
        out = _mock_summary(data)
    else:
        out = {"error": f"mock llm: unknown step '{x_agent_step}'"}
    return _completion({"role": "assistant", "content": json.dumps(out)}, "stop")


@app.get("/health")
async def health():
    return {"status": "ok"}
