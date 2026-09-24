// Vitest globalSetup: starts the real StoryFlow API (fake runner, offline demo subtitles) on a free port.
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { TestProject } from "vitest/node";

declare module "vitest" {
  export interface ProvidedContext { e2eBaseUrl: string }
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BACKEND = path.resolve(HERE, "../../../backend");

function findPython(): string {
  const venv = process.platform === "win32"
    ? path.join(BACKEND, ".venv", "Scripts", "python.exe")
    : path.join(BACKEND, ".venv", "bin", "python");
  if (existsSync(venv)) return venv;
  const env = process.env.STORYFLOW_PYTHON;
  if (env && existsSync(env)) return env;
  throw new Error(`E2E needs the backend interpreter: create ${venv} or set STORYFLOW_PYTHON to a python with storyflow installed.`);
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address() as net.AddressInfo;
      srv.close(() => resolve(port));
    });
  });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export default async function setup(project: TestProject) {
  const python = findPython();
  const tmp = mkdtempSync(path.join(tmpdir(), "storyflow-e2e-"));
  const artifacts = path.join(tmp, "artifacts");
  mkdirSync(artifacts, { recursive: true });
  const port = await freePort();
  const dbUrl = `sqlite:///${path.join(tmp, "e2e.db").replace(/\\/g, "/")}`;
  const child: ChildProcess = spawn(
    python,
    ["-m", "storyflow.api", "--fake", "--port", String(port), "--database-url", dbUrl, "--artifact-root", artifacts],
    { cwd: BACKEND, stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
  let output = "";
  child.stdout?.on("data", (d) => { output += String(d); });
  child.stderr?.on("data", (d) => { output += String(d); });
  let exited = false;
  child.once("exit", () => { exited = true; });

  const baseUrl = `http://127.0.0.1:${port}`;
  const teardown = async () => {
    if (child.pid && !exited) {
      if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"]);
      else child.kill("SIGTERM");
    }
    for (let i = 0; i < 20 && !exited; i++) await sleep(100);
    for (let i = 0; i < 10; i++) {
      try { rmSync(tmp, { recursive: true, force: true }); break; } catch { await sleep(200); }
    }
  };

  const deadline = Date.now() + 60_000;
  let ready = false;
  while (Date.now() < deadline && !exited) {
    try {
      const res = await fetch(`${baseUrl}/api/health`);
      if (res.status === 200) { ready = true; break; }
    } catch { /* not up yet */ }
    await sleep(250);
  }
  if (!ready) {
    await teardown();
    throw new Error(`StoryFlow API did not become healthy on ${baseUrl}.\n${output.slice(-3000)}`);
  }
  project.provide("e2eBaseUrl", baseUrl);
  return teardown;
}
