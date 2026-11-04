from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import NotFoundError
from app.repositories import catalog
from app.repositories import telemetry as repo
from app.schemas.telemetry import DeploymentOut

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.get("", response_model=list[DeploymentOut])
async def list_deployments(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: str | None = None,
    hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeploymentOut]:
    service_id = None
    if service:
        svc = await catalog.get_service(db, service)
        if svc is None:
            raise NotFoundError(f"service '{service}' not found")
        service_id = svc.id
    rows = await repo.list_deployments(
        db, service_id, datetime.now(UTC) - timedelta(hours=hours), limit
    )
    return [
        DeploymentOut(
            id=d.id,
            service=name,
            version=d.version,
            previous_version=d.previous_version,
            kind=d.kind,
            initiated_by=d.initiated_by,
            deployed_at=d.deployed_at,
        )
        for d, name in rows
    ]
