import { api } from "../api/client";
import type { IncidentSeverity, IncidentSummary } from "../api/types";
import { usePolling } from "../hooks/usePolling";

const POLL_MS = 5_000;

const SEVERITY_STYLE: Record<IncidentSeverity, string> = {
  sev1: "bg-crit text-white",
  sev2: "bg-orange-500 text-white",
  sev3: "bg-warn text-amber-950",
  sev4: "bg-slate-200 text-slate-700",
};

function age(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function IncidentRow({ inc, now }: { inc: IncidentSummary; now: number }) {
  const recovered = inc.signals_cleared_at !== null;
  return (
    <li className="flex items-start gap-3 px-4 py-3">
      <span
        className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold uppercase ${
          SEVERITY_STYLE[inc.severity]
        }`}
      >
        {inc.severity}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-xs text-slate-400">{inc.ref}</span>
          <span className="truncate text-sm font-medium text-slate-800">{inc.title}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
          {inc.services.map((s) => (
            <span key={s} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-600">
              {s}
            </span>
          ))}
        </div>
      </div>
      <div className="shrink-0 text-right text-[11px] leading-5 text-slate-500">
        <div className="font-medium capitalize text-slate-700">{inc.status}</div>
        <div>
          {recovered ? (
            <span className="text-emerald-700">signals recovered</span>
          ) : (
            `${inc.active_anomalies} active anomal${inc.active_anomalies === 1 ? "y" : "ies"}`
          )}
        </div>
        <div>opened {age(inc.opened_at, now)} ago</div>
      </div>
    </li>
  );
}

export function ActiveIncidents() {
  const { data, error, loading, lastUpdated } = usePolling(api.activeIncidents, POLL_MS);
  const now = lastUpdated?.getTime() ?? Date.now();

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Active incidents
        </h2>
        {data && <span className="text-xs text-slate-400">{data.length} open</span>}
      </div>
      {loading && <p className="px-4 py-4 text-sm text-slate-500">Loading…</p>}
      {error && !data && (
        <p className="px-4 py-4 text-sm text-crit">Could not load incidents: {error.message}</p>
      )}
      {data && data.length === 0 && (
        <p className="flex items-center gap-2 px-4 py-4 text-sm text-slate-500">
          <span className="h-2 w-2 rounded-full bg-ok" aria-hidden />
          No active incidents
        </p>
      )}
      {data && data.length > 0 && (
        <ul className="divide-y divide-slate-100">
          {data.map((inc) => (
            <IncidentRow key={inc.id} inc={inc} now={now} />
          ))}
        </ul>
      )}
    </section>
  );
}
