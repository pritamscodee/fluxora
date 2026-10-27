"""Local stand-in for R2's presigned PUT/GET.

Mounted only when R2 is unconfigured. It exists so client upload code is byte-identical in
development and production: the client still receives an opaque URL and still does a plain
PUT of the raw bytes to it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.core.config import settings
from app.core.errors import Forbidden, NotFound
from app.core.security import verify_webhook
from app.services import storage

router = APIRouter(tags=["development"], include_in_schema=False)

MAX_LOCAL_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


@router.put("/_local-upload", status_code=status.HTTP_200_OK)
async def local_upload(request: Request, key: str, sig: str):
    store = storage.get_storage()
    if not isinstance(store, storage.LocalStorage):
        raise NotFound("local upload is only available when R2 is not configured")
    # Same HMAC construction the presign used; the signature is what authorises the write.
    if not verify_webhook(settings.jwt_secret, key.encode(), sig, tolerance=settings.upload_max_timeout_seconds):
        raise Forbidden("upload signature is not valid or has expired")

    body = await request.body()
    if len(body) > MAX_LOCAL_UPLOAD_BYTES:
        raise Forbidden("upload exceeds the local development size limit")
    await store.put_bytes(key, body, "application/octet-stream")
    return Response(status_code=status.HTTP_200_OK, headers={"ETag": f'"{len(body)}"'})


@router.get("/_local-download")
async def local_download(key: str):
    store = storage.get_storage()
    if not isinstance(store, storage.LocalStorage):
        raise NotFound("local download is only available when R2 is not configured")
    if not await store.exists(key):
        raise NotFound("object was not found")
    return Response(
        content=await store.get_bytes(key), media_type="application/octet-stream"
    )
