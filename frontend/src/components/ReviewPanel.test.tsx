import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ReviewIssue } from "../api/types";
import { makeCompletedProject, makeProject, makeReview, makeSteps } from "../test/fixtures";
import { PipelineSteps } from "./PipelineSteps";
import { ReviewPanel, groupIssues, verdictLabel } from "./ReviewPanel";

const withStep = (over = {}) => makeProject({ steps: makeSteps("review", "in_progress", true), ...over });

describe("ReviewPanel", () => {
  it("renders nothing when the workflow has no review step and no review", () => {
    const { container } = render(<ReviewPanel project={makeProject()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says the review has not started when the step exists but there is no round yet", () => {
    render(<ReviewPanel project={withStep({ review: null })} />);
    expect(screen.getByRole("region", { name: "Review" })).toHaveTextContent("Review not started yet");
  });

  it("shows a running review as a status", () => {
    const review = makeReview({ status: "processing", verdict: null, summary: null, round_number: 2 });
    render(<ReviewPanel project={withStep({ review })} />);
    expect(screen.getByRole("status")).toHaveTextContent("Review in progress (round 2)");
    expect(screen.queryByTestId("review-verdict")).toBeNull();
  });

  it.each(["failed", "cancelled"])("shows a %s review as an alert with the error code", (status) => {
    const review = makeReview({ status, verdict: null, summary: null, error_code: "invalid_review" });
    render(<ReviewPanel project={withStep({ review })} />);
    const alert = screen.getByRole("alert", { name: "Review failed" });
    expect(alert).toHaveTextContent(`Review ${status}`);
    expect(alert).toHaveTextContent("invalid_review");
  });

  it("shows an approval with no issues", () => {
    render(<ReviewPanel project={makeCompletedProject({ review: makeReview(), steps: makeSteps(null, "completed", true) })} />);
    expect(screen.getByTestId("review-verdict")).toHaveTextContent("Approved");
    expect(screen.getByText("The story keeps the canon.")).toBeInTheDocument();
    expect(screen.getByText("No issues found")).toBeInTheDocument();
    expect(screen.queryByTestId("review-revised")).toBeNull();
  });

  it("groups issues by aspect and lists the most severe first", () => {
    const issues: ReviewIssue[] = [
      { aspect: "canon", severity: "low", note: "minor name slip" },
      { aspect: "style", severity: "medium", note: "repetitive phrasing" },
      { aspect: "canon", severity: "high", note: "the rival changes side" },
    ];
    const review = makeReview({ verdict: "revise", issue_count: 3, issues });
    render(<ReviewPanel project={withStep({ review })} />);
    expect(screen.getByTestId("review-verdict")).toHaveTextContent("Needs revision");
    expect(screen.getByText("3 issues found")).toBeInTheDocument();
    const canon = screen.getByRole("group", { name: "Canon issues" });
    const items = within(canon).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("[high] the rival changes side");
    expect(items[1]).toHaveTextContent("[low] minor name slip");
    expect(items[0]).toHaveClass("review-issue-high");
    expect(within(screen.getByRole("group", { name: "Style issues" })).getAllByRole("listitem")).toHaveLength(1);
  });

  it("says when the API capped the issue list", () => {
    const issues: ReviewIssue[] = [{ aspect: "logic", severity: "medium", note: "gap" }];
    render(<ReviewPanel project={withStep({ review: makeReview({ issue_count: 26, issues, verdict: "revise" }) })} />);
    expect(screen.getByText("Showing the first 1 of 26 issues")).toBeInTheDocument();
  });

  it("uses the singular for one issue and a readable label for an unknown aspect", () => {
    const issues: ReviewIssue[] = [{ aspect: "pacing", severity: "low", note: "slow start" }];
    render(<ReviewPanel project={withStep({ review: makeReview({ issue_count: 1, issues }) })} />);
    expect(screen.getByText("1 issue found")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "pacing issues" })).toBeInTheDocument();
  });

  it("tells that the story was revised and links to the newest version", () => {
    const project = makeCompletedProject({
      steps: makeSteps(null, "completed", true), revision_count: 2,
      review: makeReview({ verdict: "revise", revised: true, revised_version_id: "v3" }),
      story_version: { id: "v3", version_number: 3, title: "Story", word_count: 900, content_path: "projects/p1/review/r/story_revised.md" },
    });
    render(<ReviewPanel project={project} storyHref="http://x/api/artifacts/projects/p1/review/r/story_revised.md" />);
    const note = screen.getByTestId("review-revised");
    expect(note).toHaveTextContent("The story was revised 2 times");
    expect(note).toHaveTextContent("(v3)");
    expect(within(note).getByRole("link", { name: "Open newest version" }))
      .toHaveAttribute("href", "http://x/api/artifacts/projects/p1/review/r/story_revised.md");
  });

  it("uses the singular for a single revision and needs no link", () => {
    const project = makeCompletedProject({ steps: makeSteps(null, "completed", true), revision_count: 1,
      review: makeReview({ verdict: "revise", revised: true }) });
    render(<ReviewPanel project={project} />);
    expect(screen.getByTestId("review-revised")).toHaveTextContent("revised 1 time;");
    expect(screen.queryByRole("link", { name: "Open newest version" })).toBeNull();
  });
});

describe("helpers", () => {
  it("groupIssues keeps the first-seen aspect order and sorts by severity inside a group", () => {
    const grouped = groupIssues([
      { aspect: "style", severity: "low", note: "a" }, { aspect: "canon", severity: "medium", note: "b" },
      { aspect: "style", severity: "high", note: "c" }, { aspect: "canon", severity: "weird", note: "d" },
    ]);
    expect(grouped.map(([aspect]) => aspect)).toEqual(["style", "canon"]);
    expect(grouped[0][1].map((i) => i.note)).toEqual(["c", "a"]);
    expect(grouped[1][1].map((i) => i.note)).toEqual(["b", "d"]);         // unknown severity sorts last
    expect(groupIssues([])).toEqual([]);
  });

  it("verdictLabel", () => {
    expect([verdictLabel("approve"), verdictLabel("revise"), verdictLabel(null)])
      .toEqual(["Approved", "Needs revision", "No verdict"]);
  });
});

describe("PipelineSteps with the review step", () => {
  it("lists review between story and tts, with an explanation", () => {
    render(<PipelineSteps steps={makeSteps("review", "in_progress", true)} />);
    const items = within(screen.getByRole("list", { name: "Pipeline steps" })).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector("strong")?.textContent))
      .toEqual(["source", "canon", "story", "review", "tts", "audio"]);
    expect(items[3]).toHaveTextContent("review: in progress");
    expect(items[3]).toHaveAttribute("title", expect.stringContaining("Quality review"));
    expect(items[2]).not.toHaveAttribute("title");
  });

  it("simply has no review item when the workflow does not review", () => {
    render(<PipelineSteps steps={makeSteps("canon")} />);
    const items = within(screen.getByRole("list", { name: "Pipeline steps" })).getAllByRole("listitem");
    expect(items).toHaveLength(5);
    expect(screen.queryByText("review", { exact: false })).toBeNull();
  });
});
