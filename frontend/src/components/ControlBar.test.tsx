import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "../api/client";
import type { WorkflowStatus } from "../api/types";
import { makeFakeClient } from "../test/fixtures2";
import { ControlBar, availableActions } from "./ControlBar";

type Enabled = { start: boolean; pause: boolean; resume: boolean; retry: boolean; cancel: boolean };
const E = (start: boolean, pause: boolean, resume: boolean, retry: boolean, cancel: boolean): Enabled =>
  ({ start, pause, resume, retry, cancel });

const TABLE: [WorkflowStatus, string | null, Enabled][] = [
  ["draft", null, E(true, false, false, false, true)],
  ["active", null, E(false, true, false, false, true)],
  ["paused", null, E(false, false, true, false, true)],
  ["paused", "operator", E(false, false, true, false, true)],
  ["paused", "step_failed", E(false, false, false, true, true)],
  ["finished", null, E(false, false, false, false, false)],
  ["cancelled", null, E(false, false, false, false, false)],
  ["abandoned", null, E(false, false, false, false, false)],
];

describe("availableActions", () => {
  test.each(TABLE)("%s / %s", (status, status_reason, expected) => {
    const a = availableActions({ status, status_reason });
    const got = Object.fromEntries(Object.entries(a).map(([k, v]) => [k, v.enabled]));
    expect(got).toEqual(expected);
    for (const v of Object.values(a)) expect(v.reason).not.toBe("");
  });

  test("empty draft cannot start", () => {
    expect(availableActions({ status: "draft", status_reason: null, project_count: 0 }).start.enabled).toBe(false);
  });
});

const wf = (status: WorkflowStatus, status_reason: string | null = null) =>
  ({ id: "w1", status, status_reason, project_count: 1 });

describe("ControlBar", () => {
  test.each([["Pause", "pause", "active"], ["Start", "start", "draft"], ["Resume", "resume", "paused"]] as const)(
    "%s calls the command then refreshes", async (label, action, status) => {
      const client = makeFakeClient();
      const onDone = vi.fn();
      render(<ControlBar client={client} workflow={wf(status)} onDone={onDone} />);
      await userEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
      expect(client.command).toHaveBeenCalledWith("w1", action);
    });

  test("retry when step failed", async () => {
    const client = makeFakeClient();
    render(<ControlBar client={client} workflow={wf("paused", "step_failed")} onDone={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(client.command).toHaveBeenCalledWith("w1", "retry");
  });

  test("disabled controls have an explanation and do not fire", async () => {
    const client = makeFakeClient();
    render(<ControlBar client={client} workflow={wf("active")} onDone={vi.fn()} />);
    const start = screen.getByRole("button", { name: "Start" });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute("title", expect.stringMatching(/draft/i));
    await userEvent.click(start);
    expect(client.command).not.toHaveBeenCalled();
  });

  test("cancel needs confirmation; keep does nothing", async () => {
    const client = makeFakeClient();
    render(<ControlBar client={client} workflow={wf("active")} onDone={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel workflow" }));
    expect(screen.getByText("Cancel workflow? This is permanent.")).toBeInTheDocument();
    expect(client.command).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Keep workflow" }));
    expect(screen.queryByText(/This is permanent/)).not.toBeInTheDocument();
    expect(client.command).not.toHaveBeenCalled();
  });

  test("confirm cancel calls the command", async () => {
    const client = makeFakeClient();
    const onDone = vi.fn();
    render(<ControlBar client={client} workflow={wf("active")} onDone={onDone} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel workflow" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm cancel" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(client.command).toHaveBeenCalledWith("w1", "cancel");
  });

  test("backend conflict is shown as a dismissible alert and still refreshes", async () => {
    const client = makeFakeClient({
      command: vi.fn().mockRejectedValue(new ApiError(409, "conflict", "State changed elsewhere")),
    });
    const onDone = vi.fn();
    render(<ControlBar client={client} workflow={wf("active")} onDone={onDone} />);
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("State changed elsewhere");
    expect(alert).toHaveTextContent("conflict");
    expect(onDone).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss message" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
