import os

import pytest

# Minimal env so Settings() loads without a real .env
os.environ.update({
    "WEBHOOK_SECRET": "test-secret-123",
    "LLM_BASE_URL": "http://llm.test/v1",
    "LLM_API_KEY": "sk-test",
    "RAG_API_URL": "http://rag.test",
    "HISTORY_API_URL": "http://history.test",
    "RESULT_API_URL": "http://result.test/results",
    "RESULT_API_TOKEN": "token",
    "STATUS_CALLBACK_URL": "",
    "PROMPTS_FILE": "prompts/prompts.yaml",   # the real file: tests also validate it
})


@pytest.fixture
def settings():
    from app.config import Settings
    return Settings(_env_file=None)


@pytest.fixture
def payload_dict():
    return {
        "feedback_id": 123,
        "campaign_id": 221,
        "ocr": {
            "name": "JOHN SMITH", "id_number": "9001015009087", "invoice_no": "INV-1",
            "purchase_date": "2026-10-02",
            "items": [
                {"imei_or_serial": "356789104512345", "model": "Samsung SM-A556E"},
                {"imei_or_serial": "123", "model": "mobile Pouch"},
            ],
            "store_name": "MTN Southafrica", "confidence": 0.94,
        },
        "user": {
            "first_name": "John", "last_name": "Smith", "imei_or_serial": "356789104512345",
            "store_name": "MTN Stores", "model": "Galaxy A55", "submitted_date": "2026-10-20",
        },
    }
