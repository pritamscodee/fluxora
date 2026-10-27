"""Direct uploads: mint a single-use presigned PUT, then reconcile what actually landed.

The client is never trusted to tell us an upload succeeded. `complete` is a hint that lets
us react fast; the reaper reconciles independently, so an upload still converges if the
client crashes after the PUT or never calls back at all.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models import DirectUpload, Project, utcnow
from app.schemas.video import UploadCreate
from app.services import assets, events, storage

log = logging.getLogger("fluxora.uploads")

TERMINAL_STATUSES = {"asset_created", "cancelled", "errored", "timed_out"}


async def create(db: AsyncSession, project: Project, body: UploadCreate) -> DirectUpload:
    upload = DirectUpload(
        project_id=project.id,
        status="waiting",
        object_key="",  # filled below; needs the generated id
        cors_origin=body.cors_origin,
        timeout_seconds=body.timeout,
        expires_at=utcnow() + timedelta(seconds=body.timeout),
        new_asset_settings=body.new_asset_settings.model_dump(mode="json"),
        test=body.test or body.new_asset_settings.test,
    )
    db.add(upload)
    await db.flush()

    upload.object_key = storage.source_key(project.id, upload.id)
    upload.upload_url = await storage.get_storage().presign_put(
        upload.object_key,
        expires_in=body.timeout,
        content_type="application/octet-stream",
    )
    await db.flush()

    await events.emit(
        db,
        project_id=project.id,
        event_type="video.upload.created",
        object_type="upload",
        object_id=upload.id,
        data={"status": "waiting", "timeout": upload.timeout_seconds},
    )
    return upload


async def get_for_project(db: AsyncSession, project_id: str, upload_id: str) -> DirectUpload:
    upload = await db.scalar(
        select(DirectUpload).where(
            DirectUpload.id == upload_id, DirectUpload.project_id == project_id
        )
    )
    if upload is None:
        raise NotFound(f"upload {upload_id} was not found")
    return upload


async def list_for_project(
    db: AsyncSession, project_id: str, limit: int, offset: int
) -> tuple[list[DirectUpload], int]:
    base = select(DirectUpload).where(DirectUpload.project_id == project_id)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await db.scalars(
            base.order_by(DirectUpload.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def cancel(db: AsyncSession, upload: DirectUpload) -> DirectUpload:
    if upload.status == "cancelled":
        return upload
    if upload.status in TERMINAL_STATUSES:
        raise Conflict(f"upload is already {upload.status} and cannot be cancelled")
    upload.status = "cancelled"
    # Dropping the URL revokes the capability as far as our API is concerned; the presign
    # itself is also bounded by its own expiry.
    upload.upload_url = None
    await db.flush()
    await events.emit(
        db,
        project_id=upload.project_id,
        event_type="video.upload.cancelled",
        object_type="upload",
        object_id=upload.id,
        data={"status": "cancelled"},
    )
    return upload


async def reconcile(
    db: AsyncSession, upload: DirectUpload, project: Project
) -> DirectUpload:
    """Promote a waiting upload to an asset if its object is present.

    Idempotent and safe to call from both the client callback and the reaper: an upload
    that already produced an asset is returned untouched.
    """
    if upload.status in TERMINAL_STATUSES:
        return upload

    size = await storage.get_storage().size(upload.object_key)
    if size is None:
        if upload.expires_at.timestamp() < utcnow().timestamp():
            return await _time_out(db, upload)
        return upload
    if size == 0:
        return await _error(db, upload, "invalid_input", ["the uploaded object is empty"])

    asset = await assets.create_from_upload(db, project, upload, upload.new_asset_settings)
    asset.source_bytes = size
    upload.status = "asset_created"
    upload.asset_id = asset.id
    upload.upload_url = None
    await db.flush()

    await events.emit(
        db,
        project_id=upload.project_id,
        event_type="video.upload.asset_created",
        object_type="upload",
        object_id=upload.id,
        data={"status": "asset_created", "asset_id": asset.id},
    )
    return upload


async def _time_out(db: AsyncSession, upload: DirectUpload) -> DirectUpload:
    upload.status = "timed_out"
    upload.upload_url = None
    await db.flush()
    await events.emit(
        db,
        project_id=upload.project_id,
        event_type="video.upload.errored",
        object_type="upload",
        object_id=upload.id,
        data={"status": "timed_out"},
    )
    return upload


async def _error(
    db: AsyncSession, upload: DirectUpload, error_type: str, messages: list[str]
) -> DirectUpload:
    upload.status = "errored"
    upload.error_type = error_type
    upload.error_messages = messages
    upload.upload_url = None
    await db.flush()
    await events.emit(
        db,
        project_id=upload.project_id,
        event_type="video.upload.errored",
        object_type="upload",
        object_id=upload.id,
        data={"status": "errored", "errors": {"type": error_type, "messages": messages}},
    )
    return upload


async def find_expired(db: AsyncSession, limit: int = 100) -> list[DirectUpload]:
    rows = await db.scalars(
        select(DirectUpload)
        .where(DirectUpload.status == "waiting", DirectUpload.expires_at < utcnow())
        .limit(limit)
    )
    return list(rows.all())


async def find_waiting(db: AsyncSession, limit: int = 100) -> list[DirectUpload]:
    rows = await db.scalars(
        select(DirectUpload).where(DirectUpload.status == "waiting").limit(limit)
    )
    return list(rows.all())
