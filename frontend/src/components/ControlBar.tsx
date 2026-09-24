import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { WorkflowStatus } from "../api/types";
import { errorMessage } from "../format";

export type ActionName = "start" | "pause" | "resume" | "retry" | "cancel";
export interface ActionState { enabled: boolean; reason: string }
export type AvailableActions = Record<ActionName, ActionState>;

const ok = (reason: string): ActionState => ({ enabled: true, reason });
const no = (reason: string): ActionState => ({ enabled: false, reason });
const TERMINAL: readonly WorkflowStatus[] = ["finished", "cancelled", "abandoned"];

export function isTerminal(status: WorkflowStatus): boolean {
  return TERMINAL.includes(status);
}

/**
 * Which lifecycle commands make sense for this workflow (mirrors backend services rules; the backend stays
 * authoritative). draft->start; active->pause; paused(operator)->resume; paused(step_failed)->retry;
 * draft/active/paused->cancel. `project_count` (optional) disables start on an empty draft.
 */
export function availableActions(
  wf: { status: WorkflowStatus; status_reason: string | null; project_count?: number },
): AvailableActions {
  const terminal = isTerminal(wf.status);
  const why = terminal ? `Workflow is ${wf.status === "finished" ? "completed" : "cancelled"}` : "";
  const failed = wf.status === "paused" && wf.status_reason === "step_failed";
  return {
    start: wf.status === "draft"
      ? (wf.project_count === 0 ? no("Add a project before starting") : ok("Start the workflow"))
      : no(terminal ? why : "Only a draft can be started"),
    pause: wf.status === "active" ? ok("Pause the workflow")
      : no(terminal ? why : "Only an active workflow can be paused"),
    resume: wf.status === "paused" && !failed ? ok("Resume the workflow")
      : no(terminal ? why : failed ? "A step failed: use Retry instead" : "Only a workflow paused by the operator can resume"),
    retry: failed ? ok("Retry the failed step")
      : no(terminal ? why : "Retry is only available after a step failed"),
    cancel: wf.status === "draft" || wf.status === "active" || wf.status === "paused"
      ? ok("Cancel the workflow permanently") : no(why || "Workflow can no longer be cancelled"),
  };
}

const LABELS: Record<ActionName, string> = {
  start: "Start", pause: "Pause", resume: "Resume", retry: "Retry", cancel: "Cancel workflow",
};
const ORDER: ActionName[] = ["start", "pause", "resume", "retry", "cancel"];

export function ControlBar({ client, workflow, onDone }: {
  client: ApiClient;
  workflow: { id: string; status: WorkflowStatus; status_reason: string | null; project_count: number };
  onDone: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const actions = availableActions(workflow);

  const run = async (action: ActionName) => {
    setBusy(true);
    setError(null);
    try {
      await client.command(workflow.id, action);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
      setConfirming(false);
      await onDone();
    }
  };

  return (
    <div className="controls">
      <div role="group" aria-label="Workflow controls" className="row">
        {ORDER.map((a) => (
          <button key={a} type="button" disabled={!actions[a].enabled || busy} title={actions[a].reason}
                  onClick={() => (a === "cancel" ? setConfirming(true) : void run(a))}>
            {LABELS[a]}
          </button>
        ))}
      </div>
      {confirming && (
        <div role="group" aria-label="Confirm cancel" className="confirm">
          <p>Cancel workflow? This is permanent.</p>
          <button type="button" className="danger" disabled={busy} onClick={() => void run("cancel")}>
            Confirm cancel
          </button>{" "}
          <button type="button" onClick={() => setConfirming(false)}>Keep workflow</button>
        </div>
      )}
      {error && (
        <div role="alert" className="banner banner-error">
          {error}{" "}
          <button type="button" onClick={() => setError(null)} aria-label="Dismiss message">Dismiss</button>
        </div>
      )}
    </div>
  );
}
