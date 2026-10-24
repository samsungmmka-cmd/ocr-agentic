# Live run record

Run on 25 Oct 2026 in a Linux workspace: Redis 7, FastAPI API (port 8000), arq worker, and the mock services (port 9000) including the mock OpenAI-compatible LLM. Every call went through the real code path: webhook → Redis queue → worker → HTTP calls to RAG / History / Result / Status APIs and OpenAI-format chat completions with tool calling.

Payloads use the snake_case contract with `ocr.items[]` and the optional `user.selected_gift`.

> The mock LLM uses heuristics, so the *reasoning text* is canned. With a real model the same pipeline produces model-written reasoning.

## Summary

| Sample | Scenario | Claimed line | Decision | Score | Gift |
|---|---|---|---|---|---|
| `claim_valid.json` (123) | Phone + pouch on invoice; MTN Southafrica vs MTN Stores | 0 | **approve** | 96 | R500 Takealot voucher |
| `claim_two_phones.json` (126) | Two phones + charger; customer claims the S25 (line 1) | 1 | **approve** | 98 | Samsung Galaxy Buds3 |
| `claim_duplicate_imei.json` (124) | IMEI already claimed once before | 0 | **reject** | 20 | not_eligible |
| `claim_mismatch.json` (125) | Customer claims a Galaxy S25 Ultra; invoice is an iPhone 16 + case from iStore, before the promo window | none | **reject** | 54 | not_eligible |

Tests: `57 passed in 0.65s`

## 1. Two phones on one invoice (feedback 126)

Shows the item loop: the customer's IMEI matches line 1 exactly, so line 1 is claimed and only its model goes to the LLM. The other phone and the charger are scored but not sent to the AI.

### Webhook call and status timeline

```
POST /webhook/validate -> 202 {"task_id":"00905c6e-79fc-41e8-8656-5021e01d392f","status":"queued","duplicate":false}
GET /tasks/00905c6e… -> status=queued     step=received
GET /tasks/00905c6e… -> status=completed  step=done
Status timeline (pushed to Status API):
  21:49:00.487  queued     received
  21:49:00.671  processing field_match
  21:49:00.676  processing tc_validation
  21:49:00.681  processing history_check
  21:49:00.689  processing gift_assignment
  21:49:00.693  processing reasoning
  21:49:00.697  processing submitting
  21:49:00.700  completed  done
```

### Worker log (condensed)

```
21:49:00.671 INFO worker       pipeline started           
21:49:00.671 INFO app.tasks    task status                {"status": "processing", "step": "field_match"}
21:49:00.673 INFO app.pipeline field match                {"fields": 7, "invoice_items": 3, "ai_fields": 2}
21:49:00.676 INFO app.llm      llm call                   {"step": "field_match", "model": "gpt-4o-mini", "tokens": 0}
21:49:00.676 INFO app.pipeline claimed item               {"index": 1, "flags": []}
21:49:00.677 INFO app.tasks    task status                {"status": "processing", "step": "tc_validation"}
21:49:00.681 INFO app.llm      llm call                   {"step": "tc_validation", "model": "gpt-4o-mini", "tokens": 0}
21:49:00.682 INFO app.tasks    task status                {"status": "processing", "step": "history_check"}
21:49:00.685 INFO app.llm      llm call                   {"step": "history_check", "model": "gpt-4o-mini", "tokens": 0}
21:49:00.686 INFO app.tools    tool check_claim_history   {"records": [{"field_name": "imei", "num_of_claims": 0}, {"field_name": "id_number", "num_of_claims": 0}, {"field_name": "invoice_no", "num_of_claims": 0}]}
21:49:00.688 INFO app.llm      llm call                   {"step": "history_check", "model": "gpt-4o-mini", "tokens": 0}
21:49:00.688 INFO app.pipeline history agent finished     {"tool_calls": 1}
21:49:00.689 INFO app.tasks    task status                {"status": "processing", "step": "gift_assignment"}
21:49:00.692 INFO app.llm      llm call                   {"step": "gift_assignment", "model": "gpt-4o-mini", "tokens": 0}
21:49:00.692 INFO app.pipeline gift assignment            {"status": "assigned", "gift_type": "product_based", "gift": "Samsung Galaxy Buds3", "selection_valid": null}
21:49:00.693 INFO app.tasks    task status                {"status": "processing", "step": "reasoning"}
21:49:00.696 INFO app.llm      llm call                   {"step": "summary", "model": "gpt-4o-mini", "tokens": 0}
21:49:00.697 INFO app.tasks    task status                {"status": "processing", "step": "submitting"}
21:49:00.699 INFO app.tools    tool submit_result         {"response": {"status": "stored", "feedback_id": 126}}
21:49:00.700 INFO app.tasks    task status                {"status": "completed", "step": "done"}
21:49:00.702 INFO worker       pipeline completed         {"decision": "approve", "overall_score": 98}
```

