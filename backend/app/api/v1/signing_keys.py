from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbSession, OwnedProject
from app.core.errors import Conflict, NotFound
from app.core.ids import prefixed_id
from app.core.security import (
    VALID_AUDIENCES,
    generate_signing_keypair,
    sign_playback_token,
)
from app.models import PlaybackId, SigningKey, utcnow
from app.schemas.common import RequestModel, envelope

router = APIRouter(prefix="/projects/{project_id}", tags=["signing keys"])


class SigningKeyOut(BaseModel):
    id: str
    public_key: str
    created_at: object
    revoked_at: object | None = None


class SigningKeyCreated(SigningKeyOut):
    """The private key is returned exactly once, at creation."""

    private_key: str


class TokenRequest(RequestModel):
    playback_id: str = Field(max_length=40)
    audience: str = Field(default="v", max_length=1)
    expires_in: int = Field(default=3600, ge=60, le=604800)


@router.post("/signing-keys", status_code=status.HTTP_201_CREATED)
async def create_signing_key(project: OwnedProject, db: DbSession):
    private_pem, public_pem = generate_signing_keypair()
    key = SigningKey(
        id=prefixed_id("sk", 20),
        project_id=project.id,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
    )
    db.add(key)
    await db.flush()
    return envelope(
        SigningKeyCreated(
            id=key.id,
            public_key=public_pem,
            private_key=private_pem,
            created_at=key.created_at,
        )
    )


@router.get("/signing-keys")
async def list_signing_keys(project: OwnedProject, db: DbSession):
    rows = (
        await db.scalars(
            select(SigningKey)
            .where(SigningKey.project_id == project.id)
            .order_by(SigningKey.created_at.desc())
        )
    ).all()
    return envelope(
        [
            SigningKeyOut(
                id=k.id,
                public_key=k.public_key_pem,
                created_at=k.created_at,
                revoked_at=k.revoked_at,
            )
            for k in rows
        ]
    )


@router.delete("/signing-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_signing_key(key_id: str, project: OwnedProject, db: DbSession):
    key = await db.scalar(
        select(SigningKey).where(
            SigningKey.id == key_id, SigningKey.project_id == project.id
        )
    )
    if key is None:
        raise NotFound(f"signing key {key_id} was not found")
    # Revoking invalidates every token already minted with this key, by design.
    key.revoked_at = key.revoked_at or utcnow()
    await db.flush()


@router.post("/playback-tokens")
async def mint_playback_token(
    body: TokenRequest, project: OwnedProject, db: DbSession
):
    """Convenience endpoint for the dashboard's player preview.

    In production a customer signs tokens on their own server with the private key they
    were given — the point of signed playback is that minting does not require calling us.
    """
    if body.audience not in VALID_AUDIENCES:
        raise Conflict(f"audience must be one of {sorted(VALID_AUDIENCES)}")

    playback = await db.scalar(
        select(PlaybackId).where(PlaybackId.id == body.playback_id)
    )
    if playback is None:
        raise NotFound(f"playback id {body.playback_id} was not found")

    key = await db.scalar(
        select(SigningKey)
        .where(SigningKey.project_id == project.id, SigningKey.revoked_at.is_(None))
        .order_by(SigningKey.created_at.desc())
        .limit(1)
    )
    if key is None:
        raise Conflict("create a signing key for this project first")

    token = sign_playback_token(
        key.private_key_pem, key.id, body.playback_id, body.audience, body.expires_in
    )
    return envelope(
        {"token": token, "key_id": key.id, "expires_in": body.expires_in}
    )
