import { api } from "../api/client";
import type { HealthStatus, ServiceSummary, SimulationStatus } from "../api/types";
import { usePolling } from "../hooks/usePolling";

const POLL_MS = 5_000;

const STATUS_STYLE: Record<HealthStatus, { dot: string; text: string; label: string }> = {
  healthy: { dot: "bg-ok", text: "text-slate-700", label: "Healthy" },
  degraded: { dot: "bg-warn", text: "text-amber-700", label: "Degraded" },
  down: { dot: "bg-crit", text: "text-crit", label: "Down" },
  unknown: { dot: "bg-slate-300", text: "text-slate-500", label: "Unknown" },
};
const ORDER: HealthStatus[] = ["down", "degraded", "unknown", "healthy"];

const fmtPct = (x: number, digits = 1) => `${(x * 100).toFixed(digits)}%`;

function StatusPill({ status }: { status: HealthStatus }) {
  const s = STATUS_STYLE[status];
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${s.text}`}>
      <span className={`h-2 w-2 rounded-full ${s.dot}`} aria-hidden />
      {s.label}
    </span>
  );
}

/** A metric cell that turns red when it breaches its threshold. */
function Metric({ value, breach }: { value: string; breach?: boolean }) {
  return (
    <td
      className={`px-3 py-2 text-right font-mono text-xs tabular-nums ${
        breach ? "font-semibold text-crit" : "text-slate-600"
      }`}
    >
      {value}
    </td>
  );
}

function ServiceRow({ svc }: { svc: ServiceSummary }) {
  const m = svc.latest;
  const pool =
    m && m.db_conn_active !== null && m.db_conn_max
      ? { text: `${m.db_conn_active}/${m.db_conn_max}`, breach: m.db_conn_active / m.db_conn_max >= 0.9 }
      : null;
  return (
    <tr className="border-t border-slate-100 align-top">
      <td className="px-3 py-2">
        <div className="text-sm font-medium text-slate-800">{svc.name}</div>
        <div className="font-mono text-[11px] text-slate-400">
          {svc.kind} · {svc.current_version ?? "—"}
        </div>
      </td>
      <td className="px-3 py-2">
        <StatusPill status={svc.health.status} />
        {svc.health.reasons.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-[11px] leading-tight text-slate-500">
            {svc.health.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
      </td>
      {m ? (
        <>
          <Metric
            value={`${m.latency_p95_ms.toFixed(0)} / ${svc.slo_latency_p95_ms.toFixed(0)}`}
            breach={m.latency_p95_ms > svc.slo_latency_p95_ms}
          />
          <Metric value={fmtPct(m.error_rate, 2)} breach={m.error_rate > svc.slo_error_rate} />
          <Metric value={m.rps.toFixed(0)} />
          <Metric value={`${m.cpu_pct.toFixed(0)}%`} breach={m.cpu_pct > 90} />
          <Metric value={`${m.memory_pct.toFixed(0)}%`} breach={m.memory_pct > 90} />
          <Metric value={pool?.text ?? "—"} breach={pool?.breach} />
          <Metric
            value={`${m.instances_healthy}/${m.instances_total}`}
            breach={m.instances_healthy < m.instances_total}
          />
        </>
      ) : (
        <td colSpan={7} className="px-3 py-2 text-center text-xs text-slate-400">
          no recent data
        </td>
      )}
    </tr>
  );
}

function SimulatorBadge({ sim }: { sim: SimulationStatus | null }) {
  if (!sim) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 ${
          sim.running ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-crit"
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${sim.running ? "bg-ok" : "bg-crit"}`} />
        Simulator {sim.running ? `ticking every ${sim.tick_seconds}s` : "stalled"}
      </span>
      {sim.active_faults.map((f) => (
        <span key={f.id} className="rounded-full bg-amber-50 px-2 py-0.5 font-mono text-amber-800">
          {f.type} → {f.service}
        </span>
      ))}
    </div>
  );
}

const HEADERS = ["p95 / SLO ms", "Errors", "RPS", "CPU", "Mem", "DB conns", "Inst."];

export function FleetHealth() {
  const fleet = usePolling(api.fleet, POLL_MS);
  const sim = usePolling(api.simulation, POLL_MS);

  const services = [...(fleet.data?.services ?? [])].sort(
    (a, b) =>
      ORDER.indexOf(a.health.status) - ORDER.indexOf(b.health.status) ||
      a.name.localeCompare(b.name),
  );

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <div className="flex items-center gap-4">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Fleet health
          </h2>
          {fleet.data && (
            <div className="flex gap-3 text-xs text-slate-500">
              {ORDER.filter((s) => fleet.data!.counts[s] > 0).map((s) => (
                <span key={s} className="inline-flex items-center gap-1">
                  <span className={`h-2 w-2 rounded-full ${STATUS_STYLE[s].dot}`} />
                  {fleet.data!.counts[s]} {s}
                </span>
              ))}
            </div>
          )}
        </div>
        <SimulatorBadge sim={sim.data} />
      </div>

      {fleet.error && !fleet.data && (
        <p className="px-4 py-6 text-sm text-crit">Could not load fleet: {fleet.error.message}</p>
      )}
      {fleet.loading && <p className="px-4 py-6 text-sm text-slate-500">Loading…</p>}

      {services.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] text-left">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-slate-400">
                <th className="px-3 py-2 font-medium">Service</th>
                <th className="px-3 py-2 font-medium">Status</th>
                {HEADERS.map((h) => (
                  <th key={h} className="px-3 py-2 text-right font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {services.map((svc) => (
                <ServiceRow key={svc.name} svc={svc} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
