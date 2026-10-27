"""Job handlers: the expensive half of the pipeline.

Every handler is written to be safely re-run. A retry re-derives its outputs and writes
them to deterministic storage keys, overwriting whatever a crashed earlier attempt left
behind, rather than appending a second copy (ADR-0001 decision 3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import sign_webhook
from app.models import (
    Asset,
    AssetTrack,
    DirectUpload,
    Event,
    Job,
    Project,
    StaticRendition,
    UsageRecord,
    WebhookDelivery,
    WebhookEndpoint,
    utcnow,
)
from app.services import assets as asset_service
from app.services import events as event_service
from app.services import jobs, media, storage, uploads
from app.workers.ai_handlers import analyze_asset

log = logging.getLogger("fluxora.worker")

DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=600.0)
MAX_SOURCE_BYTES = 10 * 1024 * 1024 * 1024


def _scratch() -> TemporaryDirectory:
    Path(settings.media_scratch_dir).mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(dir=settings.media_scratch_dir)


async def _get_asset(db: AsyncSession, asset_id: str) -> Asset | None:
    return await db.scalar(select(Asset).where(Asset.id == asset_id))


async def _record_usage(db: AsyncSession, project_id: str, metric: str, value: float) -> None:
    day = utcnow().strftime("%Y-%m-%d")
    record_id = f"{project_id}:{day}:{metric}"
    existing = await db.scalar(select(UsageRecord).where(UsageRecord.id == record_id))
    if existing is None:
        db.add(
            UsageRecord(
                id=record_id, project_id=project_id, day=day, metric=metric, value=value
            )
        )
    else:
        existing.value = (existing.value or 0.0) + value
    await db.flush()


# ---- Ingest ----

async def ingest_from_url(db: AsyncSession, job: Job) -> dict:
    """Pull a web-addressable source into our own storage before touching it, so the
    transcode never depends on a third-party URL still being up on retry."""
    asset = await _get_asset(db, job.payload["asset_id"])
    if asset is None or asset.status == "deleted":
        return {"skipped": "asset is gone"}
    if asset.source_key and await storage.get_storage().exists(asset.source_key):
        return await probe_and_transcode(db, job)

    key = storage.source_key(asset.project_id, asset.id, "source")
    with _scratch() as tmp:
        dest = Path(tmp) / "source"
        try:
            async with httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as client:
                async with client.stream("GET", asset.source_url) as response:
                    response.raise_for_status()
                    declared = int(response.headers.get("content-length") or 0)
                    if declared > MAX_SOURCE_BYTES:
                        raise ValueError(
                            f"source is {declared} bytes, over the {MAX_SOURCE_BYTES} limit"
                        )
                    written = 0
                    with dest.open("wb") as fh:
                        async for chunk in response.aiter_bytes(1024 * 256):
                            written += len(chunk)
                            if written > MAX_SOURCE_BYTES:
                                raise ValueError("source exceeded the maximum size mid-stream")
                            fh.write(chunk)
        except (httpx.HTTPError, ValueError) as exc:
            await asset_service.mark_errored(
                db, asset, "invalid_input", [f"could not fetch the source: {exc}"]
            )
            await db.commit()
            return {"errored": str(exc)}

        size = await storage.get_storage().put_file(key, dest, "application/octet-stream")

    asset.source_key = key
    asset.source_bytes = size
    await db.flush()
    await db.commit()
    return await probe_and_transcode(db, job)


# ---- Transcode ----

async def probe_and_transcode(db: AsyncSession, job: Job) -> dict:
    asset = await _get_asset(db, job.payload["asset_id"])
    if asset is None or asset.status == "deleted":
        return {"skipped": "asset is gone"}
    if asset.status == "ready" and asset.hls_key:
        return {"skipped": "already ready"}
    if not asset.source_key:
        await asset_service.mark_errored(
            db, asset, "invalid_input", ["the asset has no stored source object"]
        )
        await db.commit()
        return {"errored": "no source"}

    store = storage.get_storage()
    with _scratch() as tmp:
        scratch = Path(tmp)
        source = await store.download(asset.source_key, scratch / "source")

        try:
            probe = await media.probe(source)
        except media.MediaError as exc:
            await asset_service.mark_errored(
                db, asset, "invalid_input",
                [f"the source could not be decoded: {exc}", exc.stderr_tail[-500:]],
            )
            await db.commit()
            return {"errored": "undecodable"}

        if not probe.has_video:
            await asset_service.mark_errored(
                db, asset, "invalid_input", ["the source contains no video track"]
            )
            await db.commit()
            return {"errored": "no video track"}

        duration = probe.duration
        trimmed = source
        # Test assets are duration-capped so they stay cheap; this mirrors the documented
        # behaviour of test inputs on the platform we modelled.
        if asset.test and duration > settings.test_asset_max_duration_seconds:
            trimmed = scratch / "trimmed.mp4"
            await media.cut_clip(source, trimmed, 0, settings.test_asset_max_duration_seconds)
            duration = settings.test_asset_max_duration_seconds

        asset.duration = duration
        asset.aspect_ratio = probe.aspect_ratio
        asset.max_stored_resolution = probe.max_stored_resolution
        asset.max_stored_frame_rate = probe.frame_rate
        asset.input_info = {
            "file": {
                "container_format": probe.raw.get("format", {}).get("format_name"),
                "bytes": asset.source_bytes,
                "duration": probe.duration,
                "tracks": [
                    {
                        "type": s.get("codec_type"),
                        "codec": s.get("codec_name"),
                        "width": s.get("width"),
                        "height": s.get("height"),
                        "channels": s.get("channels"),
                        "sample_rate": s.get("sample_rate"),
                    }
                    for s in probe.raw.get("streams", [])
                ],
            },
            "settings": {"url": asset.source_url} if asset.source_url else {},
        }
        await _sync_source_tracks(db, asset, probe)
        await db.flush()
        await jobs.heartbeat(db, job.id, 0.15)

        rungs = media.select_ladder(probe.height, asset.encoding_tier)
        loudnorm = None
        if asset.normalize_audio and probe.has_audio:
            loudnorm = media.build_loudnorm(await media.measure_loudness(trimmed))
            await jobs.heartbeat(db, job.id, 0.3)

        hls_dir = scratch / "hls"
        try:
            await media.transcode_hls(trimmed, hls_dir, rungs, probe.has_audio, loudnorm)
        except media.MediaError as exc:
            await asset_service.mark_errored(
                db, asset, "transcode_failed",
                [f"transcoding failed: {exc}", exc.stderr_tail[-500:]],
            )
            await db.commit()
            return {"errored": "transcode failed"}

        await jobs.heartbeat(db, job.id, 0.8)
        uploaded = await _upload_hls(store, hls_dir, asset)

        # Verify before declaring readiness: an asset that reports `ready` while a rung is
        # missing from storage is worse than one that reports an error.
        master_key = storage.master_manifest_key(asset.project_id, asset.id)
        if not await store.exists(master_key):
            await asset_service.mark_errored(
                db, asset, "transcode_failed", ["the master playlist did not reach storage"]
            )
            await db.commit()
            return {"errored": "master missing"}

        asset.hls_key = master_key
        await _replace_video_tracks(db, asset, rungs, probe, duration)
        await asset_service.mark_ready(db, asset)
        await _record_usage(db, asset.project_id, "encoding_minutes", duration / 60.0)
        await db.commit()

    if asset.mp4_support != "none":
        await jobs.enqueue(
            db, jobs.RENDER_MP4,
            {"asset_id": asset.id, "mp4_support": asset.mp4_support},
            idempotency_key=f"mp4:{asset.id}:{asset.mp4_support}",
            project_id=asset.project_id, lease_seconds=7200,
        )
    await jobs.enqueue(
        db, jobs.RENDER_STORYBOARD, {"asset_id": asset.id},
        idempotency_key=f"storyboard:{asset.id}",
        project_id=asset.project_id, priority=150, lease_seconds=1800,
    )
    for track in asset.tracks:
        if track.type == "text" and track.status == "preparing" and track.source_url:
            await jobs.enqueue(
                db, jobs.INGEST_TEXT_TRACK,
                {"asset_id": asset.id, "track_id": track.id},
                idempotency_key=f"track:{track.id}", project_id=asset.project_id,
            )
    await db.commit()
    return {"renditions": [r.name for r in rungs], "files": uploaded, "duration": duration}


async def _upload_hls(store, hls_dir: Path, asset: Asset) -> int:
    count = 0
    for path in sorted(hls_dir.iterdir()):
        if not path.is_file():
            continue
        key = storage.hls_file_key(asset.project_id, asset.id, path.name)
        content_type = (
            "application/vnd.apple.mpegurl" if path.suffix == ".m3u8"
            else "video/mp4" if path.suffix == ".mp4"
            else "video/iso.segment"
        )
        await store.put_file(key, path, content_type)
        count += 1
    return count


async def _sync_source_tracks(db: AsyncSession, asset: Asset, probe: media.ProbeResult) -> None:
    """Record the audio track discovered in the source. Video tracks are written after
    transcoding, where the real output dimensions are known."""
    if not probe.has_audio:
        return
    existing = await db.scalar(
        select(AssetTrack).where(AssetTrack.asset_id == asset.id, AssetTrack.type == "audio")
    )
    track = existing or AssetTrack(asset_id=asset.id, type="audio")
    track.status = "ready"
    track.duration = probe.duration
    track.max_channels = probe.audio_channels
    track.sample_rate = probe.audio_sample_rate
    track.codec = "aac"
    if existing is None:
        db.add(track)


async def _replace_video_tracks(
    db: AsyncSession, asset: Asset, rungs: list[media.Rung], probe: media.ProbeResult,
    duration: float,
) -> None:
    for old in list(asset.tracks):
        if old.type == "video":
            await db.delete(old)
    top = rungs[-1]
    width = int(probe.width * top.height / probe.height) if probe.height else top.height
    db.add(
        AssetTrack(
            asset_id=asset.id,
            type="video",
            status="ready",
            duration=duration,
            max_width=width - (width % 2),
            max_height=top.height,
            max_frame_rate=min(probe.frame_rate or 24, 24),
            codec="h264",
            bitrate=top.video_bitrate_k * 1000,
        )
    )
    await db.flush()


# ---- Derived outputs ----

async def render_mp4(db: AsyncSession, job: Job) -> dict:
    asset = await _get_asset(db, job.payload["asset_id"])
    if asset is None or asset.status != "ready":
        return {"skipped": "asset is not ready"}

    level = job.payload.get("mp4_support", asset.mp4_support)
    wanted = ["low", "medium", "high"] if level == "standard" else ["low", "medium", "high", "highest"]
    store = storage.get_storage()

    with _scratch() as tmp:
        scratch = Path(tmp)
        source = await store.download(asset.source_key, scratch / "source")
        made = []
        for name in wanted:
            rung = next(r for r in media.LADDER if r.name == media.MP4_RENDITIONS[name])
            if asset.max_stored_resolution and rung.height > 1080 and level == "capped-1080p":
                continue
            existing = await db.scalar(
                select(StaticRendition).where(
                    StaticRendition.asset_id == asset.id, StaticRendition.name == name
                )
            )
            rendition = existing or StaticRendition(asset_id=asset.id, name=name, ext="mp4")
            rendition.status = "preparing"
            if existing is None:
                db.add(rendition)
            await db.flush()

            dest = scratch / f"{name}.mp4"
            await media.render_mp4(source, dest, rung)
            key = storage.mp4_key(asset.project_id, asset.id, name)
            size = await store.put_file(key, dest, "video/mp4")

            rendition.object_key = key
            rendition.filesize = size
            rendition.height = rung.height
            rendition.bitrate = rung.video_bitrate_k * 1000
            rendition.status = "ready"
            await db.flush()
            await event_service.emit(
                db,
                project_id=asset.project_id,
                event_type="video.asset.static_rendition.ready",
                object_type="asset",
                object_id=asset.id,
                data={"name": name, "ext": "mp4", "filesize": size},
            )
            made.append(name)
            await jobs.heartbeat(db, job.id, len(made) / max(len(wanted), 1))
            await db.commit()
    return {"renditions": made}


async def render_storyboard(db: AsyncSession, job: Job) -> dict:
    asset = await _get_asset(db, job.payload["asset_id"])
    if asset is None or asset.status != "ready" or not asset.duration:
        return {"skipped": "asset is not ready"}

    store = storage.get_storage()
    prefix = storage.asset_prefix(asset.project_id, asset.id)
    with _scratch() as tmp:
        scratch = Path(tmp)
        source = await store.download(asset.source_key, scratch / "source")
        image = scratch / "storyboard.jpg"
        _, vtt = await media.build_storyboard(source, image, asset.duration)
        await store.put_file(f"{prefix}/storyboard.jpg", image, "image/jpeg")
        await store.put_bytes(f"{prefix}/storyboard.vtt", vtt.encode(), "text/vtt")
    return {"storyboard": "ready"}


async def ingest_text_track(db: AsyncSession, job: Job) -> dict:
    """Fetch a sidecar subtitle file and normalise it to WEBVTT, which is the only text
    format HLS players are required to support."""
    track = await db.scalar(
        select(AssetTrack).where(AssetTrack.id == job.payload["track_id"])
    )
    if track is None:
        return {"skipped": "track is gone"}
    asset = await _get_asset(db, job.payload["asset_id"])
    if asset is None or asset.status == "deleted":
        return {"skipped": "asset is gone"}

    with _scratch() as tmp:
        scratch = Path(tmp)
        raw = scratch / "subtitle.input"
        try:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(track.source_url)
                response.raise_for_status()
                if len(response.content) > 10 * 1024 * 1024:
                    raise ValueError("subtitle file is larger than 10 MB")
                raw.write_bytes(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            track.status = "errored"
            track.error_messages = [f"could not fetch the subtitle file: {exc}"]
            await db.flush()
            await event_service.emit(
                db, project_id=asset.project_id,
                event_type="video.asset.track.errored",
                object_type="asset", object_id=asset.id,
                data={"track_id": track.id, "errors": track.error_messages},
            )
            await db.commit()
            return {"errored": str(exc)}

        vtt = scratch / "subtitle.vtt"
        try:
            await media._run(
                [settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y",
                 "-i", str(raw), str(vtt)],
                timeout=120,
            )
        except media.MediaError as exc:
            track.status = "errored"
            track.error_messages = [f"the subtitle file could not be converted: {exc}"]
            await db.flush()
            await db.commit()
            return {"errored": "conversion failed"}

        key = storage.track_key(asset.project_id, asset.id, track.id, "vtt")
        await storage.get_storage().put_file(key, vtt, "text/vtt")

    track.object_key = key
    track.status = "ready"
    track.error_messages = None
    await db.flush()
    await event_service.emit(
        db, project_id=asset.project_id,
        event_type="video.asset.track.ready",
        object_type="asset", object_id=asset.id,
        data={"track_id": track.id, "language_code": track.language_code},
    )
    await db.commit()
    return {"track": track.id}


async def create_clip(db: AsyncSession, job: Job) -> dict:
    clip = await _get_asset(db, job.payload["asset_id"])
    source = await _get_asset(db, job.payload["source_asset_id"])
    if clip is None or clip.status == "deleted":
        return {"skipped": "clip is gone"}
    if source is None or not source.source_key:
        await asset_service.mark_errored(
            db, clip, "invalid_input", ["the source asset is no longer available"]
        )
        await db.commit()
        return {"errored": "source gone"}

    store = storage.get_storage()
    key = storage.source_key(clip.project_id, clip.id, "source")
    if not await store.exists(key):
        with _scratch() as tmp:
            scratch = Path(tmp)
            original = await store.download(source.source_key, scratch / "source")
            cut = scratch / "clip.mp4"
            await media.cut_clip(original, cut, clip.clip_start or 0, clip.clip_end or 0)
            size = await store.put_file(key, cut, "video/mp4")
        clip.source_bytes = size

    clip.source_key = key
    await db.flush()
    await db.commit()

    # The clip is now an ordinary source object, so it follows the normal path.
    job.payload = {"asset_id": clip.id}
    return await probe_and_transcode(db, job)


async def purge_storage(db: AsyncSession, job: Job) -> dict:
    removed = await storage.get_storage().delete_prefix(job.payload["prefix"])
    return {"objects_removed": removed}


# ---- Webhook delivery ----

async def deliver_webhook(db: AsyncSession, job: Job) -> dict:
    delivery = await db.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == job.payload["delivery_id"])
    )
    if delivery is None:
        return {"skipped": "delivery is gone"}
    if delivery.status == "delivered":
        return {"skipped": "already delivered"}

    endpoint = await db.scalar(
        select(WebhookEndpoint).where(WebhookEndpoint.id == delivery.endpoint_id)
    )
    event = await db.scalar(select(Event).where(Event.id == delivery.event_id))
    if endpoint is None or event is None:
        delivery.status = "failed"
        delivery.error = "endpoint or event no longer exists"
        await db.commit()
        return {"skipped": "endpoint or event is gone"}
    if not endpoint.active:
        delivery.status = "failed"
        delivery.error = "endpoint is disabled"
        await db.commit()
        return {"skipped": "endpoint disabled"}

    project = await db.scalar(select(Project).where(Project.id == endpoint.project_id))
    body = event_service.canonical_body(event, project)
    signature = sign_webhook(endpoint.secret, body)

    delivery.attempts += 1
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(
                endpoint.url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Fluxora-Signature": signature,
                    "User-Agent": "Fluxora-Webhooks/1.0",
                    "Fluxora-Event-Id": event.id,
                    "Fluxora-Event-Type": event.type,
                },
            )
        delivery.response_status = response.status_code
        delivery.response_body = response.text[:2000]
        ok = 200 <= response.status_code < 300
        error = None if ok else f"endpoint returned HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        ok, error = False, f"request failed: {exc}"
        delivery.response_status = None

    if ok:
        delivery.status = "delivered"
        delivery.delivered_at = utcnow()
        delivery.next_attempt_at = None
        delivery.error = None
        await db.commit()
        return {"delivered": delivery.id, "status": delivery.response_status}

    delivery.error = error
    retry_in = event_service.next_retry_delay(delivery.attempts)
    if retry_in is None:
        delivery.status = "exhausted"
        delivery.next_attempt_at = None
        await db.commit()
        log.warning("webhook %s exhausted after %d attempts", delivery.id, delivery.attempts)
        return {"exhausted": delivery.id}

    delivery.status = "pending"
    delivery.next_attempt_at = utcnow() + retry_in
    await db.commit()
    raise jobs.RetryLater(int(retry_in.total_seconds()), error or "delivery failed")


# ---- Maintenance ----

async def reap_uploads(db: AsyncSession, job: Job) -> dict:
    """Reconcile waiting uploads against storage, independently of any client callback."""
    promoted = timed_out = 0
    for upload in await uploads.find_waiting(db, limit=200):
        project = await db.scalar(select(Project).where(Project.id == upload.project_id))
        if project is None:
            continue
        before = upload.status
        await uploads.reconcile(db, upload, project)
        if upload.status == "asset_created":
            promoted += 1
        elif upload.status == "timed_out" and before != "timed_out":
            timed_out += 1
    await db.commit()
    return {"promoted": promoted, "timed_out": timed_out}


async def rollup_usage(db: AsyncSession, job: Job) -> dict:
    """Storage metering: sum retained source bytes per project into GB-hours for the day."""
    from sqlalchemy import func

    rows = (
        await db.execute(
            select(Asset.project_id, func.sum(Asset.source_bytes))
            .where(Asset.status == "ready", Asset.source_bytes.is_not(None))
            .group_by(Asset.project_id)
        )
    ).all()
    for project_id, total_bytes in rows:
        gb = float(total_bytes or 0) / (1024**3)
        await _record_usage(db, project_id, "storage_gb_hours", gb * 24)
    await db.commit()
    return {"projects": len(rows)}


HANDLERS = {
    jobs.INGEST_FROM_URL: ingest_from_url,
    jobs.PROBE_AND_TRANSCODE: probe_and_transcode,
    jobs.RENDER_MP4: render_mp4,
    jobs.RENDER_STORYBOARD: render_storyboard,
    jobs.INGEST_TEXT_TRACK: ingest_text_track,
    jobs.CREATE_CLIP: create_clip,
    jobs.DELIVER_WEBHOOK: deliver_webhook,
    jobs.REAP_UPLOADS: reap_uploads,
    jobs.ROLLUP_USAGE: rollup_usage,
    jobs.AI_ANALYZE: analyze_asset,
    "asset.purge_storage": purge_storage,
}