### invoice_items

| line | model | imei_or_serial | imei_score | model_score | claimed |
|---|---|---|---|---|---|
| 0 | Samsung Galaxy A55 128GB | 351234567890123 | 40 | 47 |  |
| 1 | Samsung SM-S931B Galaxy S25 | 352222333444555 | 100 | 92 | **yes** |
| 2 | Samsung 25W Travel Adapter | CHG-25W-0091 | 16 | 22 |  |

### ocr_vs_user

| field | user | ocr | matching_score | method | item |
|---|---|---|---|---|---|
| name | Lerato Mokoena | Lerato Mokoena | 100 | exact |  |
| id_number | — | 9203030223085 | — | single_side |  |
| country | — | South Africa | — | single_side |  |
| invoice_no | — | TK-90031 | — | single_side |  |
| purchase_date | — | 2026-10-10 | — | single_side |  |
| store_name | Takealot.com | Takealot | 90 | ai |  |
| submitted_date | 2026-10-21 | — | — | single_side |  |
| imei_or_serial | 352222333444555 | 352222333444555 | 100 | exact | 1 |
| model | Galaxy S25 | Samsung SM-S931B Galaxy S25 | 92 | ai | 1 |

### Full result received by the Result API

```json
{
  "task_id": "00905c6e-79fc-41e8-8656-5021e01d392f",
  "feedback_id": 126,
  "campaign_id": 221,
  "tc_version": 3,
  "ocr_vs_user": [
    {
      "field": "name",
      "user": "Lerato Mokoena",
      "ocr": "Lerato Mokoena",
      "matching_score": 100,
      "method": "exact",
      "ai_reason": null,
      "item_index": null
    },
    {
      "field": "id_number",
      "user": null,
      "ocr": "9203030223085",
      "matching_score": null,
      "method": "single_side",
      "ai_reason": null,
      "item_index": null
    },
    {
      "field": "country",
      "user": null,
      "ocr": "South Africa",
      "matching_score": null,
      "method": "single_side",
      "ai_reason": null,
      "item_index": null
    },
    {
      "field": "invoice_no",
      "user": null,
      "ocr": "TK-90031",
      "matching_score": null,
      "method": "single_side",
      "ai_reason": null,
      "item_index": null
    },
    {
      "field": "purchase_date",
      "user": null,
      "ocr": "2026-10-10",
      "matching_score": null,
      "method": "single_side",
      "ai_reason": null,
      "item_index": null
    },
    {
      "field": "store_name",
      "user": "Takealot.com",
      "ocr": "Takealot",
      "matching_score": 90,
      "method": "ai",
      "ai_reason": "Same retailer brand 'TAKEALOT'; branch/region naming differs",
      "item_index": null
    },
    {
      "field": "submitted_date",
      "user": "2026-10-21",
      "ocr": null,
      "matching_score": null,
      "method": "single_side",
      "ai_reason": null,
      "item_index": null
    },
    {
      "field": "imei_or_serial",
      "user": "352222333444555",
      "ocr": "352222333444555",
      "matching_score": 100,
      "method": "exact",
      "ai_reason": null,
      "item_index": 1
    },
    {
      "field": "model",
      "user": "Galaxy S25",
      "ocr": "Samsung SM-S931B Galaxy S25",
      "matching_score": 92,
      "method": "ai",
      "ai_reason": "'Galaxy S25' and 'Samsung SM-S931B Galaxy S25' are the same product (galaxy s25)",
      "item_index": 1
    }
  ],
  "claimed_item_index": 1,
  "invoice_items": [
    {
      "index": 0,
      "imei_or_serial": "351234567890123",
      "model": "Samsung Galaxy A55 128GB",
      "imei_score": 40,
      "model_score": 47,
      "model_reason": null,
      "claimed": false
    },
    {
      "index": 1,
      "imei_or_serial": "352222333444555",
      "model": "Samsung SM-S931B Galaxy S25",
      "imei_score": 100,
      "model_score": 92,
      "model_reason": "'Galaxy S25' and 'Samsung SM-S931B Galaxy S25' are the same product (galaxy s25)",
      "claimed": true
    },
    {
      "index": 2,
      "imei_or_serial": "CHG-25W-0091",
      "model": "Samsung 25W Travel Adapter",
      "imei_score": 16,
      "model_score": 22,
      "model_reason": null,
      "claimed": false
    }
  ],
  "tc_validation": {
    "passed": true,
    "compliance_score": 100,
    "tc_version": 3,
    "checks": [
      {
        "rule": "Promotion period",
        "field": "purchase_date",
        "status": "pass",
        "detail": "Purchase date 2026-10-10 vs window 2026-09-01..2026-11-30"
      },
      {
        "rule": "Qualifying devices",
        "field": "items[1].model",
        "status": "pass",
        "detail": "Invoice line 1 model 'Samsung SM-S931B Galaxy S25' interpreted as 'galaxy s25'"
      },
      {
        "rule": "Participating retailers",
        "field": "store_name",
        "status": "pass",
        "detail": "'Takealot' belongs to retailer brand 'TAKEALOT'"
      },
      {
        "rule": "Eligibility",
        "field": "country",
        "status": "pass",
        "detail": "Country 'South Africa'"
      },
      {
        "rule": "Claim window",
        "field": "submitted_date",
        "status": "pass",
        "detail": "Submitted 2026-10-21, 11 days after purchase"
      },
      {
        "rule": "Proof of purchase",
        "field": null,
        "status": "pass",
        "detail": "All required invoice fields present"
      }
    ],
    "issues": []
  },
  "risk": {
    "level": "low",
    "hard_flag": false,
    "flags": [],
    "history": [
      {
        "field_name": "imei",
        "num_of_claims": 0
      },
      {
        "field_name": "id_number",
        "num_of_claims": 0
      },
      {
        "field_name": "invoice_no",
        "num_of_claims": 0
      }
    ],
    "history_score": 100,
    "explanation": "No previous claims for this device, invoice or ID number."
  },
  "eligible_gift": {
    "status": "assigned",
    "gift_type": "product_based",
    "gift": {
      "name": "Samsung Galaxy Buds3",
      "value": null,
      "type": "gift"
    },
    "selected_gift": null,
    "selection_valid": null,
    "options": [
      {
        "name": "Samsung Galaxy Buds3",
        "value": null,
        "type": "gift"
      }
    ],
    "tc_reference": "Gifts",
    "reason": "TC gift table assigns 'Samsung Galaxy Buds3' to galaxy s25."
  },
  "ai_summary": "Claim 126 is recommended for 'approve' with an overall score of 98. The invoice lists 3 item(s); the claimed product is line 1 (Samsung SM-S931B Galaxy S25, 352222333444555). Fields matched by reasoning: store_name ('Takealot.com' vs 'Takealot': Same retailer brand 'TAKEALOT'; branch/region naming differs); model ('Galaxy S25' vs 'Samsung SM-S931B Galaxy S25': 'Galaxy S25' and 'Samsung SM-S931B Galaxy S25' are the same product (galaxy s25)). All verifiable TC rules passed. History risk is low. No previous claims for this device, invoice or ID number. Gift (assigned): Samsung Galaxy Buds3. TC gift table assigns 'Samsung Galaxy Buds3' to galaxy s25.",
  "overall_score": 98,
  "decision": "approve",
  "flags": [],
  "processed_at": "2026-10-24T21:49:00.696682Z",
  "error": null
}
```

