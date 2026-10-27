"""The headline test: a byte of video goes in one end and plays out the other.

This is the workflow that has to work before anything else matters, so it is asserted
end-to-end rather than in pieces — presigned upload, reconcile, probe, ABR transcode,
manifest rewriting, segment delivery, thumbnails, and the webhooks that announce it all.
"""

from __future__ import annotations

import pytest

from app.core.security import verify_webhook
from app.workers.runner import drain_once
from tests.conftest import put_to_upload_url as _put_object
from tests.webhook_receiver import WebhookReceiver


@pytest.mark.asyncio
async def test_upload_to_playback(client, account, sample_video):
    receiver = await WebhookReceiver().start()
    try:
        r = await client.post(
            f"/v1/projects/{account.project_id}/webhooks",
            json={"url": receiver.url, "events": []},
            headers=account.bearer,
        )
        assert r.status_code == 201, r.text
        secret = r.json()["data"]["secret"]

        # 1. Ask for an upload URL.
        r = await client.post(
            "/v1/video/uploads",
            json={
                "timeout": 3600,
                "new_asset_settings": {
                    "playback_policy": ["public"],
                    "mp4_support": "standard",
                    "normalize_audio": True,
                },
            },
            auth=account.basic,
        )
        assert r.status_code == 201, r.text
        upload = r.json()["data"]
        assert upload["status"] == "waiting"
        assert upload["url"], "a presigned upload URL should be returned"

        # 2. PUT the bytes.
        await _put_object(client, upload["url"], sample_video.read_bytes())

        # 3. Polling the upload promotes it to an asset.
        r = await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
        assert r.status_code == 200, r.text
        promoted = r.json()["data"]
        assert promoted["status"] == "asset_created"
        assert promoted["url"] is None, "a spent upload URL must not be handed back"
        asset_id = promoted["asset_id"]

        # 4. Run the queue: ingest -> probe -> transcode -> mp4 -> storyboard.
        assert await drain_once() > 0

        r = await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
        assert r.status_code == 200, r.text
        asset = r.json()["data"]
        assert asset["status"] == "ready", asset.get("errors")
        assert asset["duration"] == pytest.approx(6.0, abs=0.5)
        assert asset["aspect_ratio"] == "16:9"
        assert asset["max_stored_resolution"] == "SD"
        assert {t["type"] for t in asset["tracks"]} == {"video", "audio"}
        assert asset["playback_ids"], "a ready asset must expose a playback id"
        playback_id = asset["playback_ids"][0]["id"]

        # 5. Master manifest lists the ladder, with URIs rewritten onto the playback id.
        r = await client.get(f"/v1/stream/{playback_id}.m3u8")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
        master = r.text
        assert "#EXTM3U" in master
        variants = [ln for ln in master.splitlines() if ln.endswith(".m3u8")]
        assert len(variants) >= 2, f"expected an ABR ladder, got {variants}"
        assert all(ln.startswith(f"{playback_id}/") for ln in variants), variants
        assert "\\" not in master, "playlist URIs must use forward slashes"

        # 6. Variant playlist, init segment, and a media segment all resolve.
        variant_path = variants[0]
        r = await client.get(f"/v1/stream/{variant_path}")
        assert r.status_code == 200, r.text
        playlist = r.text
        assert "#EXT-X-MAP:URI=" in playlist
        init_name = playlist.split('#EXT-X-MAP:URI="')[1].split('"')[0]
        segments = [ln for ln in playlist.splitlines() if ln.endswith(".m4s")]
        assert segments

        rendition = variant_path.split("/")[1].removesuffix("_playlist.m3u8")
        r = await client.get(f"/v1/stream/{playback_id}/{init_name}")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert r.content[:12].find(b"ftyp") > 0, "init segment should be an fMP4 header"

        r = await client.get(f"/v1/stream/{playback_id}/{segments[0]}")
        assert r.status_code == 200
        assert len(r.content) > 1000, "a media segment should carry real payload"

        # 7. Thumbnails render on demand and then come from cache.
        r = await client.get(f"/v1/stream/{playback_id}/thumbnail.jpg?time=2&width=320")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content[:2] == b"\xff\xd8", "expected JPEG magic bytes"
        assert r.headers["x-fluxora-cache"] == "miss"

        r = await client.get(f"/v1/stream/{playback_id}/thumbnail.jpg?time=2&width=320")
        assert r.headers["x-fluxora-cache"] == "hit"

        # 8. Progressive MP4 renditions exist (mp4_support: standard).
        r = await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
        renditions = {sr["name"]: sr for sr in r.json()["data"]["static_renditions"]}
        assert {"low", "medium", "high"} <= set(renditions), renditions
        assert all(sr["status"] == "ready" for sr in renditions.values())

        r = await client.get(f"/v1/stream/{playback_id}/medium.mp4")
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"

        # 9. Playback IDs resolve back to the asset without leaking it in the URL.
        r = await client.get(f"/v1/video/playback-ids/{playback_id}")
        assert r.json()["data"]["object"] == {"type": "asset", "id": asset_id}

        # 10. Webhooks arrived, signed, in lifecycle order.
        await drain_once()
        types = [rec.event["type"] for rec in receiver.received]
        assert "video.upload.created" in types
        assert "video.asset.created" in types
        assert "video.asset.ready" in types
        assert types.index("video.asset.created") < types.index("video.asset.ready")

        ready = receiver.events_of_type("video.asset.ready")[0]
        assert verify_webhook(secret, ready.body, ready.headers["fluxora-signature"])
        assert not verify_webhook("whsec_wrong", ready.body, ready.headers["fluxora-signature"])
        envelope = ready.event
        assert envelope["object"] == {"type": "asset", "id": asset_id}
        assert envelope["data"]["duration"] == pytest.approx(6.0, abs=0.5)
        assert envelope["environment"]["id"] == account.project_id
        assert ready.headers["fluxora-event-type"] == "video.asset.ready"
    finally:
        await receiver.stop()


