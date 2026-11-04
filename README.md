# SentinelAI

An AI-assisted cloud operations platform: it monitors a simulated distributed
fleet, detects anomalies, has a Claude tool-using agent investigate incidents
against real (simulated) evidence, and proposes remediations that only run
after a human approves them.

> **Status:** Milestone 1 of 7 — platform foundation. See [Roadmap](#roadmap).

Design rationale, data model and the agent safety boundary are in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Stack

| Layer | Tech |
|---|---|
| API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async) + asyncpg, Alembic |
| Data | PostgreSQL 16 (system of record), Redis 7 (queue, cache, locks) |
| AI | Anthropic Claude API with native tool use *(Milestone 4)* |
| Observability | Prometheus, Grafana, structured JSON logs with request IDs |
| Frontend | React 19, TypeScript, Tailwind CSS v4, Vite, Recharts *(Milestone 6)* |
| Tests | Pytest, pytest-asyncio, httpx — integration tests use real Postgres/Redis |

---

## Quick start (Docker)

Requires Docker with Compose v2.

```bash
cp .env.example .env          # then edit the passwords
docker compose up --build -d
docker compose ps             # wait until api shows "healthy"
```

| URL | What |
|---|---|
| http://localhost:5173 | Dashboard |
| http://localhost:8000/docs | API docs (OpenAPI) |
| http://localhost:8000/api/v1/health/ready | Readiness (Postgres + Redis) |
| http://localhost:8000/metrics | Prometheus metrics for the API |
| http://localhost:9090 | Prometheus |
| http://localhost:3000 | Grafana (`GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`) |

Run the tests inside the stack:

```bash
make test          # all backend tests (unit + integration)
make test-unit     # unit tests only
make lint          # ruff + mypy (strict)
make fe-check      # frontend type-check + production build
```

> If you created the Postgres volume **before** pulling this version, the test
> database won't exist (the init script only runs on a fresh volume). Either
> `docker compose down -v` to recreate, or create it once:
> `docker compose exec postgres createdb -U sentinel sentinel_test`.

---

## Local development (without Docker for the app)

Run Postgres and Redis however you like (e.g. `docker compose up -d postgres redis`),
then:

```bash
# Backend
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example .env                     # DATABASE_URL / REDIS_URL point at localhost
alembic upgrade head
uvicorn app.main:create_app --factory --reload

# Tests (create a test DB first: createdb -U sentinel sentinel_test)
pytest -q

# Frontend (separate terminal)
cd frontend
npm install
npm run dev                                  # proxies /api to localhost:8000
```

Integration tests read `TEST_DATABASE_URL` and `TEST_REDIS_URL`
(defaults: `postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel_test`
and `redis://localhost:6379/15`).

---

## Configuration

All configuration is via environment variables, validated at startup by
`app/core/config.py`. The app refuses to start with a malformed value.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | — | `postgresql://` is accepted and normalised to `postgresql+asyncpg://` |
| `REDIS_URL` | yes | — | `redis://` or `rediss://` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Compose | — | Used by the postgres container and to build `DATABASE_URL` in Compose |
| `ENVIRONMENT` | no | `development` | `development` \| `test` \| `production` |
| `LOG_LEVEL` | no | `INFO` | |
| `LOG_JSON` | no | `true` | `false` for human-readable local logs |
| `CORS_ORIGINS` | no | `["http://localhost:5173"]` | JSON list |
| `ANTHROPIC_API_KEY` | from M4 | — | Stored as `SecretStr`; never logged |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-5-5` | |
| `GRAFANA_ADMIN_PASSWORD` | Compose | — | |

---

## Project layout

```
sentinelai/
├── backend/
│   ├── app/
│   │   ├── main.py            # app factory + lifespan
│   │   ├── core/              # config, logging, errors
│   │   ├── db/                # engine/session, Base + naming conventions
│   │   ├── models/            # SQLAlchemy tables
│   │   ├── schemas/           # Pydantic request/response models
│   │   ├── api/               # routers + dependencies
│   │   └── observability/     # Prometheus metrics, request-context middleware
│   ├── alembic/               # migrations
│   └── tests/{unit,integration}/
├── frontend/src/
│   ├── api/                   # typed client + response types
│   ├── hooks/                 # usePolling
│   └── components/
├── infra/                     # prometheus, grafana provisioning, postgres init
├── docs/ARCHITECTURE.md
└── docker-compose.yml
```

Packages for simulation, monitoring, detection, logs, agent and remediation are
added in their milestones (see the planned layout in ARCHITECTURE.md §4).

---

## Roadmap

- [x] **M1 — Foundation:** architecture, app factory, config, structured logging,
      health/readiness, Prometheus metrics, Alembic, Compose, dashboard shell
- [ ] **M2 — Infrastructure simulation:** service graph, metrics, logs, deployments, failure scenarios
- [ ] **M3 — Monitoring & anomaly detection:** threshold + statistical detectors, incident dedup
- [ ] **M4 — AI investigation agent:** Claude tool use, bounded loop, grounded reports
- [ ] **M5 — Remediation:** approval state machine, simulated execution, audit log
- [ ] **M6 — Dashboard:** metrics charts, incidents, investigations, approvals
- [ ] **M7 — Hardening:** failure-mode tests, performance, production images, deployment
