import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/delivery-api": "http://127.0.0.1:8923" } },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    // 单线程：与 public_web 保持一致，受限环境下也能跑。
    pool: "threads",
    maxWorkers: 1,
    fileParallelism: false,
  },
});