@pytest.mark.asyncio
async def test_clip_produces_an_independent_playable_asset(client, account, sample_video):
    r = await client.post("/v1/video/uploads", json={}, auth=account.basic)
    upload = r.json()["data"]
    await _put_object(client, upload["url"], sample_video.read_bytes())
    await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    await drain_once()

    source_id = (
        await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    ).json()["data"]["asset_id"]

    r = await client.post(
        f"/v1/video/assets/{source_id}/clip",
        json={"start_time": 1.0, "end_time": 3.0, "playback_policy": ["public"]},
        auth=account.basic,
    )
    assert r.status_code == 201, r.text
    clip = r.json()["data"]
    assert clip["status"] == "preparing"
    assert clip["source_asset_id"] == source_id

    await drain_once()
    r = await client.get(f"/v1/video/assets/{clip['id']}", auth=account.basic)
    ready = r.json()["data"]
    assert ready["status"] == "ready", ready.get("errors")
    assert ready["duration"] == pytest.approx(2.0, abs=0.4)

    # The clip plays on its own playback id, independent of the source.
    r = await client.get(f"/v1/stream/{ready['playback_ids'][0]['id']}.m3u8")
    assert r.status_code == 200
    assert "#EXTM3U" in r.text


@pytest.mark.asyncio
async def test_test_asset_duration_is_capped(client, account, sample_video):
    r = await client.post(
        "/v1/video/uploads",
        json={"test": True, "new_asset_settings": {"test": True}},
        auth=account.basic,
    )
    upload = r.json()["data"]
    await _put_object(client, upload["url"], sample_video.read_bytes())
    await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    await drain_once()

    asset_id = (
        await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    ).json()["data"]["asset_id"]
    asset = (
        await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
    ).json()["data"]
    assert asset["status"] == "ready", asset.get("errors")
    assert asset["test"] is True
    assert asset["duration"] <= 10.5, "a test asset should be duration-capped"
