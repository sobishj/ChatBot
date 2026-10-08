import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, `npm run dev` proxies API calls to the admin server in Docker.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8001",
      "/health": "http://localhost:8001",
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
