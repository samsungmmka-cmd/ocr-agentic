"""Task status store + duplicate protection, backed by Redis."""
import logging
import uuid
from datetime import datetime, timezone

from redis.asyncio import Redis

from app.clients import StatusClient
from app.schemas import TaskState, TaskStatus, TaskStep

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStore:
    def __init__(self, redis: Redis, ttl: int, status_client: StatusClient | None = None):
        self.r = redis
        self.ttl = ttl
        self.status_client = status_client

    @staticmethod
    def _key(task_id: str) -> str:
        return f"task:{task_id}"

    @staticmethod
    def _active_key(feedback_id: int) -> str:
        return f"feedback:active:{feedback_id}"

    async def create(self, feedback_id: int, campaign_id: int) -> tuple[TaskState, bool]:
        """Create a task, or return the active one for this feedback_id.

        Returns (state, is_duplicate). A feedback_id can be processed again
        once its previous task has finished.
        """
        task_id = str(uuid.uuid4())
        claimed = await self.r.set(self._active_key(feedback_id), task_id, nx=True, ex=self.ttl)
        if not claimed:
            existing_id = await self.r.get(self._active_key(feedback_id))
            existing = await self.get(existing_id.decode() if isinstance(existing_id, bytes) else existing_id)
            if existing:
                return existing, True
            # stale pointer: take it over
            await self.r.set(self._active_key(feedback_id), task_id, ex=self.ttl)

        now = _now()
        state = TaskState(task_id=task_id, feedback_id=feedback_id, campaign_id=campaign_id,
                          status=TaskStatus.queued, current_step=TaskStep.received,
                          created_at=now, updated_at=now)
        await self._save(state)
        return state, False

    async def get(self, task_id: str | None) -> TaskState | None:
        if not task_id:
            return None
        raw = await self.r.get(self._key(task_id))
        return TaskState.model_validate_json(raw) if raw else None

    async def update(self, task_id: str, **changes) -> TaskState:
        state = await self.get(task_id)
        if state is None:
            raise KeyError(task_id)
        state = state.model_copy(update={**changes, "updated_at": _now()})
        await self._save(state)
        if state.status in (TaskStatus.completed, TaskStatus.failed):
            await self.r.delete(self._active_key(state.feedback_id))
        return state

    async def step(self, task_id: str, step: TaskStep) -> TaskState:
        return await self.update(task_id, status=TaskStatus.processing, current_step=step)

    async def _save(self, state: TaskState) -> None:
        await self.r.set(self._key(state.task_id), state.model_dump_json(), ex=self.ttl)
        log.info("task status", extra={"data": {"status": state.status.value,
                                                 "step": state.current_step.value}})
        if self.status_client:
            await self.status_client.push(state.model_dump(mode="json"))
