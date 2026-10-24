"""FastAPI entry point: webhook, task status, health."""
import hmac
import logging
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Header, HTTPException, status

from app.clients import StatusClient
from app.config import get_settings
from app.logging_conf import bind, clear, setup_logging
from app.schemas import TaskState, TaskStatus, WebhookAccepted, WebhookPayload
from app.tasks import TaskStore

settings = get_settings()
setup_logging(settings.log_level)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.status_client = StatusClient(settings)
    app.state.store = TaskStore(app.state.redis, settings.task_ttl_seconds, app.state.status_client)
    log.info("api started")
    yield
    await app.state.status_client.aclose()
    await app.state.redis.aclose()


app = FastAPI(title="Document Validation Agent", version="1.0.0", lifespan=lifespan)


def verify_secret(x_webhook_secret: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_webhook_secret.encode(), settings.webhook_secret.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook secret")


@app.post("/webhook/validate", status_code=status.HTTP_202_ACCEPTED,
          response_model=WebhookAccepted, dependencies=[Depends(verify_secret)])
async def webhook(payload: WebhookPayload) -> WebhookAccepted:
    store: TaskStore = app.state.store
    state, duplicate = await store.create(payload.feedback_id, payload.campaign_id)
    bind(task_id=state.task_id, feedback_id=payload.feedback_id)
    try:
        if duplicate:
            log.info("duplicate webhook, returning active task")
        else:
            try:
                await app.state.redis.enqueue_job(
                    "run_validation", state.task_id,
                    payload.model_dump(mode="json", exclude_unset=True), _job_id=state.task_id,
                )
            except Exception as e:
                log.exception("enqueue failed")
                await store.update(state.task_id, status=TaskStatus.failed, error=f"enqueue failed: {e}")
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "queue unavailable")
            log.info("task queued")
        return WebhookAccepted(task_id=state.task_id, status=state.status, duplicate=duplicate)
    finally:
        clear()


@app.get("/tasks/{task_id}", response_model=TaskState, dependencies=[Depends(verify_secret)])
async def get_task(task_id: str) -> TaskState:
    state = await app.state.store.get(task_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return state


@app.get("/health")
async def health() -> dict:
    try:
        await app.state.redis.ping()
        return {"status": "ok", "redis": "ok"}
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "redis unavailable")
