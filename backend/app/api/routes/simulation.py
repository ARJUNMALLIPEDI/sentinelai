"""Simulation control: inject and clear faults in the simulated fleet.

Only mounted outside production (``Settings.simulation_control_mounted``).
The API never touches the engine directly — it lives in the worker process —
so requests are validated here and then queued as jobs. Validation happens
*before* queuing so a caller gets a synchronous 4xx for a bad request instead
of a 202 for a job that will fail.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis

from app.api.deps import get_app_settings, get_arq, get_redis
from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.monitoring.snapshots import FAULTS_KEY, HEARTBEAT_KEY
from app.schemas.simulation import (
    ActiveFault,
    FaultAccepted,
    FaultCreate,
    FaultTypeOut,
    SimulationStatus,
)
from app.simulation.faults import FAULT_CATALOG, InvalidFaultError, validate_fault
from app.simulation.topology import FLEET, index_by_name

router = APIRouter(prefix="/simulation", tags=["simulation"])

_SPECS = index_by_name(FLEET)

Cache = Annotated[Redis, Depends(get_redis)]
Queue = Annotated[ArqRedis, Depends(get_arq)]


class InvalidFaultRequest(AppError):
    status_code = 422
    code = "invalid_fault"


async def _active_faults(redis: Redis) -> list[ActiveFault]:
    raw = await redis.get(FAULTS_KEY)
    return [ActiveFault.model_validate(f) for f in json.loads(raw)] if raw else []


@router.get("/status", response_model=SimulationStatus)
async def simulation_status(
    redis: Cache, settings: Annotated[Settings, Depends(get_app_settings)]
) -> SimulationStatus:
    heartbeat = await redis.get(HEARTBEAT_KEY)
    return SimulationStatus(
        running=heartbeat is not None,
        last_tick=datetime.fromisoformat(heartbeat) if heartbeat else None,
        tick_seconds=settings.sim_tick_seconds,
        active_faults=await _active_faults(redis),
    )


@router.get("/fault-types", response_model=list[FaultTypeOut])
async def fault_types() -> list[FaultTypeOut]:
    return [
        FaultTypeOut(
            type=d.type,
            title=d.title,
            description=d.description,
            default_duration_s=d.default_duration_s,
            applicable_services=[s.name for s in FLEET if d.applies_to(s)],
        )
        for d in FAULT_CATALOG.values()
    ]


@router.post("/faults", response_model=FaultAccepted, status_code=status.HTTP_202_ACCEPTED)
async def inject_fault(body: FaultCreate, redis: Cache, queue: Queue) -> FaultAccepted:
    try:
        validate_fault(body.type, _SPECS.get(body.service), body.intensity, body.duration_s)
    except InvalidFaultError as exc:
        raise InvalidFaultRequest(str(exc)) from exc
    if any(f.service == body.service and f.type == body.type for f in await _active_faults(redis)):
        raise ConflictError(f"{body.type.value} is already active on {body.service}")

    fault_id = f"flt_{uuid.uuid4().hex[:12]}"
    # The fault id doubles as the job id, so a retried request can't enqueue twice.
    await queue.enqueue_job(
        "inject_fault",
        fault_id,
        body.type.value,
        body.service,
        body.intensity,
        body.duration_s,
        _job_id=fault_id,
    )
    return FaultAccepted(fault_id=fault_id)


@router.delete(
    "/faults/{fault_id}", response_model=FaultAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def clear_fault(fault_id: str, redis: Cache, queue: Queue) -> FaultAccepted:
    if not any(f.id == fault_id for f in await _active_faults(redis)):
        raise NotFoundError(f"fault '{fault_id}' is not active")
    await queue.enqueue_job("clear_fault", fault_id, "api")
    return FaultAccepted(fault_id=fault_id)
