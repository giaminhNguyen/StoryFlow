import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, BackendUnavailableError } from "../api/client";

export interface PollingOptions {
  /** Delay between the END of one request and the start of the next. Default 2000ms. */
  intervalMs?: number;
  /** Upper bound of the exponential backoff applied while the backend is unreachable. Default 15000ms. */
  maxIntervalMs?: number;
  enabled?: boolean;
}

export interface PollingState<T> {
  data: T | null;
  /** Last error (ApiError for a documented API error, BackendUnavailableError when unreachable). */
  error: ApiError | BackendUnavailableError | Error | null;
  /** False while the last attempt could not reach the backend (UI shows a recoverable banner). */
  connected: boolean;
  /** True until the first attempt finishes. */
  loading: boolean;
  lastUpdated: number | null;
  /** Fetch now (e.g. right after a command) and re-arm the timer; never overlaps an in-flight request. */
  refresh: () => Promise<void>;
}

/**
 * Bounded polling: one request at a time (timer re-armed only after completion), exponential backoff up to
 * `maxIntervalMs` while unreachable (reset on success), paused while the tab is hidden, aborted and stopped on
 * unmount. Keeps the last good `data` across failures so screens stay readable during a backend restart.
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { intervalMs = 2000, maxIntervalMs = 15000, enabled = true }: PollingOptions = {},
): PollingState<T> {
  const [state, setState] = useState<Omit<PollingState<T>, "refresh">>({
    data: null, error: null, connected: true, loading: true, lastUpdated: null,
  });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const runRef = useRef<() => Promise<void>>(async () => {});

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight: Promise<void> | null = null;
    let controller: AbortController | null = null;
    let delay = intervalMs;

    const schedule = () => {
      if (stopped) return;
      clearTimeout(timer);
      timer = setTimeout(() => { void run(); }, delay);
    };

    const run = (): Promise<void> => {
      if (stopped) return Promise.resolve();
      if (inFlight) return inFlight;
      clearTimeout(timer);
      controller = new AbortController();
      const signal = controller.signal;
      inFlight = (async () => {
        try {
          const data = await fetcherRef.current(signal);
          if (stopped) return;
          delay = intervalMs;
          setState({ data, error: null, connected: true, loading: false, lastUpdated: Date.now() });
        } catch (error) {
          if (stopped || (error instanceof DOMException && error.name === "AbortError")) return;
          const unreachable = error instanceof BackendUnavailableError;
          delay = unreachable ? Math.min(delay * 2, maxIntervalMs) : intervalMs;
          setState((prev) => ({
            ...prev, error: error as Error, connected: !unreachable, loading: false,
          }));
        } finally {
          inFlight = null;
          if (!stopped && !document.hidden) schedule();
        }
      })();
      return inFlight;
    };
    runRef.current = run;

    const onVisibility = () => {
      if (stopped) return;
      if (document.hidden) clearTimeout(timer);
      else void run();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void run();

    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, intervalMs, maxIntervalMs]);

  const refresh = useCallback(() => runRef.current(), []);
  return { ...state, refresh };
}
