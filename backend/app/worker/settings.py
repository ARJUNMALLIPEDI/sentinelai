"""arq entrypoint:  arq app.worker.settings.WorkerSettings

Kept separate from ``jobs`` because arq reads these class attributes at import
time, which requires the environment to be configured. Tests import ``jobs``
directly and never touch this module.
"""

from arq import cron, func
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.worker import jobs

_settings = get_settings()
_DETECT_OFFSET = min(2, _settings.sim_tick_seconds - 1)


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    functions = [
        jobs.inject_fault,
        jobs.clear_fault,
        # The agent enforces its own wall-clock budget; arq's timeout is the backstop.
        func(jobs.investigate, timeout=_settings.agent_timeout_s + 60, max_tries=1),
    ]
    cron_jobs = [
        # unique=True: if a tick overruns its interval, the next one is skipped
        # rather than run concurrently.
        cron(
            jobs.tick,
            second=set(range(0, 60, _settings.sim_tick_seconds)),
            unique=True,
            timeout=_settings.sim_tick_seconds * 2,
        ),
        # Detection runs once per tick, offset so it sees the tick that just landed.
        cron(
            jobs.detect,
            second={(s + _DETECT_OFFSET) % 60 for s in range(0, 60, _settings.sim_tick_seconds)},
            unique=True,
            timeout=_settings.sim_tick_seconds * 2,
        ),
        cron(jobs.prune, minute={0, 15, 30, 45}, second=30, unique=True, timeout=120),
    ]
    on_startup = jobs.startup
    on_shutdown = jobs.shutdown
    max_jobs = 10
    job_timeout = 30
    keep_result = 300
