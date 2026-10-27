"""Postgres-backed work queue (ADR-0001 decision 2).

Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, which lets N workers pull disjoint rows
without a coordinator. SQLite has no such clause, so the dev path falls back to a guarded
compare-and-set update; correctness holds either way, only contention behaviour differs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Job, utcnow

log = logging.getLogger("fluxora.jobs")

# Job types
PROBE_AND_TRANSCODE = "asset.transcode"
INGEST_FROM_URL = "asset.ingest_url"
RENDER_MP4 = "asset.render_mp4"
RENDER_STORYBOARD = "asset.render_storyboard"
INGEST_TEXT_TRACK = "asset.ingest_text_track"
CREATE_CLIP = "asset.create_clip"
DELIVER_WEBHOOK = "webhook.deliver"
REAP_UPLOADS = "maintenance.reap_uploads"
ROLLUP_USAGE = "maintenance.rollup_usage"
AI_ANALYZE = "ai.analyze_asset"

# Backoff for failed jobs: 30s, 2m, 8m, 32m, capped.
_BACKOFF_SECONDS = (30, 120, 480, 1920)


async def enqueue(
    db: AsyncSession,
    job_type: str,
    payload: dict,
    *,
    idempotency_key: str,
    project_id: str | None = None,
    max_attempts: int = 3,
    priority: int = 100,
    run_after: datetime | None = None,
    lease_seconds: int = 3600,
) -> Job | None:
    """Insert a job unless its idempotency key already exists.

    Returning `None` on a duplicate is the point: callers may be retried (an API retry, a
    redelivered webhook, a reclaimed parent job) and must not fan out duplicate work.
    """
    job = Job(
        project_id=project_id,
        type=job_type,
        payload=payload,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        priority=priority,
        run_after=run_after,
        lease_seconds=lease_seconds,
    )
    db.add(job)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        log.info("job %s already queued as %s", idempotency_key, existing.id if existing else "?")
        return None
    return job


async def claim_next(db: AsyncSession, worker_id: str, types: list[str] | None = None) -> Job | None:
    now = utcnow()
    if settings.is_sqlite:
        return await _claim_sqlite(db, worker_id, now, types)
    return await _claim_postgres(db, worker_id, now, types)


def _claimable(now: datetime):
    """A row is claimable when it is queued and due, or when it is `running` but its lease
    has expired — the latter is how work survives a worker being killed mid-ffmpeg."""
    return (
        (
            (Job.status == "queued")
            & ((Job.run_after.is_(None)) | (Job.run_after <= now))
        )
        | (
            (Job.status == "running")
            & (Job.locked_at.is_not(None))
            & (Job.locked_at < now - timedelta(seconds=1))
        )
    )


async def _claim_postgres(
    db: AsyncSession, worker_id: str, now: datetime, types: list[str] | None
) -> Job | None:
    type_filter = "AND type = ANY(:types)" if types else ""
    sql = text(
        f"""
        WITH claimed AS (
            SELECT id FROM jobs
            WHERE (
                      (status = 'queued' AND (run_after IS NULL OR run_after <= :now))
                   OR (status = 'running'
                       AND locked_at IS NOT NULL
                       AND locked_at < :now - make_interval(secs => lease_seconds))
                  )
              AND attempts < max_attempts
              {type_filter}
            ORDER BY priority ASC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE jobs j
           SET status = 'running',
               locked_at = :now,
               locked_by = :worker,
               started_at = COALESCE(j.started_at, :now),
               attempts = j.attempts + 1,
               updated_at = :now
          FROM claimed
         WHERE j.id = claimed.id
        RETURNING j.id
        """
    )
    params: dict = {"now": now, "worker": worker_id}
    if types:
        params["types"] = types
    job_id = await db.scalar(sql, params)
    await db.commit()
    if job_id is None:
        return None
    return await db.scalar(select(Job).where(Job.id == job_id))


async def _claim_sqlite(
    db: AsyncSession, worker_id: str, now: datetime, types: list[str] | None
) -> Job | None:
    stmt = (
        select(Job)
        .where(_claimable(now), Job.attempts < Job.max_attempts)
        .order_by(Job.priority.asc(), Job.created_at.asc())
        .limit(10)
    )
    for candidate in (await db.scalars(stmt)).all():
        if types and candidate.type not in types:
            continue
        if candidate.status == "running":
            lease_cutoff = now - timedelta(seconds=candidate.lease_seconds)
            if candidate.locked_at and candidate.locked_at.replace(tzinfo=timezone.utc) > lease_cutoff:
                continue
        # Compare-and-set on the previous lock holder stands in for SKIP LOCKED.
        result = await db.execute(
            update(Job)
            .where(Job.id == candidate.id, Job.locked_by.is_(candidate.locked_by))
            .values(
                status="running",
                locked_at=now,
                locked_by=worker_id,
                started_at=candidate.started_at or now,
                attempts=Job.attempts + 1,
                updated_at=now,
            )
        )
        await db.commit()
        if result.rowcount:
            return await db.scalar(select(Job).where(Job.id == candidate.id))
    return None


async def heartbeat(db: AsyncSession, job_id: str, progress: float | None = None) -> None:
    """Extend the lease on a long transcode so it is not reclaimed while still running."""
    values: dict = {"locked_at": utcnow(), "updated_at": utcnow()}
    if progress is not None:
        values["progress"] = max(0.0, min(progress, 1.0))
    await db.execute(update(Job).where(Job.id == job_id).values(**values))
    await db.commit()


class RetryLater(Exception):
    """Raised by a handler that wants a specific retry delay instead of the generic
    backoff — webhook delivery, which follows its own published schedule."""

    def __init__(self, delay_seconds: int, reason: str = ""):
        self.delay_seconds = delay_seconds
        self.reason = reason
        super().__init__(reason or f"retry in {delay_seconds}s")


async def defer(db: AsyncSession, job: Job, delay_seconds: int, reason: str = "") -> None:
    await db.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(
            status="queued",
            run_after=utcnow() + timedelta(seconds=delay_seconds),
            locked_at=None,
            locked_by=None,
            error=reason[:4000] or None,
            updated_at=utcnow(),
        )
    )
    await db.commit()


async def succeed(db: AsyncSession, job_id: str, result: dict | None = None) -> None:
    await db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="succeeded",
            result=result or {},
            progress=1.0,
            finished_at=utcnow(),
            locked_at=None,
            locked_by=None,
            error=None,
        )
    )
    await db.commit()


async def fail(db: AsyncSession, job: Job, error: str) -> bool:
    """Record a failure. Returns True when the job will be retried, False when exhausted."""
    exhausted = job.attempts >= job.max_attempts
    backoff = _BACKOFF_SECONDS[min(job.attempts - 1, len(_BACKOFF_SECONDS) - 1)]
    values: dict = {
        "error": error[:4000],
        "locked_at": None,
        "locked_by": None,
        "updated_at": utcnow(),
    }
    if exhausted:
        values |= {"status": "failed", "finished_at": utcnow()}
    else:
        values |= {"status": "queued", "run_after": utcnow() + timedelta(seconds=backoff)}
    await db.execute(update(Job).where(Job.id == job.id).values(**values))
    await db.commit()
    log.warning(
        "job %s (%s) failed on attempt %d/%d: %s",
        job.id, job.type, job.attempts, job.max_attempts, error[:300],
    )
    return not exhausted