## 2. Phone + pouch (feedback 123)

| line | model | imei_or_serial | imei_score | model_score | claimed |
|---|---|---|---|---|---|
| 0 | Samsung SM-A556E/DS | 356789104512345 | 100 | 92 | **yes** |
| 1 | mobile Pouch | 123 | 33 | 18 |  |

| field | user | ocr | matching_score | method | item |
|---|---|---|---|---|---|
| name | John Smith | JOHN M SMITH | 91 | fuzzy |  |
| id_number | — | 9001015009087 | — | single_side |  |
| gender | — | M | — | single_side |  |
| dob | — | 1990-01-01 | — | single_side |  |
| country | — | South Africa | — | single_side |  |
| invoice_no | — | INV-2026-77812 | — | single_side |  |
| purchase_date | — | 2026-10-02 | — | single_side |  |
| store_name | MTN Stores | MTN Southafrica | 90 | ai |  |
| store_address | — | Shop 12, Sandton City, Johannesburg | — | single_side |  |
| submitted_date | 2026-10-20 | — | — | single_side |  |
| imei_or_serial | 356789104512345 | 356789104512345 | 100 | exact | 0 |
| model | Galaxy A55 | Samsung SM-A556E/DS | 92 | ai | 0 |

**Decision:** approve · **Score:** 96 · **TC passed:** True (compliance 100) · **Risk:** low

