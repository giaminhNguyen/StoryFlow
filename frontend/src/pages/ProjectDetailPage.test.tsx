import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, BackendUnavailableError } from "../api/client";
import type { ProjectSnapshot } from "../api/types";
import { makeCompletedProject, makeProject, makeSteps } from "../test/fixtures";
import { ProjectDetailPage } from "./ProjectDetailPage";

function fakeClient(getProject: (...a: unknown[]) => Promise<ProjectSnapshot>, text = "Once upon a time") {
  const real = new ApiClient("http://x");
  return {
    getProject: vi.fn(getProject),
    artifactText: vi.fn(async () => text),
    artifactUrl: (p: string) => real.artifactUrl(p),
  } as unknown as ApiClient & { getProject: ReturnType<typeof vi.fn> };
}

describe("ProjectDetailPage", () => {
  it("shows not-started project with empty states", async () => {
    const c = fakeClient(async () => makeProject({ state: "not_started", current_step: null, step_status: null,
      steps: makeSteps("source", "not_started") }));
    render(<ProjectDetailPage projectId="p1" client={c} />);
    expect(await screen.findByText("The Keeper")).toBeInTheDocument();
    expect(screen.getByText("Story not generated yet")).toBeInTheDocument();
    expect(screen.getByText("Audio not generated yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to workflow/i })).toHaveAttribute("href", "#/workflows/w1");
  });

  it("shows mid-pipeline state and step", async () => {
    const c = fakeClient(async () => makeProject());
    render(<ProjectDetailPage projectId="p1" client={c} />);
    expect(await screen.findByTestId("project-state")).toHaveTextContent("in_progress");
    expect(screen.getByText(/current step: canon/)).toBeInTheDocument();
  });

  it("shows completed project with story and chunks", async () => {
    const c = fakeClient(async () => makeCompletedProject());
    render(<ProjectDetailPage projectId="p1" client={c} />);
    expect(await screen.findByText("Once upon a time")).toBeInTheDocument();
    expect(screen.getByText(/Run 1: completed; 3\/3 chunks registered/)).toBeInTheDocument();
    expect(screen.getByText(/Video id: demo-video/)).toBeInTheDocument();
    expect(screen.getAllByTestId("audio-chunk")).toHaveLength(3);
    expect(c.artifactText).toHaveBeenCalledWith("projects/p1/story/g1/story.md", expect.anything());
  });

  it("shows business and infrastructure failures with attempts", async () => {
    const failure = { category: "business" as const, code: "story_invalid", message: "bad story", step: "story",
      attempts: 2, max_attempts: 3, infrastructure_failures: 1, max_infra_attempts: 5 };
    const c = fakeClient(async () => makeProject({ state: "failed", failure }));
    render(<ProjectDetailPage projectId="p1" client={c} />);
    const panel = await screen.findByRole("alert", { name: "Failure" });
    expect(panel).toHaveTextContent("business failure");
    expect(panel).toHaveTextContent("Attempts: 2/3");
    expect(panel).toHaveTextContent("Infrastructure failures: 1/5");
  });

  it("shows infrastructure failure category", async () => {
    const failure = { category: "infrastructure" as const, code: null, message: null, step: null,
      attempts: null, max_attempts: null, infrastructure_failures: 5, max_infra_attempts: 5 };
    const c = fakeClient(async () => makeProject({ state: "failed", failure }));
    render(<ProjectDetailPage projectId="p1" client={c} />);
    expect(await screen.findByRole("alert", { name: "Failure" })).toHaveTextContent("infrastructure failure");
  });

  it("shows blocked chunks_missing and waiting_capacity", async () => {
    const c = fakeClient(async () => makeProject({ state: "blocked",
      block: { kind: "chunks_missing", message: "2 missing", until: null } }));
    const { unmount } = render(<ProjectDetailPage projectId="p1" client={c} />);
    expect(await screen.findByRole("status", { name: "Blocked" })).toHaveTextContent("Audio chunks missing: 2 missing");
    unmount();
    const c2 = fakeClient(async () => makeProject({ state: "waiting_capacity",
      block: { kind: "waiting_capacity", message: null, until: null } }));
    render(<ProjectDetailPage projectId="p1" client={c2} />);
    expect(await screen.findByRole("status", { name: "Blocked" })).toHaveTextContent("Waiting for runner capacity");
  });

  it("handles not_found", async () => {
    const c = fakeClient(async () => { throw new ApiError(404, "not_found", "nope"); });
    render(<ProjectDetailPage projectId="zz" client={c} />);
    expect(await screen.findByText("Project not found")).toBeInTheDocument();
  });

  it("keeps last data when backend becomes unavailable", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let n = 0;
      const c = fakeClient(async () => {
        if (n++ === 0) return makeProject();
        throw new BackendUnavailableError();
      });
      render(<ProjectDetailPage projectId="p1" client={c} />);
      expect(await screen.findByText("The Keeper")).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(2100);
      await waitFor(() => expect(screen.getByText(/showing last known data/)).toBeInTheDocument());
      expect(screen.getByText("The Keeper")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops polling on unmount and polls every 2s then", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const c = fakeClient(async () => makeProject());
      const { unmount } = render(<ProjectDetailPage projectId="p1" client={c} />);
      await screen.findByText("The Keeper");
      await vi.advanceTimersByTimeAsync(2100);
      expect(c.getProject.mock.calls.length).toBeGreaterThanOrEqual(2);
      unmount();
      const calls = c.getProject.mock.calls.length;
      await vi.advanceTimersByTimeAsync(10000);
      expect(c.getProject.mock.calls.length).toBe(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it("lists artifacts in a collapsed details element", async () => {
    const c = fakeClient(async () => makeCompletedProject());
    render(<ProjectDetailPage projectId="p1" client={c} />);
    const summary = await screen.findByText("Artifacts");
    const details = summary.closest("details")!;
    expect(details).not.toHaveAttribute("open");
    expect(within(details).getAllByRole("link").length).toBeGreaterThan(3);
  });
});

describe("ProjectDetailPage final audio", () => {
  it("offers the single joined audio file when the run produced one", async () => {
    const done = makeCompletedProject();
    const project = { ...done, audio: { ...done.audio!, final_path: "projects/p1/audio/t1/run-001/final.wav" } };
    const c = fakeClient(async () => project);
    render(<ProjectDetailPage projectId="p1" client={c} />);
    const group = await screen.findByRole("group", { name: "Final audio" });
    expect(within(group).getByLabelText("Full audio")).toHaveAttribute(
      "src", "http://x/api/artifacts/projects/p1/audio/t1/run-001/final.wav");
    expect(within(group).getByRole("link", { name: "Download" })).toHaveAttribute(
      "href", "http://x/api/artifacts/projects/p1/audio/t1/run-001/final.wav");
  });

  it("shows no full-audio player when there is no joined file", async () => {
    const c = fakeClient(async () => makeCompletedProject());
    render(<ProjectDetailPage projectId="p1" client={c} />);
    expect(await screen.findByText(/chunks registered/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Final audio" })).toBeNull();
  });
});

describe("ProjectDetailPage final audio load failure", () => {
  it("explains a player that cannot load the (very long) file and keeps the download link", async () => {
    const done = makeCompletedProject();
    const project = { ...done, audio: { ...done.audio!, final_path: "projects/p1/audio/t1/run-001/final.wav" } };
    const c = fakeClient(async () => project);
    render(<ProjectDetailPage projectId="p1" client={c} />);
    const group = await screen.findByRole("group", { name: "Final audio" });
    expect(within(group).queryByRole("alert")).toBeNull();
    fireEvent.error(within(group).getByLabelText("Full audio"));
    expect(await within(group).findByRole("alert")).toHaveTextContent("The audio could not be loaded; use Download.");
    expect(within(group).getByRole("link", { name: "Download" })).toHaveAttribute(
      "href", "http://x/api/artifacts/projects/p1/audio/t1/run-001/final.wav");
  });

  it("shows no error while the audio is fine", async () => {
    const done = makeCompletedProject();
    const project = { ...done, audio: { ...done.audio!, final_path: "projects/p1/audio/t1/run-001/final.wav" } };
    render(<ProjectDetailPage projectId="p1" client={fakeClient(async () => project)} />);
    const group = await screen.findByRole("group", { name: "Final audio" });
    expect(within(group).queryByText(/could not be loaded/)).toBeNull();
  });
});
