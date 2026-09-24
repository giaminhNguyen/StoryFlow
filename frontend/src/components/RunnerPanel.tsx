import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { RunnerSnapshot } from "../api/types";
import { errorMessage, formatTime } from "../format";

function runnerName(r: RunnerSnapshot): string {
  return r.external_id ?? r.runner_type;
}

export function RunnerPanel({ client, workflowId, terminal, assigned, all, onChanged }: {
  client: ApiClient; workflowId: string; terminal: boolean;
  assigned: RunnerSnapshot[]; all: RunnerSnapshot[] | null; onChanged: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const unassigned = (all ?? []).filter((r) => !r.assigned);

  const run = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
      await onChanged();
    }
  };

  return (
    <section aria-labelledby="runners-h" className="card">
      <h2 id="runners-h">Runners</h2>
      {error && (
        <div role="alert" className="banner banner-error">
          {error}{" "}
          <button type="button" onClick={() => setError(null)} aria-label="Dismiss runner message">Dismiss</button>
        </div>
      )}
      <h3>Assigned to this workflow</h3>
      {assigned.length === 0 ? <p className="muted">No runners assigned.</p> : (
        <ul className="list">
          {assigned.map((r) => (
            <li key={r.id} aria-label={`Runner ${runnerName(r)}`}>
              <strong>{runnerName(r)}</strong> ({r.runner_type}) - state {r.state}
              {r.effective_state !== r.state && `, effective ${r.effective_state}`}
              {!r.enabled && ", disabled"}; active {r.active_count}/{r.max_concurrency};
              roles: {r.supported_roles.join(", ") || "none"}
              {r.cooldown_until && `; cooldown until ${formatTime(r.cooldown_until)}`}
              {r.quota_reset_at && `; quota resets ${formatTime(r.quota_reset_at)}`}
              <div className="row">
                <button type="button" disabled={busy !== null}
                        onClick={() => void run(r.id, () => client.unassignRunner(r.id))}>Unassign</button>
                {r.enabled
                  ? <button type="button" disabled={busy !== null}
                            onClick={() => void run(r.id, () => client.setRunnerEnabled(r.id, false))}>Disable</button>
                  : <button type="button" disabled={busy !== null}
                            onClick={() => void run(r.id, () => client.setRunnerEnabled(r.id, true))}>Enable</button>}
              </div>
            </li>
          ))}
        </ul>
      )}
      <h3>Unassigned runners</h3>
      <p className="muted">Detected runners are not used until you assign them.</p>
      {unassigned.length === 0 ? <p className="muted">No unassigned runners detected.</p> : (
        <ul className="list">
          {unassigned.map((r) => (
            <li key={r.id} aria-label={`Unassigned runner ${runnerName(r)}`}>
              <strong>{runnerName(r)}</strong> ({r.runner_type}) - {r.effective_state}; roles: {r.supported_roles.join(", ") || "none"}
              <div>
                <button type="button" disabled={terminal || busy !== null}
                        title={terminal ? "Workflow is finished or cancelled" : "Assign this runner to the workflow"}
                        onClick={() => void run(r.id, () => client.assignRunner(r.id, workflowId))}>
                  Assign to this workflow
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
