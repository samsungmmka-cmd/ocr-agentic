"""Application settings, loaded from environment / .env."""
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.prompts import Prompts, load_prompts

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return value
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "doc-validation-agent"
    log_level: str = "INFO"
    webhook_secret: str = Field(..., min_length=8)

    # Queue / task store
    redis_url: str = "redis://localhost:6379/0"
    task_ttl_seconds: int = 7 * 24 * 3600
    worker_max_jobs: int = 10
    worker_job_timeout: int = 300

    # LLM
    llm_base_url: str
    llm_api_key: str
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_timeout: float = 60
    llm_max_retries: int = 3
    llm_json_mode: bool = True
    llm_max_tool_rounds: int = 4

    # External services
    rag_api_url: str
    rag_api_key: str = ""
    history_api_url: str
    history_api_key: str = ""
    result_api_url: str
    result_api_token: str
    status_callback_url: str = ""
    http_timeout: float = 20
    http_max_retries: int = 3

    # Matching
    fuzzy_auto_accept: int = 90
    strict_fields: Annotated[list[str], NoDecode] = ["imei_or_serial", "id_number", "invoice_no"]
    excluded_fields: Annotated[list[str], NoDecode] = ["confidence"]
    ocr_min_confidence: float = 0.6
    item_model_min_score: int = 80

    # Scoring
    weight_fields: float = 0.5
    weight_tc: float = 0.3
    weight_history: float = 0.2
    approve_threshold: int = 80
    reject_threshold: int = 50
    hard_flag_score_cap: int = 20
    tc_high_issue_decision: Literal["reject", "review"] = "reject"

    # Risk rules
    # keys = field_name values returned by the History API
    risk_rules: dict[str, str] = {"imei": "high", "serial": "high", "imei_or_serial": "high",
                                  "invoice_no": "high", "id_number": "medium"}
    risk_default_level: str = "low"
    risk_penalty_high: int = 100
    risk_penalty_medium: int = 40
    risk_penalty_low: int = 15

    # Prompts: versioned YAML file (relative paths resolve from the project root)
    prompts_file: str = "prompts/prompts.yaml"
    _prompts: Prompts = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        path = Path(self.prompts_file)
        self._prompts = load_prompts(path if path.is_absolute() else PROJECT_ROOT / path)

    @property
    def prompts(self) -> Prompts:
        return self._prompts

    @field_validator("strict_fields", "excluded_fields", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> list[str]:
        return _csv(v)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
