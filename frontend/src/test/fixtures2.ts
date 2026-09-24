// Extra test helpers for workstream B: a fake ApiClient made of vi.fn methods.
import type { ApiClient } from "../api/client";
import { makeHealth, makeSummary, makeWorkflow } from "./fixtures";

export type FakeClient = { [K in keyof ApiClient]: ReturnType<typeof vi.fn> } & ApiClient;

export function makeFakeClient(over: Record<string, unknown> = {}): FakeClient {
  const wf = makeWorkflow();
  const result = { workflow_id: "w1", status: "active", changed: true, detail: {} };
  const runnerResult = { runner_id: "r1", changed: true, workflow_session_id: null, enabled: true, state: "ready", detail: {} };
  const fake = {
    health: vi.fn().mockResolvedValue(makeHealth()),
    providers: vi.fn().mockResolvedValue({ configured: false, ready: null, providers: [] }),
    listWorkflows: vi.fn().mockResolvedValue([makeSummary()]),
    getWorkflow: vi.fn().mockResolvedValue(wf),
    createWorkflow: vi.fn().mockResolvedValue({ result, workflow: wf }),
    command: vi.fn().mockResolvedValue({ result, workflow: wf }),
    addProject: vi.fn().mockResolvedValue({ result, project: wf.projects[0] }),
    getProject: vi.fn().mockResolvedValue(wf.projects[0]),
    listRunners: vi.fn().mockResolvedValue([]),
    assignRunner: vi.fn().mockResolvedValue({ result: runnerResult, runner: {} }),
    unassignRunner: vi.fn().mockResolvedValue({ result: runnerResult, runner: {} }),
    setRunnerEnabled: vi.fn().mockResolvedValue({ result: runnerResult, runner: {} }),
    artifactUrl: vi.fn((p: string) => `/api/artifacts/${p}`),
    artifactText: vi.fn().mockResolvedValue(""),
    ...over,
  };
  return fake as unknown as FakeClient;
}
