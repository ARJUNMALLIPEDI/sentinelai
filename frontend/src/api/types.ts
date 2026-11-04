// Mirrors backend/app/schemas. Kept hand-written for now; in Milestone 6 we can
// generate these from the FastAPI OpenAPI schema so the two can't drift.

export type CheckStatus = "ok" | "error";

export interface DependencyCheck {
  status: CheckStatus;
  latency_ms: number;
  error: string | null;
}

export interface ReadinessResponse {
  status: "ok" | "degraded";
  checks: Record<string, DependencyCheck>;
}

export interface ApiErrorBody {
  error: { code: string; message: string; request_id: string | null };
}

export type HealthStatus = "healthy" | "degraded" | "down" | "unknown";

export interface MetricSnapshot {
  service: string;
  ts: string;
  cpu_pct: number;
  memory_pct: number;
  rps: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  latency_p99_ms: number;
  error_rate: number;
  db_conn_active: number | null;
  db_conn_max: number | null;
  cache_hit_rate: number | null;
  instances_healthy: number;
  instances_total: number;
}

export interface ServiceSummary {
  name: string;
  kind: "api" | "database" | "cache";
  tier: "critical" | "standard" | "internal";
  description: string | null;
  current_version: string | null;
  slo_latency_p95_ms: number;
  slo_error_rate: number;
  health: {
    status: HealthStatus;
    reasons: string[];
    availability: number | null;
    observed_at: string | null;
  };
  latest: MetricSnapshot | null;
}

export interface FleetOverview {
  generated_at: string;
  counts: Record<HealthStatus, number>;
  services: ServiceSummary[];
}

export interface ActiveFault {
  id: string;
  type: string;
  service: string;
  intensity: number;
  started_at: string;
  expires_at: string | null;
}

export interface SimulationStatus {
  running: boolean;
  last_tick: string | null;
  tick_seconds: number;
  active_faults: ActiveFault[];
}
