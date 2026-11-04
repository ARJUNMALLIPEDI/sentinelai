import type { ReactNode } from "react";

const NAV = [
  { label: "Overview", active: true },
  { label: "Services", active: false },
  { label: "Incidents", active: false },
  { label: "Investigations", active: false },
  { label: "Approvals", active: false },
  { label: "Audit log", active: false },
];

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-800 bg-slate-900 text-slate-300 md:flex">
        <div className="flex h-14 items-center gap-2 border-b border-slate-800 px-4">
          <span className="h-2.5 w-2.5 rounded-full bg-ok" aria-hidden />
          <span className="font-semibold tracking-tight text-white">SentinelAI</span>
        </div>
        <nav className="flex flex-col gap-0.5 p-2 text-sm">
          {NAV.map((item) => (
            <span
              key={item.label}
              aria-current={item.active ? "page" : undefined}
              className={
                item.active
                  ? "rounded-md bg-slate-800 px-3 py-2 font-medium text-white"
                  : "cursor-not-allowed rounded-md px-3 py-2 text-slate-500"
              }
              title={item.active ? undefined : "Coming in a later milestone"}
            >
              {item.label}
            </span>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center border-b border-slate-200 bg-white px-6">
          <h1 className="text-sm font-semibold text-slate-700">Overview</h1>
        </header>
        <main className="flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}
