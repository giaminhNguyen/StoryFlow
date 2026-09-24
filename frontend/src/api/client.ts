// Typed client for the local StoryFlow API. The frontend talks to nothing else: no SQLite, no filesystem.
import type {
  CommandResult, CreateWorkflowInput, ErrorCode, Health, ProjectSnapshot, RunnerCommandResult, RunnerSnapshot,
  WorkflowSnapshot, WorkflowSummary,
} from "./types";

/** The API answered with the documented error contract {"error": {code, message, details}}. */
export class ApiError extends Error {
  constructor(public status: number, public code: ErrorCode | string, message: string,
              public details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
  }
  get reason(): string | undefined {
    const r = this.details["reason"];
    return typeof r === "string" ? r : undefined;
  }
}

/** The backend could not be reached at all (network error / not JSON): recoverable, keep polling. */
export class BackendUnavailableError extends Error {
  constructor(message = "Backend unavailable") {
    super(message);
    this.name = "BackendUnavailableError";
  }
}

type Fetch = typeof fetch;

export class ApiClient {
  constructor(private baseUrl = "", private fetchImpl?: Fetch) {}

  private async request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const doFetch = this.fetchImpl ?? globalThis.fetch.bind(globalThis);
    let response: Response;
    try {
      response = await doFetch(`${this.baseUrl}/api${path}`, {
        method, signal,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new BackendUnavailableError();
    }
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      if (response.ok) throw new BackendUnavailableError("Backend returned an unreadable response");
    }
    if (!response.ok) {
      const err = (payload as { error?: { code?: string; message?: string; details?: Record<string, unknown> } } | null)
        ?.error;
      if (err?.code) throw new ApiError(response.status, err.code, err.message ?? err.code, err.details ?? {});
      throw new BackendUnavailableError(`Unexpected response (${response.status})`);
    }
    return payload as T;
  }

  /** /health answers 503 with the normal body when the DB is down: return it instead of throwing. */
  async health(signal?: AbortSignal): Promise<Health> {
    const doFetch = this.fetchImpl ?? globalThis.fetch.bind(globalThis);
    let response: Response;
    try {
      response = await doFetch(`${this.baseUrl}/api/health`, { signal });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new BackendUnavailableError();
    }
    let payload: Health | null = null;
    try {
      payload = (await response.json()) as Health;
    } catch {
      /* fall through */
    }
    if (!payload || typeof payload !== "object" || !("db" in payload)) {
      throw new BackendUnavailableError(`Unexpected health response (${response.status})`);
    }
    return payload;
  }

  listWorkflows(signal?: AbortSignal): Promise<WorkflowSummary[]> {
    return this.request<{ workflows: WorkflowSummary[] }>("GET", "/workflows", undefined, signal).then((r) => r.workflows);
  }
  getWorkflow(id: string, signal?: AbortSignal): Promise<WorkflowSnapshot> {
    return this.request("GET", `/workflows/${encodeURIComponent(id)}`, undefined, signal);
  }
  createWorkflow(input: CreateWorkflowInput): Promise<{ result: CommandResult; workflow: WorkflowSnapshot }> {
    return this.request("POST", "/workflows", input);
  }
  command(id: string, action: "start" | "pause" | "resume" | "retry" | "cancel", body?: { project_id?: string }):
    Promise<{ result: CommandResult; workflow: WorkflowSnapshot }> {
    return this.request("POST", `/workflows/${encodeURIComponent(id)}/${action}`, body ?? (action === "retry" ? {} : undefined));
  }
  addProject(workflowId: string, input: { title: string; slug?: string; description?: string }):
    Promise<{ result: CommandResult; project: ProjectSnapshot }> {
    return this.request("POST", `/workflows/${encodeURIComponent(workflowId)}/projects`, input);
  }
  getProject(id: string, signal?: AbortSignal): Promise<ProjectSnapshot> {
    return this.request("GET", `/projects/${encodeURIComponent(id)}`, undefined, signal);
  }

  listRunners(signal?: AbortSignal): Promise<RunnerSnapshot[]> {
    return this.request<{ runners: RunnerSnapshot[] }>("GET", "/runners", undefined, signal).then((r) => r.runners);
  }
  assignRunner(runnerId: string, workflowId: string, roles?: string[]):
    Promise<{ result: RunnerCommandResult; runner: RunnerSnapshot }> {
    return this.request("POST", `/runners/${encodeURIComponent(runnerId)}/assign`,
      roles ? { workflow_id: workflowId, roles } : { workflow_id: workflowId });
  }
  unassignRunner(runnerId: string): Promise<{ result: RunnerCommandResult; runner: RunnerSnapshot }> {
    return this.request("POST", `/runners/${encodeURIComponent(runnerId)}/unassign`, {});
  }
  setRunnerEnabled(runnerId: string, enabled: boolean): Promise<{ result: RunnerCommandResult; runner: RunnerSnapshot }> {
    return this.request("POST", `/runners/${encodeURIComponent(runnerId)}/${enabled ? "enable" : "disable"}`, {});
  }

  /** URL of a stored artifact for <audio src> / links. `relPath` is the store-relative path from a snapshot. */
  artifactUrl(relPath: string): string {
    return `${this.baseUrl}/api/artifacts/${relPath.split("/").map(encodeURIComponent).join("/")}`;
  }
  /** Fetch a text artifact (story, source, canon). */
  async artifactText(relPath: string, signal?: AbortSignal): Promise<string> {
    const doFetch = this.fetchImpl ?? globalThis.fetch.bind(globalThis);
    let response: Response;
    try {
      response = await doFetch(this.artifactUrl(relPath), { signal });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new BackendUnavailableError();
    }
    if (!response.ok) throw new ApiError(response.status, response.status === 404 ? "not_found" : "internal", "Artifact unavailable");
    return response.text();
  }
}

export const defaultClient = new ApiClient(import.meta.env.VITE_API_BASE ?? "");
