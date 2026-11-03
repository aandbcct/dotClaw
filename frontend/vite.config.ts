import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    // Docker Desktop 的绑定挂载使用轮询，原生开发保持默认文件监听。
    watch: process.env.DOTCLAW_VITE_POLLING === "1"
      ? { usePolling: true, interval: 300 }
      : undefined,
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
});
