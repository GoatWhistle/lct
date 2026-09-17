import { createBrowserRouter, Navigate } from "react-router-dom";

import { Call } from "@/pages/trainee/Call";
import { Stub } from "@/shared/ui/Stub";

// Четыре интерфейса — одна SPA (docs/arch/FRONTEND.md).
// session_id живёт в URL: /instructor?session=..., монитор открывают ссылкой.
export const router = createBrowserRouter([
  { path: "/", element: <Navigate to="/trainee" replace /> },
  { path: "/trainee", element: <Call /> },
  { path: "/instructor", element: <Stub title="Пульт преподавателя" card="lct-17" /> },
  { path: "/wall", element: <Stub title="Внешний монитор" card="lct-16" /> },
  { path: "/dds", element: <Stub title="АРМ диспетчера ДДС" card="lct-20" /> },
]);
