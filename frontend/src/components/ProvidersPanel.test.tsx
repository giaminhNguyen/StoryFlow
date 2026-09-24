import { render, screen } from "@testing-library/react";
import { AppContext } from "../connection";
import type { ProviderStatus, ProvidersInfo } from "../api/types";
import { ProviderChip, ProvidersPanel, summarizeProviders } from "./ProvidersPanel";

const st = (kind: ProviderStatus["kind"], state: ProviderStatus["state"], over: Partial<ProviderStatus> = {}):
  ProviderStatus => ({ name: "x", kind, state, usable: state === "ready" || state === "fake", message: "", details: {}, ...over });
const info = (providers: ProviderStatus[], ready = providers.every((p) => p.usable)): ProvidersInfo =>
  ({ configured: true, ready, providers });

function ctx(providers: ProvidersInfo | null, children: React.ReactNode) {
  return (
    <AppContext.Provider value={{ health: null, providers, report: () => {}, forget: () => {} }}>
      {children}
    </AppContext.Provider>
  );
}

describe("summarizeProviders", () => {
  test("null / unconfigured -> nothing", () => {
    expect(summarizeProviders(null)).toBeNull();
    expect(summarizeProviders({ configured: false, ready: null, providers: [] })).toBeNull();
  });
  test("all real ready", () => {
    expect(summarizeProviders(info([st("subtitle", "ready"), st("story", "ready"), st("tts", "ready")])))
      .toEqual({ text: "Providers ready", tone: "chip-ok" });
  });
  test("fakes are labelled demo, never plain ready", () => {
    expect(summarizeProviders(info([st("subtitle", "fake"), st("story", "fake"), st("tts", "fake")])))
      .toEqual({ text: "Providers: demo (fake)", tone: "chip-warn" });
  });
  test("some unusable -> count", () => {
    expect(summarizeProviders(info([st("subtitle", "ready"), st("story", "disabled"), st("tts", "unavailable")])))
      .toEqual({ text: "Providers: 1/3 usable", tone: "chip-bad" });
  });
});

describe("ProvidersPanel", () => {
  test("shows every state with distinct text and messages", () => {
    render(ctx(info([
      st("subtitle", "ready", { name: "external" }),
      st("story", "disabled", { name: "none", message: "no story runner selected" }),
      st("tts", "unavailable", { name: "vieneu", message: "VieNeu venv not found" }),
    ]), <ProvidersPanel />));
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/no story runner selected/)).toBeInTheDocument();
    expect(screen.getByText(/Some providers are not usable/)).toBeInTheDocument();
  });
  test("misconfigured and fake states", () => {
    render(ctx(info([st("story", "misconfigured"), st("tts", "fake")]), <ProvidersPanel />));
    expect(screen.getByText("Misconfigured")).toBeInTheDocument();
    expect(screen.getByText("Demo (fake)")).toBeInTheDocument();
  });
  test("renders nothing when unconfigured or unknown", () => {
    const { container } = render(ctx({ configured: false, ready: null, providers: [] }, <ProvidersPanel />));
    expect(container).toBeEmptyDOMElement();
  });
  test("no hint when everything is usable", () => {
    render(ctx(info([st("subtitle", "ready"), st("story", "ready"), st("tts", "ready")]), <ProvidersPanel />));
    expect(screen.queryByText(/Some providers are not usable/)).toBeNull();
  });
});

test("ProviderChip renders the summary in the header", () => {
  render(ctx(info([st("subtitle", "ready"), st("story", "disabled")]), <ProviderChip />));
  expect(screen.getByRole("status")).toHaveTextContent("Providers: 1/2 usable");
});
