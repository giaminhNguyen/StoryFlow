import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import { makeAudio } from "../test/fixtures";
import { ArtifactText } from "./ArtifactText";
import { AudioChunkList, formatDuration } from "./AudioChunkList";
import { StoryViewer } from "./StoryViewer";

const real = new ApiClient("http://x");
function client(text: () => Promise<string>) {
  return { artifactText: vi.fn(text), artifactUrl: (p: string) => real.artifactUrl(p) } as unknown as ApiClient;
}

describe("ArtifactText", () => {
  it("shows loading then text, not HTML", async () => {
    const evil = '<img src=x onerror="alert(1)"> **md**';
    const { container } = render(<ArtifactText client={client(async () => evil)} path="projects/a.md" />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(await screen.findByTestId("artifact-text")).toHaveTextContent(evil);
    expect(container.querySelector("img")).toBeNull();
  });
  it("shows error", async () => {
    render(<ArtifactText client={client(async () => { throw new ApiError(404, "not_found", "Artifact unavailable"); })}
                         path="projects/a.md" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Artifact unavailable");
  });
  it("shows empty", async () => {
    render(<ArtifactText client={client(async () => "  ")} path="projects/a.md" />);
    expect(await screen.findByText("The artifact is empty")).toBeInTheDocument();
  });
  it("truncates large text with link", async () => {
    render(<ArtifactText client={client(async () => "x".repeat(50))} path="projects/a b.md" maxChars={10} />);
    expect(await screen.findByText(/Showing first 10 chars of 50/)).toBeInTheDocument();
    expect(screen.getByTestId("artifact-text").textContent?.trim()).toHaveLength(10);
    expect(screen.getByRole("link", { name: /open full/i })).toHaveAttribute("href", "http://x/api/artifacts/projects/a%20b.md");
  });
  it("aborts on unmount and reloads on path change", async () => {
    let signal: AbortSignal | undefined;
    const c = { artifactText: vi.fn((_p: string, s: AbortSignal) => { signal = s; return new Promise<string>(() => {}); }),
      artifactUrl: real.artifactUrl.bind(real) } as unknown as ApiClient;
    const { rerender, unmount } = render(<ArtifactText client={c} path="projects/a.md" />);
    const first = signal!;
    rerender(<ArtifactText client={c} path="projects/b.md" />);
    expect(first.aborted).toBe(true);
    expect(c.artifactText).toHaveBeenCalledTimes(2);
    unmount();
    expect(signal!.aborted).toBe(true);
  });
});

describe("StoryViewer", () => {
  it("shows empty state without story", () => {
    render(<StoryViewer client={client(async () => "")} story={null} />);
    expect(screen.getByText("Story not generated yet")).toBeInTheDocument();
  });
  it("shows metadata and text", async () => {
    render(<StoryViewer client={client(async () => "Hello")}
                        story={{ id: "v", version_number: 2, title: "T", word_count: 9, content_path: "projects/s.md" }} />);
    expect(screen.getByText(/Version 2: T \(9 words\)/)).toBeInTheDocument();
    expect(await screen.findByText("Hello")).toBeInTheDocument();
  });
});

describe("AudioChunkList", () => {
  it("formats durations", () => {
    expect(formatDuration(1500)).toBe("00:01.5");
    expect(formatDuration(65_250)).toBe("01:05.3");
  });
  it("orders chunks, labels and encodes src", () => {
    const chunks = [
      { chunk_index: 2, artifact_path: "projects/p 1/0002.wav", duration_ms: 1000 },
      { chunk_index: 1, artifact_path: "projects/p 1/0001.wav", duration_ms: 2000 },
    ];
    render(<AudioChunkList client={real} chunks={chunks} />);
    const rows = screen.getAllByTestId("audio-chunk");
    expect(rows[0]).toHaveTextContent("Chunk 1");
    expect(rows[1]).toHaveTextContent("Chunk 2");
    const audio = screen.getByLabelText("Chunk 1");
    expect(audio).toHaveAttribute("src", "http://x/api/artifacts/projects/p%201/0001.wav");
    expect(audio).toHaveAttribute("controls");
    expect(audio).toHaveAttribute("preload", "none");
  });
  it("renders makeAudio fixture and empty state", () => {
    const { rerender } = render(<AudioChunkList client={real} chunks={makeAudio(3).chunks} />);
    expect(screen.getAllByTestId("audio-chunk")).toHaveLength(3);
    rerender(<AudioChunkList client={real} chunks={[]} />);
    expect(screen.getByText("No audio chunks yet")).toBeInTheDocument();
  });
});
