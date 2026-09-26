// Wire types of the StoryFlow local API (backend/storyflow/api, readmodels.py). Timestamps are ISO strings
// (naive local time from the backend). Only relative artifact paths are ever exposed.

export type DisplayState =
  | "draft" | "active" | "paused" | "waiting_capacity" | "failed" | "blocked" | "cancelled" | "completed";

export type WorkflowStatus = "draft" | "active" | "paused" | "finished" | "cancelled" | "abandoned";
export type StepName = "source" | "canon" | "story" | "tts" | "audio";
export type StepStatus = "not_started" | "in_progress" | "completed" | "failed";
export type ProjectState =
  | "completed" | "failed" | "blocked" | "waiting_capacity" | "in_progress" | "not_started"
  | "skipped" | "needs_attention";
export type FailureCategory = "business" | "infrastructure" | "capacity" | "provider" | "unknown";
export type BlockKind = "waiting_capacity" | "chunks_missing" | "provider_blocked" | "delayed" | "inconsistent";

export interface JobSummary {
  id: string; kind: string; role: string; status: string;
  attempts: number; max_attempts: number;
  infrastructure_failures: number; max_infra_attempts: number; execution_count: number;
  scheduled_at: string | null; last_error_code: string | null; last_error_message: string | null;
  outcome: string | null;
}
export interface StepSummary {
  step: StepName; status: StepStatus; domain_id: string | null; error_code: string | null; job: JobSummary | null;
}
export interface SourceInfo {
  id: string; snapshot_number: number; title: string; content_hash: string | null;
  language: string | null; language_code: string | null;
  provenance: Record<string, unknown>; artifact_path: string | null;
}
export interface CanonInfo { id: string; status: string; has_canon: boolean; error_code: string | null }
export interface StoryGenerationInfo {
  id: string; status: string; trigger: string; error_code: string | null; created_at: string | null;
}
export interface StoryVersionInfo {
  id: string; version_number: number; title: string; word_count: number; content_path: string | null;
}
export interface TtsInfo {
  id: string; status: string; voice: string; engine: string; chunk_count: number | null; error_code: string | null;
}
export interface AudioChunkInfo { chunk_index: number; artifact_path: string | null; duration_ms: number }
export interface AudioInfo {
  id: string; run_number: number; status: string; chunk_count: number; store_dir: string | null;
  error_code: string | null; registered_chunks: number; chunks: AudioChunkInfo[];
}
export interface BlockInfo { kind: BlockKind; message: string | null; until: string | null }
export interface FailureInfo {
  category: FailureCategory; code: string | null; message: string | null; step: string | null;
  attempts: number | null; max_attempts: number | null;
  infrastructure_failures: number | null; max_infra_attempts: number | null;
}

export interface ProjectSnapshot {
  id: string; workflow_id: string; title: string; slug: string | null; status: string;
  state: ProjectState; current_step: StepName | null; step_status: StepStatus | null;
  steps: StepSummary[];
  source: SourceInfo | null; canon: CanonInfo | null; story_generation: StoryGenerationInfo | null;
  story_version: StoryVersionInfo | null; tts: TtsInfo | null; audio: AudioInfo | null;
  block: BlockInfo | null; failure: FailureInfo | null;
  // batch outcome (failure policy) and source retry state; absent on older backends
  status_reason?: string | null; status_detail?: Record<string, unknown> | null;
  source_attempts?: number; next_attempt_at?: string | null;
}

export interface RunnerSnapshot {
  id: string; runner_type: string; external_id: string | null; workflow_session_id: string | null;
  assigned: boolean; enabled: boolean; state: string; effective_state: string;
  active_count: number; max_concurrency: number; free_slots: number; supported_roles: string[];
  cooldown_until: string | null; quota_reset_at: string | null;
  last_health_at: string | null; last_success_at: string | null;
  error_code: string | null; error_message: string | null;
}
export interface RoleCapacity { role: string; capable: number; eligible: number; waiting_jobs: number }
export interface CapacitySummary {
  registered: number; ready: number; busy: number; offline: number; quota: number; cooldown: number;
  roles: RoleCapacity[]; unserved_roles: string[]; message: string | null;
}
export interface WorkflowCounts {
  completed: number; failed: number; blocked: number; in_progress: number; waiting_capacity: number; not_started: number;
  skipped: number; needs_attention: number;
}
export interface WorkflowSummary {
  id: string; name: string; mode: string; status: WorkflowStatus; status_reason: string | null;
  display_state: DisplayState; project_count: number; completed_projects: number;
  session_id: string | null; created_at: string | null; updated_at: string | null; finished_at: string | null;
}
export interface WorkflowSnapshot extends Omit<WorkflowSummary, "completed_projects"> {
  status_detail: Record<string, unknown> | null;
  counts: WorkflowCounts; projects: ProjectSnapshot[]; runners: RunnerSnapshot[]; capacity: CapacitySummary;
}

export interface CommandResult {
  workflow_id: string; status: string; changed: boolean; detail: Record<string, unknown>;
}
export interface RunnerCommandResult {
  runner_id: string; changed: boolean; workflow_session_id: string | null; enabled: boolean; state: string;
  detail: Record<string, unknown>;
}

export interface Health {
  status: "ok" | "degraded";
  db: { ok: boolean; schema_revision: string | null; at_head: boolean };
  runtime: { mode: "embedded" | "external"; running: boolean | null; iteration: number | null; errors: number | null };
  runners: { registered: number; assigned: number; ready: number; offline: number };
  demo: { video_id: string } | null;
  version: string;
}

export interface CreateWorkflowInput {
  name: string; mode?: string; config?: Record<string, unknown>; client_key?: string;
}

export type ErrorCode =
  | "validation" | "not_found" | "conflict" | "invalid_state" | "not_retryable" | "capacity_unavailable"
  | "internal";

export type ProviderKind = "subtitle" | "story" | "tts";
export type ProviderState = "ready" | "unavailable" | "misconfigured" | "disabled" | "fake";
export interface ProviderStatus {
  name: string; kind: ProviderKind; state: ProviderState; usable: boolean; message: string;
  details: Record<string, unknown>;
}
export interface ProvidersInfo {
  /** False when the app was assembled with injected providers (no provider configuration). */
  configured: boolean;
  /** True when subtitle + story + tts are all usable right now (fakes count as usable, see state). */
  ready: boolean | null;
  providers: ProviderStatus[];
}
