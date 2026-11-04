import { ApiError, api } from "../api/client";
import type { DependencyCheck } from "../api/types";
import { usePolling } from "../hooks/usePolling";

const POLL_MS = 10_000;

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${ok ? "bg-ok" : "bg-crit"}`}
      aria-hidden
    />
  );
}

function DependencyRow({ name, check }: { name: string; check: DependencyCheck }) {
  const ok = check.status === "ok";
  return (
    <li className="flex items-center justify-between py-2.5 text-sm">
      <span className="flex items-center gap-2 font-medium capitalize text-slate-700">
        <StatusDot ok={ok} />
        {name}
      </span>
      <span className="font-mono text-xs text-slate-500">
        {ok ? `${check.latency_ms.toFixed(1)} ms` : (check.error ?? "error")}
      </span>
    </li>
  );
}

/** Platform self-health: is the SentinelAI backend able to reach its own dependencies? */
export function SystemStatus() {
  const { data, error, loading, lastUpdated } = usePolling(api.readiness, POLL_MS);

  const unreachable = error !== null && data === null;
  const healthy = data?.status === "ok" && error === null;
  const headline = loading
    ? "Checking…"
    : unreachable
      ? "API unreachable"
      : healthy
        ? "All systems operational"
        : "Degraded";

  return (
    <section className="max-w-md rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Platform status
        </h2>
        {lastUpdated && (
          <span className="text-xs text-slate-400">
            updated {lastUpdated.toLocaleTimeString()}
          </span>
        )}
      </div>
      <div className="px-4 py-3">
        <p
          className={`flex items-center gap-2 text-base font-semibold ${
            loading ? "text-slate-500" : healthy ? "text-slate-900" : "text-crit"
          }`}
          role="status"
        >
          {!loading && <StatusDot ok={healthy} />}
          {headline}
        </p>
        {error && (
          <p className="mt-1 text-xs text-slate-500">
            {error.message}
            {error instanceof ApiError && error.requestId ? ` (request ${error.requestId})` : ""}
          </p>
        )}
        {data && (
          <ul className="mt-2 divide-y divide-slate-100">
            {Object.entries(data.checks).map(([name, check]) => (
              <DependencyRow key={name} name={name} check={check} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
