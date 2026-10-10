import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/public-api": "http://127.0.0.1:8100" } },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    // Threads keep the public-web test suite runnable on restricted Windows
    // hosts where child-process spawning is unavailable. CI can still override
    // this through the Vitest CLI when it needs more parallel workers.
    pool: "threads",
    maxWorkers: 1,
    fileParallelism: false,
  },
});
