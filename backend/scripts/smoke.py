"""End-to-end smoke test against a running stack.

The pytest suite drives the app in-process. This drives it over real HTTP against whatever
`.env` points at — Neon and an S3-compatible store — with a separate worker process doing
the transcoding. It is the check that the pieces work when they are actually separate
processes, which is the one thing in-process tests cannot tell you.

    python -m scripts.smoke [--base-url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

SAMPLE = Path("tmp/smoke-sample.mp4")


def make_sample() -> Path:
    if SAMPLE.exists():
        return SAMPLE
    SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            os.environ.get("FFMPEG_PATH", "ffmpeg"),
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=5",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(SAMPLE),
        ],
        check=True,
        capture_output=True,
    )
    return SAMPLE


def step(n: int, message: str) -> None:
    print(f"  {n:>2}. {message}", flush=True)


async def run(base_url: str) -> int:
    sample = make_sample()
    email = f"smoke-{uuid.uuid4().hex[:10]}@example.com"
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        if not condition:
            failures.append(label)
            print(f"      FAIL: {label}", flush=True)

    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as c:
        r = await c.get("/healthz")
        check(r.status_code == 200, f"/healthz returned {r.status_code}")
        step(1, f"health ok, database reachable: {r.json()}")

        r = await c.post(
            "/v1/auth/register",
            json={"email": email, "password": "smoke-test-password", "name": "Smoke"},
        )
        check(r.status_code == 201, f"register returned {r.status_code}: {r.text[:200]}")
        if r.status_code != 201:
            return 1
        bearer = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
        step(2, "registered a user against the live database")

        r = await c.post(
            "/v1/projects", json={"name": f"smoke-{uuid.uuid4().hex[:6]}"}, headers=bearer
        )
        project_id = r.json()["data"]["id"]
        r = await c.post(
            f"/v1/projects/{project_id}/api-keys", json={"name": "smoke"}, headers=bearer
        )
        key = r.json()["data"]
        auth = httpx.BasicAuth(key["id"], key["secret"])
        step(3, f"created project {project_id} and api key {key['id']}")

        r = await c.post(
            "/v1/video/uploads",
            json={"new_asset_settings": {"playback_policy": ["public"], "mp4_support": "standard"}},
            auth=auth,
        )
        check(r.status_code == 201, f"create upload returned {r.status_code}: {r.text[:200]}")
        upload = r.json()["data"]
        step(4, f"got a presigned upload URL ({upload['url'].split('?')[0][:72]}…)")

        # Straight to object storage, bypassing the API entirely.
        async with httpx.AsyncClient(timeout=300.0) as net:
            put = await net.put(upload["url"], content=sample.read_bytes())
        check(put.status_code in (200, 204), f"presigned PUT returned {put.status_code}")
        step(5, f"uploaded {sample.stat().st_size:,} bytes directly to object storage")

        r = await c.get(f"/v1/video/uploads/{upload['id']}", auth=auth)
        promoted = r.json()["data"]
        check(promoted["status"] == "asset_created", f"upload status {promoted['status']}")
        asset_id = promoted.get("asset_id")
        step(6, f"upload reconciled into asset {asset_id}")

        step(7, "waiting for the worker process to transcode…")
        asset = {}
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            r = await c.get(f"/v1/video/assets/{asset_id}", auth=auth)
            asset = r.json()["data"]
            if asset["status"] in ("ready", "errored"):
                break
            await asyncio.sleep(3)
        check(asset.get("status") == "ready", f"asset status {asset.get('status')}: {asset.get('errors')}")
        if asset.get("status") != "ready":
            return 1
        step(
            8,
            f"asset ready: {asset['duration']:.2f}s {asset['aspect_ratio']} "
            f"{asset['max_stored_resolution']}, {len(asset['tracks'])} tracks",
        )

        playback_id = asset["playback_ids"][0]["id"]
        r = await c.get(f"/v1/stream/{playback_id}.m3u8")
        check(r.status_code == 200, f"master manifest returned {r.status_code}")
        variants = [ln for ln in r.text.splitlines() if ln.endswith(".m3u8")]
        check(len(variants) >= 2, f"expected an ABR ladder, got {variants}")
        step(9, f"master manifest serves {len(variants)} renditions")

        r = await c.get(f"/v1/stream/{variants[0]}")
        check(r.status_code == 200, f"variant playlist returned {r.status_code}")
        segments = [ln for ln in r.text.splitlines() if ln.endswith(".m4s")]
        init = r.text.split('#EXT-X-MAP:URI="')[1].split('"')[0] if "#EXT-X-MAP" in r.text else None
        check(bool(segments and init), "variant playlist is missing segments or an init map")
        step(10, f"variant playlist has {len(segments)} segments")

        r = await c.get(f"/v1/stream/{playback_id}/{init}")
        check(r.status_code == 200, f"init segment returned {r.status_code}")
        r = await c.get(f"/v1/stream/{playback_id}/{segments[0]}")
        check(r.status_code == 200 and len(r.content) > 1000, "first media segment is not servable")
        step(11, f"init + first segment delivered ({len(r.content):,} bytes)")

        r = await c.get(f"/v1/stream/{playback_id}/thumbnail.jpg?time=2&width=640")
        check(r.status_code == 200 and r.content[:2] == b"\xff\xd8", "thumbnail is not a JPEG")
        step(12, f"thumbnail rendered ({len(r.content):,} bytes, cache={r.headers.get('x-fluxora-cache')})")

        r = await c.get(f"/v1/video/assets/{asset_id}", auth=auth)
        names = sorted(sr["name"] for sr in r.json()["data"]["static_renditions"])
        step(13, f"static mp4 renditions: {names or 'none yet'}")

        r = await c.get(f"/v1/projects/{project_id}/events", headers=bearer)
        kinds = [e["type"] for e in r.json()["data"]]
        check("video.asset.ready" in kinds, f"no ready event in the log: {kinds}")
        step(14, f"event log recorded {len(kinds)} events")

        r = await c.delete(f"/v1/video/assets/{asset_id}", auth=auth)
        check(r.status_code == 204, f"delete returned {r.status_code}")
        r = await c.get(f"/v1/stream/{playback_id}.m3u8")
        check(r.status_code == 404, f"playback still served after delete: {r.status_code}")
        step(15, "asset deleted and playback stopped immediately")

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SMOKE PASSED — upload to playback works on the live stack.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", "http://localhost:8000"))
    args = parser.parse_args()
    print(f"Fluxora smoke test against {args.base_url}\n")
    return asyncio.run(run(args.base_url))


if __name__ == "__main__":
    sys.exit(main())
