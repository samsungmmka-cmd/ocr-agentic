"""Thin wrapper around any OpenAI-compatible chat completions endpoint."""
import json
import logging
import re
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from app.config import Settings

log = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict], Awaitable[Any]]


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating ``` fences / chatter."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class LLMClient:
    def __init__(self, settings: Settings):
        self.s = settings
        # The SDK already retries 408/429/5xx/connection errors with backoff.
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
        )

    async def _create(self, step: str, messages: list[dict], **kwargs) -> Any:
        resp = await self.client.chat.completions.create(
            model=self.s.llm_model,
            messages=messages,
            temperature=self.s.llm_temperature,
            # Harmless for real providers; lets the mock LLM know the step.
            extra_headers={"X-Agent-Step": step},
            **kwargs,
        )
        usage = getattr(resp, "usage", None)
        log.info(
            "llm call",
            extra={"data": {"step": step, "model": self.s.llm_model,
                            "tokens": getattr(usage, "total_tokens", None)}},
        )
        return resp

    async def chat_json(self, step: str, system: str, payload: dict) -> dict:
        """One-shot call that must return a JSON object. Retries once on bad JSON."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, default=str)},
        ]
        kwargs = {"response_format": {"type": "json_object"}} if self.s.llm_json_mode else {}
        last_err: Exception | None = None
        for _ in range(2):
            resp = await self._create(step, messages, **kwargs)
            content = resp.choices[0].message.content or ""
            try:
                return parse_json(content)
            except (json.JSONDecodeError, ValueError) as e:
                last_err = e
                log.warning("llm returned invalid json, retrying", extra={"data": {"step": step}})
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": "Return ONLY a valid JSON object."})
        raise LLMError(f"{step}: model did not return valid JSON: {last_err}")

    async def run_with_tools(
        self, step: str, system: str, payload: dict, tools: list[dict], executor: ToolExecutor
    ) -> tuple[dict, list[dict]]:
        """Agent loop: let the model call tools until it returns a final JSON answer.

        Returns (final_json, tool_calls_made).
        """
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, default=str)},
        ]
        calls_made: list[dict] = []
        for _ in range(self.s.llm_max_tool_rounds):
            resp = await self._create(step, messages, tools=tools, tool_choice="auto")
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return parse_json(msg.content or ""), calls_made

            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                name = tc.function.name
                args: dict = {}
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    result = await executor(name, args)
                except Exception as e:  # tool errors go back to the model, not up
                    log.warning("tool failed", extra={"data": {"tool": name, "error": str(e)}})
                    result = {"error": str(e)}
                calls_made.append({"tool": name, "arguments": args,
                                   "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })
        raise LLMError(f"{step}: exceeded {self.s.llm_max_tool_rounds} tool rounds")
