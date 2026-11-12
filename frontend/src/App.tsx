import { ActiveIncidents } from "./components/ActiveIncidents";
import { AppShell } from "./components/layout/AppShell";
import { FleetHealth } from "./components/FleetHealth";
import { SystemStatus } from "./components/SystemStatus";

export default function App() {
  return (
    <AppShell>
      <div className="flex flex-col gap-6">
        <ActiveIncidents />
        <FleetHealth />
        <SystemStatus />
      </div>
    </AppShell>
  );
}
