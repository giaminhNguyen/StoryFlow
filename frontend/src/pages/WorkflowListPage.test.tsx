import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "../api/client";
import type { DisplayState } from "../api/types";
import { STATE_LABELS } from "../components/StateBadge";
import { AppContext } from "../connection";
import { makeHealth, makeSummary } from "../test/fixtures";
import { makeFakeClient } from "../test/fixtures2";
import { WorkflowListPage } from "./WorkflowListPage";

afterEach(() => { window.location.hash = ""; });

describe("WorkflowListPage", () => {
  test("empty state", async () => {
    render(<WorkflowListPage client={makeFakeClient({ listWorkflows: vi.fn().mockResolvedValue([]) })} />);
    expect(await screen.findByText(/No workflows yet/)).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Create workflow" })).toBeInTheDocument();
  });

  test("row shows name link, mode, progress and updated time", async () => {
    const client = makeFakeClient({
      listWorkflows: vi.fn().mockResolvedValue([makeSummary({ id: "w9", name: "Harbor", project_count: 4, completed_projects: 1 })]),
    });
    render(<WorkflowListPage client={client} />);
    const link = await screen.findByRole("link", { name: "Harbor" });
    expect(link).toHaveAttribute("href", "#/workflows/w9");
    const row = link.closest("tr")!;
    expect(within(row).getByText("1/4")).toBeInTheDocument();
    expect(within(row).getByRole("progressbar")).toHaveAttribute("value", "1");
    expect(within(row).getByText("2026-09-25 05:01")).toBeInTheDocument();
    expect(within(row).queryByText(/Needs attention/)).not.toBeInTheDocument();
  });

  test.each(Object.keys(STATE_LABELS) as DisplayState[])("badge for %s", async (state) => {
    const client = makeFakeClient({ listWorkflows: vi.fn().mockResolvedValue([makeSummary({ display_state: state })]) });
    render(<WorkflowListPage client={client} />);
    const row = (await screen.findByRole("link", { name: "Lighthouse" })).closest("tr")!;
    expect(within(row).getByText(STATE_LABELS[state], { exact: false })).toBeInTheDocument();
    const attention = ["failed", "blocked", "waiting_capacity"].includes(state);
    expect(!!within(row).queryByText(/Needs attention/)).toBe(attention);
  });

  test("create form prefills demo video id and posts the expected payload", async () => {
    const client = makeFakeClient({ createWorkflow: vi.fn().mockResolvedValue({ workflow: { id: "new-1" } }) });
    render(
      <AppContext.Provider value={{ health: makeHealth(), report: () => {}, forget: () => {} }}>
        <WorkflowListPage client={client} />
      </AppContext.Provider>,
    );
    const video = screen.getByLabelText("YouTube video id");
    await waitFor(() => expect(video).toHaveValue("demo-video"));
    expect(screen.getByLabelText("Language")).toHaveValue("en");
    expect(screen.getByLabelText("Voice")).toHaveValue("narrator");
    await userEvent.type(screen.getByLabelText("Name"), "My flow");
    await userEvent.type(screen.getByLabelText("Story branch (optional)"), "dark");
    await userEvent.click(screen.getByRole("button", { name: "Create workflow" }));
    await waitFor(() => expect(window.location.hash).toBe("#/workflows/new-1"));
    expect(client.createWorkflow).toHaveBeenCalledWith({
      name: "My flow", client_key: expect.any(String),
      config: { source: { video_id: "demo-video", languages: ["en"] }, story: { branch: "dark" }, tts: { voice: "narrator" } },
    });
  });

  test("retry after an error reuses the client_key; editing changes it; error shown inline", async () => {
    const createWorkflow = vi.fn()
      .mockRejectedValueOnce(new ApiError(400, "validation", "Invalid config", { field: "video_id" }))
      .mockRejectedValueOnce(new ApiError(400, "validation", "Invalid config"))
      .mockResolvedValue({ workflow: { id: "ok" } });
    render(<WorkflowListPage client={makeFakeClient({ createWorkflow })} />);
    await userEvent.type(screen.getByLabelText("Name"), "n");
    await userEvent.type(screen.getByLabelText("YouTube video id"), "vid");
    const submit = screen.getByRole("button", { name: "Create workflow" });
    await userEvent.click(submit);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Invalid config");
    expect(alert).toHaveTextContent("field: video_id");
    await userEvent.click(submit);
    await waitFor(() => expect(createWorkflow).toHaveBeenCalledTimes(2));
    const [k1, k2] = createWorkflow.mock.calls.map((c) => c[0].client_key);
    expect(k1).toBe(k2);
    await screen.findByRole("alert");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(submit);
    await waitFor(() => expect(createWorkflow).toHaveBeenCalledTimes(3));
    expect(createWorkflow.mock.calls[2][0].client_key).not.toBe(k1);
  });

  test("submit button is disabled while creating (no double submit)", async () => {
    let resolve!: (v: unknown) => void;
    const createWorkflow = vi.fn(() => new Promise((r) => { resolve = r; }));
    render(<WorkflowListPage client={makeFakeClient({ createWorkflow })} />);
    await userEvent.type(screen.getByLabelText("Name"), "n");
    await userEvent.type(screen.getByLabelText("YouTube video id"), "v");
    await userEvent.click(screen.getByRole("button", { name: "Create workflow" }));
    const busy = await screen.findByRole("button", { name: "Creating..." });
    expect(busy).toBeDisabled();
    resolve({ workflow: { id: "z" } });
    await waitFor(() => expect(window.location.hash).toBe("#/workflows/z"));
    expect(createWorkflow).toHaveBeenCalledTimes(1);
  });
});
