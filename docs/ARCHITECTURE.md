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
├── detection/           # detectors, correlation planner, evaluation harness   (M3)
├── worker/              # arq jobs: tick, detect, backfill, faults, retention  (M2/M3)
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
api ─► monitoring (pure)        worker ─► simulation + detection + repositories + logs
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

incidents (number → INC-n, status, severity, signals_cleared_at, last_activity_at)
   ├──< incident_services >── services
   ├──< anomalies (service, signal, detector, status, value/peak/baseline/threshold/score)
   │       UNIQUE (service_id, signal, detector) WHERE status = 'active'
   ├──< incident_events  — append-only timeline (kind, actor, message, data JSONB)
   ├──< log_entries.incident_id — evidence; exempt from retention
   ├──< investigations (status, model, effort, tokens, cost)
   │       ONE active per incident (partial unique index)  ──< agent_steps (audit trail)
   └──< incident_reports (report JSONB, partial, root-cause status)

--- planned ---
incidents ──< remediations (action, params, status: proposed→approved|rejected→executing→succeeded|failed)

audit_log (actor, action, entity, entity_id, payload JSONB, ts) — append-only
```

Shipped so far: everything above the last `planned` line (M1: `services`;
M2: telemetry; M3: incidents; M4: investigations). Instance-level rows are deliberately absent — instance counts live on
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

## 7. Detection and incidents (M3)

```
worker cron (every tick, +2 s)
  └─ one transaction, pg_try_advisory_xact_lock
       load ~30 min of metric_samples  →  detectors (pure)  →  findings
       load active anomalies + open incidents
       plan(findings, state, topology)  (pure)  →  Plan
       apply Plan (insert/update + timeline events)  →  link evidence logs
```

**Two detectors, one interface.** `Detector.detect(series, policy) → [Finding]`.

* *Threshold* — objectives and capacity limits with a Prometheus-style
  `for:` duration (3 samples for SLOs; 4 for lost instances so a rolling
  deploy's 2-tick restart never pages; 1 for "no healthy instances").
* *Robust z-score* — `(x − median) / (1.4826·MAD)` over a 30-minute baseline,
  confirmed on 3 consecutive samples, gated by direction plus minimum
  absolute/relative change. Median/MAD rather than mean/σ so an earlier spike
  in the window can't inflate the spread and mask the next anomaly (a unit test
  pins this). Its value is *early warning*: it flags a connection leak about a
  minute, and a memory leak about five, before any objective is breached.

**Correlation is a pure planner** (`detection/correlation.py`). Anomalies are
keyed `(service, signal, detector)` and refreshed, not duplicated. New
findings are grouped into connected components of the dependency graph over
currently-anomalous services and attached to an open incident touching the
component or its neighbours (if active in the last 10 minutes), else open one.
Severity = worst `anomaly severity × service tier` and only escalates
automatically. Anomalies resolve after 30 s of quiet (hysteresis). When an
incident's signals all recover, an untouched (`open`) incident auto-resolves
after `INCIDENT_AUTO_RESOLVE_MINUTES`; once anyone engages, they own closure.

*Known limitation:* topology grouping over-merges through hubs. Two unrelated
faults that both reach `postgres-primary` land in one incident (reproducible:
error burst on payments + cache degradation). Separating them needs reasoning
over evidence, which is the M4 agent's job.

**Concurrency, defended at three levels.** arq's `unique` cron prevents
overlapping runs in one worker; an advisory lock skips a cycle that races
another process; and a partial unique index makes a duplicate *active* anomaly
impossible even if both fail. Status changes from the API take a row lock
(`SELECT … FOR UPDATE`). The detector's own writes are compare-and-set
(`UPDATE … WHERE status = 'open'`, `WHERE severity > :new`), so it can never
resolve an incident someone just acknowledged or lower a severity a human set.

**One plan, two executors.** `InMemoryStore` (evaluation, tests) and the
Postgres executor apply the same `Plan`. An integration test feeds identical
samples through both and requires identical results, so the evaluation numbers
describe what production actually does.

**Evaluation** (`python -m app.detection.evaluate`): 0 false positives across
24 simulated fault-free hours (3 seeds, peak and trough, about two dozen routine deploys);
every scenario detected (outage 0 s, most 5–10 s, connection leak 20–25 s,
memory leak 85–115 s), grouped into exactly one incident, recovered and
auto-resolved. A trimmed version runs in the unit suite as a regression guard.

**Evidence outlives telemetry.** WARNING+ logs from an incident's services
(from 2 minutes before it opened) are linked to it and exempt from retention.

**Incident status machine** (`domain/incidents.py`):
`open → acknowledged → investigating → mitigated → resolved`, forward skips
allowed, `mitigated → investigating` (a mitigation can fail), `resolved → open`
(reopen). Resolving closes the incident's still-active anomalies; if the
problem persists, the next cycle opens a *new* incident.

---

## 8. Investigation agent (M4)

```
POST /incidents/INC-1/investigations ─► investigations row (queued) ─► arq job
  investigate: claim (queued→running, CAS) ─► incident → investigating
    brief (anomalies, timeline, topology) ─► orchestrator loop:
      Claude ─► tool_use ─► validate ─► read-only tool (timeout) ─► tool_result + E-id
        └─ ... ─► generate_incident_report ─► schema + grounding checks
    persist: agent_steps (every call), incident_reports, timeline event
