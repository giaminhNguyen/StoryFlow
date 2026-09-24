import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";

export const MAX_DISPLAY_CHARS = 200_000;

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; text: string };

/** Loads a text artifact and shows it as PLAIN TEXT (never HTML). Aborts on unmount / path change. */
export function ArtifactText(props: { client: ApiClient; path: string; maxChars?: number; emptyLabel?: string }) {
  const { client, path, maxChars = MAX_DISPLAY_CHARS, emptyLabel = "The artifact is empty" } = props;
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading" });
    client.artifactText(path, controller.signal).then(
      (text) => { if (!controller.signal.aborted) setState({ kind: "ready", text }); },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const message = error instanceof Error && error.message ? error.message : "Could not load the artifact";
        setState({ kind: "error", message });
      },
    );
    return () => controller.abort();
  }, [client, path]);

  if (state.kind === "loading") return <p role="status">Loading text...</p>;
  if (state.kind === "error") return <p role="alert">Could not load text: {state.message}</p>;
  if (state.text.trim() === "") return <p>{emptyLabel}</p>;
  const truncated = state.text.length > maxChars;
  return (
    <div>
      <pre data-testid="artifact-text" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        {truncated ? state.text.slice(0, maxChars) : state.text}
      </pre>
      {truncated && (
        <p>
          Showing first {maxChars} chars of {state.text.length}.{" "}
          <a href={client.artifactUrl(path)} target="_blank" rel="noreferrer">Open full artifact</a>
        </p>
      )}
    </div>
  );
}
