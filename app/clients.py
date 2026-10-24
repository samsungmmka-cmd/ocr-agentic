"""HTTP clients for the external services (RAG / History / Result / Status).

All of them retry on network errors and 5xx with exponential backoff.
4xx responses are not retried: they mean the request itself is wrong.
"""
import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings

log = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    def __init__(self, service: str, message: str, status_code: int | None = None):
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status_code = status_code


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


class BaseClient:
    service = "service"

    def __init__(self, base_url: str, settings: Settings, headers: dict | None = None):
        self.s = settings
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=settings.http_timeout, headers=headers or {}
        )

    async def request(self, method: str, url: str, **kwargs) -> Any:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.s.http_max_retries),
                wait=wait_exponential(multiplier=0.5, max=8),
                retry=retry_if_exception(_retryable),
                reraise=True,
            ):
                with attempt:
                    # "" means "the configured URL itself" (avoids a trailing slash)
                    resp = await self.http.request(method, url or self.base_url, **kwargs)
                    resp.raise_for_status()
                    return resp.json() if resp.content else None
        except httpx.HTTPStatusError as e:
            raise ServiceError(self.service, f"HTTP {e.response.status_code}: {e.response.text[:300]}",
                               e.response.status_code) from e
        except httpx.TransportError as e:
            raise ServiceError(self.service, f"unreachable: {e!r}") from e

    async def aclose(self) -> None:
        await self.http.aclose()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


class RagClient(BaseClient):
    """Terms & Conditions rules from the (future) RAG API."""

    service = "rag_api"

    def __init__(self, s: Settings):
        super().__init__(s.rag_api_url, s, _bearer(s.rag_api_key))

    async def get_rules(self, campaign_id: int) -> dict:
        """-> {campaign_id, tc_version, chunks: [{id, text, section}]}"""
        return await self.request("GET", f"/tc/{campaign_id}/rules")

    async def search(self, campaign_id: int, query: str, top_k: int = 5) -> dict:
        return await self.request(
            "POST", "/tc/search", json={"campaign_id": campaign_id, "query": query, "top_k": top_k}
        )


class HistoryClient(BaseClient):
    service = "history_api"

    def __init__(self, s: Settings):
        super().__init__(s.history_api_url, s, _bearer(s.history_api_key))

    async def check(self, body: dict) -> list[dict]:
        """-> [{field_name, num_of_claims}]"""
        data = await self.request("POST", "/history/check", json=body)
        return data or []


class ResultClient(BaseClient):
    service = "result_api"

    def __init__(self, s: Settings):
        super().__init__(s.result_api_url, s, _bearer(s.result_api_token))

    async def submit(self, result: dict) -> Any:
        return await self.request(
            "POST", "", json=result,
            headers={"Idempotency-Key": f"feedback-{result['feedback_id']}-{result['task_id']}"},
        )


class StatusClient(BaseClient):
    """Optional push of task status changes. Never fails the pipeline."""

    service = "status_api"

    def __init__(self, s: Settings):
        super().__init__(s.status_callback_url or "http://disabled", s)
        self.enabled = bool(s.status_callback_url)

    async def push(self, state: dict) -> None:
        if not self.enabled:
            return
        try:
            await self.request("POST", "", json=state)
        except ServiceError as e:
            log.warning("status callback failed", extra={"data": {"error": str(e)}})
