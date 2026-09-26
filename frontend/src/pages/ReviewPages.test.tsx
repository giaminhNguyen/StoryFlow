import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import type { ProjectSnapshot } from "../api/types";
import { makeCompletedProject, makeProject, makeReview, makeSteps, makeWorkflow } from "../test/fixtures";
import { makeFakeClient } from "../test/fixtures2";
import { ProjectDetailPage } from "./ProjectDetailPage";
import { WorkflowDetailPage } from "./WorkflowDetailPage";

function projectClient(project: ProjectSnapshot) {
  const real = new ApiClient("http://x");
  return {
    getProject: vi.fn(async () => project),
    artifactText: vi.fn(async () => "Once upon a time"),
    artifactUrl: (p: string) => real.artifactUrl(p),
  } as unknown as ApiClient;
}

describe("ProjectDetailPage review section", () => {
  it("shows the verdict, the issues and a link to the newest (revised) version", async () => {
    const project = makeCompletedProject({
      steps: makeSteps(null, "completed", true), revision_count: 1,
      review: makeReview({ verdict: "revise", revised: true, issue_count: 1,
        issues: [{ aspect: "canon", severity: "high", note: "the rival changes side" }] }),
      story_version: { id: "v2", version_number: 2, title: "Story", word_count: 900,
        content_path: "projects/p1/review/r1/story_revised.md" },
    });
    render(<ProjectDetailPage projectId="p1" client={projectClient(project)} />);
    const section = await screen.findByRole("region", { name: "Review" });
    expect(within(section).getByTestId("review-verdict")).toHaveTextContent("Needs revision");
    expect(within(section).getByText(/the rival changes side/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Open newest version" }))
      .toHaveAttribute("href", "http://x/api/artifacts/projects/p1/review/r1/story_revised.md");
    // the Story section shows the newest version's text and comes before the review
    expect(await screen.findByText("Once upon a time")).toBeInTheDocument();
  });

  it("has no review section when the workflow does not review", async () => {
    render(<ProjectDetailPage projectId="p1" client={projectClient(makeCompletedProject())} />);
    await screen.findByText("Once upon a time");
    expect(screen.queryByRole("region", { name: "Review" })).toBeNull();
  });

  it("shows a review that has not started yet", async () => {
    const project = makeProject({ steps: makeSteps("story", "in_progress", true) });
    render(<ProjectDetailPage projectId="p1" client={projectClient(project)} />);
    expect(await screen.findByRole("region", { name: "Review" })).toHaveTextContent("Review not started yet");
  });
});

describe("WorkflowDetailPage preset", () => {
  const show = async (over: Record<string, unknown>) => {
    const client = makeFakeClient({ getWorkflow: vi.fn().mockResolvedValue(makeWorkflow(over)) });
    render(<WorkflowDetailPage workflowId="w1" client={client} />);
    await screen.findByRole("heading", { level: 1 });
  };

  it("shows the preset next to the workflow name", async () => {
    await show({ preset: "quality" });
    expect(screen.getByTestId("workflow-preset")).toHaveTextContent("preset: quality");
  });

  it("shows nothing when the backend does not report a preset", async () => {
    await show({});
    expect(screen.queryByTestId("workflow-preset")).toBeNull();
  });

  it("lists the review step in the project card pipeline of a reviewing workflow", async () => {
    await show({ preset: "balanced", projects: [makeProject({ steps: makeSteps("review", "in_progress", true) })] });
    const card = screen.getByRole("listitem", { name: "Project The Keeper" });
    const steps = within(card).getByRole("list", { name: "Pipeline steps" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(6);
    expect(steps).toHaveTextContent("review: in progress");
  });
});
