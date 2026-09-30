"""Async engine factory."""
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(url: str, pool_size: int = 5, statement_timeout_ms: int = 30_000) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": str(statement_timeout_ms),
                                          "application_name": "ofmp"}},
    )
