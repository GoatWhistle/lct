import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Бэкенд один, порт один: фронт ходит на /api и /ws через прокси,
// чтобы в классе не было разъезда адресов между машинами.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.VITE_BACKEND_URL || "http://localhost:8000", changeOrigin: true },
      "/ws": { target: process.env.VITE_WS_BACKEND_URL || "ws://localhost:8000", ws: true },
    },
  },
  resolve: { alias: { "@": "/src" } },
});
