# Document Validation Agent

AI agent that validates promotion / warranty claims. An OCR service calls the
webhook with the OCR result and the customer's submitted data; the agent checks
them against the campaign Terms & Conditions, against each other, and against
claim history, then posts a structured result to the Result API.

```
OCR Service ──POST /webhook/validate──► api ──enqueue──► redis ◄──pull── worker
                      │                                                   │
               202 + task_id               1. user vs OCR   (fuzzy + LLM, loop invoice items)
                                           2. TC rules      ──► RAG API
                                           3. history tool  ──► History API
                                           4. risk rules + score + decision (code)
                                           5. gift / voucher from TC (LLM + code checks)
                                           6. LLM summary
                                           5. submit tool   ──► Result API (Bearer)
                                           + status push    ──► Status API (optional)
```

## How it works

| Step | What happens | Who decides |
|---|---|---|
| Field match | Invoice-level fields from OCR **and** user are listed. Then the agent **loops over the invoice items** to find the line the customer is claiming (see below). Exact / fuzzy matches are accepted in code; unclear semantic fields (store name, model…) go to the LLM in **one** batched call. Identifiers (IMEI, ID, invoice) are compared strictly, never "reasoned" | Code, LLM for unclear fields |
| TC validation | Fetches **all** TC chunks for the campaign from the RAG API, LLM checks the OCR data against them. Device rules are checked against the **claimed item** only, so accessories on the invoice are ignored | LLM |
| History | LLM agent calls the `check_claim_history` tool (OpenAI function calling). If it skips the tool, the pipeline calls it anyway | Rules + LLM |
| Risk | Rules from `RISK_RULES` (e.g. IMEI reuse = high = hard flag). LLM can **raise** risk, never lower it | Rules |
| Score & decision | Weighted score (fields / TC / history) + decision rules, all configurable | Code |
| Gift assignment | For approved / in-review claims: LLM reads the TC gift rule, code checks the result and assigns `eligible_gift` (see below) | LLM + rules |
| Summary | LLM writes `ai_summary` for the reviewer | LLM |
| Submit | `submit_result` tool posts to the Result API with `Idempotency-Key` | Code |

Field match runs before TC validation because device rules (qualifying model, IMEI on
the invoice) can only be checked once the agent knows which invoice line is being claimed.

**How the claimed item is found** (invoice with several lines, e.g. phone + pouch + charger)
1. Every line is scored against the customer's `imei_or_serial` (strict) and `model`.
2. A line with the customer's **exact IMEI/serial** is the claimed item. Only that line's model goes to the LLM.
3. No exact IMEI: every line's model goes to the LLM (one batched call). The best line whose model
   score ≥ `ITEM_MODEL_MIN_SCORE` is the claimed item, and the claim is flagged
   `customer imei_or_serial not found on invoice`.
4. No line matches: `claimed_item_index` is `null`, the claim is flagged
   `customer's device not found on invoice`, and the TC device rule fails.

Other flags: `no items found on invoice`, `customer imei_or_serial appears on N invoice lines`,
`customer model does not match invoice line N`. Any flag blocks auto-approve.
History is checked with the claimed line's IMEI.

**How the gift is assigned**

The step runs only for claims that are `approve` or `review`; `reject` / `manual_review` get
`status: not_eligible` with no LLM call. The TC chunks fetched in the TC step are reused.
The LLM classifies the TC gift rule and lists the gift options that apply; the code then decides:

| TC gift rule | `selected_gift` empty | `selected_gift` sent |
|---|---|---|
| **fixed** ("every successful claim receives an R500 voucher") | the fixed gift | the fixed gift; `selection_valid` true/false (a different selection does not change the TC gift) |
| **product_based** (table: product → gift) | the gift for the claimed item's model | the table gift; `selection_valid` true/false |
| **customer_choice** (customer picks from a list) | `selection_required`, options listed | the matching option (`assigned`), or `selection_invalid` |
| product not in table / no gift rule | `not_found` | `not_found` |

