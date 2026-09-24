import { ApiError } from "./api/client";

/** Backend timestamps are naive local ISO strings; show them as "YYYY-MM-DD HH:MM". */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  return iso.replace("T", " ").slice(0, 16);
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const reason = error.reason;
    return reason ? `${error.message} (${error.code}: ${reason})` : `${error.message} (${error.code})`;
  }
  if (error instanceof Error) return error.message;
  return "Unexpected error";
}

/** Validation details from the API, one line per field when present. */
export function errorDetails(error: unknown): string[] {
  if (!(error instanceof ApiError)) return [];
  const out: string[] = [];
  for (const [k, v] of Object.entries(error.details)) {
    if (k === "reason") continue;
    if (typeof v === "string" || typeof v === "number") out.push(`${k}: ${v}`);
  }
  return out;
}

export function newKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
