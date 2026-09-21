// Кто сейчас за экраном. Роль приходит с сервера, а не хранится на фронте:
// подписанная cookie — источник истины, здесь только её отражение (lct-23).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Role } from "@/shared/types/generated";

// Vite подставляет значение при сборке. В обычном стенде демо-вход и его
// элементы интерфейса отсутствуют, даже если backend поддерживает endpoint.
export const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === "true";

export interface Principal {
  login: string;
  full_name: string;
  role: Role;
  service: string | null;
  trainee_id: string | null;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) throw new Error(String(response.status));
  return response.json() as Promise<T>;
}

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    // Отсутствие входа — не ошибка, а состояние: возвращаем null, чтобы
    // экран показал форму, а не «что-то пошло не так».
    queryFn: async () => {
      try {
        return await call<Principal>("/api/auth/me");
      } catch {
        return null;
      }
    },
    retry: false,
  });
}

export function useLogin() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (body: { login: string; password: string }) =>
      call<Principal>("/api/auth/login", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (who) => cache.setQueryData(["me"], who),
  });
}

export function useDemoLogin() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: () => {
      if (!DEMO_MODE) throw new Error("demo_disabled");
      return call<Principal>("/api/auth/dev-token", { method: "POST" });
    },
    onSuccess: (who) => cache.setQueryData(["me"], who),
  });
}

export function useLogout() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: () => call<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
    onSuccess: () => cache.setQueryData(["me"], null),
  });
}

/** Какие экраны открыты роли. Повторяет domain/roles.py: список короткий
 *  и меняется редко, а держать его на сервере ради двух строк — лишний запрос. */
export const SCREENS: Record<Role, string[]> = {
  admin: ["/admin", "/profile", "/groups"],
  instructor: ["/instructor", "/wall", "/profile", "/dds", "/groups"],
  trainee: ["/trainee", "/dds", "/profile"],
};
