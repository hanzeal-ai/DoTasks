import {defineConfig} from "vite";
import react from "@vitejs/plugin-react";
import {fileURLToPath, URL} from "node:url";

const backendOrigin = process.env.DOTASKS_BACKEND_ORIGIN || "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: fileURLToPath(new URL("../static", import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {target: backendOrigin, changeOrigin: true},
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
});
