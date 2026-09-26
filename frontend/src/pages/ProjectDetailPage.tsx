import { useEffect, useState, type ReactNode } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { ProjectSnapshot } from "../api/types";
import { AudioChunkList } from "../components/AudioChunkList";
import { ReviewPanel } from "../components/ReviewPanel";
import { StoryViewer } from "../components/StoryViewer";
import { usePolling } from "../hooks/usePolling";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

/** The single joined audio file: a player plus a download link; a failed load explains itself instead of staying mute. */
function FinalAudio({ client, path }: { client: ApiClient; path: string }) {
  const [failed, setFailed] = useState(false);
  const url = client.artifactUrl(path);
  return (
    <div className="final-audio" role="group" aria-label="Final audio">
      <strong>Full audio</strong>{" "}
      <audio controls preload="none" aria-label="Full audio" src={url} onError={() => setFailed(true)} />{" "}
      <a href={url} download>Download</a>
      {failed && <p role="alert" className="muted">The audio could not be loaded; use Download.</p>}
    </div>
  );
}

function str(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "-";
}

const BLOCK_LABELS: Record<string, string> = {
  waiting_capacity: "Waiting for runner capacity",
  chunks_missing: "Audio chunks missing",
  provider_blocked: "Provider blocked",
  delayed: "Delayed",
  inconsistent: "Inconsistent state",
};

function Panels({ project }: { project: ProjectSnapshot }) {
  const { failure, block } = project;
  return (
    <>
      {failure && (
        <div role="alert" aria-label="Failure">
          <strong>{failure.category} failure</strong>
          {failure.code ? ` (${failure.code})` : ""}
          {failure.step ? ` at step ${failure.step}` : ""}
          {failure.message ? `: ${failure.message}` : ""}
          {failure.attempts !== null && failure.max_attempts !== null &&
            <div>Attempts: {failure.attempts}/{failure.max_attempts}</div>}
          {failure.infrastructure_failures !== null && failure.max_infra_attempts !== null &&
            <div>Infrastructure failures: {failure.infrastructure_failures}/{failure.max_infra_attempts}</div>}
        </div>
      )}
      {block && (
        <div role="status" aria-label="Blocked">
          <strong>{BLOCK_LABELS[block.kind] ?? block.kind}</strong>
          {block.message ? `: ${block.message}` : ""}
          {block.until ? ` (until ${block.until})` : ""}
        </div>
      )}
    </>
  );
}

function ProjectBody({ project, client }: { project: ProjectSnapshot; client: ApiClient }) {
  const { source, canon, story_generation: gen, story_version: story, tts, audio } = project;
  const paths = [source?.artifact_path, story?.content_path, ...(audio?.chunks.map((c) => c.artifact_path) ?? [])]
    .filter((p): p is string => !!p);
  return (
    <>
      <Panels project={project} />
      <Section title="Source">
        {source ? (
          <ul>
            <li>Language: {source.language ?? source.language_code ?? "-"}</li>
            <li>Provider: {str(source.provenance["provider"])}</li>
            <li>Video id: {str(source.provenance["video_id"])}</li>
            <li>Snapshot: #{source.snapshot_number}</li>
            <li>Hash: {source.content_hash ? source.content_hash.slice(0, 12) : "-"}</li>
          </ul>
        ) : <p>Source not fetched yet</p>}
      </Section>
      <Section title="Canon">
        {canon ? <p>Status: {canon.status}; canon {canon.has_canon ? "available" : "not available"}</p>
          : <p>Canon not extracted yet</p>}
      </Section>
      <Section title="Story">
        {gen && <p>Generation: {gen.status}</p>}
        <StoryViewer client={client} story={story} />
      </Section>
      <ReviewPanel project={project} storyHref={story?.content_path ? client.artifactUrl(story.content_path) : undefined} />
      <Section title="TTS">
        {tts ? (
          <p>Voice: {tts.voice}; engine: {tts.engine}; status: {tts.status}; chunks: {tts.chunk_count ?? "-"}</p>
        ) : <p>TTS not prepared yet</p>}
      </Section>
      <Section title="Audio">
        {audio ? (
          <>
            <p>Run {audio.run_number}: {audio.status}; {audio.registered_chunks}/{audio.chunk_count} chunks registered</p>
            {audio.final_path && <FinalAudio client={client} path={audio.final_path} />}
            <AudioChunkList client={client} chunks={audio.chunks} />
          </>
        ) : <p>Audio not generated yet</p>}
      </Section>
      {paths.length > 0 && (
        <details>
          <summary>Artifacts</summary>
          <ul>
            {paths.map((p) => (
              <li key={p}><a href={client.artifactUrl(p)} target="_blank" rel="noreferrer">{p}</a></li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

export function ProjectDetailPage({ projectId, client }: { projectId: string; client: ApiClient }) {
  const [slow, setSlow] = useState(false);
  const { data, error, connected, loading } = usePolling(
    (signal) => client.getProject(projectId, signal),
    { intervalMs: slow ? 10000 : 2000 },
  );
  const completed = data?.state === "completed";
  useEffect(() => { setSlow(completed); }, [completed]);
  if (error instanceof ApiError && error.code === "not_found") {
    return (
      <div>
        <p role="alert">Project not found</p>
        <a href="#/">Back to workflows</a>
      </div>
    );
  }
  if (!data) {
    if (loading) return <p role="status">Loading project...</p>;
    return <p role="alert">{connected ? `Could not load project: ${error?.message ?? "unknown error"}` : "Backend unavailable"}</p>;
  }
  return (
    <div>
      <a href={`#/workflows/${encodeURIComponent(data.workflow_id)}`}>Back to workflow</a>
      <h2>{data.title}</h2>
      <p>
        State: <span data-testid="project-state">{data.state}</span>
        {data.current_step ? <> - current step: {data.current_step}</> : null}
      </p>
      {!connected && <p role="status">Backend unavailable - showing last known data</p>}
      {connected && error && <p role="alert">Refresh failed: {error.message}</p>}
      <ProjectBody project={data} client={client} />
    </div>
  );
}