- The gift must be one of the options the TC lists, so a hallucinated gift is rejected (`not_found`).
- Short or informal selections ("Buds3") are matched by fuzzy match first, then by the LLM, but only onto a TC option.
- A `review` claim gets `status: provisional`: the gift applies if a reviewer approves it.
- An `approve` claim whose gift is `selection_required`, `selection_invalid` or `not_found` moves to `review`
  (flag `gift <status>`), because the gift cannot be issued automatically.

Scores and decisions are computed in code so they are repeatable and auditable.
The LLM does the reasoning: "MTN Southafrica" vs "MTN Stores", "SM-A556E" vs "Galaxy A55".

**Decision rules**
- `reject`: hard history flag (score capped at `HARD_FLAG_SCORE_CAP`), high-severity TC failure (`TC_HIGH_ISSUE_DECISION`), or score < `REJECT_THRESHOLD`
- `approve`: score ≥ `APPROVE_THRESHOLD` and nothing flagged
- `review`: anything in between (low OCR confidence, IMEI mismatch, medium risk, missing TC rules…)
- `manual_review`: a step failed after retries. The result is still posted, so no claim is lost.

## Run with Docker

```bash
cp .env.example .env          # defaults use the mock services + mock LLM
docker compose up --build
```

Use a real model: set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` in `.env`.
Any OpenAI-compatible endpoint works (OpenAI, Azure OpenAI, vLLM, Ollama, OpenRouter…).
If the provider does not support `response_format`, set `LLM_JSON_MODE=false`.

## Run locally (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
sed -i 's#redis://redis#redis://localhost#; s#http://mocks:9000#http://localhost:9000#g' .env

redis-server &
uvicorn mocks.app:app --port 9000 &
uvicorn app.main:app --port 8000 &
arq app.worker.WorkerSettings
```

## Try it

```bash
python scripts/send_sample.py samples/claim_valid.json            # approve (phone + pouch on invoice)
python scripts/send_sample.py samples/claim_two_phones.json       # approve (claims line 1 of 3)
python scripts/send_sample.py samples/claim_duplicate_imei.json   # reject (IMEI already claimed)
python scripts/send_sample.py samples/claim_mismatch.json         # reject (device not on invoice, wrong store / period)
python scripts/send_sample.py samples/gifts/*.json                # every gift scenario (campaigns 221 / 222 / 223)
```

Or with curl:

```bash
curl -X POST localhost:8000/webhook/validate \
  -H "Content-Type: application/json" -H "X-Webhook-Secret: change-me-webhook-secret" \
  -d @samples/claim_valid.json
# -> 202 {"task_id": "...", "status": "queued", "duplicate": false}

curl localhost:8000/tasks/<task_id> -H "X-Webhook-Secret: change-me-webhook-secret"
curl localhost:9000/results/123          # what the mock Result API received
```

## API

| Endpoint | Description |
|---|---|
| `POST /webhook/validate` | Header `X-Webhook-Secret`. Returns `202 {task_id, status, duplicate}`. If the same `feedback_id` is already being processed, returns the existing task (`duplicate: true`) |
| `GET /tasks/{task_id}` | `status`: queued → processing → completed / failed; `current_step`: received, field_match, tc_validation, history_check, gift_assignment, reasoning, submitting, done |
| `GET /health` | Liveness + Redis check |
| `GET /docs` | OpenAPI UI |

### Webhook payload

All keys are snake_case. Unknown or misspelled keys (e.g. `"purchase date"`) are rejected with 422,
so a contract mistake is visible immediately instead of a field being silently ignored.

```json
{
  "feedback_id": 123,
  "campaign_id": 221,
  "ocr": {
    "name": "JOHN M SMITH",
    "id_number": "9001015009087",
    "gender": "M",
    "dob": "1990-01-01",
    "country": "South Africa",
    "invoice_no": "INV-2026-77812",
    "purchase_date": "2026-10-02",
    "items": [
      {"imei_or_serial": "356789104512345", "model": "Samsung SM-A556E/DS"},
      {"imei_or_serial": "123", "model": "mobile Pouch"}
    ],
    "store_name": "MTN Southafrica",
    "store_address": "Shop 12, Sandton City, Johannesburg",
    "confidence": 0.94
  },
  "user": {
    "first_name": "John",
    "last_name": "Smith",
    "imei_or_serial": "356789104512345",
    "store_name": "MTN Stores",
    "model": "Galaxy A55",
    "submitted_date": "2026-10-20",
    "selected_gift": "R750 Takealot voucher"
  }
}
```

