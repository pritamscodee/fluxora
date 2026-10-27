from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if settings.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Neon pools connections itself; keep ours small and recycled ahead of its idle cut.
        kwargs.update(pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=280)
        if settings.requires_tls:
            # Re-applied here because the libpq `sslmode` parameter is stripped from the
            # URL for asyncpg's benefit (see Settings.async_database_url).
            kwargs["connect_args"] = {"ssl": True}
    return kwargs


engine = create_async_engine(settings.async_database_url, **_engine_kwargs())
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """Create tables when running without Alembic (local dev, tests)."""
    from app import models  # noqa: F401  -- registers mappers

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
