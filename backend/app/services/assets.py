"""Asset lifecycle orchestration: the API-facing side of the pipeline.

Nothing here touches ffmpeg. Routes create rows and enqueue work; the worker
(app/workers/processor.py) does the expensive part and drives status transitions.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, InvalidParameters, NotFound
from app.models import Asset, AssetTrack, PlaybackId, Project, utcnow
from app.schemas.video import AssetCreate, ClipCreate, NewAssetSettings
from app.services import events, jobs, storage

log = logging.getLogger("fluxora.assets")


def _apply_settings(asset: Asset, s: NewAssetSettings) -> None:
    asset.mp4_support = s.mp4_support
    asset.master_access = s.master_access
    asset.normalize_audio = s.normalize_audio
    asset.passthrough = s.passthrough
    asset.test = s.test
    asset.encoding_tier = s.encoding_tier


async def load_relations(db: AsyncSession, asset: Asset) -> Asset:
    """Populate the collections a response serialises.

    A freshly inserted Asset has no loaded relationships, and `selectin` loading only runs
    on a query — so touching `asset.playback_ids` during serialisation would attempt lazy
    IO outside the async greenlet and raise. Refreshing explicitly keeps response building
    free of implicit database work.
    """
    await db.refresh(asset, attribute_names=["playback_ids", "tracks", "static_renditions"])
    return asset


def _attach_playback_ids(db: AsyncSession, asset: Asset, policies: list[str]) -> None:
    # De-duplicated while preserving order: asking for ["public","public"] should not mint
    # two interchangeable ids.
    for policy in dict.fromkeys(policies or ["public"]):
        db.add(PlaybackId(asset_id=asset.id, policy=policy))


async def create_from_request(
    db: AsyncSession, project: Project, body: AssetCreate
) -> Asset:
    video_input = next(i for i in body.input if i.type == "video")
    asset = Asset(project_id=project.id, status="preparing", source_url=str(video_input.url))
    _apply_settings(asset, body)
    if video_input.start_time is not None or video_input.end_time is not None:
        asset.clip_start = video_input.start_time
        asset.clip_end = video_input.end_time
    db.add(asset)
    await db.flush()

    _attach_playback_ids(db, asset, body.playback_policy)

    for text_input in (i for i in body.input if i.type == "text"):
        db.add(
            AssetTrack(
                asset_id=asset.id,
                type="text",
                status="preparing",
                text_type=text_input.text_type or "subtitles",
                language_code=text_input.language_code,
                name=text_input.name or text_input.language_code,
                closed_captions=text_input.closed_captions,
                source_url=str(text_input.url),
            )
        )
    await db.flush()

    await events.emit(
        db,
        project_id=project.id,
        event_type="video.asset.created",
        object_type="asset",
        object_id=asset.id,
        data={"status": asset.status, "passthrough": asset.passthrough},
    )
    await jobs.enqueue(
        db,
        jobs.INGEST_FROM_URL,
        {"asset_id": asset.id},
        idempotency_key=f"ingest:{asset.id}",
        project_id=project.id,
        lease_seconds=7200,
    )
    return await load_relations(db, asset)


async def create_from_upload(
    db: AsyncSession, project: Project, upload, settings_dict: dict | None
) -> Asset:
    """Called once the uploaded object has landed in storage."""
    parsed = NewAssetSettings.model_validate(settings_dict or {})
    asset = Asset(
        project_id=project.id,
        status="preparing",
        source_key=upload.object_key,
        upload_id=upload.id,
    )
    _apply_settings(asset, parsed)
    asset.test = asset.test or upload.test
    db.add(asset)
    await db.flush()

    _attach_playback_ids(db, asset, parsed.playback_policy)
    await db.flush()

    await events.emit(
        db,
        project_id=project.id,
        event_type="video.asset.created",
        object_type="asset",
        object_id=asset.id,
        data={"status": asset.status, "upload_id": upload.id},
    )
    await jobs.enqueue(
        db,
        jobs.PROBE_AND_TRANSCODE,
        {"asset_id": asset.id},
        idempotency_key=f"transcode:{asset.id}",
        project_id=project.id,
        lease_seconds=7200,
    )
    return await load_relations(db, asset)


async def create_clip(
    db: AsyncSession, project: Project, source: Asset, body: ClipCreate
) -> Asset:
    if source.status != "ready":
        raise Conflict("an asset can only be clipped once it is ready")
    if source.duration and body.start_time >= source.duration:
        raise InvalidParameters(
            f"start_time {body.start_time}s is beyond the source duration of {source.duration:.3f}s"
        )

    end = min(body.end_time, source.duration) if source.duration else body.end_time
    clip = Asset(
        project_id=project.id,
        status="preparing",
        source_asset_id=source.id,
        clip_start=body.start_time,
        clip_end=end,
        mp4_support=body.mp4_support,
        passthrough=body.passthrough,
        normalize_audio=source.normalize_audio,
        encoding_tier=source.encoding_tier,
        test=source.test,
    )
    db.add(clip)
    await db.flush()
    _attach_playback_ids(db, clip, body.playback_policy)
    await db.flush()

    await events.emit(
        db,
        project_id=project.id,
        event_type="video.asset.created",
        object_type="asset",
        object_id=clip.id,
        data={"status": clip.status, "source_asset_id": source.id},
    )
    await jobs.enqueue(
        db,
        jobs.CREATE_CLIP,
        {"asset_id": clip.id, "source_asset_id": source.id},
        idempotency_key=f"clip:{clip.id}",
        project_id=project.id,
        lease_seconds=7200,
    )
    return await load_relations(db, clip)


async def get_for_project(db: AsyncSession, project_id: str, asset_id: str) -> Asset:
    asset = await db.scalar(
        select(Asset).where(
            Asset.id == asset_id,
            Asset.project_id == project_id,
            Asset.status != "deleted",
        )
    )
    if asset is None:
        raise NotFound(f"asset {asset_id} was not found")
    return asset


async def list_for_project(
    db: AsyncSession, project_id: str, limit: int, offset: int
) -> tuple[list[Asset], int]:
    base = select(Asset).where(Asset.project_id == project_id, Asset.status != "deleted")
    total = await db.scalar(
        select(func.count()).select_from(base.subquery())
    )
    rows = (
        await db.scalars(
            base.order_by(Asset.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def soft_delete(db: AsyncSession, asset: Asset) -> None:
    """Tombstone the row, emit the event, then reclaim storage asynchronously. Flipping
    status first means playback stops immediately even if object deletion lags."""
    asset.status = "deleted"
    asset.updated_at = utcnow()
    await db.flush()
    await events.emit(
        db,
        project_id=asset.project_id,
        event_type="video.asset.deleted",
        object_type="asset",
        object_id=asset.id,
        data={},
    )
    await jobs.enqueue(
        db,
        "asset.purge_storage",
        {
            "asset_id": asset.id,
            "prefix": storage.asset_prefix(asset.project_id, asset.id),
        },
        idempotency_key=f"purge:{asset.id}",
        project_id=asset.project_id,
        priority=200,
    )


async def mark_ready(db: AsyncSession, asset: Asset) -> None:
    asset.status = "ready"
    asset.ready_at = utcnow()
    asset.error_type = None
    asset.error_messages = None
    await db.flush()
    await events.emit(
        db,
        project_id=asset.project_id,
        event_type="video.asset.ready",
        object_type="asset",
        object_id=asset.id,
        data={
            "status": "ready",
            "duration": asset.duration,
            "aspect_ratio": asset.aspect_ratio,
            "max_stored_resolution": asset.max_stored_resolution,
            "playback_ids": [
                {"id": p.id, "policy": p.policy} for p in (asset.playback_ids or [])
            ],
        },
    )


async def mark_errored(
    db: AsyncSession, asset: Asset, error_type: str, messages: list[str]
) -> None:
    asset.status = "errored"
    asset.errored_at = utcnow()
    asset.error_type = error_type
    asset.error_messages = messages
    await db.flush()
    await events.emit(
        db,
        project_id=asset.project_id,
        event_type="video.asset.errored",
        object_type="asset",
        object_id=asset.id,
        data={"status": "errored", "errors": {"type": error_type, "messages": messages}},
    )
