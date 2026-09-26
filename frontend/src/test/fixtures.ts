// Snapshot factories for component tests: realistic API payloads with overridable fields.
import type {
  AudioInfo, Health, ProjectSnapshot, RunnerSnapshot, StepName, StepSummary, WorkflowSnapshot, WorkflowSummary,
} from "../api/types";

const STEPS: StepName[] = ["source", "canon", "story", "tts", "audio"];

export function makeSteps(current: StepName | null, currentStatus: StepSummary["status"] = "in_progress"): StepSummary[] {
  const idx = current ? STEPS.indexOf(current) : STEPS.length;
  return STEPS.map((step, i) => ({
    step, domain_id: i <= idx ? `${step}-1` : null, error_code: null, job: null,
    status: i < idx ? "completed" : i === idx ? currentStatus : "not_started",
  }));
}

export function makeAudio(chunks = 3): AudioInfo {
  return {
    id: "audio-1", run_number: 1, status: "completed", chunk_count: chunks, registered_chunks: chunks,
    store_dir: "projects/p1/audio/t1/run-001", error_code: null,
    chunks: Array.from({ length: chunks }, (_, i) => ({
      chunk_index: i + 1, artifact_path: `projects/p1/audio/t1/run-001/${String(i + 1).padStart(4, "0")}.wav`,
      duration_ms: 1500,
    })),
  };
}

export function makeProject(over: Partial<ProjectSnapshot> = {}): ProjectSnapshot {
  return {
    id: "p1", workflow_id: "w1", title: "The Keeper", slug: "the-keeper", status: "active",
    state: "in_progress", current_step: "canon", step_status: "in_progress", steps: makeSteps("canon"),
    source: null, canon: null, story_generation: null, story_version: null, tts: null, audio: null,
    block: null, failure: null, ...over,
  };
}

export function makeCompletedProject(over: Partial<ProjectSnapshot> = {}): ProjectSnapshot {
  return makeProject({
    state: "completed", current_step: null, step_status: null, steps: makeSteps(null),
    source: { id: "s1", snapshot_number: 1, title: "demo", content_hash: "abc", language: "English",
      language_code: "en", provenance: { video_id: "demo-video", provider: "FakeSubtitleClient", translated: false },
      artifact_path: "projects/p1/source/0001/source.txt" },
    canon: { id: "c1", status: "completed", has_canon: true, error_code: null },
    story_generation: { id: "g1", status: "completed", trigger: "pipeline", error_code: null, created_at: null },
    story_version: { id: "v1", version_number: 1, title: "Story", word_count: 42,
      content_path: "projects/p1/story/g1/story.md" },
    tts: { id: "t1", status: "completed", voice: "narrator", engine: "fake-tts", chunk_count: 3, error_code: null },
    audio: makeAudio(3), ...over,
  });
}

export function makeRunner(over: Partial<RunnerSnapshot> = {}): RunnerSnapshot {
  return {
    id: "r1", runner_type: "fake", external_id: "fake-1", workflow_session_id: null, assigned: false, enabled: true,
    state: "ready", effective_state: "ready", active_count: 0, max_concurrency: 1, free_slots: 1,
    supported_roles: ["story_writer", "tts_adapter"], cooldown_until: null, quota_reset_at: null,
    last_health_at: null, last_success_at: null, error_code: null, error_message: null, ...over,
  };
}

export function makeSummary(over: Partial<WorkflowSummary> = {}): WorkflowSummary {
  return {
    id: "w1", name: "Lighthouse", mode: "auto", status: "active", status_reason: null, display_state: "active",
    project_count: 1, completed_projects: 0, session_id: "sess-1", created_at: "2026-09-25T05:00:00",
    updated_at: "2026-09-25T05:01:00", finished_at: null, ...over,
  };
}

export function makeWorkflow(over: Partial<WorkflowSnapshot> = {}): WorkflowSnapshot {
  const { completed_projects: _unused, ...summary } = makeSummary();
  void _unused;
  return {
    ...summary, status_detail: null,
    counts: { completed: 0, failed: 0, blocked: 0, in_progress: 1, waiting_capacity: 0, not_started: 0, skipped: 0, needs_attention: 0 },
    projects: [makeProject()], runners: [],
    capacity: { registered: 0, ready: 0, busy: 0, offline: 0, quota: 0, cooldown: 0, roles: [], unserved_roles: [],
      message: null },
    ...over,
  };
}

export function makeHealth(over: Partial<Health> = {}): Health {
  return {
    status: "ok", db: { ok: true, schema_revision: "0007_source_feeds", at_head: true },
    runtime: { mode: "embedded", running: true, iteration: 1, errors: 0 },
    runners: { registered: 1, assigned: 0, ready: 1, offline: 0 }, demo: { video_id: "demo-video" },
    version: "0.6.0", ...over,
  };
}
