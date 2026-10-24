"""arq worker: pulls jobs from Redis and runs the pipeline.

Run:  arq app.worker.WorkerSettings
"""
import logging

from arq.connections import RedisSettings

from app.clients import HistoryClient, RagClient, ResultClient, StatusClient
from app.config import get_settings
from app.llm import LLMClient
from app.logging_conf import bind, clear, setup_logging
from app.pipeline import Pipeline
from app.schemas import TaskStatus, TaskStep, WebhookPayload
from app.tasks import TaskStore

settings = get_settings()
setup_logging(settings.log_level)
log = logging.getLogger("worker")


async def startup(ctx: dict) -> None:
    status = StatusClient(settings)
    ctx["clients"] = [RagClient(settings), HistoryClient(settings), ResultClient(settings), status]
    rag, history, results, _ = ctx["clients"]
    ctx["store"] = TaskStore(ctx["redis"], settings.task_ttl_seconds, status)
    ctx["pipeline"] = Pipeline(settings, LLMClient(settings), rag, history, results, ctx["store"])
    log.info("worker started", extra={"data": {"model": settings.llm_model,
                                                "llm_base_url": settings.llm_base_url,
                                                "prompts_file": settings.prompts_file,
                                                "prompt_version": settings.prompts.version}})


async def shutdown(ctx: dict) -> None:
    for c in ctx.get("clients", []):
        await c.aclose()


async def run_validation(ctx: dict, task_id: str, payload: dict) -> dict:
    store: TaskStore = ctx["store"]
    pipeline: Pipeline = ctx["pipeline"]
    data = WebhookPayload.model_validate(payload)
    bind(task_id=task_id, feedback_id=data.feedback_id, campaign_id=data.campaign_id)
    log.info("pipeline started")
    try:
        result = await pipeline.run(task_id, data)
        await store.update(task_id, status=TaskStatus.completed, current_step=TaskStep.done,
                           decision=result.decision, overall_score=result.overall_score)
        log.info("pipeline completed", extra={"data": {"decision": result.decision,
                                                       "overall_score": result.overall_score}})
        return {"decision": result.decision, "overall_score": result.overall_score}
    except Exception as e:
        log.exception("pipeline failed")
        error = f"{type(e).__name__}: {e}"
        try:
            await pipeline.submit_failure(task_id, data, error)
        except Exception as e2:
            log.exception("failure result could not be submitted")
            error += f" | submit failed: {e2}"
        await store.update(task_id, status=TaskStatus.failed, error=error,
                           decision="manual_review")
        return {"decision": "manual_review", "error": error}
    finally:
        clear()


class WorkerSettings:
    functions = [run_validation]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout
    max_tries = 1  # the pipeline retries internally and always reports an outcome
    keep_result = 3600
