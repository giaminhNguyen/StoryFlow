import { useContext } from "react";
import type { ProviderKind, ProviderState, ProviderStatus, ProvidersInfo } from "../api/types";
import { AppContext } from "../connection";

export const PROVIDER_KIND_LABELS: Record<ProviderKind, string> = {
  subtitle: "Subtitles", story: "Story runner", tts: "Text-to-speech",
};

/** Text (not colour-only) label per state; "Demo (fake)" makes clear a fake is never production. */
export const PROVIDER_STATE_LABELS: Record<ProviderState, string> = {
  ready: "Ready", unavailable: "Unavailable", misconfigured: "Misconfigured", disabled: "Not configured",
  fake: "Demo (fake)",
};

const STATE_CLASS: Record<ProviderState, string> = {
  ready: "chip-ok", fake: "chip-warn", unavailable: "chip-bad", misconfigured: "chip-bad", disabled: "",
};

/** One-line summary used by the header chip. */
export function summarizeProviders(info: ProvidersInfo | null | undefined): { text: string; tone: string } | null {
  if (!info || !info.configured) return null;
  const total = info.providers.length;
  const usable = info.providers.filter((p) => p.usable).length;
  const demo = info.providers.some((p) => p.state === "fake");
  if (info.ready) return { text: demo ? "Providers: demo (fake)" : "Providers ready", tone: demo ? "chip-warn" : "chip-ok" };
  return { text: `Providers: ${usable}/${total} usable`, tone: "chip-bad" };
}

export function ProviderChip() {
  const { providers } = useContext(AppContext);
  const summary = summarizeProviders(providers);
  if (!summary) return null;
  return <a href="#/" className={`chip ${summary.tone}`} role="status" title="Provider readiness">{summary.text}</a>;
}

function ProviderRow({ p }: { p: ProviderStatus }) {
  return (
    <li className="provider-row">
      <strong>{PROVIDER_KIND_LABELS[p.kind] ?? p.kind}</strong>{" "}
      <span className={`chip ${STATE_CLASS[p.state] ?? ""}`}>{PROVIDER_STATE_LABELS[p.state] ?? p.state}</span>{" "}
      <span className="muted">{p.name}{p.message ? ` - ${p.message}` : ""}</span>
    </li>
  );
}

/** Readiness of the real integrations. Being "detected/ready" never implies a runner is assigned to a workflow. */
export function ProvidersPanel({ info: given }: { info?: ProvidersInfo | null }) {
  const ctx = useContext(AppContext);
  const info = given === undefined ? ctx.providers : given;
  if (!info || !info.configured) return null;
  return (
    <section aria-labelledby="providers-heading" className="card">
      <h2 id="providers-heading">Providers</h2>
      <ul className="plain-list">
        {info.providers.map((p) => <ProviderRow key={`${p.kind}:${p.name}`} p={p} />)}
      </ul>
      {!info.ready && (
        <p className="muted">
          Some providers are not usable, so parts of the pipeline will wait. Configure them with STORYFLOW_* settings
          (see .env.example) and restart.
        </p>
      )}
    </section>
  );
}