`selected_gift` is only sent when the campaign lets the customer choose; leave it out or null otherwise.
`feedback_id`, `campaign_id`, `ocr` and `user` are required; every field inside `ocr`, `ocr.items[]`
and `user` is optional. `confidence` accepts 0–1 or 0–100. Numbers are accepted for string fields.

## Contracts for the services you will build

The agent only depends on these contracts. Swap a mock for the real service by changing its URL in `.env`.

**RAG API** (`RAG_API_URL`)
```
GET /tc/{campaign_id}/rules
→ {"campaign_id": 221, "tc_version": 3, "chunks": [{"id": "...", "section": "...", "text": "..."}]}
```
Recommendation for re-embedding a campaign: upsert the new chunks with `tc_version = N+1`,
switch reads to N+1, then delete N. Deleting first leaves a window where claims are validated against no rules.
(`POST /tc/search {campaign_id, query, top_k}` is in the client for when documents grow large.)

**History API** (`HISTORY_API_URL`)
```
POST /history/check
{"campaign_id": 221, "feedback_id": 123, "imei": "...", "id_number": "...", "invoice_no": "..."}
→ [{"field_name": "imei", "num_of_claims": 0}, {"field_name": "id_number", "num_of_claims": 2}]
```
Must **exclude the current `feedback_id`**, otherwise every claim counts itself.

**Result API** (`RESULT_API_URL`)
```
POST  Authorization: Bearer <RESULT_API_TOKEN>   Idempotency-Key: feedback-<id>-<task_id>
body: the final result JSON (see below)
```
5xx / timeouts are retried (`HTTP_MAX_RETRIES`), 4xx are not. Use the Idempotency-Key to ignore duplicates.

**Status API** (`STATUS_CALLBACK_URL`, optional; empty = off)
```
POST  body: {task_id, feedback_id, campaign_id, status, current_step, updated_at, error, decision, overall_score}
```

## Result format

```json
{
  "task_id": "991692fd-…",
  "feedback_id": 123,
  "campaign_id": 221,
  "tc_version": 3,
  "prompt_version": "2026-10-25.1",
  "ocr_vs_user": [
    {"field": "name", "user": "John Smith", "ocr": "JOHN M SMITH", "matching_score": 91, "method": "fuzzy", "ai_reason": null},
    {"field": "store_name", "user": "MTN Stores", "ocr": "MTN Southafrica", "matching_score": 90, "method": "ai",
     "ai_reason": "Same retailer brand 'MTN'; branch/region naming differs"},
    {"field": "purchase_date", "user": null, "ocr": "2026-10-02", "matching_score": null, "method": "single_side", "ai_reason": null},
    {"field": "imei_or_serial", "user": "356789104512345", "ocr": "356789104512345", "matching_score": 100, "method": "exact", "item_index": 0},
    {"field": "model", "user": "Galaxy A55", "ocr": "Samsung SM-A556E/DS", "matching_score": 92, "method": "ai", "item_index": 0,
     "ai_reason": "Same device (Galaxy A55)"}
  ],
  "claimed_item_index": 0,
  "invoice_items": [
    {"index": 0, "imei_or_serial": "356789104512345", "model": "Samsung SM-A556E/DS", "imei_score": 100, "model_score": 92, "claimed": true},
    {"index": 1, "imei_or_serial": "123", "model": "mobile Pouch", "imei_score": 33, "model_score": 18, "claimed": false}
  ],
  "tc_validation": {"passed": true, "compliance_score": 100, "tc_version": 3, "checks": [...], "issues": []},
  "risk": {"level": "low", "hard_flag": false, "flags": [], "history": [...], "history_score": 100, "explanation": "..."},
  "eligible_gift": {
    "status": "assigned",
    "gift_type": "product_based",
    "gift": {"name": "R500 Takealot voucher", "value": "R500", "type": "voucher"},
    "selected_gift": null,
    "selection_valid": null,
    "options": [{"name": "R500 Takealot voucher", "value": "R500", "type": "voucher"}],
    "tc_reference": "Gifts",
    "reason": "TC gift table assigns 'R500 Takealot voucher' to galaxy a55."
  },
  "ai_summary": "...",
  "overall_score": 96,
  "decision": "approve",
  "flags": [],
  "processed_at": "2026-10-24T20:38:07Z",
  "error": null
}
```
`method`: `exact`, `fuzzy`, `strict` (identifiers / dates), `ai` (LLM reasoning), `single_side` (only one side has the field).
The `imei_or_serial` and `model` rows come from the claimed invoice line (`item_index`); `invoice_items` shows how every line scored.

