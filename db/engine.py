"""
PostgreSQL async engine — pgvector support
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text
from astrbot.api import logger

from ..config import DatabaseConfig


class Database:
    """Manages the async SQLAlchemy engine and session factory."""

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    async def start(self) -> None:
        """Create the engine, enable pgvector, and create all tables."""
        from .models import Base  # local import to avoid circular deps

        if self._config.pool_min_size > self._config.pool_size:
            logger.warning(
                "[DB] pool_min_size exceeds pool_size; clamping persistent pool "
                f"to {self._config.pool_size}"
            )

        pool_options = self._config.sqlalchemy_pool_options()
        self.engine = create_async_engine(
            self._config.connection_url(),
            echo=self._config.echo,
            pool_pre_ping=True,
            **pool_options,
        )
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        # Enable pgvector extension and create tables
        async with self.engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text(
                    "ALTER TABLE group_persona_bindings "
                    "ADD COLUMN IF NOT EXISTS base_persona_prompt TEXT NOT NULL DEFAULT ''"
                )
            )

        logger.info("[DB] PostgreSQL engine started, tables ensured")

    async def stop(self) -> None:
        """Dispose the engine."""
        if self.engine:
            await self.engine.dispose()
            logger.info("[DB] PostgreSQL engine disposed")

    def session(self) -> AsyncSession:
        """Convenience: create a new AsyncSession."""
        if not self.session_factory:
            raise RuntimeError("Database not started — call start() first")
        return self.session_factory()
