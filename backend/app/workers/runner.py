"""Worker entrypoint: `python -m app.workers.runner`.

Runs `WORKER_CONCURRENCY` claim loops plus a scheduler that keeps the maintenance jobs
topped up. Each loop owns its own DB session so a rollback in one does not disturb another.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from datetime import timedelta

from app.core.config import settings
from app.core.db import SessionLocal, engine, init_models
from app.core.telemetry import setup_telemetry
from app.models import utcnow
from app.services import jobs
from app.workers.handlers import HANDLERS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("fluxora.runner")

_shutdown = asyncio.Event()

MAINTENANCE_INTERVAL_SECONDS = 60
USAGE_ROLLUP_INTERVAL_SECONDS = 3600


async def _run_one(worker_id: str) -> bool:
    """Claim and run a single job. Returns True if work was done."""
    async with SessionLocal() as db:
        job = await jobs.claim_next(db, worker_id)
        if job is None:
            return False

        handler = HANDLERS.get(job.type)
        if handler is None:
            await jobs.fail(db, job, f"no handler is registered for job type {job.type!r}")
            return True

        log.info("job %s %s attempt %d/%d", job.id, job.type, job.attempts, job.max_attempts)
        try:
            result = await handler(db, job)
        except jobs.RetryLater as retry:
            await db.rollback()
            await jobs.defer(db, job, retry.delay_seconds, retry.reason)
            log.info("job %s deferred %ss: %s", job.id, retry.delay_seconds, retry.reason)
        except asyncio.CancelledError:
            # Shutdown mid-job: release the claim so another worker picks it up promptly
            # instead of waiting out the lease.
            await db.rollback()
            await jobs.defer(db, job, 5, "worker shut down mid-job")
            raise
        except Exception as exc:  # a handler crash must not kill the loop
            await db.rollback()
            log.exception("job %s (%s) raised", job.id, job.type)
            await jobs.fail(db, job, f"{type(exc).__name__}: {exc}")
        else:
            await jobs.succeed(db, job.id, result if isinstance(result, dict) else None)
            log.info("job %s %s done: %s", job.id, job.type, result)
        return True


async def _loop(index: int, worker_id: str) -> None:
    idle_backoff = settings.worker_poll_interval_seconds
    while not _shutdown.is_set():
        try:
            did_work = await _run_one(f"{worker_id}#{index}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("claim loop %d failed; backing off", index)
            did_work = False

        if did_work:
            continue
        # Nothing queued: sleep, but wake immediately on shutdown.
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=idle_backoff)
        except asyncio.TimeoutError:
            pass


async def _scheduler() -> None:
    """Keeps recurring maintenance queued. Enqueue is idempotent on a time-bucketed key,
    so several workers racing here produce exactly one job per bucket."""
    while not _shutdown.is_set():
        async with SessionLocal() as db:
            try:
                bucket = int(utcnow().timestamp() // MAINTENANCE_INTERVAL_SECONDS)
                await jobs.enqueue(
                    db, jobs.REAP_UPLOADS, {},
                    idempotency_key=f"reap:{bucket}", priority=180, max_attempts=1,
                    lease_seconds=300,
                )
                hour_bucket = int(utcnow().timestamp() // USAGE_ROLLUP_INTERVAL_SECONDS)
                await jobs.enqueue(
                    db, jobs.ROLLUP_USAGE, {},
                    idempotency_key=f"usage:{hour_bucket}", priority=200, max_attempts=1,
                    run_after=utcnow() + timedelta(seconds=5), lease_seconds=600,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=MAINTENANCE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    if settings.is_sqlite:
        await init_models()
    setup_telemetry()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except NotImplementedError:
            # Windows asyncio has no add_signal_handler; fall back to the sync handler.
            signal.signal(sig, lambda *_: _shutdown.set())

    log.info(
        "worker %s starting concurrency=%d handlers=%d",
        worker_id, settings.worker_concurrency, len(HANDLERS),
    )
    tasks = [
        asyncio.create_task(_loop(i, worker_id))
        for i in range(max(settings.worker_concurrency, 1))
    ]
    tasks.append(asyncio.create_task(_scheduler()))

    await _shutdown.wait()
    log.info("shutdown requested; draining")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine.dispose()
    log.info("worker stopped")


async def drain_once(max_jobs: int = 100) -> int:
    """Run queued jobs until the queue is empty. Used by tests and the dev CLI, where
    spinning up a separate worker process is more ceremony than it is worth."""
    worker_id = "drain"
    done = 0
    while done < max_jobs:
        if not await _run_one(f"{worker_id}#0"):
            break
        done += 1
    return done


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
