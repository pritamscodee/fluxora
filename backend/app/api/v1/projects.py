from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession, OwnedProject, Pagination
from app.core.errors import Conflict, NotFound
from app.core.ids import api_key_id, api_key_secret
from app.core.security import hash_secret
from app.models import ApiKey, Project, utcnow
from app.schemas.account import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from app.schemas.common import envelope

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, db: DbSession, user: CurrentUser):
    project = Project(user_id=user.id, name=body.name, environment=body.environment)
    db.add(project)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise Conflict(
            f"a {body.environment} project named {body.name!r} already exists"
        ) from exc
    return envelope(ProjectOut.model_validate(project))


@router.get("")
async def list_projects(db: DbSession, user: CurrentUser, page: Pagination, response: Response):
    base = select(Project).where(Project.user_id == user.id)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await db.scalars(
            base.order_by(Project.created_at.desc()).limit(page.limit).offset(page.offset)
        )
    ).all()
    response.headers["X-Total-Count"] = str(total or 0)
    return envelope([ProjectOut.model_validate(p) for p in rows])


@router.get("/{project_id}")
async def get_project(project: OwnedProject):
    return envelope(ProjectOut.model_validate(project))


@router.patch("/{project_id}")
async def update_project(body: ProjectUpdate, project: OwnedProject, db: DbSession):
    if body.name is not None:
        project.name = body.name
    project.updated_at = utcnow()
    await db.flush()
    return envelope(ProjectOut.model_validate(project))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project: OwnedProject, db: DbSession):
    await db.delete(project)


# ---- API keys ----

@router.post("/{project_id}/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate, project: OwnedProject, db: DbSession
):
    """The plaintext secret exists only in this response. We store an Argon2id hash, so a
    lost secret is replaced rather than recovered."""
    secret = api_key_secret()
    key = ApiKey(
        id=api_key_id(),
        project_id=project.id,
        name=body.name,
        secret_hash=hash_secret(secret),
    )
    db.add(key)
    await db.flush()
    return envelope(
        ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), secret=secret)
    )


@router.get("/{project_id}/api-keys")
async def list_api_keys(project: OwnedProject, db: DbSession):
    rows = (
        await db.scalars(
            select(ApiKey)
            .where(ApiKey.project_id == project.id)
            .order_by(ApiKey.created_at.desc())
        )
    ).all()
    return envelope([ApiKeyOut.model_validate(k) for k in rows])


@router.delete("/{project_id}/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(key_id: str, project: OwnedProject, db: DbSession):
    key = await db.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == project.id)
    )
    if key is None:
        raise NotFound(f"api key {key_id} was not found")
    # Revoked rather than deleted: the row is the audit record of what was used when.
    key.revoked_at = key.revoked_at or utcnow()
    await db.flush()
