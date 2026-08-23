import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite proxies `/api` and `/ask` to the local FastAPI process during
// `npm run dev`. In production, FastAPI serves the SPA same-origin
// from `web/dist/`, so the same paths work without a proxy.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/ask": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
