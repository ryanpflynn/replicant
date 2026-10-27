import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `pnpm dev` proxies /v1 to a control-plane-api (default the local stack's http://127.0.0.1:8401).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: { "/v1": { target: process.env.REPLICANT_API_UPSTREAM ?? "http://127.0.0.1:8401", changeOrigin: true } },
  },
  build: { outDir: "dist", sourcemap: true, chunkSizeWarningLimit: 800 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
