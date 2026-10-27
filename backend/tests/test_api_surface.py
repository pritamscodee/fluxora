"""Auth, authorisation, validation, and the failure paths.

These are the tests that matter for a public API: not that the happy path works, but that
a caller cannot reach another tenant's data, replay a token out of context, or get a
confusing error when they send something wrong.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.conftest import Account, put_to_upload_url


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


# ---- Authentication ----

@pytest.mark.asyncio
async def test_register_rejects_a_weak_password(client):
    r = await client.post(
        "/v1/auth/register", json={"email": _email(), "password": "short"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "invalid_parameters"


@pytest.mark.asyncio
async def test_duplicate_registration_does_not_confirm_the_address(client):
    email = _email()
    first = await client.post(
        "/v1/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    assert first.status_code == 201

    second = await client.post(
        "/v1/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    wrong_password = await client.post(
        "/v1/auth/login", json={"email": email, "password": "not-the-password"}
    )
    # Identical status and error type, so registration cannot enumerate accounts.
    assert second.status_code == wrong_password.status_code == 401
    assert second.json()["error"]["type"] == wrong_password.json()["error"]["type"]


@pytest.mark.asyncio
async def test_refresh_rotates_and_reuse_revokes_the_family(client):
    email = _email()
    r = await client.post(
        "/v1/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    original = r.json()["data"]["refresh_token"]

    r = await client.post("/v1/auth/refresh", json={"refresh_token": original})
    assert r.status_code == 200
    rotated = r.json()["data"]["refresh_token"]
    assert rotated != original

    # Replaying the spent token is treated as a leak: everything is revoked.
    r = await client.post("/v1/auth/refresh", json={"refresh_token": original})
    assert r.status_code == 401

    r = await client.post("/v1/auth/refresh", json={"refresh_token": rotated})
    assert r.status_code == 401, "reuse detection should revoke the rotated token too"


@pytest.mark.asyncio
async def test_access_token_is_not_accepted_as_a_refresh_token(client, account):
    r = await client.post(
        "/v1/auth/refresh", json={"refresh_token": account.access_token}
    )
    assert r.status_code == 401
    assert "refresh" in r.json()["error"]["messages"][0]


@pytest.mark.asyncio
async def test_dashboard_token_cannot_drive_the_video_api(client, account):
    """The two credential systems are deliberately disjoint (ADR-0001 decision 6)."""
    r = await client.post("/v1/video/uploads", json={}, headers=account.bearer)
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "unauthorized"


@pytest.mark.asyncio
async def test_api_key_cannot_reach_dashboard_routes(client, account):
    r = await client.get("/v1/auth/me", auth=account.basic)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_revoked_api_key_is_rejected(client, account):
    r = await client.delete(
        f"/v1/projects/{account.project_id}/api-keys/{account.key_id}",
        headers=account.bearer,
    )
    assert r.status_code == 204

    r = await client.post("/v1/video/uploads", json={}, auth=account.basic)
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "forbidden"


@pytest.mark.asyncio
async def test_wrong_api_secret_is_rejected(client, account):
    r = await client.post(
        "/v1/video/uploads", json={}, auth=httpx.BasicAuth(account.key_id, "wrong")
    )
    assert r.status_code == 401


# ---- Tenant isolation ----

@pytest.mark.asyncio
async def test_a_project_is_invisible_to_another_user(client, account):
    other = await Account(client).bootstrap(_email())

    r = await client.get(f"/v1/projects/{account.project_id}", headers=other.bearer)
    assert r.status_code == 404, "another tenant's project must not be confirmable"

    r = await client.post(
        f"/v1/projects/{account.project_id}/api-keys",
        json={"name": "stolen"},
        headers=other.bearer,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_assets_are_scoped_to_the_api_key(client, account, sample_video):
    from app.workers.runner import drain_once

    r = await client.post("/v1/video/uploads", json={}, auth=account.basic)
    upload = r.json()["data"]
    await put_to_upload_url(client, upload["url"], sample_video.read_bytes())
    await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    await drain_once()
    asset_id = (
        await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    ).json()["data"]["asset_id"]

    other = await Account(client).bootstrap(_email())
    r = await client.get(f"/v1/video/assets/{asset_id}", auth=other.basic)
    assert r.status_code == 404

    r = await client.delete(f"/v1/video/assets/{asset_id}", auth=other.basic)
    assert r.status_code == 404

    # The owner still sees it, so the 404 above is isolation and not a broken fixture.
    r = await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
    assert r.status_code == 200


# ---- Validation ----

@pytest.mark.asyncio
async def test_asset_create_requires_exactly_one_video_input(client, account):
    r = await client.post(
        "/v1/video/assets",
        json={"input": [{"url": "https://example.com/a.mp4"}, {"url": "https://example.com/b.mp4"}]},
        auth=account.basic,
    )
    assert r.status_code == 422
    assert "exactly one" in " ".join(r.json()["error"]["messages"])


@pytest.mark.asyncio
async def test_text_input_requires_a_language_code(client, account):
    r = await client.post(
        "/v1/video/assets",
        json={
            "input": [
                {"url": "https://example.com/a.mp4"},
                {"url": "https://example.com/a.vtt", "type": "text", "text_type": "subtitles"},
            ]
        },
        auth=account.basic,
    )
    assert r.status_code == 422
    assert "language_code" in " ".join(r.json()["error"]["messages"])


@pytest.mark.asyncio
async def test_unknown_fields_are_rejected_rather_than_ignored(client, account):
    r = await client.post(
        "/v1/video/uploads", json={"timeuot": 600}, auth=account.basic
    )
    assert r.status_code == 422, "a typo must not be silently accepted"


@pytest.mark.asyncio
async def test_clip_window_must_be_ordered(client, account):
    r = await client.post(
        "/v1/video/assets/does-not-matter/clip",
        json={"start_time": 5, "end_time": 2},
        auth=account.basic,
    )
    assert r.status_code == 422
    assert "end_time" in " ".join(r.json()["error"]["messages"])


@pytest.mark.asyncio
async def test_pagination_bounds_are_enforced(client, account):
    assert (await client.get("/v1/video/assets?limit=0", auth=account.basic)).status_code == 422
    assert (await client.get("/v1/video/assets?limit=500", auth=account.basic)).status_code == 422

    r = await client.get("/v1/video/assets?limit=5&page=1", auth=account.basic)
    assert r.status_code == 200
    assert "x-total-count" in r.headers


@pytest.mark.asyncio
async def test_webhook_url_must_be_https_unless_local(client, account):
    r = await client.post(
        f"/v1/projects/{account.project_id}/webhooks",
        json={"url": "http://example.com/hook"},
        headers=account.bearer,
    )
    assert r.status_code == 422
    assert "https" in " ".join(r.json()["error"]["messages"])


@pytest.mark.asyncio
async def test_webhook_rejects_unknown_event_types(client, account):
    r = await client.post(
        f"/v1/projects/{account.project_id}/webhooks",
        json={"url": "https://example.com/hook", "events": ["video.asset.exploded"]},
        headers=account.bearer,
    )
    assert r.status_code == 422
    assert "video.asset.exploded" in " ".join(r.json()["error"]["messages"])


@pytest.mark.asyncio
async def test_missing_resource_returns_the_error_envelope(client, account):
    r = await client.get("/v1/video/assets/nope", auth=account.basic)
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["type"] == "not_found"
    assert isinstance(body["error"]["messages"], list)


# ---- Idempotency ----

@pytest.mark.asyncio
async def test_upload_cancel_is_idempotent_but_blocks_revival(client, account):
    r = await client.post("/v1/video/uploads", json={}, auth=account.basic)
    upload_id = r.json()["data"]["id"]

    first = await client.put(f"/v1/video/uploads/{upload_id}/cancel", auth=account.basic)
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "cancelled"

    again = await client.put(f"/v1/video/uploads/{upload_id}/cancel", auth=account.basic)
    assert again.status_code == 200, "cancelling twice should not be an error"

    # A cancelled upload must not be promotable, even if bytes somehow land later.
    r = await client.put(f"/v1/video/uploads/{upload_id}/complete", auth=account.basic)
    assert r.json()["data"]["status"] == "cancelled"


# ---- Signed playback ----

@pytest.mark.asyncio
async def test_signed_playback_enforces_token_and_audience(client, account, sample_video):
    from app.workers.runner import drain_once

    r = await client.post(
        "/v1/video/uploads",
        json={"new_asset_settings": {"playback_policy": ["signed"]}},
        auth=account.basic,
    )
    upload = r.json()["data"]
    await put_to_upload_url(client, upload["url"], sample_video.read_bytes())
    await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    await drain_once()

    asset_id = (
        await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    ).json()["data"]["asset_id"]
    asset = (
        await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
    ).json()["data"]
    assert asset["status"] == "ready", asset.get("errors")
    playback_id = asset["playback_ids"][0]["id"]
    assert asset["playback_ids"][0]["policy"] == "signed"

    # No token at all.
    r = await client.get(f"/v1/stream/{playback_id}.m3u8")
    assert r.status_code == 403
    assert "signed token" in " ".join(r.json()["error"]["messages"])

    # A garbage token is rejected rather than crashing.
    r = await client.get(f"/v1/stream/{playback_id}.m3u8?token=not-a-jwt")
    assert r.status_code == 403

    r = await client.post(
        f"/v1/projects/{account.project_id}/signing-keys", headers=account.bearer
    )
    assert r.status_code == 201, r.text
    key = r.json()["data"]
    assert "BEGIN" in key["private_key"] and "BEGIN" in key["public_key"]

    r = await client.post(
        f"/v1/projects/{account.project_id}/playback-tokens",
        json={"playback_id": playback_id, "audience": "v"},
        headers=account.bearer,
    )
    assert r.status_code == 200, r.text
    video_token = r.json()["data"]["token"]

    r = await client.get(f"/v1/stream/{playback_id}.m3u8?token={video_token}")
    assert r.status_code == 200, r.text
    assert "#EXTM3U" in r.text
    # The token must be carried through to the variant URIs, or playback breaks one hop in.
    assert f"token={video_token}" in r.text

    # A thumbnail-audience token must not unlock the video stream.
    r = await client.post(
        f"/v1/projects/{account.project_id}/playback-tokens",
        json={"playback_id": playback_id, "audience": "t"},
        headers=account.bearer,
    )
    thumb_token = r.json()["data"]["token"]

    r = await client.get(f"/v1/stream/{playback_id}.m3u8?token={thumb_token}")
    assert r.status_code == 403, "audience must be enforced"

    r = await client.get(f"/v1/stream/{playback_id}/thumbnail.jpg?token={thumb_token}")
    assert r.status_code == 200
    assert r.content[:2] == b"\xff\xd8"

    # A token for a different playback id is refused even though it is well signed.
    other = await client.post(
        f"/v1/video/assets/{asset_id}/playback-ids",
        json={"policy": "signed"},
        auth=account.basic,
    )
    second_id = other.json()["data"]["id"]
    r = await client.get(f"/v1/stream/{second_id}.m3u8?token={video_token}")
    assert r.status_code == 403

    # Revoking the key invalidates tokens already minted from it.
    r = await client.delete(
        f"/v1/projects/{account.project_id}/signing-keys/{key['id']}",
        headers=account.bearer,
    )
    assert r.status_code == 204
    r = await client.get(f"/v1/stream/{playback_id}.m3u8?token={video_token}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_public_playback_needs_no_token(client, account, sample_video):
    from app.workers.runner import drain_once

    r = await client.post(
        "/v1/video/uploads",
        json={"new_asset_settings": {"playback_policy": ["public"]}},
        auth=account.basic,
    )
    upload = r.json()["data"]
    await put_to_upload_url(client, upload["url"], sample_video.read_bytes())
    await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    await drain_once()
    asset_id = (
        await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    ).json()["data"]["asset_id"]
    asset = (
        await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
    ).json()["data"]

    r = await client.get(f"/v1/stream/{asset['playback_ids'][0]['id']}.m3u8")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_deleted_asset_stops_playing_immediately(client, account, sample_video):
    from app.workers.runner import drain_once

    r = await client.post("/v1/video/uploads", json={}, auth=account.basic)
    upload = r.json()["data"]
    await put_to_upload_url(client, upload["url"], sample_video.read_bytes())
    await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    await drain_once()
    asset_id = (
        await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    ).json()["data"]["asset_id"]
    asset = (
        await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
    ).json()["data"]
    playback_id = asset["playback_ids"][0]["id"]

    assert (await client.get(f"/v1/stream/{playback_id}.m3u8")).status_code == 200

    r = await client.delete(f"/v1/video/assets/{asset_id}", auth=account.basic)
    assert r.status_code == 204

    # Playback stops before the storage purge job has necessarily run.
    assert (await client.get(f"/v1/stream/{playback_id}.m3u8")).status_code == 404
    assert (await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)).status_code == 404


# ---- Delivery hardening ----

@pytest.mark.asyncio
async def test_delivery_rejects_path_traversal_filenames(client, account):
    for bad in ["..%2Fsecret", "master.m3u8.bak", "segment_1.m4s", "evil.sh"]:
        r = await client.get(f"/v1/stream/nonexistent/{bad}")
        assert r.status_code in (403, 404), f"{bad} returned {r.status_code}"


@pytest.mark.asyncio
async def test_thumbnail_parameters_are_bounded(client, account, sample_video):
    from app.workers.runner import drain_once

    r = await client.post("/v1/video/uploads", json={}, auth=account.basic)
    upload = r.json()["data"]
    await put_to_upload_url(client, upload["url"], sample_video.read_bytes())
    await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    await drain_once()
    asset_id = (
        await client.get(f"/v1/video/uploads/{upload['id']}", auth=account.basic)
    ).json()["data"]["asset_id"]
    asset = (
        await client.get(f"/v1/video/assets/{asset_id}", auth=account.basic)
    ).json()["data"]
    pid = asset["playback_ids"][0]["id"]

    assert (await client.get(f"/v1/stream/{pid}/thumbnail.jpg?width=99999")).status_code == 400
    assert (await client.get(f"/v1/stream/{pid}/thumbnail.jpg?width=abc")).status_code == 400
    assert (await client.get(f"/v1/stream/{pid}/thumbnail.jpg?fit_mode=nonsense")).status_code == 400
    # Past the end of the asset is a client error, not a 500 from ffmpeg.
    assert (await client.get(f"/v1/stream/{pid}/thumbnail.jpg?time=9999")).status_code == 400

    assert (await client.get(f"/v1/stream/{pid}/animated.gif?start=0&end=99")).status_code == 400
    r = await client.get(f"/v1/stream/{pid}/animated.gif?start=0&end=2&width=240")
    assert r.status_code == 200
    assert r.content[:3] == b"GIF"
