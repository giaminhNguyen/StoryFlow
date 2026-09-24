import type { ApiClient } from "../api/client";
import type { AudioChunkInfo } from "../api/types";

export function formatDuration(ms: number): string {
  const total = Math.max(0, ms) / 1000;
  const minutes = Math.floor(total / 60);
  const seconds = total - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(1).padStart(4, "0")}`;
}

export function AudioChunkList({ client, chunks }: { client: ApiClient; chunks: AudioChunkInfo[] }) {
  if (chunks.length === 0) return <p>No audio chunks yet</p>;
  const ordered = [...chunks].sort((a, b) => a.chunk_index - b.chunk_index);
  return (
    <ol aria-label="Audio chunks" style={{ listStyle: "none", padding: 0 }}>
      {ordered.map((chunk) => (
        <li key={chunk.chunk_index} data-testid="audio-chunk">
          <span>Chunk {chunk.chunk_index}</span> <span>{formatDuration(chunk.duration_ms)}</span>{" "}
          {chunk.artifact_path ? (
            <audio controls preload="none" aria-label={`Chunk ${chunk.chunk_index}`}
                   src={client.artifactUrl(chunk.artifact_path)} />
          ) : (
            <em>file missing</em>
          )}
        </li>
      ))}
    </ol>
  );
}
