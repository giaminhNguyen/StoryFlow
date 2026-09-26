import type { ProjectSnapshot, ReviewIssue } from "../api/types";

const ASPECT_LABELS: Record<string, string> = {
  canon: "Canon", logic: "Logic", style: "Style", length: "Length", other: "Other",
};
const SEVERITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };

/** Issues grouped by aspect (first appearance order), most severe first inside each group. */
export function groupIssues(issues: ReviewIssue[]): [string, ReviewIssue[]][] {
  const groups = new Map<string, ReviewIssue[]>();
  for (const issue of issues) {
    const list = groups.get(issue.aspect) ?? [];
    list.push(issue);
    groups.set(issue.aspect, list);
  }
  return [...groups.entries()].map(([aspect, list]) => [
    aspect,
    [...list].sort((a, b) => (SEVERITY_ORDER[a.severity] ?? 3) - (SEVERITY_ORDER[b.severity] ?? 3)),
  ]);
}

export function verdictLabel(verdict: string | null): string {
  return verdict === "approve" ? "Approved" : verdict === "revise" ? "Needs revision" : "No verdict";
}

/** Project detail: the story review (balanced / quality presets). Renders nothing when the workflow has no review step. */
export function ReviewPanel({ project, storyHref }: { project: ProjectSnapshot; storyHref?: string }) {
  const review = project.review ?? null;
  const revisions = project.revision_count ?? 0;
  const hasStep = project.steps.some((s) => s.step === "review");
  if (!review && !hasStep) return null;

  let body;
  if (!review) {
    body = <p>Review not started yet</p>;
  } else if (review.status === "failed" || review.status === "cancelled") {
    body = (
      <div role="alert" aria-label="Review failed">
        <strong>Review {review.status}</strong> ({review.error_code ?? "unknown error"}) - round {review.round_number}
      </div>
    );
  } else if (review.status !== "completed") {
    body = <p role="status">Review in progress (round {review.round_number})</p>;
  } else {
    const groups = groupIssues(review.issues);
    const hidden = review.issue_count - review.issues.length;
    body = (
      <>
        <p>
          <span className={`badge badge-verdict-${review.verdict ?? "none"}`} data-testid="review-verdict">
            {verdictLabel(review.verdict)}
          </span>{" "}
          <span className="muted">round {review.round_number}</span>
        </p>
        {review.summary && <p>{review.summary}</p>}
        {review.issue_count === 0 ? <p className="muted">No issues found</p> : (
          <>
            <p>{review.issue_count} {review.issue_count === 1 ? "issue" : "issues"} found</p>
            {groups.map(([aspect, list]) => (
              <div key={aspect} role="group" aria-label={`${ASPECT_LABELS[aspect] ?? aspect} issues`}>
                <h4>{ASPECT_LABELS[aspect] ?? aspect}</h4>
                <ul className="review-issues">
                  {list.map((issue, i) => (
                    <li key={i} className={`review-issue review-issue-${issue.severity}`}>
                      <strong>[{issue.severity}]</strong> {issue.note}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            {hidden > 0 && <p className="muted">Showing the first {review.issues.length} of {review.issue_count} issues</p>}
          </>
        )}
      </>
    );
  }

  return (
    <section aria-label="Review">
      <h3>Review</h3>
      {body}
      {(review?.revised || revisions > 0) && (
        <p data-testid="review-revised">
          The story was revised {revisions > 0 ? revisions : 1} {revisions > 1 ? "times" : "time"}; the newest version
          {project.story_version ? ` (v${project.story_version.version_number})` : ""} is the one used for the audio.
          {storyHref && <> <a href={storyHref} target="_blank" rel="noreferrer">Open newest version</a></>}
        </p>
      )}
    </section>
  );
}
