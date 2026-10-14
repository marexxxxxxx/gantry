import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Der Build landet direkt im Static-Mount des Gateways (aiserver_monitor/static),
// damit ein Prozess und ein Port das Dashboard ausliefern. Im Dev-Betrieb geht
// die API ueber den Proxy an das lokal laufende Gateway (Port 30000).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": "http://127.0.0.1:30000",
      "/monitor": "http://127.0.0.1:30000",
      "/healthz": "http://127.0.0.1:30000",
    },
  },
  build: {
    outDir: "../aiserver_monitor/static",
    emptyOutDir: true,
  },
});
