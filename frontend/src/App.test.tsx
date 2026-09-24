import { act, render, screen } from "@testing-library/react";
import { BackendUnavailableError } from "./api/client";
import { App } from "./App";
import { makeHealth } from "./test/fixtures";
import { makeFakeClient } from "./test/fixtures2";

const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

beforeEach(() => { vi.useFakeTimers(); window.location.hash = ""; });
afterEach(() => { vi.useRealTimers(); window.location.hash = ""; });

describe("App shell", () => {
  test("routes: list, workflow detail, unknown", async () => {
    const client = makeFakeClient();
    render(<App client={client} />);
    await tick(0);
    expect(screen.getByRole("heading", { level: 1, name: "Workflows" })).toBeInTheDocument();
    act(() => { window.location.hash = "#/workflows/w1"; });
    await tick(0);
    expect(screen.getByRole("heading", { level: 1, name: "Lighthouse" })).toBeInTheDocument();
    act(() => { window.location.hash = "#/projects/p1"; });
    await tick(0);
    expect(screen.getByRole("link", { name: /workflow/i })).toBeInTheDocument();
    expect(client.getProject).toHaveBeenCalled();
    act(() => { window.location.hash = "#/bogus"; });
    await tick(0);
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to workflows" })).toHaveAttribute("href", "#/");
  });

  test.each([
    ["ok", makeHealth(), /Backend ok \(embedded, running\)/],
    ["degraded", makeHealth({ status: "degraded" }), /Backend degraded/],
  ])("health chip %s", async (_n, health, text) => {
    render(<App client={makeFakeClient({ health: vi.fn().mockResolvedValue(health) })} />);
    await tick(0);
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("banner appears when the backend is unreachable and disappears on recovery, keeping data", async () => {
    const listWorkflows = vi.fn().mockResolvedValue([]);
    const health = vi.fn().mockResolvedValue(makeHealth());
    render(<App client={makeFakeClient({ listWorkflows, health })} />);
    await tick(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    listWorkflows.mockRejectedValue(new BackendUnavailableError());
    health.mockRejectedValue(new BackendUnavailableError());
    await tick(6000);
    expect(screen.getByRole("alert")).toHaveTextContent("Backend unreachable - retrying");
    expect(screen.getByText("Backend unavailable")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Workflows" })).toBeInTheDocument();

    listWorkflows.mockResolvedValue([]);
    health.mockResolvedValue(makeHealth());
    await tick(20000);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/Backend ok/)).toBeInTheDocument();
  });

  test("health is polled slowly (5s) and stops after unmount", async () => {
    const client = makeFakeClient();
    const { unmount } = render(<App client={client} />);
    await tick(0);
    expect(client.health).toHaveBeenCalledTimes(1);
    await tick(4000);
    expect(client.health).toHaveBeenCalledTimes(1);
    await tick(1200);
    expect(client.health).toHaveBeenCalledTimes(2);
    unmount();
    const calls = [client.health, client.listWorkflows].map((f) => (f as ReturnType<typeof vi.fn>).mock.calls.length);
    await tick(60000);
    expect([client.health, client.listWorkflows].map((f) => (f as ReturnType<typeof vi.fn>).mock.calls.length)).toEqual(calls);
  });
});
