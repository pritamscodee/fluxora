"""Blob storage. Cloudflare R2 in any real deployment; a local directory when R2 is not
configured, so the whole upload-to-playback path is exercisable before credentials exist.

Buckets are private by every path. Nothing here ever returns a public object URL; delivery
goes through the API so playback policy is enforced in one place (ADR-0001).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from app.core.config import settings

log = logging.getLogger("fluxora.storage")


def source_key(project_id: str, upload_id: str, filename: str = "source") -> str:
    return f"projects/{project_id}/uploads/{upload_id}/{filename}"


def asset_prefix(project_id: str, asset_id: str) -> str:
    return f"projects/{project_id}/assets/{asset_id}"


def hls_prefix(project_id: str, asset_id: str) -> str:
    return f"{asset_prefix(project_id, asset_id)}/hls"


def hls_file_key(project_id: str, asset_id: str, filename: str) -> str:
    """HLS output is a flat directory (see media.build_hls_argv), so the storage layout
    mirrors it one-to-one and delivery is a single filename lookup."""
    return f"{hls_prefix(project_id, asset_id)}/{filename}"


def master_manifest_key(project_id: str, asset_id: str) -> str:
    return hls_file_key(project_id, asset_id, "master.m3u8")


def mp4_key(project_id: str, asset_id: str, name: str) -> str:
    return f"{asset_prefix(project_id, asset_id)}/mp4/{name}.mp4"


def thumbnail_key(project_id: str, asset_id: str, spec: str) -> str:
    """Content-addressed by the render spec so cached thumbnails are immutable."""
    digest = hashlib.sha256(spec.encode()).hexdigest()[:16]
    return f"{asset_prefix(project_id, asset_id)}/thumbs/{digest}.jpg"


def track_key(project_id: str, asset_id: str, track_id: str, ext: str) -> str:
    return f"{asset_prefix(project_id, asset_id)}/text/{track_id}.{ext}"


class Storage(ABC):
    @abstractmethod
    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def put_file(self, key: str, path: str | Path, content_type: str) -> int: ...

    @abstractmethod
    async def get_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    async def download(self, key: str, dest: str | Path) -> Path: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def size(self, key: str) -> int | None: ...

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> int: ...

    @abstractmethod
    async def presign_put(self, key: str, expires_in: int, content_type: str | None = None) -> str: ...

    @abstractmethod
    async def presign_get(self, key: str, expires_in: int) -> str: ...


class S3Storage(Storage):
    """Any S3-compatible store: MinIO in development, Cloudflare R2 or AWS S3 in
    production. Buckets are always private; see ADR-0001 decision 4."""

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        self._bucket = settings.s3_bucket
        addressing = "path" if settings.s3_force_path_style else "auto"
        # SigV4 is required by MinIO and R2 alike. Path-style addressing keeps
        # `http://host:9000/bucket/key` working without per-bucket DNS.
        self._config = Config(
            signature_version="s3v4", s3={"addressing_style": addressing}
        )
        self._credentials = {
            "aws_access_key_id": settings.s3_access_key_id,
            "aws_secret_access_key": settings.s3_secret_access_key,
            "region_name": settings.s3_region,
        }
        self._client = boto3.client(
            "s3", endpoint_url=settings.s3_endpoint, config=self._config, **self._credentials
        )
        # A separate client bound to the browser-facing origin. Presigned URLs embed the
        # host in the signature, so one signed against an internal Docker hostname would
        # fail the moment a browser used it.
        if settings.s3_public_endpoint and settings.s3_public_endpoint != settings.s3_endpoint:
            self._signer = boto3.client(
                "s3",
                endpoint_url=settings.s3_public_endpoint,
                config=self._config,
                **self._credentials,
            )
        else:
            self._signer = self._client

        if settings.s3_create_bucket:
            self.ensure_bucket()

    def ensure_bucket(self) -> None:
        """Create the bucket if it is absent. MinIO starts empty, so without this the
        first upload of a fresh stack fails with NoSuchBucket."""
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status not in (403, 404):
                raise
            if status == 403:
                # The bucket exists but is owned elsewhere; creating it would fail anyway.
                log.warning("bucket %s exists but is not accessible to us", self._bucket)
                return
        try:
            self._client.create_bucket(Bucket=self._bucket)
            log.info("created bucket %s", self._bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            # A concurrent worker winning the race is a success, not a failure.
            if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def put_file(self, key: str, path: str | Path, content_type: str) -> int:
        path = Path(path)
        await asyncio.to_thread(
            self._client.upload_file,
            str(path),
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return path.stat().st_size

    async def get_bytes(self, key: str) -> bytes:
        def _get() -> bytes:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

        return await asyncio.to_thread(_get)

    async def download(self, key: str, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._client.download_file, self._bucket, key, str(dest))
        return dest

    async def exists(self, key: str) -> bool:
        return await self.size(key) is not None

    async def size(self, key: str) -> int | None:
        def _head() -> int | None:
            try:
                return int(self._client.head_object(Bucket=self._bucket, Key=key)["ContentLength"])
            except Exception:
                return None

        return await asyncio.to_thread(_head)

    async def delete_prefix(self, prefix: str) -> int:
        def _delete() -> int:
            paginator = self._client.get_paginator("list_objects_v2")
            removed = 0
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                for i in range(0, len(keys), 1000):
                    chunk = keys[i : i + 1000]
                    self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": chunk})
                    removed += len(chunk)
            return removed

        return await asyncio.to_thread(_delete)

    async def presign_put(self, key: str, expires_in: int, content_type: str | None = None) -> str:
        # ContentType is deliberately left out of the signature: including it forces the
        # client to send a byte-identical Content-Type header or the PUT is rejected, and
        # browsers do not reliably reproduce it.
        return await asyncio.to_thread(
            self._signer.generate_presigned_url,
            "put_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    async def presign_get(self, key: str, expires_in: int) -> str:
        return await asyncio.to_thread(
            self._signer.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )


class LocalStorage(Storage):
    """Development stand-in. `presign_put` points at an API route that accepts the bytes,
    so client upload code is identical in both modes."""

    def __init__(self, root: str = "./media") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are server-generated, but treat them as untrusted anyway: a '..' must not
        # be able to escape the storage root.
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"storage key escapes root: {key!r}")
        return resolved

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def put_file(self, key: str, path: str | Path, content_type: str) -> int:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, str(path), str(p))
        return p.stat().st_size

    async def get_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def download(self, key: str, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, str(self._path(key)), str(dest))
        return dest

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def size(self, key: str) -> int | None:
        p = self._path(key)
        return p.stat().st_size if p.exists() else None

    async def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix)
        if base.is_dir():
            count = sum(1 for _ in base.rglob("*") if _.is_file())
            await asyncio.to_thread(shutil.rmtree, str(base), True)
            return count
        if base.exists():
            os.remove(base)
            return 1
        return 0

    async def presign_put(self, key: str, expires_in: int, content_type: str | None = None) -> str:
        from app.core.security import sign_webhook

        token = sign_webhook(settings.jwt_secret, key.encode())
        return f"{settings.api_base_url}/v1/_local-upload?key={key}&sig={token}"

    async def presign_get(self, key: str, expires_in: int) -> str:
        return f"{settings.api_base_url}/v1/_local-download?key={key}"


_storage: Storage | None = None


def get_storage() -> Storage:
    """Pick a backend once per process.

    The local-disk fallback is what lets the whole upload-to-playback path run, and be
    tested, before any object-store credentials exist.
    """
    global _storage
    if _storage is None:
        if settings.storage_configured:
            _storage = S3Storage()
            log.info(
                "storage backend: S3-compatible endpoint=%s bucket=%s",
                settings.s3_endpoint,
                settings.s3_bucket,
            )
        else:
            _storage = LocalStorage()
            log.warning(
                "no object storage configured; falling back to the local filesystem"
            )
    return _storage
