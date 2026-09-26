import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import type { WorkflowSnapshot } from "../api/types";
import { AddProjectForm } from "../components/AddProjectForm";
import { ControlBar, isTerminal } from "../components/ControlBar";
import { ProvidersPanel } from "../components/ProvidersPanel";
import { ProjectCard } from "../components/ProjectCard";
import { RunnerPanel } from "../components/RunnerPanel";
import { StateBadge, describeState } from "../components/StateBadge";
import { useReportConnection } from "../connection";
import { usePolling } from "../hooks/usePolling";

export function statusExplanation(wf: WorkflowSnapshot): string {
  if (wf.status === "paused") {
    if (wf.status_reason === "step_failed") {
      const d = wf.status_detail ?? {};
      const project = wf.projects.find((p) => p.id === d["project_id"]);
      const parts = [project?.title, typeof d["step"] === "string" ? d["step"] : undefined].filter(Boolean).join("/");
      const code = typeof d["error_code"] === "string" ? ` (${d["error_code"]})` : "";
      return `Paused because a step failed${parts ? `: ${parts}` : ""}${code}`;
    }
    return "Paused by operator";
  }
  return describeState(wf.display_state);
}

export function WorkflowDetailPage({ workflowId, client }: { workflowId: string; client: ApiClient }) {
  // Poll faster while work may change; slow down once the workflow is terminal.
  const [terminal, setTerminal] = useState(false);
  const wfPoll = usePolling<WorkflowSnapshot>((s) => client.getWorkflow(workflowId, s), {
    intervalMs: terminal ? 10000 : 2000,
  });
  const wf = wfPoll.data;
  const isTerm = wf ? isTerminal(wf.status) : false;
  useEffect(() => setTerminal(isTerm), [isTerm]);
  const runners = usePolling((s) => client.listRunners(s), { intervalMs: 5000 });
  useReportConnection(wfPoll.connected);
  useReportConnection(runners.connected);

  const refreshAll = async () => {
    await Promise.all([wfPoll.refresh(), runners.refresh()]);
  };

  if (!wf) {
    if (wfPoll.error instanceof ApiError) {
      return (
        <section>
          <p role="alert" className="banner banner-error">{wfPoll.error.message}</p>
          <a href="#/">Back to workflows</a>
        </section>
      );
    }
    return <p role="status">{wfPoll.loading ? "Loading workflow..." : "Workflow unavailable"}</p>;
  }

  const c = wf.counts;
  return (
    <section>
      <p><a href="#/">&larr; All workflows</a></p>
      <header className="detail-head">
        <h1>{wf.name}</h1>
        <StateBadge state={wf.display_state} />
      </header>
      <p className="status-reason">{statusExplanation(wf)}</p>
      <p className="counts" aria-label="Project counts">
        {wf.projects.length} projects: {c.completed} completed, {c.in_progress} in progress, {c.waiting_capacity} waiting,{" "}
        {c.blocked} blocked, {c.failed} failed, {c.not_started} not started
        {c.skipped + c.needs_attention > 0 && `, ${c.skipped} skipped, ${c.needs_attention} need attention`}
      </p>
      <ControlBar client={client} workflow={wf} onDone={refreshAll} />
      <h2>Projects</h2>
      {wf.projects.length === 0 ? <p className="empty">No projects yet.</p> : (
        <ul className="list">
          {wf.projects.map((p) => <ProjectCard key={p.id} project={p} capacity={wf.capacity} />)}
        </ul>
      )}
      <AddProjectForm client={client} workflowId={wf.id} disabled={isTerm} onAdded={refreshAll} />
      <RunnerPanel client={client} workflowId={wf.id} terminal={isTerm} assigned={wf.runners}
                   all={runners.data} onChanged={refreshAll} />
      <section className="card" aria-label="Capacity">
        <h2>Capacity</h2>
        <p>
          {wf.capacity.registered} registered, {wf.capacity.ready} ready, {wf.capacity.busy} busy,{" "}
          {wf.capacity.offline} offline, {wf.capacity.quota} quota, {wf.capacity.cooldown} cooldown
        </p>
        {wf.capacity.message && <p>{wf.capacity.message}</p>}
        {wf.capacity.unserved_roles.length > 0 && <p>Unserved roles: {wf.capacity.unserved_roles.join(", ")}</p>}
        {wf.capacity.unserved_roles.length > 0 && <ProvidersPanel />}
      </section>
    </section>
  );
}
