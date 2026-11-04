"""Latest-sample cache in Redis.

The dashboard polls fleet health every few seconds. Serving that from Redis
(one MGET for all services) keeps the hot read off the growing
``metric_samples`` table.

Each key carries a TTL of a few ticks. If the worker stops, snapshots expire
and services report UNKNOWN — the cache can never keep serving a frozen
"everything is healthy" picture.
"""

from collections.abc import Iterable, Sequence

from redis.asyncio import Redis

from app.schemas.telemetry import MetricSnapshot

SNAPSHOT_KEY = "sentinel:snapshot:{service}"
HEARTBEAT_KEY = "sentinel:sim:heartbeat"
FAULTS_KEY = "sentinel:sim:faults"


async def write_snapshots(redis: Redis, snapshots: Iterable[MetricSnapshot], ttl_s: int) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        for snap in snapshots:
            pipe.set(SNAPSHOT_KEY.format(service=snap.service), snap.model_dump_json(), ex=ttl_s)
        await pipe.execute()


async def read_snapshots(redis: Redis, services: Sequence[str]) -> dict[str, MetricSnapshot | None]:
    if not services:
        return {}
    raw = await redis.mget([SNAPSHOT_KEY.format(service=s) for s in services])
    return {
        name: MetricSnapshot.model_validate_json(value) if value else None
        for name, value in zip(services, raw, strict=True)
    }
