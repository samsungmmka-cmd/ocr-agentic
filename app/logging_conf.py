"""JSON-line logging with task context (task_id / feedback_id) on every line."""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

log_context: ContextVar[dict] = ContextVar("log_context", default={})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **log_context.get(),
        }
        extra = getattr(record, "data", None)
        if extra:
            data.update(extra)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind(**kwargs) -> None:
    log_context.set({**log_context.get(), **kwargs})


def clear() -> None:
    log_context.set({})
