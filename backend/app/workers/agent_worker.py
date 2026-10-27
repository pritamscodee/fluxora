"""BullMQ worker for agent tool calls: `python -m app.workers.agent_worker`.

Consumes `AGENT_TOOL_QUEUE`, validates and executes the named tool, and publishes the
outcome back for the waiting graph. On the final attempt of a failing job the payload is
copied to the dead-letter queue before the failure is re-raised, so nothing is lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from typing import Any

from sqlalchemy import select

from app.agents import bus
from app.agents.tools import ToolContext, ToolError, execute
from app.core.config import settings
from app.core.db import SessionLocal, engine, init_models
from app.core.telemetry import setup_telemetry
from app.models import Project

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("fluxora.agents.worker")

_shutdown = asyncio.Event()


class PermanentToolError(Exception):
    """A failure that retrying cannot fix — a bad tool name, invalid arguments, a missing
    project. Dead-lettered immediately rather than burning five attempts on a certainty."""


async def _process(job, job_token: str | None = None) -> dict:
    """BullMQ processor. Returning normally completes the job; raising triggers a retry."""
    from redis.asyncio import Redis

    data: dict[str, Any] = job.data or {}
    tool_name = str(data.get("tool") or "")
    args = data.get("args") or {}
    project_id = str(data.get("project_id") or "")
    run_id = str(data.get("run_id") or "")
    attempts = int(getattr(job, "attemptsMade", 0) or 0) + 1

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        try:
            if not tool_name or not project_id:
                raise PermanentToolError("task payload is missing a tool name or project id")

            async with SessionLocal() as db:
                project = await db.scalar(select(Project).where(Project.id == project_id))
                if project is None:
                    raise PermanentToolError(f"project {project_id} no longer exists")

                ctx = ToolContext(db=db, project=project, run_id=run_id)
                output = await execute(ctx, tool_name, args)
                # Tools mutate through the session; the job is only complete once that
                # work is durable, so commit before reporting success.
                await db.commit()

            payload = {
                "ok": True,
                "job_id": job.id,
                "tool": tool_name,
                "attempts": attempts,
                "output": output,
            }
            await bus.publish_result(redis, job.id, payload)
            log.info("job %s (%s) succeeded on attempt %d", job.id, tool_name, attempts)
            return output

        except (PermanentToolError, ToolError) as exc:
            # Deterministic failures: another attempt would fail identically.
            message = f"{type(exc).__name__}: {exc}"
            await bus.push_dead_letter(redis, job.id, data, message, attempts)
            await bus.publish_result(
                redis,
                job.id,
                {
                    "ok": False,
                    "job_id": job.id,
                    "tool": tool_name,
                    "attempts": attempts,
                    "error": str(exc),
                    "dead_lettered": True,
                },
            )
            log.warning("job %s (%s) permanently failed: %s", job.id, tool_name, exc)
            # Swallowed on purpose: the run has its answer and retrying is pointless.
            return {"error": str(exc), "dead_lettered": True}

        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.exception("job %s (%s) failed on attempt %d", job.id, tool_name, attempts)
            if attempts >= bus.MAX_ATTEMPTS:
                await bus.push_dead_letter(redis, job.id, data, message, attempts)
                await bus.publish_result(
                    redis,
                    job.id,
                    {
                        "ok": False,
                        "job_id": job.id,
                        "tool": tool_name,
                        "attempts": attempts,
                        "error": message,
                        "dead_lettered": True,
                    },
                )
            # Re-raised so BullMQ schedules the next attempt with backoff.
            raise
    finally:
        await redis.aclose()


async def main() -> None:
    from bullmq import Worker

    if settings.is_sqlite:
        await init_models()
    setup_telemetry()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _shutdown.set())

    worker = Worker(
        settings.agent_tool_queue,
        _process,
        {
            "connection": settings.redis_url,
            "concurrency": settings.agent_tool_concurrency,
            "lockDuration": int(settings.agent_tool_timeout_seconds * 1000),
        },
    )
    log.info(
        "agent tool worker listening queue=%s concurrency=%d attempts=%d dlq=%s",
        settings.agent_tool_queue,
        settings.agent_tool_concurrency,
        bus.MAX_ATTEMPTS,
        bus.dlq_name(),
    )

    await _shutdown.wait()
    log.info("shutting down agent tool worker")
    await worker.close()
    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
