import { useEffect, useState } from "react";

export type Route =
  | { name: "list" }
  | { name: "workflow"; id: string }
  | { name: "project"; id: string }
  | { name: "notfound"; hash: string };

/** Parse a location hash ("#/workflows/abc") into a route. Never throws. */
export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, "").split("?")[0].replace(/\/+$/, "");
  if (path === "" || path === "/") return { name: "list" };
  const m = /^\/(workflows|projects)\/([^/]+)$/.exec(path);
  if (m) {
    let id: string;
    try {
      id = decodeURIComponent(m[2]);
    } catch {
      return { name: "notfound", hash };
    }
    return m[1] === "workflows" ? { name: "workflow", id } : { name: "project", id };
  }
  return { name: "notfound", hash };
}

export const workflowHref = (id: string) => `#/workflows/${encodeURIComponent(id)}`;
export const projectHref = (id: string) => `#/projects/${encodeURIComponent(id)}`;

export function navigate(hash: string): void {
  window.location.hash = hash;
}

export function useHashRoute(): Route {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    on();
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parseRoute(hash);
}
