"""Application factory.

Run with:  uvicorn app.main:create_app --factory

The factory takes optional ``Settings`` so tests can build isolated apps.
Connections are created in the lifespan, not at import time. Creating the
engine doesn't connect, so the API starts even if Postgres is down and
``/health/ready`` reports it — rather than crash-looping.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq.connections import ArqRedis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import ConnectionPool, Redis

from app import __version__
from app.api.router import build_api_router
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.db.session import build_engine, build_sessionmaker
from app.observability.metrics import metrics_router
from app.observability.middleware import RequestContextMiddleware

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = build_sessionmaker(engine)
        app.state.redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.health_check_timeout_s,
        )
        # Job queue client for handing work to the worker. A connection pool
        # connects lazily, so the API still starts while Redis is unavailable.
        app.state.arq = ArqRedis(ConnectionPool.from_url(settings.redis_url))
        logger.info(
            "startup", extra={"ctx": {"environment": settings.environment, "version": __version__}}
        )
        try:
            yield
        finally:
            await app.state.arq.aclose()
            await app.state.redis.aclose()
            await engine.dispose()
            logger.info("shutdown")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url="/docs",
    )

    # Middleware order: last added = outermost. Request context wraps CORS so
    # even CORS preflights get a request ID and are counted in metrics.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    app.include_router(
        build_api_router(settings.simulation_control_mounted), prefix=settings.api_prefix
    )
    app.include_router(metrics_router)
    return app