`eligible_gift.status`: `assigned`, `provisional` (claim in review), `not_eligible` (rejected),
`selection_required`, `selection_invalid`, `not_found`, `not_evaluated` (pipeline error).
`selection_valid` is `null` when no `selected_gift` was sent.

## Configuration

`.env` (see `.env.example`): LLM endpoint & model, service URLs and tokens, fuzzy threshold,
strict fields, score weights, thresholds and risk rules.

### Prompts

The five LLM prompts live in `prompts/prompts.yaml` (path set by `PROMPTS_FILE`), not in `.env`:
prompts are logic, so they are versioned with the code and reviewed in pull requests, while
`.env` holds per-environment values and secrets.

```yaml
version: "2026-10-25.1"        # bump on every change; written to each result as prompt_version
prompts:
  tc_validation: |
    You are a strict but fair warranty/promotion claim validator.
    ...
  field_match: |  ...
  history: |  ...
  gift_assignment: |  ...
  summary: |  ...
```

- Loaded and validated at startup: a missing, empty or unknown prompt key stops the agent from starting.
- Keep the `Respond ONLY with JSON: {...}` shape at the end of each prompt; the code reads those keys.
  `tests/test_prompts.py` fails if a prompt stops mentioning one of them.
- Docker mounts `./prompts` into the api and worker containers: edit the file, then
  `docker compose restart api worker`. No rebuild needed.
- Every result carries `prompt_version`, so you can compare outcomes before and after a prompt change.

## Project layout

```
app/
  main.py        FastAPI: webhook, task status, health
  worker.py      arq worker: runs the pipeline
  pipeline.py    the steps (field match + item loop, TC, history, decision, gift, summary, submit)
  gifts.py       gift / voucher assignment rules
  tools.py       check_claim_history / submit_result (OpenAI tool schemas + implementations)
  llm.py         OpenAI-compatible client: JSON calls + tool-calling loop
  matching.py    field pairing, fuzzy scoring, invoice item selection
  scoring.py     risk rules, weighted score, decision
  clients.py     RAG / History / Result / Status HTTP clients (retries)
  tasks.py       task status + duplicate protection (Redis)
  schemas.py     payload / result models
  config.py      settings from .env
  prompts.py     loads and validates prompts/prompts.yaml
prompts/
  prompts.yaml   versioned LLM prompts
mocks/app.py     dummy RAG, History, Result, Status APIs + mock LLM
                 (TC campaigns: 221 product→gift table, 222 fixed R500 voucher, 223 customer choice)
samples/         example webhook payloads
scripts/         send_sample.py
tests/           pytest (no network needed)
```

## Tests

```bash
pytest
```

## Production notes

- Scale workers horizontally: `docker compose up --scale worker=3`. Concurrency per worker: `WORKER_MAX_JOBS`.
- Redis runs with AOF so queued jobs survive restarts. Task status is kept for `TASK_TTL_SECONDS`.
- Logs are JSON lines with `task_id`, `feedback_id`, `campaign_id` on every line.
- The mock LLM uses simple heuristics so the pipeline runs without a key. It is not accurate; use a real model for real evaluation.
- Before going live, test the prompts with a real model on 20–50 real past claims with known outcomes and tune
  `APPROVE_THRESHOLD` / `REJECT_THRESHOLD` against them.
