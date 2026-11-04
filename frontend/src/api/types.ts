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