**Flags:**

- none

**ai_summary:** Claim 123 is recommended for 'approve' with an overall score of 96. The invoice lists 2 item(s); the claimed product is line 0 (Samsung SM-A556E/DS, 356789104512345). Fields matched by reasoning: store_name ('MTN Stores' vs 'MTN Southafrica': Same retailer brand 'MTN'; branch/region naming differs); model ('Galaxy A55' vs 'Samsung SM-A556E/DS': 'Galaxy A55' and 'Samsung SM-A556E/DS' are the same product (galaxy a55)). All verifiable TC rules passed. History risk is low. No previous claims for this device, invoice or ID number.

## 3. Duplicate IMEI (feedback 124)

| line | model | imei_or_serial | imei_score | model_score | claimed |
|---|---|---|---|---|---|
| 0 | Galaxy S25 | 356789104599999 | 100 | 92 | **yes** |
| 1 | Screen protector | — | — | 18 |  |

| field | user | ocr | matching_score | method | item |
|---|---|---|---|---|---|
| name | Thabo Nkosi | Thabo Nkosi | 100 | exact |  |
| id_number | — | 8805125800081 | — | single_side |  |
| country | — | South Africa | — | single_side |  |
| invoice_no | — | VS-55120 | — | single_side |  |
| purchase_date | — | 2026-09-18 | — | single_side |  |
| store_name | Vodacom | Vodacom Shop Menlyn | 90 | ai |  |
| submitted_date | 2026-10-01 | — | — | single_side |  |
| imei_or_serial | 356789104599999 | 356789104599999 | 100 | exact | 0 |
| model | Samsung Galaxy S25 | Galaxy S25 | 92 | ai | 0 |

**Decision:** reject · **Score:** 20 · **TC passed:** True (compliance 100) · **Risk:** high (hard flag)

**Flags:**

- hard history flag
- score below 50

**ai_summary:** Claim 124 is recommended for 'reject' with an overall score of 20. The invoice lists 2 item(s); the claimed product is line 0 (Galaxy S25, 356789104599999). Fields matched by reasoning: store_name ('Vodacom' vs 'Vodacom Shop Menlyn': Same retailer brand 'VODACOM'; branch/region naming differs); model ('Samsung Galaxy S25' vs 'Galaxy S25': 'Samsung Galaxy S25' and 'Galaxy S25' are the same product (galaxy s25)). All verifiable TC rules passed. History risk is high. Previous claims found: imei has 1 previous claim(s). Device or invoice reuse is a strong fraud signal.

