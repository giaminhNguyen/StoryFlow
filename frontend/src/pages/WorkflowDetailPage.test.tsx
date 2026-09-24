import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError, BackendUnavailableError } from "../api/client";
import type { DisplayState, ProjectSnapshot, WorkflowSnapshot } from "../api/types";
import { STATE_LABELS } from "../components/StateBadge";
import { makeProject, makeRunner, makeSteps, makeWorkflow } from "../test/fixtures";
import { makeFakeClient } from "../test/fixtures2";
import { WorkflowDetailPage } from "./WorkflowDetailPage";

const show = async (wf: WorkflowSnapshot, over: Record<string, unknown> = {}) => {
  const client = makeFakeClient({ getWorkflow: vi.fn().mockResolvedValue(wf), ...over });
  render(<WorkflowDetailPage workflowId="w1" client={client} />);
  await screen.findByRole("heading", { level: 1 });
  return client;
};

describe("detail rendering per state", () => {
  test.each<[DisplayState, Partial<WorkflowSnapshot>]>([
    ["draft", { status: "draft", display_state: "draft", projects: [] }],
    ["active", {}],
    ["paused", { status: "paused", status_reason: "operator", display_state: "paused" }],
    ["waiting_capacity", { display_state: "waiting_capacity" }],
    ["failed", { status: "paused", status_reason: "step_failed", display_state: "failed" }],
    ["blocked", { display_state: "blocked" }],
    ["cancelled", { status: "cancelled", display_state: "cancelled" }],
    ["completed", { status: "finished", display_state: "completed" }],
  ])("%s", async (state, over) => {
    await show(makeWorkflow(over));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Lighthouse");
    expect(screen.getAllByText(STATE_LABELS[state], { exact: false }).length).toBeGreaterThan(0);
  });

  test("paused by operator vs step_failed explanations", async () => {
    await show(makeWorkflow({ status: "paused", status_reason: "operator", display_state: "paused" }));
    expect(screen.getByText("Paused by operator")).toBeInTheDocument();
  });

  test("step_failed uses status_detail with project title", async () => {
    await show(makeWorkflow({
      status: "paused", status_reason: "step_failed", display_state: "failed",
      status_detail: { project_id: "p1", step: "story", error_code: "task_failed" },
    }));
    expect(screen.getByText("Paused because a step failed: The Keeper/story (task_failed)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
  });

  test("counts, project card, pipeline and link", async () => {
    await show(makeWorkflow({ projects: [makeProject({ steps: makeSteps("canon") })] }));
    expect(screen.getByLabelText("Project counts")).toHaveTextContent("1 in progress");
    const card = screen.getByRole("listitem", { name: "Project The Keeper" });
    expect(within(card).getByRole("link", { name: "The Keeper" })).toHaveAttribute("href", "#/projects/p1");
    expect(within(card).getByText("Current step: canon")).toBeInTheDocument();
    const steps = within(card).getByRole("list", { name: "Pipeline steps" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(5);
    expect(steps).toHaveTextContent("source: completed");
    expect(steps).toHaveTextContent("canon: in progress");
  });

  const withProject = (over: Partial<ProjectSnapshot>, wf: Partial<WorkflowSnapshot> = {}) =>
    makeWorkflow({ projects: [makeProject(over)], ...wf });

  test("business failure", async () => {
    await show(withProject({ state: "failed", failure: {
      category: "business", code: "invalid_story", message: "bad", step: "story", attempts: 3, max_attempts: 3,
      infrastructure_failures: 0, max_infra_attempts: 3 } }));
    expect(screen.getByText(/Step failed after 3\/3 attempts: invalid_story/)).toBeInTheDocument();
  });

  test("infrastructure failure differs from business", async () => {
    await show(withProject({ state: "failed", failure: {
      category: "infrastructure", code: "INFRA_EXHAUSTED", message: null, step: "tts", attempts: 1, max_attempts: 3,
      infrastructure_failures: 3, max_infra_attempts: 3 } }));
    expect(screen.getByText(/Infrastructure problem \(3\/3 infrastructure attempts\): INFRA_EXHAUSTED/)).toBeInTheDocument();
    expect(screen.queryByText(/Step failed after/)).not.toBeInTheDocument();
  });

  test("capacity and provider failures", async () => {
    await show(withProject({ state: "failed", failure: {
      category: "capacity", code: "all_agents_unavailable", message: null, step: "canon", attempts: null,
      max_attempts: null, infrastructure_failures: null, max_infra_attempts: null } }));
    expect(screen.getByText(/No eligible runner is available/)).toBeInTheDocument();
  });

  test("provider failure", async () => {
    await show(withProject({ state: "failed", failure: {
      category: "provider", code: "subtitles_unavailable", message: null, step: "source", attempts: null,
      max_attempts: null, infrastructure_failures: null, max_infra_attempts: null } }));
    expect(screen.getByText(/Provider problem: subtitles_unavailable/)).toBeInTheDocument();
  });

  test("waiting_capacity block shows capacity message and roles", async () => {
    await show(withProject({ state: "waiting_capacity", block: { kind: "waiting_capacity", message: null, until: null } }, {
      display_state: "waiting_capacity",
      capacity: { registered: 0, ready: 0, busy: 0, offline: 0, quota: 0, cooldown: 0, roles: [],
        unserved_roles: ["story_writer"], message: "No runner can serve story_writer" },
    }));
    expect(screen.getByText(/Waiting for capacity: no eligible runner yet/)).toBeInTheDocument();
    expect(screen.getAllByText("No runner can serve story_writer").length).toBeGreaterThan(0);
    expect(screen.getByText("Unserved roles: story_writer")).toBeInTheDocument();
  });

  test("chunks_missing, provider_blocked and delayed blocks", async () => {
    await show(makeWorkflow({ display_state: "blocked", projects: [
      makeProject({ id: "a", title: "A", state: "blocked", block: { kind: "chunks_missing", message: null, until: null } }),
      makeProject({ id: "b", title: "B", state: "blocked", block: { kind: "provider_blocked", message: "captcha", until: null } }),
      makeProject({ id: "c", title: "C", block: { kind: "delayed", message: null, until: "2026-09-25T06:30:00" } }),
    ] }));
    expect(screen.getByText("Inconsistent output: audio chunks missing")).toBeInTheDocument();
    expect(screen.getByText("Provider blocked: captcha")).toBeInTheDocument();
    expect(screen.getByText("Delayed until 2026-09-25 06:30")).toBeInTheDocument();
  });

  test("terminal workflow disables add project and assign", async () => {
    await show(makeWorkflow({ status: "cancelled", display_state: "cancelled" }), {
      listRunners: vi.fn().mockResolvedValue([makeRunner()]),
    });
    expect(screen.getByRole("button", { name: "Add project" })).toBeDisabled();
    expect(await screen.findByRole("button", { name: "Assign to this workflow" })).toBeDisabled();
    expect(screen.getByLabelText("Title")).toBeDisabled();
  });
});

describe("commands and forms", () => {
  test("pause calls the API and refreshes immediately", async () => {
    const client = await show(makeWorkflow());
    const before = client.getWorkflow.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(client.getWorkflow.mock.calls.length).toBeGreaterThan(before));
    expect(client.command).toHaveBeenCalledWith("w1", "pause");
  });

  test("conflict from backend surfaces as alert", async () => {
    const client = await show(makeWorkflow(), {
      command: vi.fn().mockRejectedValue(new ApiError(409, "invalid_state", "Workflow is not active")),
    });
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Workflow is not active");
    expect(client.command).toHaveBeenCalledTimes(1);
  });

  test("add project sends title and optional slug", async () => {
    const client = await show(makeWorkflow());
    await userEvent.type(screen.getByLabelText("Title"), "  New one ");
    await userEvent.type(screen.getByLabelText("Slug (optional)"), "new-one");
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    await waitFor(() => expect(client.addProject).toHaveBeenCalledWith("w1", { title: "New one", slug: "new-one" }));
    await waitFor(() => expect(screen.getByLabelText("Title")).toHaveValue(""));
  });

  test("add project without slug omits it; error shown inline", async () => {
    const client = await show(makeWorkflow(), {
      addProject: vi.fn().mockRejectedValue(new ApiError(400, "validation", "Title too long")),
    });
    await userEvent.type(screen.getByLabelText("Title"), "T");
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Title too long");
    expect(client.addProject).toHaveBeenCalledWith("w1", { title: "T" });
  });
});

describe("runner panel", () => {
  const assigned = makeRunner({ id: "ra", external_id: "alpha", assigned: true, workflow_session_id: "s", active_count: 1,
    max_concurrency: 2, cooldown_until: "2026-09-25T07:00:00", state: "busy", effective_state: "cooldown" });
  const free = makeRunner({ id: "rb", external_id: "beta" });

  test("lists assigned and unassigned with the not-used-until-assigned notice", async () => {
    await show(makeWorkflow({ runners: [assigned] }), { listRunners: vi.fn().mockResolvedValue([assigned, free]) });
    const a = screen.getByRole("listitem", { name: "Runner alpha" });
    expect(a).toHaveTextContent("active 1/2");
    expect(a).toHaveTextContent("cooldown until 2026-09-25 07:00");
    expect(screen.getByText("Detected runners are not used until you assign them.")).toBeInTheDocument();
    expect(await screen.findByRole("listitem", { name: "Unassigned runner beta" })).toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "Unassigned runner alpha" })).not.toBeInTheDocument();
  });

  test("assign, unassign, disable, enable", async () => {
    const client = await show(makeWorkflow({ runners: [assigned, makeRunner({ id: "rc", external_id: "gamma", enabled: false, assigned: true })] }),
      { listRunners: vi.fn().mockResolvedValue([free]) });
    await userEvent.click(await screen.findByRole("button", { name: "Assign to this workflow" }));
    await waitFor(() => expect(client.assignRunner).toHaveBeenCalledWith("rb", "w1"));
    const alpha = screen.getByRole("listitem", { name: "Runner alpha" });
    await userEvent.click(within(alpha).getByRole("button", { name: "Unassign" }));
    await waitFor(() => expect(client.unassignRunner).toHaveBeenCalledWith("ra"));
    await userEvent.click(within(alpha).getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(client.setRunnerEnabled).toHaveBeenCalledWith("ra", false));
    const gamma = screen.getByRole("listitem", { name: "Runner gamma" });
    await userEvent.click(within(gamma).getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(client.setRunnerEnabled).toHaveBeenCalledWith("rc", true));
  });

  test("assign conflict/busy is shown inline", async () => {
    await show(makeWorkflow(), {
      listRunners: vi.fn().mockResolvedValue([free]),
      assignRunner: vi.fn().mockRejectedValue(new ApiError(409, "conflict", "Runner is busy")),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Assign to this workflow" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Runner is busy");
  });
});

describe("polling", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });
  const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

  test("polls at 2s, never overlaps, and stops after unmount", async () => {
    let resolveSlow: ((w: WorkflowSnapshot) => void) | null = null;
    const getWorkflow = vi.fn(() => new Promise<WorkflowSnapshot>((r) => { resolveSlow = r; }));
    const client = makeFakeClient({ getWorkflow });
    const { unmount } = render(<WorkflowDetailPage workflowId="w1" client={client} />);
    await tick(0);
    expect(getWorkflow).toHaveBeenCalledTimes(1);
    await tick(10000); // slow request still in flight: no overlap
    expect(getWorkflow).toHaveBeenCalledTimes(1);
    await act(async () => { resolveSlow!(makeWorkflow()); });
    await tick(1900);
    expect(getWorkflow).toHaveBeenCalledTimes(1);
    await tick(200);
    expect(getWorkflow.mock.calls.length).toBe(2);
    unmount();
    const w = getWorkflow.mock.calls.length;
    const r = (client.listRunners as ReturnType<typeof vi.fn>).mock.calls.length;
    await tick(60000);
    expect(getWorkflow.mock.calls.length).toBe(w);
    expect((client.listRunners as ReturnType<typeof vi.fn>).mock.calls.length).toBe(r);
  });

  test("terminal workflows poll slowly (10s)", async () => {
    const getWorkflow = vi.fn().mockResolvedValue(makeWorkflow({ status: "finished", display_state: "completed" }));
    render(<WorkflowDetailPage workflowId="w1" client={makeFakeClient({ getWorkflow })} />);
    await tick(0);
    await tick(0); // interval switch triggers one re-run
    const n = getWorkflow.mock.calls.length;
    await tick(9000);
    expect(getWorkflow.mock.calls.length).toBe(n);
    await tick(1500);
    expect(getWorkflow.mock.calls.length).toBe(n + 1);
  });

  test("unreachable backend keeps last data, backs off, and recovers", async () => {
    const getWorkflow = vi.fn().mockResolvedValueOnce(makeWorkflow({ name: "Kept" }))
      .mockRejectedValue(new BackendUnavailableError());
    render(<WorkflowDetailPage workflowId="w1" client={makeFakeClient({ getWorkflow })} />);
    await tick(0);
    await tick(2100); // failure #1
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Kept");
    const n = getWorkflow.mock.calls.length;
    await tick(3000); // backoff: 4s next delay, nothing yet
    expect(getWorkflow.mock.calls.length).toBe(n);
    await tick(1500);
    expect(getWorkflow.mock.calls.length).toBe(n + 1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Kept");
  });
});
