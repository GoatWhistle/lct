import { createBrowserRouter, Navigate } from "react-router-dom";

import { Call } from "@/pages/trainee/Call";
import { Instructor } from "@/pages/instructor/Instructor";
import { Dds } from "@/pages/dds/Dds";
import { Admin } from "@/pages/admin/Admin";
import { Profile } from "@/pages/profile/Profile";
import { Wall } from "@/pages/wall/Wall";
import { RequireAuth } from "@/features/auth/Login";

// Четыре интерфейса — одна SPA (docs/arch/FRONTEND.md).
// session_id живёт в URL: /instructor?session=..., монитор открывают ссылкой.
// Каждый экран за проверкой роли: курсант не открывает пульт даже по прямой
// ссылке. Сервер проверяет то же самое на сокетах — здесь только чтобы человек
// увидел понятное объяснение вместо пустого экрана (lct-23).
const guarded = (screen: string, element: JSX.Element) => (
  <RequireAuth screen={screen}>{element}</RequireAuth>
);

export const router = createBrowserRouter([
  { path: "/", element: <Navigate to="/trainee" replace /> },
  { path: "/trainee", element: guarded("/trainee", <Call />) },
  { path: "/instructor", element: guarded("/instructor", <Instructor />) },
  { path: "/wall", element: guarded("/wall", <Wall />) },
  { path: "/profile", element: guarded("/profile", <Profile />) },
  { path: "/dds", element: guarded("/dds", <Dds />) },
  { path: "/admin", element: guarded("/admin", <Admin />) },
]);
