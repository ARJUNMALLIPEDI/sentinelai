import type {
  ApiErrorBody,
  FleetOverview,
  IncidentSummary,
  ReadinessResponse,
  SimulationStatus,
} from "./types";

const API_BASE = "/api/v1";

/** Error thrown for non-success responses; carries the server's request ID for debugging. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(status: number, code: string, message: string, requestId: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { acceptStatuses?: number[] } = {},
): Promise<T> {
  const { acceptStatuses = [], ...fetchInit } = init;
  const res = await fetch(`${API_BASE}${path}`, {
    ...fetchInit,
    headers: { Accept: "application/json", ...fetchInit.headers },
  });

  if (res.ok || acceptStatuses.includes(res.status)) {
    return (await res.json()) as T;
  }

  const requestId = res.headers.get("x-request-id");
  let body: Partial<ApiErrorBody> = {};
  try {
    body = (await res.json()) as ApiErrorBody;
  } catch {
    // Non-JSON error (e.g. proxy failure) — fall through with defaults.
  }
  throw new ApiError(
    res.status,
    body.error?.code ?? "http_error",
    body.error?.message ?? res.statusText,
    body.error?.request_id ?? requestId,
  );
}

export const api = {
  // 503 is an expected answer from readiness ("degraded"), not a failure to fetch.
  readiness: (signal?: AbortSignal) =>
    request<ReadinessResponse>("/health/ready", { signal, acceptStatuses: [503] }),
  fleet: (signal?: AbortSignal) => request<FleetOverview>("/services", { signal }),
  // 404 when simulation control isn't mounted (production); treated as "no simulator".
  activeIncidents: (signal?: AbortSignal) =>
    request<IncidentSummary[]>("/incidents?active=true&limit=20", { signal }),
  simulation: (signal?: AbortSignal) =>
    request<SimulationStatus>("/simulation/status", { signal }),
};
