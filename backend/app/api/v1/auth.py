from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.errors import Unauthorized
from app.core.ids import public_id
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_secret,
    verify_secret,
)
from app.models import RefreshToken, User, utcnow
from app.schemas.account import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from app.schemas.common import envelope

router = APIRouter(prefix="/auth", tags=["auth"])


async def _issue_pair(db, user: User) -> TokenPair:
    jti = public_id(32)
    db.add(
        RefreshToken(
            user_id=user.id,
            jti=jti,
            expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    await db.flush()
    return TokenPair(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id, jti),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: DbSession):
    existing = await db.scalar(select(User).where(User.email == body.email.lower()))
    if existing:
        # Same shape as a bad password, so registration cannot be used to enumerate
        # which addresses have accounts.
        raise Unauthorized("That email cannot be registered")
    user = User(
        email=body.email.lower(),
        password_hash=hash_secret(body.password),
        name=body.name,
    )
    db.add(user)
    await db.flush()
    return envelope(await _issue_pair(db, user))


@router.post("/login")
async def login(body: LoginRequest, db: DbSession):
    user = await db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None:
        verify_secret(body.password, hash_secret("decoy"))
        raise Unauthorized("Email or password is incorrect")
    if not verify_secret(body.password, user.password_hash) or not user.is_active:
        raise Unauthorized("Email or password is incorrect")
    return envelope(await _issue_pair(db, user))


@router.post("/refresh")
async def refresh(body: RefreshRequest, db: DbSession):
    """Rotating refresh tokens: the presented token is revoked as it is exchanged.

    Presenting an already-revoked token means it leaked, so the whole family for that user
    is revoked rather than just rejecting the one request.
    """
    payload = decode_token(body.refresh_token, "refresh")
    stored = await db.scalar(select(RefreshToken).where(RefreshToken.jti == payload["jti"]))
    if stored is None:
        raise Unauthorized("Refresh token is not recognised")
    if stored.revoked_at is not None:
        await db.execute(
            RefreshToken.__table__.update()
            .where(RefreshToken.user_id == stored.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        # Commit before raising: the request dependency rolls back on an exception, which
        # would otherwise discard the very revocation this branch exists to perform.
        await db.commit()
        raise Unauthorized("Refresh token was already used; all sessions have been revoked")
    if stored.expires_at.timestamp() < utcnow().timestamp():
        raise Unauthorized("Refresh token has expired")

    stored.revoked_at = utcnow()
    user = await db.scalar(select(User).where(User.id == stored.user_id))
    if user is None or not user.is_active:
        raise Unauthorized("Account is not available")
    return envelope(await _issue_pair(db, user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(user: CurrentUser, db: DbSession):
    await db.execute(
        RefreshToken.__table__.update()
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


@router.get("/me")
async def me(user: CurrentUser):
    return envelope(UserOut.model_validate(user))
