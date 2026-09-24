import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { inject } from "vitest";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";

const baseUrl = inject("e2eBaseUrl");

describe("full application flow against the real backend", () => {
  it("creates, runs and inspects a workflow end to end", async () => {
    window.location.hash = "#/";
    const user = userEvent.setup();
    const client = new ApiClient(baseUrl);
    render(<App client={client} />);

    // Empty list, create form prefilled from health.demo.
    const form = await screen.findByRole("form", { name: "Create workflow" }, { timeout: 30_000 });
    const video = within(form).getByLabelText(/YouTube video id/i) as HTMLInputElement;
    await waitFor(() => expect(video.value).toBe("demo-video"));
    await user.type(within(form).getByLabelText(/^Name/), "E2E workflow");
    await user.click(within(form).getByRole("button", { name: "Create workflow" }));

    // Detail page (hash route), add a project.
    await waitFor(() => expect(window.location.hash).toMatch(/^#\/workflows\//), { timeout: 30_000 });
    const addForm = await screen.findByRole("form", { name: "Add project" }, { timeout: 30_000 });
    await user.type(within(addForm).getByLabelText(/^Title/), "E2E project");
    await user.click(within(addForm).getByRole("button", { name: "Add project" }));
    await screen.findByRole("link", { name: "E2E project" }, { timeout: 30_000 });

    // Assign the discovered runner.
    const assign = await screen.findByRole("button", { name: /assign to this workflow/i }, { timeout: 30_000 });
    await user.click(assign);
    await screen.findByText("Assigned to this workflow");
    await waitFor(() => expect(screen.queryByText("No runners assigned.")).toBeNull(), { timeout: 30_000 });

    // Start and wait for real polling to reach completion.
    const start = screen.getByRole("button", { name: "Start" });
    await waitFor(() => expect(start).toBeEnabled());
    await user.click(start);
    await waitFor(() => expect(screen.getAllByText("Completed").length).toBeGreaterThan(0), { timeout: 100_000 });

    // Project page: story and audio.
    await user.click(screen.getByRole("link", { name: "E2E project" }));
    const story = await screen.findByTestId("artifact-text", undefined, { timeout: 30_000 });
    expect(story.textContent?.trim().length ?? 0).toBeGreaterThan(0);
    const chunks = await screen.findAllByTestId("audio-chunk", undefined, { timeout: 30_000 });
    expect(chunks.length).toBeGreaterThanOrEqual(1);
    const audio = chunks[0].querySelector("audio");
    expect(audio).not.toBeNull();
    const src = audio!.getAttribute("src")!;
    expect(src).toContain(`${baseUrl}/api/artifacts/projects/`);
    for (const el of document.querySelectorAll("audio")) {
      expect(el.getAttribute("src")).toContain("/api/artifacts/projects/");
    }

    // Real fetches of the artifact endpoint.
    const res = await fetch(src);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("audio/wav");
    const bytes = new Uint8Array(await res.arrayBuffer());
    expect(String.fromCharCode(...bytes.slice(0, 4))).toBe("RIFF");

    const traversal = await fetch(`${baseUrl}/api/artifacts/projects/%2e%2e/%2e%2e/e2e.db`);
    expect(traversal.status).toBe(404);
    const raw = await fetch(`${baseUrl}/api/artifacts/projects/../../e2e.db`);
    expect(raw.status).toBe(404);
  });
});
