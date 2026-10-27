from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import Conflict, Forbidden, NotFound, Unauthorized
from app.core.security import (
    decode_token,
    hash_secret,
    needs_rehash,
    parse_basic_auth,
    verify_secret,
)
from app.models import ApiKey, IdempotencyRecord, Project, User, utcnow

DbSession = Annotated[AsyncSession, Depends(get_db)]


# ---- Dashboard auth (human) ----

async def current_user(
    db: DbSession, authorization: Annotated[str | None, Header()] = None
) -> User:
    if not authorization:
        raise Unauthorized("Authorization header is missing")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("Expected a Bearer access token")
    payload = decode_token(token, "access")
    user = await db.scalar(select(User).where(User.id == payload["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("Account is not available")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def owned_project(project_id: str, db: DbSession, user: CurrentUser) -> Project:
    project = await db.scalar(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    if project is None:
        # Deliberately 404 rather than 403: a project id the caller does not own should not
        # be confirmable as existing.
        raise NotFound(f"project {project_id} was not found")
    return project


OwnedProject = Annotated[Project, Depends(owned_project)]


# ---- API auth (machine) ----

async def api_project(
    db: DbSession, authorization: Annotated[str | None, Header()] = None
) -> Project:
    """Resolve an API key to its project. Every API-key route is implicitly scoped to the
    returned project, so no endpoint takes a project id."""
    if not authorization:
        raise Unauthorized("Authorization header is missing")
    key_id, secret = parse_basic_auth(authorization)

    api_key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id))
    if api_key is None:
        # Hash anyway so a missing key and a wrong secret cost the same time.
        verify_secret(secret, hash_secret("decoy"))
        raise Unauthorized("API key is not valid")
    if not api_key.is_active:
        raise Forbidden("API key has been revoked")
    if not verify_secret(secret, api_key.secret_hash):
        raise Unauthorized("API key is not valid")

    if needs_rehash(api_key.secret_hash):
        api_key.secret_hash = hash_secret(secret)
    api_key.last_used_at = utcnow()

    project = await db.scalar(select(Project).where(Project.id == api_key.project_id))
    if project is None:
        raise Forbidden("API key is not attached to a live project")
    return project


ApiProject = Annotated[Project, Depends(api_project)]


# ---- Pagination ----

class Page:
    def __init__(self, limit: int, page: int):
        self.limit = limit
        self.page = page

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


async def pagination(
    limit: int = Query(default=settings.default_list_limit, ge=1, le=settings.max_list_limit),
    page: int = Query(default=1, ge=1),
) -> Page:
    return Page(limit=limit, page=page)


Pagination = Annotated[Page, Depends(pagination)]


# ---- Idempotency ----

def _fingerprint(method: str, path: str, body: bytes) -> str:
    return hashlib.sha256(b"|".join([method.encode(), path.encode(), body])).hexdigest()


async def idempotency_guard(
    request: Request,
    db: DbSession,
    project: ApiProject,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict | None:
    """Return a stored response for a replayed key, or None to let the route run.

    Reusing a key with a different body is a client bug worth surfacing loudly, so it is a
    409 rather than a silent second execution.
    """
    if not idempotency_key:
        return None
    body = await request.body()
    record_id = f"{project.id}:{idempotency_key}"
    fingerprint = _fingerprint(request.method, request.url.path, body)

    existing = await db.scalar(
        select(IdempotencyRecord).where(IdempotencyRecord.id == record_id)
    )
    if existing is None:
        request.state.idempotency = (record_id, fingerprint)
        return None
    if existing.expires_at.timestamp() < utcnow().timestamp():
        await db.delete(existing)
        await db.flush()
        request.state.idempotency = (record_id, fingerprint)
        return None
    if existing.request_fingerprint != fingerprint:
        raise Conflict(
            "Idempotency-Key was already used with a different request body"
        )
    return existing.response_body


async def store_idempotent_response(
    request: Request, db: AsyncSession, status_code: int, payload: dict
) -> None:
    from datetime import timedelta

    state = getattr(request.state, "idempotency", None)
    if not state:
        return
    record_id, fingerprint = state
    db.add(
        IdempotencyRecord(
            id=record_id,
            request_fingerprint=fingerprint,
            status_code=status_code,
            response_body=json.loads(json.dumps(payload, default=str)),
            expires_at=utcnow() + timedelta(hours=24),
        )
    )
    await db.flush()
