import { AppShell } from "./components/layout/AppShell";
import { SystemStatus } from "./components/SystemStatus";

export default function App() {
  return (
    <AppShell>
      <SystemStatus />
    </AppShell>
  );
}
