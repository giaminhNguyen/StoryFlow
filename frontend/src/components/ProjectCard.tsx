import type { BlockInfo, CapacitySummary, FailureInfo, ProjectSnapshot } from "../api/types";
import { formatTime } from "../format";
import { projectHref } from "../router";
import { PipelineSteps } from "./PipelineSteps";
import { ProjectStateBadge } from "./StateBadge";

export function failureText(f: FailureInfo): string {
  const where = f.step ? `${f.step}: ` : "";
  const code = f.code ?? "unknown error";
  switch (f.category) {
    case "business":
      return f.attempts !== null && f.max_attempts !== null
        ? `${where}Step failed after ${f.attempts}/${f.max_attempts} attempts: ${code}`
        : `${where}Step failed: ${code}`;
    case "infrastructure":
      return `${where}Infrastructure problem${f.infrastructure_failures !== null && f.max_infra_attempts !== null
        ? ` (${f.infrastructure_failures}/${f.max_infra_attempts} infrastructure attempts)` : ""}: ${code}`;
    case "capacity":
      return `${where}No eligible runner is available for this step (${code})`;
    case "provider":
      return `${where}Provider problem: ${code}`;
    default:
      return `${where}Failed: ${code}`;
  }
}

const OUTCOME_REASONS: Record<string, string> = {
  subtitles_unavailable: "The video has no usable subtitles",
  language_unavailable: "No subtitle in the requested language",
  empty_source: "The subtitle was empty",
  subtitle_retries_exhausted: "The subtitle provider kept failing; retries used up",
  source_not_configured: "No source video is configured for this project",
};

export function outcomeText(p: ProjectSnapshot): string {
  const code = p.status_reason ?? "unknown";
  return `${OUTCOME_REASONS[code] ?? code} (${code}). The rest of the batch continues.`;
}

export function blockText(b: BlockInfo): string {
  switch (b.kind) {
    case "waiting_capacity": return "Waiting for capacity: no eligible runner yet";
    case "chunks_missing": return "Inconsistent output: audio chunks missing";
    case "provider_blocked": return `Provider blocked${b.message ? `: ${b.message}` : ""}`;
    case "delayed": return `Delayed until ${formatTime(b.until)}`;
    default: return `Inconsistent output${b.message ? `: ${b.message}` : ""}`;
  }
}

export function ProjectCard({ project, capacity }: { project: ProjectSnapshot; capacity: CapacitySummary }) {
  const { failure, block } = project;
  const capacityNote = capacity.message
    ? capacity.message
    : capacity.unserved_roles.length ? `No eligible runner for: ${capacity.unserved_roles.join(", ")}` : null;
  const needsCapacity = block?.kind === "waiting_capacity" || failure?.category === "capacity";
  return (
    <li className="card project" aria-label={`Project ${project.title}`}>
      <div className="row">
        <h3><a href={projectHref(project.id)}>{project.title}</a></h3>
        <ProjectStateBadge state={project.state} />
      </div>
      <p className="muted">
        {project.current_step ? `Current step: ${project.current_step}` : "No active step"}
      </p>
      <PipelineSteps steps={project.steps} />
      {(project.state === "skipped" || project.state === "needs_attention") && (
        <div className="problem problem-block" role="group" aria-label="Outcome">
          <strong>{project.state === "skipped" ? "Skipped" : "Needs attention"}</strong>
          <p>{outcomeText(project)}</p>
        </div>
      )}
      {failure && (
        <div className={`problem problem-${failure.category}`} role="group" aria-label="Failure">
          <strong>Failure ({failure.category})</strong>
          <p>{failureText(failure)}</p>
          {failure.message && <p className="muted">{failure.message}</p>}
        </div>
      )}
      {block && (
        <div className="problem problem-block" role="group" aria-label="Blocked">
          <strong>{block.kind === "delayed" ? "Delayed" : "Waiting / blocked"}</strong>
          <p>{blockText(block)}</p>
        </div>
      )}
      {needsCapacity && capacityNote && <p className="capacity-note">{capacityNote}</p>}
    </li>
  );
}
