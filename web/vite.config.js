import {defineConfig} from "vite";
import {devApiProxy} from "./dev-api-proxy.js";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import {fileURLToPath, URL} from "node:url";

const backendOrigin = process.env.DOTASKS_BACKEND_ORIGIN || "http://127.0.0.1:8765";

export default defineConfig(({ mode }) => ({
  // Concurrent dev and browser-test servers must not rewrite each other's optimizer cache.
  cacheDir: fileURLToPath(new URL(`./node_modules/.vite/${mode}`, import.meta.url)),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: fileURLToPath(new URL("../static", import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": devApiProxy(backendOrigin),
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
}));
