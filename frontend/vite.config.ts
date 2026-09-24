/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: the browser talks to this origin and Vite proxies /api to the local StoryFlow API
// (python -m storyflow.api, default 127.0.0.1:8765), so no CORS setup is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": process.env.STORYFLOW_API_URL ?? "http://127.0.0.1:8765" },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["src/**/*.e2e.test.{ts,tsx}"],
  },
});
