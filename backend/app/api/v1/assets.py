from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from app.api.deps import ApiProject, DbSession, Pagination
from app.core.errors import Conflict, InvalidParameters, NotFound
from app.models import Asset, AssetTrack, PlaybackId, utcnow
from app.schemas.common import envelope
from app.schemas.video import (
    AssetCreate,
    AssetOut,
    AssetUpdate,
    ClipCreate,
    PlaybackIdCreate,
    PlaybackIdOut,
    TrackCreate,
    TrackOut,
)
from app.services import assets as asset_service
from app.services import events, jobs

router = APIRouter(prefix="/video/assets", tags=["assets"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_asset(body: AssetCreate, project: ApiProject, db: DbSession):
    asset = await asset_service.create_from_request(db, project, body)
    return envelope(AssetOut.of(asset))


@router.get("")
async def list_assets(
    project: ApiProject, db: DbSession, page: Pagination, response: Response
):
    rows, total = await asset_service.list_for_project(
        db, project.id, page.limit, page.offset
    )
    response.headers["X-Total-Count"] = str(total)
    return envelope([AssetOut.of(a) for a in rows])


@router.get("/{asset_id}")
async def get_asset(asset_id: str, project: ApiProject, db: DbSession):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    return envelope(AssetOut.of(asset))


@router.patch("/{asset_id}")
async def update_asset(
    asset_id: str, body: AssetUpdate, project: ApiProject, db: DbSession
):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    changed: dict = {}

    if body.passthrough is not None:
        asset.passthrough = body.passthrough
        changed["passthrough"] = body.passthrough

    if body.mp4_support is not None and body.mp4_support != asset.mp4_support:
        asset.mp4_support = body.mp4_support
        changed["mp4_support"] = body.mp4_support
        if body.mp4_support != "none":
            if asset.status != "ready":
                raise Conflict("mp4_support can only be enabled on a ready asset")
            await jobs.enqueue(
                db,
                jobs.RENDER_MP4,
                {"asset_id": asset.id, "mp4_support": body.mp4_support},
                # Keyed on the requested level so flipping standard -> capped-1080p
                # enqueues real work instead of being swallowed as a duplicate.
                idempotency_key=f"mp4:{asset.id}:{body.mp4_support}",
                project_id=project.id,
                lease_seconds=7200,
            )

    if body.master_access is not None:
        asset.master_access = body.master_access
        changed["master_access"] = body.master_access

    asset.updated_at = utcnow()
    await db.flush()

    if changed:
        await events.emit(
            db,
            project_id=project.id,
            event_type="video.asset.updated",
            object_type="asset",
            object_id=asset.id,
            data=changed,
        )
    return envelope(AssetOut.of(asset))


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(asset_id: str, project: ApiProject, db: DbSession):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    await asset_service.soft_delete(db, asset)


@router.get("/{asset_id}/input-info")
async def asset_input_info(asset_id: str, project: ApiProject, db: DbSession):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    if asset.input_info is None:
        raise NotFound("input info is not available until the asset has been probed")
    return envelope(asset.input_info)


# ---- Playback IDs ----

@router.post("/{asset_id}/playback-ids", status_code=status.HTTP_201_CREATED)
async def create_playback_id(
    asset_id: str, body: PlaybackIdCreate, project: ApiProject, db: DbSession
):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    playback_id = PlaybackId(asset_id=asset.id, policy=body.policy)
    db.add(playback_id)
    await db.flush()
    return envelope(PlaybackIdOut.model_validate(playback_id))


@router.delete(
    "/{asset_id}/playback-ids/{playback_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_playback_id(
    asset_id: str, playback_id: str, project: ApiProject, db: DbSession
):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    row = await db.scalar(
        select(PlaybackId).where(
            PlaybackId.id == playback_id, PlaybackId.asset_id == asset.id
        )
    )
    if row is None:
        raise NotFound(f"playback id {playback_id} was not found on this asset")
    await db.delete(row)


# ---- Text / audio tracks ----

@router.post("/{asset_id}/tracks", status_code=status.HTTP_201_CREATED)
async def create_track(
    asset_id: str, body: TrackCreate, project: ApiProject, db: DbSession
):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    if asset.status != "ready":
        raise Conflict("tracks can only be added to a ready asset")

    track = AssetTrack(
        asset_id=asset.id,
        type=body.type,
        status="preparing",
        text_type=body.text_type,
        language_code=body.language_code,
        name=body.name or body.language_code,
        closed_captions=body.closed_captions,
        source_url=str(body.url),
    )
    db.add(track)
    await db.flush()

    await events.emit(
        db,
        project_id=project.id,
        event_type="video.asset.track.created",
        object_type="asset",
        object_id=asset.id,
        data={"track_id": track.id, "type": track.type, "language_code": track.language_code},
    )
    await jobs.enqueue(
        db,
        jobs.INGEST_TEXT_TRACK,
        {"asset_id": asset.id, "track_id": track.id},
        idempotency_key=f"track:{track.id}",
        project_id=project.id,
    )
    return envelope(TrackOut.model_validate(track))


@router.delete("/{asset_id}/tracks/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_track(
    asset_id: str, track_id: str, project: ApiProject, db: DbSession
):
    asset = await asset_service.get_for_project(db, project.id, asset_id)
    track = await db.scalar(
        select(AssetTrack).where(AssetTrack.id == track_id, AssetTrack.asset_id == asset.id)
    )
    if track is None:
        raise NotFound(f"track {track_id} was not found on this asset")
    if track.type == "video":
        raise InvalidParameters("the video track cannot be removed from an asset")
    await db.delete(track)


# ---- Clipping ----

@router.post("/{asset_id}/clip", status_code=status.HTTP_201_CREATED)
async def clip_asset(
    asset_id: str, body: ClipCreate, project: ApiProject, db: DbSession
):
    source = await asset_service.get_for_project(db, project.id, asset_id)
    clip = await asset_service.create_clip(db, project, source, body)
    return envelope(AssetOut.of(clip))


# ---- Master access ----

@router.get("/{asset_id}/master")
async def get_master(asset_id: str, project: ApiProject, db: DbSession):
    """Temporary signed URL to the original upload, for highest-quality download."""
    from app.services import storage

    asset = await asset_service.get_for_project(db, project.id, asset_id)
    if asset.master_access != "temporary":
        raise Conflict("set master_access to 'temporary' before requesting the master")
    if not asset.source_key:
        raise NotFound("this asset has no retained source object")
    url = await storage.get_storage().presign_get(asset.source_key, expires_in=86400)
    return envelope({"master": {"status": "ready", "url": url}})
