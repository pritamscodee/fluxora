"""BullMQ task bus for agent tool calls.

Every tool the agent invokes goes through Redis as a BullMQ job rather than being called
in-process. That buys three things worth the indirection:

* **Retries with backoff.** Five attempts per task, exponential backoff. A transcode
  kicked off by the agent survives a transient database or storage blip.
* **A dead-letter queue.** A task that exhausts its attempts is copied to
  `<queue>-dlq` with its error and full history, so a permanent failure is inspectable
  and replayable instead of vanishing into Redis' failed set.
* **Isolation.** Tool execution happens in a worker process. A tool that hangs or dies
  cannot take the API process with it.

Results come back over a dedicated Redis key per job rather than by polling job state:
BullMQ's `returnvalue` is only readable once the job is finished, and polling it is both
slower and racier than a blocking pop on a key the worker writes exactly once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

log = logging.getLogger("fluxora.agents.bus")

# Five attempts, as required: 2s, 4s, 8s, 16s between them.
MAX_ATTEMPTS = 5
BACKOFF_MS = 2000

RESULT_TTL_SECONDS = 3600


def dlq_name(queue: str | None = None) -> str:
    return f"{queue or settings.agent_tool_queue}-dlq"


def _result_key(job_id: str) -> str:
    return f"fluxora:agent:result:{job_id}"


@dataclass
class ToolResult:
    ok: bool
    job_id: str
    tool: str
    attempts: int
    output: dict | None = None
    error: str | None = None
    dead_lettered: bool = False

    @classmethod
    def from_payload(cls, payload: dict) -> "ToolResult":
        return cls(
            ok=bool(payload.get("ok")),
            job_id=str(payload.get("job_id", "")),
            tool=str(payload.get("tool", "")),
            attempts=int(payload.get("attempts", 0)),
            output=payload.get("output"),
            error=payload.get("error"),
            dead_lettered=bool(payload.get("dead_lettered")),
        )


class ToolBus:
    """Producer side: enqueue a tool call and await its result."""

    def __init__(self) -> None:
        self._queue = None
        self._redis = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._queue is not None:
            return
        async with self._lock:
            if self._queue is not None:
                return
            from bullmq import Queue
            from redis.asyncio import Redis

            self._queue = Queue(settings.agent_tool_queue, {"connection": settings.redis_url})
            self._redis = Redis.from_url(settings.redis_url, decode_responses=True)
            log.info(
                "agent tool bus ready queue=%s dlq=%s attempts=%d",
                settings.agent_tool_queue, dlq_name(), MAX_ATTEMPTS,
            )

    async def enqueue(
        self,
        *,
        tool: str,
        args: dict,
        project_id: str,
        run_id: str,
        step: int,
    ) -> str:
        await self._ensure()
        job_id = f"{run_id}-{step}-{uuid.uuid4().hex[:8]}"
        payload = {
            "tool": tool,
            "args": args,
            "project_id": project_id,
            "run_id": run_id,
            "step": step,
        }
        await self._queue.add(
            tool,
            payload,
            {
                "jobId": job_id,
                "attempts": MAX_ATTEMPTS,
                "backoff": {"type": "exponential", "delay": BACKOFF_MS},
                # Keep a bounded window of terminal jobs for debugging without letting
                # Redis grow without limit.
                "removeOnComplete": {"age": RESULT_TTL_SECONDS, "count": 1000},
                "removeOnFail": {"age": 86400, "count": 5000},
            },
        )
        log.info("run %s queued tool %s as job %s", run_id, tool, job_id)
        return job_id

    async def await_result(self, job_id: str, timeout: float | None = None) -> ToolResult:
        """Block until the worker publishes this job's outcome.

        A timeout here means the task is still queued or running, not that it failed — so
        the caller is told exactly that rather than being handed a false failure.
        """
        await self._ensure()
        limit = timeout or settings.agent_tool_timeout_seconds
        popped = await self._redis.blpop(_result_key(job_id), timeout=int(limit))
        if popped is None:
            return ToolResult(
                ok=False,
                job_id=job_id,
                tool="",
                attempts=0,
                error=(
                    f"tool task {job_id} did not report a result within {limit:.0f}s; "
                    "it may still be running"
                ),
            )
        _, raw = popped
        return ToolResult.from_payload(json.loads(raw))

    async def call(
        self, *, tool: str, args: dict, project_id: str, run_id: str, step: int
    ) -> ToolResult:
        job_id = await self.enqueue(
            tool=tool, args=args, project_id=project_id, run_id=run_id, step=step
        )
        return await self.await_result(job_id)

    async def dead_letters(self, limit: int = 50) -> list[dict]:
        """Read the dead-letter queue for the dashboard and for operators."""
        await self._ensure()
        raw = await self._redis.lrange(dlq_name(), 0, max(limit - 1, 0))
        out = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return out

    async def dead_letter_count(self) -> int:
        await self._ensure()
        return int(await self._redis.llen(dlq_name()))

    async def replay_dead_letter(self, job_id: str) -> str | None:
        """Re-enqueue a dead-lettered task with a fresh attempt budget."""
        await self._ensure()
        for item in await self._redis.lrange(dlq_name(), 0, -1):
            try:
                record = json.loads(item)
            except json.JSONDecodeError:
                continue
            if record.get("job_id") != job_id:
                continue
            payload = record.get("payload") or {}
            new_id = await self.enqueue(
                tool=payload.get("tool", ""),
                args=payload.get("args") or {},
                project_id=payload.get("project_id", ""),
                run_id=payload.get("run_id", ""),
                step=int(payload.get("step", 0)),
            )
            await self._redis.lrem(dlq_name(), 1, item)
            log.info("replayed dead letter %s as %s", job_id, new_id)
            return new_id
        return None

    async def health(self) -> dict:
        try:
            await self._ensure()
            await self._redis.ping()
            counts = await self._queue.getJobCounts(
                "waiting", "active", "completed", "failed", "delayed"
            ) if hasattr(self._queue, "getJobCounts") else {}
            return {
                "redis": "ok",
                "queue": settings.agent_tool_queue,
                "dead_letters": await self.dead_letter_count(),
                "counts": counts,
            }
        except Exception as exc:
            return {"redis": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

    async def close(self) -> None:
        if self._queue is not None:
            await self._queue.close()
            self._queue = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


_bus: ToolBus | None = None


def get_bus() -> ToolBus:
    global _bus
    if _bus is None:
        _bus = ToolBus()
    return _bus


# ---- Worker-side helpers (imported by app/workers/agent_worker.py) ----

async def publish_result(redis, job_id: str, payload: dict) -> None:
    """Hand a finished job's outcome back to the waiting caller.

    A list plus TTL rather than pub/sub: the producer may not be subscribed at the exact
    moment the worker finishes, and a dropped result would hang the run until timeout.
    """
    key = _result_key(job_id)
    await redis.rpush(key, json.dumps(payload, default=str))
    await redis.expire(key, RESULT_TTL_SECONDS)


async def push_dead_letter(redis, job_id: str, payload: dict, error: str, attempts: int) -> None:
    from datetime import datetime, timezone

    record = {
        "job_id": job_id,
        "payload": payload,
        "error": error[:4000],
        "attempts": attempts,
        "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
    }
    await redis.rpush(dlq_name(), json.dumps(record, default=str))
    # Bounded so a persistent failure loop cannot exhaust Redis memory.
    await redis.ltrim(dlq_name(), -10000, -1)
    log.error("dead-lettered tool task %s after %d attempts: %s", job_id, attempts, error[:300])
