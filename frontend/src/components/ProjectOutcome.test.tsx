import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CapacitySummary } from "../api/types";
import { makeProject } from "../test/fixtures";
import { ProjectCard, outcomeText } from "./ProjectCard";
import { ProjectStateBadge } from "./StateBadge";

const capacity: CapacitySummary = {
  registered: 0, ready: 0, busy: 0, offline: 0, quota: 0, cooldown: 0, roles: [], unserved_roles: [], message: null,
};

describe("batch outcomes (skipped / needs attention)", () => {
  it("labels the two new project states", () => {
    const { rerender } = render(<ProjectStateBadge state="skipped" />);
    expect(screen.getByText("Skipped")).toBeInTheDocument();
    rerender(<ProjectStateBadge state="needs_attention" />);
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
  });

  it("explains why a project was skipped and that the batch continues", () => {
    const project = makeProject({
      state: "skipped", status: "skipped", status_reason: "subtitles_unavailable",
      current_step: "source", step_status: "not_started",
    });
    render(<ProjectCard project={project} capacity={capacity} />);
    const outcome = screen.getByRole("group", { name: "Outcome" });
    expect(outcome).toHaveTextContent("Skipped");
    expect(outcome).toHaveTextContent("The video has no usable subtitles");
    expect(outcome).toHaveTextContent("The rest of the batch continues");
  });

  it("shows needs attention for a permanent error and falls back to the raw code", () => {
    const project = makeProject({ state: "needs_attention", status: "needs_attention", status_reason: "weird_code" });
    render(<ProjectCard project={project} capacity={capacity} />);
    expect(screen.getByRole("group", { name: "Outcome" })).toHaveTextContent("Needs attention");
    expect(outcomeText(project)).toContain("weird_code");
  });

  it("names the step that ended the project and says how to bring it back", () => {
    const project = makeProject({
      state: "needs_attention", status: "needs_attention", status_reason: "invalid_output",
      status_detail: { step: "story", error_code: "invalid_output" },
    });
    expect(outcomeText(project)).toBe(
      "story: The AI kept producing invalid output (invalid_output). " +
      "The rest of the batch continues; retry this project when the cause is fixed.");
  });

  it("shows no outcome box for an ordinary in-progress project", () => {
    render(<ProjectCard project={makeProject()} capacity={capacity} />);
    expect(screen.queryByRole("group", { name: "Outcome" })).toBeNull();
  });
});
