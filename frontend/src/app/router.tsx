import { createBrowserRouter, Navigate } from "react-router-dom";

import { Call } from "@/pages/trainee/Call";
import { Instructor } from "@/pages/instructor/Instructor";
import { Dds } from "@/pages/dds/Dds";
import { Profile } from "@/pages/profile/Profile";
import { Wall } from "@/pages/wall/Wall";

// Четыре интерфейса — одна SPA (docs/arch/FRONTEND.md).
// session_id живёт в URL: /instructor?session=..., монитор открывают ссылкой.
export const router = createBrowserRouter([
  { path: "/", element: <Navigate to="/trainee" replace /> },
  { path: "/trainee", element: <Call /> },
  { path: "/instructor", element: <Instructor /> },
  { path: "/wall", element: <Wall /> },
  { path: "/profile", element: <Profile /> },
  { path: "/dds", element: <Dds /> },
]);