```

**Model and API settings** (Claude Sonnet 5.5): adaptive thinking,
`output_config.effort = "medium"` (the documented starting point for
multistep tool use), `strict: true` tools, and automatic prompt caching (the
system prompt, tools and conversation prefix are re-read at the cache rate on
every turn).

**Tools are typed, read-only and anchored.** Seven tools: metrics, logs,
health and topology, DB connections, deployments, related incidents/anomalies,
plus the terminal `generate_incident_report`. Each input schema is generated
from the Pydantic model that re-validates it, cleaned for strict mode (closed
objects, no length/range keywords), with service names pinned to an `enum` of
real services. Outputs are summaries (baselines, top log patterns, 12-point
series), not raw dumps. "Now" is the incident's anchor time, so re-running an
investigation later still examines the incident.

**Grounding is enforced, not requested.** Every successful tool result gets
an evidence ID. The report schema requires evidence on findings, metrics, log
excerpts and remediations; the orchestrator rejects reports that cite IDs it
never issued, name non-existent services, or call a root cause "confirmed"
without evidence or with low confidence. Rejections go back as `is_error` tool
results listing the problems and the valid IDs, so the model corrects itself
(up to 3 attempts).

**The loop is bounded and protocol-correct.**

| Limit / rule | Behaviour |
|---|---|
| Tool calls (20), turns (14), wall clock (300 s), report attempts (3) | ends `incomplete` with a partial report of evidence gathered |
| Tool timeout (15 s), tool exception, invalid input, unknown tool | `is_error` result; the investigation continues |
| `end_turn` without a report | one nudge, then `incomplete` |
| `max_tokens` mid tool call | error result for each cut `tool_use`, one nudge |
| `refusal`, API failure after SDK retries | `failed`, recorded on the timeline |
| Assistant turns | echoed verbatim (thinking signatures included) |
| Forced `tool_choice` | not used: rejected with adaptive thinking on Sonnet 5.5 |

**Safety boundary.** The agent package cannot import the simulator, the worker
or remediation code (a unit test parses its imports), and its tool registry is
pinned to the seven read tools plus the report. Log text is untrusted input:
the system prompt and the logs tool both say it is data, never instructions.
Since `POST /logs/batch` accepts arbitrary text, prompt injection via logs is
a real vector; its blast radius is limited to a misleading report, because the
agent has no way to act.

**Lifecycle.** A partial unique index allows one queued/running investigation
per incident. Claiming is compare-and-set. Starting an investigation moves the
incident to `investigating`, which also stops detection auto-resolving it.
Every exception ends the row in a terminal state; on worker start,
investigations older than the time budget are marked abandoned (newer ones may
still be running from the CLI).

**Testing without a key.** `LLMClient` is an interface; tests drive the real
loop, real tools and real Postgres with a scripted model covering every path
above. A mock HTTP transport checks the exact request the SDK sends (effort,
thinking, caching, strict tools, no `tool_choice`) and the response parsing.
Live runs use `make investigate inc=INC-n`.

---

## 9. Engineering challenges

1. **Realistic, correlated failures.** *(Addressed in M2, §6.)* Faults are
   local; propagation comes from the dependency graph, queueing and pool
   models, and runs are reproducible by seed.
2. **Alert storms and dedup.** *(Addressed in M3, §7.)* Keyed anomalies,
   topology-aware grouping, advisory lock + partial unique index.
3. **Statistical detection without false positives.** *(Addressed in M3, §7.)*
   Robust baseline, warm-up, confirmation, direction and minimum-change
   guards — measured, not assumed, by the evaluation harness.
4. **LLM grounding and cost.** *(Addressed in M4, §8.)* Bounded loop,
   enforced evidence citations with self-correction, prompt caching, token
   and cost accounting per investigation.
5. **Approval safety.** Remediation transitions are a strict state machine
   with optimistic locking so a double-click can't execute twice; the executor
   re-validates the action against current state at execution time.
6. **Time-series growth.** ~140k metric rows/day at 5 s ticks. M2 has the
   `(service_id, ts)` index and a 15-minute retention job (24 h default);
   daily partitions (drop a partition instead of `DELETE`) come when needed.
7. **Testing async code against real infrastructure.** Integration tests run
   against real Postgres/Redis; each test gets a clean schema.
