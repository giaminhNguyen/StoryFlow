import type { DisplayState, ProjectState } from "../api/types";

export const STATE_LABELS: Record<DisplayState, string> = {
  draft: "Draft",
  active: "Active",
  paused: "Paused",
  waiting_capacity: "Waiting for capacity",
  failed: "Failed",
  blocked: "Blocked",
  cancelled: "Cancelled",
  completed: "Completed",
};

const STATE_ICONS: Record<DisplayState, string> = {
  draft: "○", active: "▶", paused: "⏸", waiting_capacity: "⏳",
  failed: "✖", blocked: "⛔", cancelled: "⊘", completed: "✔",
};

const STATE_HELP: Record<DisplayState, string> = {
  draft: "Not started yet. Add projects, then start.",
  active: "Running: projects are being processed.",
  paused: "Paused by the operator. Resume to continue.",
  waiting_capacity: "Waiting for an eligible runner. Assign or enable one.",
  failed: "Paused because a step failed. Retry after fixing the cause.",
  blocked: "Blocked by inconsistent output or a provider problem.",
  cancelled: "Cancelled. This workflow will not run again.",
  completed: "All work finished.",
};

export function describeState(state: DisplayState): string {
  return STATE_HELP[state];
}

export const ATTENTION_STATES: readonly DisplayState[] = ["failed", "blocked", "waiting_capacity"];

export function StateBadge({ state }: { state: DisplayState }) {
  return (
    <span className={`badge badge-${state}`} title={describeState(state)} data-state={state}>
      <span aria-hidden="true">{STATE_ICONS[state]}</span> {STATE_LABELS[state]}
    </span>
  );
}

export const PROJECT_STATE_LABELS: Record<ProjectState, string> = {
  completed: "Completed", failed: "Failed", blocked: "Blocked", waiting_capacity: "Waiting for capacity",
  in_progress: "In progress", not_started: "Not started", skipped: "Skipped", needs_attention: "Needs attention",
};
const PROJECT_ICONS: Record<ProjectState, string> = {
  completed: "✔", failed: "✖", blocked: "⛔", waiting_capacity: "⏳", in_progress: "▶",
  not_started: "○", skipped: "⤼", needs_attention: "⚠",
};

export function ProjectStateBadge({ state }: { state: ProjectState }) {
  return (
    <span className={`badge badge-p-${state}`} data-state={state}>
      <span aria-hidden="true">{PROJECT_ICONS[state]}</span> {PROJECT_STATE_LABELS[state]}
    </span>
  );
}
