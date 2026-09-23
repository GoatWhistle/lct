import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const proxy = {
  "/api": { target: process.env.VITE_BACKEND_URL || "http://localhost:8000", changeOrigin: true },
  "/ws": { target: process.env.VITE_WS_BACKEND_URL || "ws://localhost:8000", ws: true },
};

// Бэкенд один, порт один: фронт ходит на /api и /ws через прокси,
// чтобы в классе не было разъезда адресов между машинами.
export default defineConfig({
  server: {
    port: 5173,
    proxy,
  },
  preview: { port: 5173, host: "0.0.0.0", proxy },
  resolve: { alias: { "@": "/src" } },
});
