# SentinelAI — Architecture

This document is the design reference for SentinelAI. It is written before the
code and updated as milestones land. Sections marked *(planned)* describe
components that do not exist yet.

---

## 1. Requirements analysis

SentinelAI is four systems wearing one coat:

| Concern | What it really is | Hard part |
|---|---|---|
| Simulated infrastructure | A deterministic-but-noisy generator of metrics, logs, deployments and failures | Realism (correlated failures across services) and *reproducibility* for tests |
| Monitoring + detection | A periodic evaluator over time-series data | Avoiding alert storms; deduplicating anomalies into one incident |
| AI investigation | A bounded, auditable tool-use loop around Claude | Grounding (no invented evidence), cost/latency limits, structured output |
| Human-in-the-loop remediation | A state machine with an approval gate | Making it impossible for the agent to execute anything itself |

Two non-functional requirements shape almost every decision:

1. **Auditability** — every agent step, tool call, approval and action must be
   reconstructable after the fact. This pushes us toward append-only tables
   and explicit state machines rather than ad-hoc status flags.
2. **Safety boundary** — the agent can *read* through a fixed set of typed
   tools and can *propose* remediations. It can never execute. Execution lives
   in a different module, triggered only by an authenticated human approval.

---

## 2. System overview

```
                    ┌──────────────────────────────────────────────┐
                    │                React Dashboard               │
                    │  (TS, Tailwind, Recharts) — polls REST API   │
                    └───────────────────────┬──────────────────────┘
                                            │ HTTP /api/v1
┌───────────────────────────────────────────▼───────────────────────────────────┐
│                               FastAPI  "api" process                          │
│  routes ─► services (business logic) ─► repositories (SQLAlchemy)             │
│  /metrics (Prometheus)   /health/live   /health/ready                         │
│  Approval endpoints ──► remediation.executor (only path that mutates infra)   │
└───────┬──────────────────────────────┬──────────────────────────────┬────────┘
        │ SQL                          │ enqueue jobs / cache          │ scrape
┌───────▼────────┐            ┌────────▼────────┐             ┌────────▼───────┐
│   PostgreSQL   │◄───────────┤      Redis      ├────────────►│   Prometheus   │
│ incidents,     │   SQL      │ job queue (arq),│             └────────┬───────┘
│ reports, logs, │            │ metric cache,   │                      │
│ audit log      │            │ dedup locks     │             ┌────────▼───────┐
└───────▲────────┘            └────────▲────────┘             │    Grafana     │
        │                              │ consume jobs          └────────────────┘
┌───────┴──────────────────────────────┴──────────────────────────────────────┐
│                         "worker" process  (arq)                             │
│  simulation.engine  ─ tick ─►  metrics + logs  ─►  Postgres / Redis          │
│  monitoring.collector ─► detection.detectors ─► incidents.service           │
│  agent.orchestrator  ◄─ "investigate incident" job                          │
│      └─ Claude API (tool use) ⇄ agent.tools (read-only, typed, validated)   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Why two processes (api + worker)?

The simulator ticks every few seconds, detection runs on a schedule, and an
investigation can take 30–90 s of Claude round-trips. None of that should run
inside a request handler or compete with request latency. The worker is a
separate process consuming jobs from Redis.

**Tradeoff:** two processes means two things to deploy and a queue to reason
about. The alternative — FastAPI `BackgroundTasks` or an in-process
`asyncio` loop — is simpler but loses jobs on restart, can't scale
independently, and couples a slow LLM loop to the API's event loop.

### Why arq instead of Celery?

The whole backend is `async` (FastAPI, SQLAlchemy async, the Anthropic SDK's
async client). **arq** is an asyncio-native Redis job queue, so worker code is
the same async code as the API. Celery is more featureful (canvas, many
brokers, mature monitoring) but is sync-first; running async code in it
needs wrappers. For this scale arq is the better fit; the job interface will be
thin enough to swap.

---

## 3. Technology choices

| Tech | Role | Why this, not something else |
|---|---|---|
| **FastAPI** | HTTP API | Async-native, Pydantic-integrated validation, OpenAPI docs for free. |
| **Pydantic v2** | Schemas, settings, *agent output validation* | The same models validate API payloads, env config, and Claude's structured report — one source of truth. |
| **SQLAlchemy 2.0 (async) + asyncpg** | ORM / DB access | Typed `Mapped[...]` models, mature migrations via Alembic. asyncpg is the fastest Postgres driver for asyncio. |
| **Alembic** | Migrations | Schema changes are versioned and reviewable; `create_all()` is never used outside tests. |
| **PostgreSQL** | System of record | Relational integrity for incidents ↔ reports ↔ remediations ↔ audit; JSONB for evidence blobs. |
| **Redis** | Job queue, cache, locks | Low-latency hot data (latest metrics per service), dedup locks so two detectors don't open the same incident. |
| **Prometheus + Grafana** | Observability *of SentinelAI itself* | The platform exposes its own `/metrics` (request latency, agent tool calls, token usage). Grafana is the ops view; the React app is the product view. |
| **Anthropic Claude API (tool use)** | Investigation agent | Native tool use gives a structured, inspectable loop rather than prompt-parsing. |
| **React + TS + Tailwind + Recharts** | Dashboard | Typed API client, utility CSS for a dense enterprise UI, Recharts for time-series. |
| **Pytest** | Tests | Unit tests for pure logic; integration tests against real Postgres/Redis (no mocks of the DB). |

### A note on where simulated metrics live

Simulated service metrics (CPU, latency…) are **product data**: the agent
queries them, the dashboard charts them. They go in Postgres (time-indexed
table, later partitioned) with the latest values cached in Redis.

We *also* export them as Prometheus gauges so Grafana can chart them, but
Prometheus is not the source of truth for the agent. **Tradeoff:** we store
metrics twice. The benefit is that the agent's tools read from our own typed
store with exact timestamps we control, which makes "never invent a metric"
enforceable and tests deterministic.

---

## 4. Backend module layout and dependency rules

```
backend/app/
├── main.py              # app factory + lifespan (wiring only)
├── core/                # config, logging, errors — imported by everyone, imports nothing app-level
├── domain/              # dependency-free enums shared by every layer
├── db/                  # engine/session, declarative Base, column type helpers
├── models/              # SQLAlchemy tables
├── schemas/             # Pydantic request/response models
├── repositories/        # data access: catalog, telemetry (queries live here, not in routes)
├── api/                 # routers + dependencies (thin: parse → call repo/domain → serialise)
├── observability/       # Prometheus metrics + middleware for the API itself
├── simulation/          # pure fleet simulator: topology, faults, engine        (M2)
├── monitoring/          # health evaluation, Redis snapshot cache               (M2)
├── logs/                # log ingestion (single entry path for all logs)        (M2)
├── worker/              # arq jobs: tick, backfill, faults, retention           (M2)
├── detection/           # detector interface + threshold/statistical impls     (M3)
├── agent/               # orchestrator, tool registry, prompts, report schema   (M4)
└── remediation/         # recommendations, approval state machine, executor     (M5)
```

The business-logic layer is named `repositories/` (data access) plus pure
modules (`monitoring/health.py`, later `detection/`) rather than `services/`,
because "service" already means a microservice in the fleet, and
`services/service_service.py` helps nobody.

**Dependency direction** (enforced by convention, checked in review):

```
api ─► repositories ─► models/db
api ─► monitoring (pure)        worker ─► simulation + repositories + logs
agent ─► agent.tools ─► repositories (read-only functions)
remediation.executor ─► worker jobs ─► simulation (the ONLY path that mutates the fleet)
core, domain ◄─ everything
```

The agent package never imports `remediation.executor` or `simulation`
mutators. Its tools call read-only service functions. That is the code-level
expression of "the AI can never execute an action."

---

## 5. Data model

```
services (kind, tier, SLOs, current_version) ──< service_dependencies >── services
   │                                               (calls_per_request, criticality, is_async)
   ├──< metric_samples (service_id, ts) UNIQUE — one wide row per service per tick
   ├──< log_entries    (service_id, ts, level, message, instance, trace_id, attributes JSONB)
   └──< deployments    (service_id, version, previous_version, kind, initiated_by, deployed_at)

