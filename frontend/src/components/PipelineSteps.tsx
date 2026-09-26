import type { StepStatus, StepSummary } from "../api/types";

const ICON: Record<StepStatus, string> = {
  not_started: "○", in_progress: "▶", completed: "✔", failed: "✖",
};
const TEXT: Record<StepStatus, string> = {
  not_started: "not started", in_progress: "in progress", completed: "completed", failed: "failed",
};

const STEP_HELP: Record<string, string> = {
  review: "Quality review of the story (canon, logic, style, length); it may also correct the story",
};

export function PipelineSteps({ steps }: { steps: StepSummary[] }) {
  return (
    <ol className="pipeline" aria-label="Pipeline steps">
      {steps.map((s) => (
        <li key={s.step} className={`step step-${s.status}`} title={STEP_HELP[s.step]}>
          <span aria-hidden="true">{ICON[s.status]}</span> <strong>{s.step}</strong>: {TEXT[s.status]}
          {s.job && s.job.status !== "completed" && (
            <span className="muted"> (attempt {s.job.attempts}/{s.job.max_attempts})</span>
          )}
          {s.error_code && <span className="muted"> [{s.error_code}]</span>}
        </li>
      ))}
    </ol>
  );
}
