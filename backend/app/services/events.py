"""Event log and webhook fan-out.

Emitting an event writes one `events` row, then one `webhook_deliveries` row per
subscribed endpoint. Deliveries reference the stored event rather than re-deriving a
payload, so a redelivery days later sends byte-identical JSON to the original attempt.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import prefixed_id
from app.models import Event, Project, WebhookDelivery, WebhookEndpoint, utcnow
from app.services import jobs

log = logging.getLogger("fluxora.events")


def envelope(event: Event, project: Project | None) -> dict:
    return {
        "type": event.type,
        "id": event.id,
        "created_at": event.created_at.isoformat().replace("+00:00", "Z"),
        "object": {"type": event.object_type, "id": event.object_id},
        "environment": (
            {"name": project.environment, "id": project.id} if project else None
        ),
        "data": event.data,
    }


def canonical_body(event: Event, project: Project | None) -> bytes:
    """Serialised once and signed as-is. `sort_keys` + fixed separators mean the signature
    a receiver computes over the raw bytes always matches ours."""
    return json.dumps(
        envelope(event, project), sort_keys=True, separators=(",", ":")
    ).encode()


async def emit(
    db: AsyncSession,
    *,
    project_id: str,
    event_type: str,
    object_type: str,
    object_id: str,
    data: dict | None = None,
) -> Event:
    event = Event(
        id=prefixed_id("evt", 24),
        project_id=project_id,
        type=event_type,
        object_type=object_type,
        object_id=object_id,
        data=data or {},
    )
    db.add(event)
    await db.flush()

    endpoints = (
        await db.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.project_id == project_id,
                WebhookEndpoint.active.is_(True),
            )
        )
    ).all()

    for endpoint in endpoints:
        if not endpoint.wants(event_type):
            continue
        await _schedule_delivery(db, endpoint, event, attempt_chain=0)

    log.info("emitted %s for %s %s", event_type, object_type, object_id)
    return event


async def _schedule_delivery(
    db: AsyncSession, endpoint: WebhookEndpoint, event: Event, attempt_chain: int
) -> WebhookDelivery | None:
    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_id=event.id,
        attempt_chain=attempt_chain,
        status="pending",
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    try:
        await db.flush()
    except IntegrityError:
        # The unique (endpoint, event, chain) constraint already holds this delivery.
        await db.rollback()
        return None

    await jobs.enqueue(
        db,
        jobs.DELIVER_WEBHOOK,
        {"delivery_id": delivery.id},
        idempotency_key=f"webhook:{delivery.id}",
        project_id=endpoint.project_id,
        max_attempts=len(settings.webhook_retry_schedule_seconds) + 1,
        priority=50,
        lease_seconds=120,
    )
    return delivery


async def redeliver(db: AsyncSession, event: Event) -> int:
    """Start a fresh attempt chain for every currently-subscribed endpoint."""
    endpoints = (
        await db.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.project_id == event.project_id,
                WebhookEndpoint.active.is_(True),
            )
        )
    ).all()

    scheduled = 0
    for endpoint in endpoints:
        if not endpoint.wants(event.type):
            continue
        highest = await db.scalar(
            select(WebhookDelivery.attempt_chain)
            .where(
                WebhookDelivery.endpoint_id == endpoint.id,
                WebhookDelivery.event_id == event.id,
            )
            .order_by(WebhookDelivery.attempt_chain.desc())
            .limit(1)
        )
        if await _schedule_delivery(db, endpoint, event, (highest or 0) + 1):
            scheduled += 1
    return scheduled


def next_retry_delay(attempts: int) -> timedelta | None:
    schedule = settings.webhook_retry_schedule_seconds
    if attempts >= len(schedule):
        return None
    return timedelta(seconds=schedule[attempts])