## 4. Device not on invoice (feedback 125)

| line | model | imei_or_serial | imei_score | model_score | claimed |
|---|---|---|---|---|---|
| 0 | iPhone 16 | 352099001761481 | 93 | 10 |  |
| 1 | iPhone 16 Silicone Case | — | — | 10 |  |

| field | user | ocr | matching_score | method | item |
|---|---|---|---|---|---|
| name | Sara Jacobs | Sarah Jacobs | 96 | fuzzy |  |
| id_number | — | 8501015800088 | — | single_side |  |
| country | — | South Africa | — | single_side |  |
| invoice_no | — | INC-88001 | — | single_side |  |
| purchase_date | — | 2026-08-20 | — | single_side |  |
| store_name | MTN Stores | iStore Canal Walk | 51 | ai |  |
| submitted_date | 2026-10-22 | — | — | single_side |  |
| imei_or_serial | 352099001761431 | — | — | single_side |  |
| model | Galaxy S25 Ultra | — | — | single_side |  |

**Decision:** reject · **Score:** 54 · **TC passed:** False (compliance 17) · **Risk:** medium

**Flags:**

- low OCR confidence (0.55)
- customer's device not found on invoice
- TC high-severity failure: Promotion period, Qualifying devices, Participating retailers
- medium history risk

**ai_summary:** Claim 125 is recommended for 'reject' with an overall score of 54. The invoice lists 2 item(s); the customer's device was not found on it. Fields matched by reasoning: store_name ('MTN Stores' vs 'iStore Canal Walk': Different retailer names). Mismatched fields: store_name (51). TC issues: Purchase date 2026-08-20 vs window 2026-09-01..2026-11-30; Claimed device not found on the invoice; 'iStore Canal Walk' belongs to retailer brand 'ISTORE'; Submitted 2026-10-22, 63 days after purchase; Missing: imei_or_serial of the claimed item. History risk is medium. Previous claims found: id_number has 2 previous claim(s). Same person claiming again; check against the one-claim-per-ID rule.

## Gift assignment

Three mock campaigns, one per TC gift rule: **221** product → gift table, **222** fixed R500 voucher, **223** customer chooses one gift.

| Sample | Campaign (TC gift rule) | selected_gift | Decision | eligible_gift.status | Gift | selection_valid | Expected |
|---|---|---|---|---|---|---|---|
| `choice_invalid` (323) | 223 (customer chooses) | Apple AirPods | review | selection_invalid | — | False | not a TC option → review ✓ |
| `choice_missing` (324) | 223 (customer chooses) | — | review | selection_required | — | — | choice campaign, no selection → review ✓ |
| `choice_short_name` (322) | 223 (customer chooses) | buds3 | approve | assigned | Samsung Galaxy Buds3 | True | 'buds3' matched to Samsung Galaxy Buds3 ✓ |
| `choice_valid` (321) | 223 (customer chooses) | R750 Takealot voucher | approve | assigned | R750 Takealot voucher | True | exact option chosen ✓ |
| `fixed_no_selection` (301) | 222 (fixed R500 voucher) | — | approve | assigned | R500 MTN voucher | — | TC gift, nothing to validate ✓ |
| `fixed_other_selection` (302) | 222 (fixed R500 voucher) | Samsung Galaxy Watch7 | approve | assigned | R500 MTN voucher | False | TC gift kept, selection flagged invalid ✓ |
| `fixed_review_provisional` (303) | 222 (fixed R500 voucher) | — | review | provisional | R500 MTN voucher | — | low OCR confidence → review, gift provisional ✓ |
| `product_table_a55` (311) | 221 (product → gift table) | — | approve | assigned | R500 Takealot voucher | — | A55 → R500 Takealot voucher ✓ |
| `product_table_s25_selected` (312) | 221 (product → gift table) | Buds3 | approve | assigned | Samsung Galaxy Buds3 | True | S25 → Buds3, short selection 'Buds3' validated ✓ |
| `claim_duplicate_imei` (124) | 221 (product → gift table) | — | reject | not_eligible | — | — | rejected claim → no gift, no LLM call ✓ |
| `claim_mismatch` (125) | 221 (product → gift table) | — | reject | not_eligible | — | — | rejected claim → no gift, no LLM call ✓ |

