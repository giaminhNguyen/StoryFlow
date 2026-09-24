import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Real-backend smoke: spawns `python -m storyflow.api --fake` (see src/e2e/backend.setup.ts).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    globalSetup: ["./src/e2e/backend.setup.ts"],
    include: ["src/e2e/**/*.e2e.test.tsx"],
    testTimeout: 120_000,
    hookTimeout: 120_000,
    pool: "forks",
    fileParallelism: false,
    maxWorkers: 1,
  },
});
