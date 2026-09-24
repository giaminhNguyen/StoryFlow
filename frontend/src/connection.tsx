import { createContext, useCallback, useContext, useEffect, useId, useMemo, useState, type ReactNode } from "react";
import type { Health, ProvidersInfo } from "./api/types";

interface AppContextValue {
  health: Health | null;
  providers?: ProvidersInfo | null;
  /** Report whether a polling source can reach the backend; key identifies the source. */
  report: (key: string, connected: boolean) => void;
  /** Remove a source (on unmount). */
  forget: (key: string) => void;
}

export const AppContext = createContext<AppContextValue>({ health: null, providers: null, report: () => {}, forget: () => {} });

export function AppProvider({ health, providers = null, onDisconnectedChange, children }: {
  health: Health | null; providers?: ProvidersInfo | null; onDisconnectedChange: (disconnected: boolean) => void; children: ReactNode;
}) {
  const [down, setDown] = useState<Record<string, boolean>>({});
  const report = useCallback((key: string, connected: boolean) => {
    setDown((prev) => (prev[key] === !connected ? prev : { ...prev, [key]: !connected }));
  }, []);
  const forget = useCallback((key: string) => {
    setDown((prev) => {
      if (!(key in prev)) return prev;
      const { [key]: _gone, ...rest } = prev;
      void _gone;
      return rest;
    });
  }, []);
  const anyDown = Object.values(down).some(Boolean);
  useEffect(() => onDisconnectedChange(anyDown), [anyDown, onDisconnectedChange]);
  const value = useMemo(() => ({ health, providers, report, forget }), [health, providers, report, forget]);
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

/** Tell the shell whether this polling source is currently connected. */
export function useReportConnection(connected: boolean): void {
  const { report, forget } = useContext(AppContext);
  const key = useId();
  useEffect(() => { report(key, connected); }, [report, key, connected]);
  useEffect(() => () => forget(key), [forget, key]);
}
