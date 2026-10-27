"""Test harness.

Environment is set before any app module is imported, because `app.core.config.settings`
is a module-level singleton. Tests run against SQLite plus local-filesystem storage, which
exercises the same code paths as Neon + R2 apart from the two driver shims.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_TMP = Path(tempfile.mkdtemp(prefix="fluxora-test-"))

# `FLUXORA_TEST_STORAGE=s3` runs the identical suite against the docker-compose S3
# service instead of local disk, which is how the presigned-upload path and the S3
# backend get real coverage rather than being exercised only by the filesystem shim.
STORAGE_MODE = os.environ.get("FLUXORA_TEST_STORAGE", "local").lower()

os.environ.update(
    ENVIRONMENT="test",
    DATABASE_URL=f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}",
    JWT_SECRET="test-secret-not-used-in-production",
    MEDIA_SCRATCH_DIR=str(_TMP / "scratch"),
    API_BASE_URL="http://testserver",
    WORKER_CONCURRENCY="1",
    OTEL_EXPORTER_OTLP_ENDPOINT="",
)

if STORAGE_MODE == "s3":
    os.environ.update(
        S3_ENDPOINT=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        S3_ACCESS_KEY_ID=os.environ.get("S3_ACCESS_KEY_ID", "fluxora"),
        S3_SECRET_ACCESS_KEY=os.environ.get("S3_SECRET_ACCESS_KEY", "fluxora-dev-secret"),
        # A per-run bucket keeps parallel runs and leftover objects from colliding.
        S3_BUCKET=f"fluxora-test-{uuid.uuid4().hex[:12]}",
        S3_REGION="us-east-1",
        S3_FORCE_PATH_STYLE="true",
        S3_CREATE_BUCKET="true",
    )
else:
    os.environ.update(S3_ENDPOINT="", S3_ACCESS_KEY_ID="", S3_SECRET_ACCESS_KEY="")

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.core.db import SessionLocal, engine, init_models  # noqa: E402
from app.main import app  # noqa: E402
from app.services import storage  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def storage_root():
    (_TMP / "scratch").mkdir(parents=True, exist_ok=True)
    if STORAGE_MODE == "s3":
        storage._storage = storage.S3Storage()
    else:
        storage._storage = storage.LocalStorage(str(_TMP / "media"))
    yield _TMP / "media"
    shutil.rmtree(_TMP, ignore_errors=True)


async def put_to_upload_url(client: httpx.AsyncClient, url: str, data: bytes) -> None:
    """PUT bytes to a presigned upload URL.

    Local storage presigns a route on this API (reachable only through the ASGI
    transport); S3 presigns an absolute URL on the object store, which needs a real
    socket. Tests should not care which, so the distinction is handled here.
    """
    if url.startswith("http://testserver"):
        response = await client.put(url.removeprefix("http://testserver"), content=data)
    else:
        async with httpx.AsyncClient(timeout=180.0) as net:
            response = await net.put(url, content=data)
    assert response.status_code in (200, 204), f"{response.status_code}: {response.text[:300]}"


# One-off schema setup: explicitly pinned to the session loop because it must run
# exactly once for the whole suite, not per test.
@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def database():
    await init_models()
    yield
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=120.0
    ) as c:
        yield c


@pytest.fixture
async def db():
    async with SessionLocal() as session:
        yield session


@pytest.fixture(scope="session")
def sample_video(storage_root) -> Path:
    """A real 6-second H.264/AAC file. Synthesising one with ffmpeg keeps no binary
    fixtures in the repo and means the tests exercise genuine decoding."""
    path = _TMP / "sample.mp4"
    if path.exists():
        return path
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class Account:
    """A registered user with a project and an API key, i.e. the state every video API
    test needs before it can do anything."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self.access_token: str = ""
        self.project_id: str = ""
        self.key_id: str = ""
        self.secret: str = ""

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    @property
    def basic(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.key_id, self.secret)

    async def bootstrap(self, email: str, environment: str = "development") -> "Account":
        r = await self._client.post(
            "/v1/auth/register",
            json={"email": email, "password": "correct-horse-battery", "name": "Test User"},
        )
        assert r.status_code == 201, r.text
        self.access_token = r.json()["data"]["access_token"]

        r = await self._client.post(
            "/v1/projects",
            json={"name": f"proj-{email}", "environment": environment},
            headers=self.bearer,
        )
        assert r.status_code == 201, r.text
        self.project_id = r.json()["data"]["id"]

        r = await self._client.post(
            f"/v1/projects/{self.project_id}/api-keys",
            json={"name": "test key"},
            headers=self.bearer,
        )
        assert r.status_code == 201, r.text
        key = r.json()["data"]
        self.key_id, self.secret = key["id"], key["secret"]
        return self


@pytest.fixture
async def account(client) -> Account:
    import uuid

    return await Account(client).bootstrap(f"user-{uuid.uuid4().hex[:10]}@example.com")
