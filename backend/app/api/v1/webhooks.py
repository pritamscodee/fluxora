from __future__ import annotations

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import func, select

from app.api.deps import DbSession, OwnedProject, Pagination
from app.core.errors import NotFound
from app.core.ids import webhook_secret
from app.models import Event, WebhookDelivery, WebhookEndpoint, utcnow
from app.schemas.common import envelope
from app.schemas.platform import (
    EVENT_TYPES,
    DeliveryOut,
    EventOut,
    WebhookCreate,
    WebhookCreated,
    WebhookOut,
    WebhookUpdate,
)
from app.services import events as event_service

router = APIRouter(prefix="/projects/{project_id}", tags=["webhooks"])


@router.get("/event-types")
async def list_event_types(project: OwnedProject):
    return envelope(list(EVENT_TYPES))


@router.post("/webhooks", status_code=status.HTTP_201_CREATED)
async def create_webhook(body: WebhookCreate, project: OwnedProject, db: DbSession):
    endpoint = WebhookEndpoint(
        project_id=project.id,
        url=str(body.url),
        secret=webhook_secret(),
        events=body.events,
        description=body.description,
    )
    db.add(endpoint)
    await db.flush()
    return envelope(WebhookCreated.model_validate(endpoint))


@router.get("/webhooks")
async def list_webhooks(project: OwnedProject, db: DbSession):
    rows = (
        await db.scalars(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.project_id == project.id)
            .order_by(WebhookEndpoint.created_at.desc())
        )
    ).all()
    return envelope([WebhookOut.model_validate(w) for w in rows])


async def _endpoint(db, project, webhook_id: str) -> WebhookEndpoint:
    endpoint = await db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == webhook_id, WebhookEndpoint.project_id == project.id
        )
    )
    if endpoint is None:
        raise NotFound(f"webhook {webhook_id} was not found")
    return endpoint


@router.patch("/webhooks/{webhook_id}")
async def update_webhook(
    webhook_id: str, body: WebhookUpdate, project: OwnedProject, db: DbSession
):
    endpoint = await _endpoint(db, project, webhook_id)
    if body.url is not None:
        endpoint.url = str(body.url)
    if body.events is not None:
        endpoint.events = body.events
    if body.active is not None:
        endpoint.active = body.active
    if body.description is not None:
        endpoint.description = body.description
    endpoint.updated_at = utcnow()
    await db.flush()
    return envelope(WebhookOut.model_validate(endpoint))


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: str, project: OwnedProject, db: DbSession):
    await db.delete(await _endpoint(db, project, webhook_id))


@router.post("/webhooks/{webhook_id}/rotate-secret")
async def rotate_secret(webhook_id: str, project: OwnedProject, db: DbSession):
    endpoint = await _endpoint(db, project, webhook_id)
    endpoint.secret = webhook_secret()
    await db.flush()
    return envelope(WebhookCreated.model_validate(endpoint))


@router.get("/webhooks/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: str,
    project: OwnedProject,
    db: DbSession,
    page: Pagination,
    response: Response,
):
    endpoint = await _endpoint(db, project, webhook_id)
    base = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await db.scalars(
            base.order_by(WebhookDelivery.created_at.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
    ).all()
    response.headers["X-Total-Count"] = str(total or 0)
    return envelope([DeliveryOut.model_validate(d) for d in rows])


# ---- Event log ----

@router.get("/events")
async def list_events(
    project: OwnedProject,
    db: DbSession,
    page: Pagination,
    response: Response,
    type: str | None = Query(default=None),
    object_id: str | None = Query(default=None),
):
    base = select(Event).where(Event.project_id == project.id)
    if type:
        base = base.where(Event.type == type)
    if object_id:
        base = base.where(Event.object_id == object_id)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await db.scalars(
            base.order_by(Event.created_at.desc()).limit(page.limit).offset(page.offset)
        )
    ).all()
    response.headers["X-Total-Count"] = str(total or 0)
    return envelope([EventOut.model_validate(e) for e in rows])


@router.post("/events/{event_id}/redeliver")
async def redeliver_event(event_id: str, project: OwnedProject, db: DbSession):
    event = await db.scalar(
        select(Event).where(Event.id == event_id, Event.project_id == project.id)
    )
    if event is None:
        raise NotFound(f"event {event_id} was not found")
    scheduled = await event_service.redeliver(db, event)
    return envelope({"event_id": event.id, "deliveries_scheduled": scheduled})