--- planned ---

incidents (status, severity, detected_by, fingerprint) ──< incident_events (append-only timeline)
   ├──< investigations (status, started_at, model, tool_calls, tokens) ──< agent_steps
   ├──< incident_reports (JSONB validated by Pydantic report schema)
   └──< remediations (action, params, status: proposed→approved|rejected→executing→succeeded|failed)

audit_log (actor, action, entity, entity_id, payload JSONB, ts) — append-only
```

Shipped so far: everything above the `planned` line (M1: `services`; M2: the
rest). Instance-level rows are deliberately absent — instance counts live on
each metric sample, which is all health and investigation need.

**Wide vs narrow metrics.** `metric_samples` has a typed column per signal
rather than `(service, metric_name, value)` rows. The signal set is fixed, a
"latest snapshot" is one row, and there are ~14× fewer rows. The cost is a
migration to add a signal. `UNIQUE (service_id, ts)` doubles as the index for
"one service over a window" and makes inserts idempotent (retried ticks and
overlapping backfills become no-ops).

**Log pagination** is a keyset cursor on `(ts, id)`, not `id` alone: a
backfill inserts *new* ids with *old* timestamps, so id order is not time order.

Enums are stored as `VARCHAR + CHECK` (`native_enum=False`), not Postgres
`ENUM` types. **Tradeoff:** we lose a little type strictness at the DB level,
but adding a value to a native Postgres enum requires a non-transactional
`ALTER TYPE`, which is a common migration footgun.

---

## 6. The simulated fleet (M2)

Eight services: `api-gateway`, `auth`, `orders`, `inventory`, `payments`,
`notification` (async consumer), `postgres-primary`, `redis-cache`.

```
api-gateway ─┬─► auth-service ─────────┬─► redis-cache
             ├─► orders-service ─┬─────┼─► postgres-primary
             │                   ├─► payments-service ─► postgres-primary
             │                   ├─► inventory-service ─┬─► postgres-primary
             │                   └┄► notification (async)└─► redis-cache
             └─► inventory-service
