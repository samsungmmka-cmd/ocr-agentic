"""Agent tools (OpenAI function-calling format) and their implementations."""
import logging
from typing import Any

from app.clients import HistoryClient, ResultClient

log = logging.getLogger(__name__)

CHECK_CLAIM_HISTORY = {
    "type": "function",
    "function": {
        "name": "check_claim_history",
        "description": (
            "Look up how many previous claims exist for the identifiers of the current claim "
            "(IMEI/serial, ID number, invoice number) in the campaign. The current claim is "
            "excluded. Returns a list of {field_name, num_of_claims}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "imei_or_serial": {"type": ["string", "null"]},
                "id_number": {"type": ["string", "null"]},
                "invoice_no": {"type": ["string", "null"]},
            },
            "required": ["campaign_id"],
            "additionalProperties": False,
        },
    },
}

SUBMIT_RESULT = {
    "type": "function",
    "function": {
        "name": "submit_result",
        "description": "Submit the final validation result JSON to the Result API.",
        "parameters": {
            "type": "object",
            "properties": {"result": {"type": "object"}},
            "required": ["result"],
        },
    },
}


class AgentTools:
    """Holds the tool implementations for one pipeline run.

    `check_claim_history` is exposed to the LLM. `submit_result` is defined as a
    tool too, but the pipeline calls it directly: submission must never depend
    on the model deciding to call it.
    """

    def __init__(self, history: HistoryClient, results: ResultClient, feedback_id: int):
        self.history = history
        self.results = results
        self.feedback_id = feedback_id
        self.history_records: list[dict] | None = None

    @property
    def llm_tools(self) -> list[dict]:
        return [CHECK_CLAIM_HISTORY]

    async def execute(self, name: str, args: dict) -> Any:
        if name == "check_claim_history":
            return await self.check_claim_history(**args)
        raise ValueError(f"unknown tool: {name}")

    async def check_claim_history(
        self,
        campaign_id: int,
        imei_or_serial: str | None = None,
        id_number: str | None = None,
        invoice_no: str | None = None,
    ) -> list[dict]:
        body = {
            "campaign_id": campaign_id,
            "feedback_id": self.feedback_id,  # always exclude the current claim
            "imei": imei_or_serial,
            "id_number": id_number,
            "invoice_no": invoice_no,
        }
        records = await self.history.check(body)
        log.info("tool check_claim_history", extra={"data": {"records": records}})
        self.history_records = records
        return records

    async def submit_result(self, result: dict) -> Any:
        resp = await self.results.submit(result)
        log.info("tool submit_result", extra={"data": {"response": resp}})
        return resp
