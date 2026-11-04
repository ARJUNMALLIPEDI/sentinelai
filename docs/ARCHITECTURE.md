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
│                         "worker" process  (planned, M2+)                     │
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

### Why arq instead of Celery? *(planned)*

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
├── db/                  # engine/session, declarative Base
├── models/              # SQLAlchemy tables
├── schemas/             # Pydantic request/response models
├── api/                 # routers + dependencies (thin: parse → call service → serialise)
├── observability/       # Prometheus metrics + middleware
├── services/            # business logic (incidents, reports)              (M3+)
├── simulation/          # simulated services, failure injection           (M2)
├── monitoring/          # metric collection loop                           (M3)
├── detection/           # detector interface + threshold/statistical impls (M3)
├── logs/                # log ingestion + query                            (M2)
├── agent/               # orchestrator, tool registry, prompts, report schema (M4)
├── remediation/         # recommendations, approval state machine, executor (M5)
└── worker.py            # arq worker entrypoint                            (M2)
```

**Dependency direction** (enforced by convention, checked in review):

```
api ─► services ─► models/db
agent ─► agent.tools ─► services (read-only methods)
remediation.executor ─► simulation (the ONLY module allowed to mutate sim state)
core ◄─ everything
```

The agent package never imports `remediation.executor` or `simulation`
mutators. Its tools call read-only service functions. That is the code-level
expression of "the AI can never execute an action."

---

## 5. Data model (initial sketch)

```
services ──< service_instances
   │
   ├──< metric_samples (service_id, ts, cpu, mem, latency_p95, rps, error_rate, db_conns, …)
   ├──< log_entries    (service_id, ts, level, message, attrs JSONB, incident_id?)
   └──< deployments    (service_id, version, ts, status)

incidents (status, severity, detected_by, fingerprint) ──< incident_events (append-only timeline)
   ├──< investigations (status, started_at, model, tool_calls, tokens) ──< agent_steps
   ├──< incident_reports (JSONB validated by Pydantic report schema)
   └──< remediations (action, params, status: proposed→approved|rejected→executing→succeeded|failed)

audit_log (actor, action, entity, entity_id, payload JSONB, ts) — append-only
```

Milestone 1 ships only `services` to prove the migration pipeline end-to-end.

Enums are stored as `VARCHAR + CHECK` (`native_enum=False`), not Postgres
`ENUM` types. **Tradeoff:** we lose a little type strictness at the DB level,
but adding a value to a native Postgres enum requires a non-transactional
`ALTER TYPE`, which is a common migration footgun.

---

## 6. Agent design (preview of M4)

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

## 7. Engineering challenges we expect

1. **Realistic, correlated failures.** Independent random noise per metric is
   unconvincing. The simulator needs a dependency graph (api-gateway →
   orders → postgres) so a DB pool exhaustion shows up as upstream latency
   and errors. Failures are injected scenarios with seeds for reproducibility.
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
6. **Time-series growth.** `metric_samples` grows fast. Start with a
   `(service_id, ts)` index and a retention job; partition by day later.
7. **Testing async code against real infrastructure.** Integration tests run
   against real Postgres/Redis; each test gets a clean schema.
