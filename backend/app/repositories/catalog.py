"""Service catalog: services and the dependency graph between them."""

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service, ServiceDependency
from app.simulation.topology import ServiceSpec


async def sync_topology(
    session: AsyncSession,
    specs: Sequence[ServiceSpec],
    versions: Mapping[str, str],
) -> dict[str, uuid.UUID]:
    """Upsert the fleet definition into the catalog; returns {name: id}.

    Idempotent and safe to run on every worker start. Rows keep their IDs, so
    telemetry already stored against a service stays attached to it.
    """
    for spec in specs:
        values = {
            "kind": spec.kind,
            "tier": spec.tier,
            "description": spec.description,
            "desired_instances": spec.instances,
            "slo_latency_p95_ms": spec.slo_latency_p95_ms,
            "slo_error_rate": spec.slo_error_rate,
            "current_version": versions.get(spec.name),
        }
        stmt = insert(Service).values(id=uuid.uuid4(), name=spec.name, **values)
        await session.execute(
            stmt.on_conflict_do_update(index_elements=[Service.name], set_=values)
        )

    ids = await service_ids(session)
    fleet_ids = [ids[s.name] for s in specs]
    await session.execute(
        delete(ServiceDependency).where(ServiceDependency.service_id.in_(fleet_ids))
    )
    edges = [
        {
            "service_id": ids[spec.name],
            "depends_on_id": ids[dep.target],
            "calls_per_request": dep.calls_per_request,
            "criticality": dep.criticality,
            "is_async": dep.is_async,
        }
        for spec in specs
        for dep in spec.dependencies
    ]
    if edges:
        await session.execute(insert(ServiceDependency), edges)
    return ids


async def service_ids(session: AsyncSession) -> dict[str, uuid.UUID]:
    rows = await session.execute(select(Service.name, Service.id))
    return {name: id_ for name, id_ in rows.all()}


async def list_services(session: AsyncSession) -> list[Service]:
    result = await session.execute(select(Service).order_by(Service.name))
    return list(result.scalars().all())


async def get_service(session: AsyncSession, name: str) -> Service | None:
    result = await session.execute(select(Service).where(Service.name == name))
    return result.scalar_one_or_none()


async def dependents_of(session: AsyncSession, service_id: uuid.UUID) -> list[str]:
    result = await session.execute(
        select(Service.name)
        .join(ServiceDependency, ServiceDependency.service_id == Service.id)
        .where(ServiceDependency.depends_on_id == service_id)
        .order_by(Service.name)
    )
    return list(result.scalars().all())