### Status timeline (new `gift_assignment` step)

```
  21:49:01.997  queued     received
  21:49:02.176  processing field_match
  21:49:02.181  processing tc_validation
  21:49:02.186  processing history_check
  21:49:02.193  processing gift_assignment
  21:49:02.196  processing reasoning
  21:49:02.200  processing submitting
  21:49:02.203  completed  done
```

### eligible_gift: invalid customer choice (feedback 323)

```json
{
  "status": "selection_invalid",
  "gift_type": "customer_choice",
  "gift": null,
  "selected_gift": "Apple AirPods",
  "selection_valid": false,
  "options": [
    {
      "name": "Samsung Galaxy Buds3",
      "value": null,
      "type": "gift"
    },
    {
      "name": "R750 Takealot voucher",
      "value": "R750",
      "type": "voucher"
    },
    {
      "name": "Samsung 25W Travel Adapter",
      "value": null,
      "type": "gift"
    }
  ],
  "tc_reference": "Gifts",
  "reason": "'Apple AirPods' is not one of the TC gift options (Samsung Galaxy Buds3, R750 Takealot voucher, Samsung 25W Travel Adapter)."
}
```

Flags: `['gift selection_invalid']` → decision `review`

### eligible_gift: product table with a validated short selection (feedback 312)

```json
{
  "status": "assigned",
  "gift_type": "product_based",
  "gift": {
    "name": "Samsung Galaxy Buds3",
    "value": null,
    "type": "gift"
  },
  "selected_gift": "Buds3",
  "selection_valid": true,
  "options": [
    {
      "name": "Samsung Galaxy Buds3",
      "value": null,
      "type": "gift"
    }
  ],
  "tc_reference": "Gifts",
  "reason": "TC gift table assigns 'Samsung Galaxy Buds3' to galaxy s25."
}
```

**ai_summary:** Claim 312 is recommended for 'approve' with an overall score of 98. The invoice lists 3 item(s); the claimed product is line 1 (Samsung SM-S931B Galaxy S25, 352222333444555). Fields matched by reasoning: store_name ('Takealot.com' vs 'Takealot': Same retailer brand 'TAKEALOT'; branch/region naming differs); model ('Galaxy S25' vs 'Samsung SM-S931B Galaxy S25': 'Galaxy S25' and 'Samsung SM-S931B Galaxy S25' are the same product (galaxy s25)). All verifiable TC rules passed. History risk is low. No previous claims for this device, invoice or ID number. Gift (assigned): Samsung Galaxy Buds3. TC gift table assigns 'Samsung Galaxy Buds3' to galaxy s25.

## Edge cases

```
### 1. Duplicate webhook (same feedback_id sent twice while the first is still running)
{"task_id":"1af77414-5465-449e-9921-a7f76a6685a0","status":"queued","duplicate":false}
{"task_id":"1af77414-5465-449e-9921-a7f76a6685a0","status":"queued","duplicate":true}

### 2. Wrong webhook secret
HTTP 401

### 3. Keys not in snake_case (old format)
{"detail":[{"type":"extra_forbidden","loc":["body","ocr","purchase date"],"msg":"Extra inputs are not permitted","input":"2026-10-02"},{"type":"extra_forbidden","loc":["body","user","store name"],"msg":"Extra inputs are not permitted","input":"MTN"}]}
HTTP 422

### 4. Campaign 999 has no TC rules in the RAG API
POST /webhook/validate -> 202 {"task_id":"01435f14-908e-47bb-905b-f7130d40ec6b","status":"queued","duplicate":false}
  "overall_score": 95,
  "decision": "review",
    "no TC rules found for campaign",

### 5. Invoice with no items
POST /webhook/validate -> 202 {"task_id":"c98ed20b-efee-4725-97bc-697125591040","status":"queued","duplicate":false}
  "claimed_item_index": null,
  "overall_score": 85,
  "decision": "reject",
    "no items found on invoice",
```
