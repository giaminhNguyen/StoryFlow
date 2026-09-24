import { useContext } from "react";
import type { ApiClient } from "../api/client";
import { CreateWorkflowForm } from "../components/CreateWorkflowForm";
import { ProvidersPanel } from "../components/ProvidersPanel";
import { ATTENTION_STATES, StateBadge } from "../components/StateBadge";
import { AppContext, useReportConnection } from "../connection";
import { formatTime } from "../format";
import { usePolling } from "../hooks/usePolling";
import { navigate, workflowHref } from "../router";

export function WorkflowListPage({ client }: { client: ApiClient }) {
  const { health } = useContext(AppContext);
  const poll = usePolling((signal) => client.listWorkflows(signal), { intervalMs: 3000 });
  useReportConnection(poll.connected);
  const workflows = poll.data;

  return (
    <section>
      <h1>Workflows</h1>
      {poll.loading && <p role="status">Loading workflows...</p>}
      {poll.error && poll.connected && <p role="alert" className="banner banner-error">{poll.error.message}</p>}
      {workflows && workflows.length === 0 && (
        <p className="empty">No workflows yet. Create your first workflow below.</p>
      )}
      {workflows && workflows.length > 0 && (
        <table className="table">
          <caption className="sr-only">Workflows</caption>
          <thead>
            <tr><th>Name</th><th>Mode</th><th>State</th><th>Progress</th><th>Updated</th></tr>
          </thead>
          <tbody>
            {workflows.map((w) => {
              const attention = ATTENTION_STATES.includes(w.display_state);
              return (
                <tr key={w.id} className={attention ? "attention" : undefined}>
                  <td>
                    <a href={workflowHref(w.id)}>{w.name}</a>
                    {attention && <span className="attention-flag"> Needs attention</span>}
                  </td>
                  <td>{w.mode}</td>
                  <td><StateBadge state={w.display_state} /></td>
                  <td>
                    <span>{w.completed_projects}/{w.project_count}</span>{" "}
                    <progress value={w.completed_projects} max={Math.max(w.project_count, 1)}
                              aria-label={`${w.name} progress`} />
                  </td>
                  <td>{formatTime(w.updated_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <ProvidersPanel />
      <CreateWorkflowForm client={client} defaultVideoId={health?.demo?.video_id ?? null}
                          onCreated={(id) => navigate(workflowHref(id))} />
    </section>
  );
}
