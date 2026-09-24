import type { Health } from "../api/types";

export function HealthChip({ health, connected }: { health: Health | null; connected: boolean }) {
  if (!connected) {
    return <span className="chip chip-bad" role="status">Backend unavailable</span>;
  }
  if (!health) return <span className="chip" role="status">Checking backend...</span>;
  const rt = health.runtime;
  const runtime = `${rt.mode}${rt.running === null ? "" : rt.running ? ", running" : ", stopped"}`;
  return (
    <span className={`chip ${health.status === "ok" ? "chip-ok" : "chip-warn"}`} role="status"
          title={`Version ${health.version}`}>
      Backend {health.status === "ok" ? "ok" : "degraded"} ({runtime})
    </span>
  );
}