```

The engine (`app/simulation/engine.py`) is pure and deterministic per seed.
Each tick:

1. **Traffic flows callers-first.** Only edge services get external traffic
   (with a daily cycle); everyone else gets callers' *admitted* traffic ×
   calls-per-request. An upstream outage removes downstream load.
2. **Latency uses M/M/1 queueing**, `R = S / (1 − ρ)`: flat at low
   utilisation, steep near saturation. Sync dependency latency is added.
3. **Connection pools use Little's law**: in use = rate × hold time. Slow
   queries lengthen hold time, so a slow DB appears as pool exhaustion in the
   *callers* — the classic "the DB looks fine but every service is timing out"
   incident.
4. **Errors propagate by criticality**: a cache failure barely hurts auth (it
   falls back to the DB); an inventory outage fails ~90% of orders but only
   ~50% of gateway traffic; a notification outage fails nothing (async).
5. **Noise is AR(1)**, so series are autocorrelated like real metrics.

**Faults change one service's local behaviour; everything else emerges.** A
DB connection leak is injected into `orders-service` alone; gateway errors
and the 503 logs in its callers are consequences, not scripted. If faults
edited upstream metrics directly, the root cause would be baked into the data
and the M4 agent would have nothing real to reason about.

**A bad deploy looks routine.** `latency_regression` records an ordinary
`ci-pipeline` deployment — routine deploys happen too (~4/day/service) — so
the agent has to correlate deploy time with the latency change, not read a
label. Clearing it records a rollback.

**Logs are rendered from the same tick's metrics**, so "pool active=60/60" in
a log line equals that tick's `db_conn_active/db_conn_max` (a unit test
enforces this). Evidence sources never contradict each other.

### Worker

`arq` runs a cron tick every `SIM_TICK_SECONDS` plus queued jobs (fault
inject/clear, retention). Engine calls are synchronous, so interleaving jobs
can't observe half-updated state. Each tick: persist samples/logs/deploys →
write per-service snapshots to Redis (TTL = 3 ticks) → heartbeat → gauges.

- **One replica only.** The fleet lives in the worker's memory; a second
  replica would be a second, divergent fleet. Restarting resets faults and
  backfills the gap (bounded by `SIM_BACKFILL_MINUTES`).
- **Snapshots expire.** If the worker dies, health becomes `unknown` within
  three ticks — the dashboard can't show a frozen "all healthy".
- **The API never touches the engine.** It validates fault requests (so bad
  ones get a synchronous 422/409) and enqueues a job whose id is the fault id,
  so a retried request can't enqueue twice. Simulation control is not mounted
  in production.

### Health vs detection

`monitoring/health.py` compares the latest sample with fixed objectives
(p95 and error-rate SLOs per service, CPU/memory/pool ceilings) and returns
`healthy | degraded | down | unknown` with human-readable reasons. That is
*not* anomaly detection (M3), which compares a signal with its own history and
can fire while a service is still within SLO — e.g. the pool climbing from 15
to 38 during a leak is "healthy" but clearly abnormal.

---

## 7. Agent design (preview of M4)

- **Orchestrator loop:** `messages.create(tools=…)` → for each `tool_use`
  block, validate input with the tool's Pydantic model → execute against
  read-only services with a per-tool timeout → return `tool_result` → repeat
  until the model calls `generate_incident_report` or limits trip.
- **Limits:** max tool calls, max wall-clock, max tokens per investigation;
  hitting a limit produces a partial report marked as such, never a silent
  failure.
- **Grounding:** every tool result gets an `evidence_id`. The final report
  schema requires findings to cite evidence IDs; the orchestrator rejects
  reports that cite IDs it never issued, and separates `confirmed_findings`
  from `hypotheses`.
- **Persistence:** every step (prompt, tool input, tool output summary,
  tokens, latency) is written to `agent_steps` for audit and replay.
- **Testing:** the Claude client sits behind an interface, so integration
  tests drive the orchestrator with scripted model responses — no API key
  needed in CI.

---

## 8. Engineering challenges

1. **Realistic, correlated failures.** *(Addressed in M2, §6.)* Faults are
   local; propagation comes from the dependency graph, queueing and pool
   models, and runs are reproducible by seed.
2. **Alert storms and dedup.** One failure trips many detectors across many
   services. Anomalies are fingerprinted (service + signal + window) and
   merged into an open incident under a Redis lock instead of creating one
   incident each.
3. **Statistical detection without false positives.** Rolling z-score / EWMA
   need warm-up windows and minimum-duration rules; we'll test them against
   recorded synthetic series with known anomalies.
4. **LLM grounding and cost.** Bounded loops, evidence IDs, schema validation
   with a retry-on-invalid-output step, token accounting.
5. **Approval safety.** Remediation transitions are a strict state machine
   with optimistic locking so a double-click can't execute twice; the executor
   re-validates the action against current state at execution time.
6. **Time-series growth.** ~140k metric rows/day at 5 s ticks. M2 has the
   `(service_id, ts)` index and a 15-minute retention job (24 h default);
   daily partitions (drop a partition instead of `DELETE`) come when needed.
7. **Testing async code against real infrastructure.** Integration tests run
   against real Postgres/Redis; each test gets a clean schema.
