import { lazy, Suspense } from "react";
import { createBrowserRouter, Navigate } from "react-router-dom";

import { RequireAuth } from "@/features/auth/Login";
import { DEMO_MODE } from "@/features/auth/useAuth";

const Call = lazy(() => import("@/pages/trainee/Call").then((module) => ({ default: module.Call })));
const Instructor = lazy(() => import("@/pages/instructor/Instructor").then((module) => ({ default: module.Instructor })));
const Dds = lazy(() => import("@/pages/dds/Dds").then((module) => ({ default: module.Dds })));
const Admin = lazy(() => import("@/pages/admin/Admin").then((module) => ({ default: module.Admin })));
const Profile = lazy(() => import("@/pages/profile/Profile").then((module) => ({ default: module.Profile })));
const Groups = lazy(() => import("@/pages/groups/Groups").then((module) => ({ default: module.Groups })));
const Wall = lazy(() => import("@/pages/wall/Wall").then((module) => ({ default: module.Wall })));
const Phone = lazy(() => import("@/pages/phone/Phone").then((module) => ({ default: module.Phone })));
const Materials = lazy(() => import("@/pages/materials/Materials").then((module) => ({ default: module.Materials })));

// Четыре интерфейса — одна SPA (docs/arch/FRONTEND.md).
// session_id живёт в URL: /instructor?session=..., монитор открывают ссылкой.
// Каждый экран за проверкой роли: курсант не открывает пульт даже по прямой
// ссылке. Сервер проверяет то же самое на сокетах — здесь только чтобы человек
// увидел понятное объяснение вместо пустого экрана (lct-23).
const guarded = (screen: string, element: JSX.Element) => (
  <RequireAuth screen={screen}>
    <Suspense fallback={<main className="page"><p>Загружаем рабочее место…</p></main>}>
      {element}
    </Suspense>
  </RequireAuth>
);

export const router = createBrowserRouter([
  { path: "/", element: <Navigate to={DEMO_MODE ? "/instructor" : "/trainee"} replace /> },
  { path: "/trainee", element: guarded("/trainee", <Call />) },
  { path: "/trainee/completed", element: guarded("/trainee", <Call />) },
  { path: "/instructor", element: guarded("/instructor", <Instructor />) },
  { path: "/wall", element: guarded("/wall", <Wall />) },
  { path: "/profile", element: guarded("/profile", <Profile />) },
  { path: "/groups", element: guarded("/groups", <Groups />) },
  { path: "/dds", element: guarded("/dds", <Dds />) },
  { path: "/phone", element: guarded("/phone", <Phone />) },
  { path: "/materials", element: guarded("/materials", <Materials />) },
  { path: "/admin", element: guarded("/admin", <Admin />) },
]);
