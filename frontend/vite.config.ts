import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// REGIME_API_PROXY overrides the backend the dev server forwards to. The default is the
// local API. A judge demo sets this to a SIM-only process so the public page never
// shares a port with a LIVE agent.
const api = process.env.REGIME_API_PROXY || "http://127.0.0.1:8000";
const ws = api.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": api,
      "/health": api,
      "/ws": { target: ws, ws: true },
    },
  },
});
