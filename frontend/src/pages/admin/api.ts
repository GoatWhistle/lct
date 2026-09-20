// Запросы АРМ администратора. Типы описаны руками, как и остальной HTTP:
// в generated.ts едут только события сокетов (docs/arch/CONTRACT.md).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Role } from "@/shared/types/generated";

export interface AdminUser {
  id: string;
  login: string;
  full_name: string;
  role: Role;
  service: string | null;
  blocked: boolean;
  created_at: string;
}

export interface ServiceState { name: string; ok: boolean; detail: string }
export interface AuditRow {
  at: string; actor: string; role: string; action: string; object_id: string | null; detail: string;
}
export interface Backup { name: string; size_bytes: number; at: string }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? String(response.status));
  }
  return response.json() as Promise<T>;
}

export const useUsers = () =>
  useQuery({ queryKey: ["admin-users"], queryFn: () => call<AdminUser[]>("/api/admin/users") });

export const useStatus = () =>
  useQuery({
    queryKey: ["admin-status"],
    queryFn: () => call<ServiceState[]>("/api/admin/status"),
    // Состояние стенда смотрят перед занятием и во время: пусть обновляется само.
    refetchInterval: 10_000,
  });

export const useAudit = (action: string) =>
  useQuery({
    queryKey: ["admin-audit", action],
    queryFn: () => call<AuditRow[]>(`/api/admin/audit${action ? `?action=${encodeURIComponent(action)}` : ""}`),
  });

export const useBackups = () =>
  useQuery({ queryKey: ["admin-backups"], queryFn: () => call<Backup[]>("/api/admin/backups") });

export function useCreateUser() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (body: Omit<AdminUser, "id" | "blocked" | "created_at"> & { password: string }) =>
      call<AdminUser>("/api/admin/users", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function usePatchUser() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      call<AdminUser>(`/api/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useCreateBackup() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: () => call<Backup>("/api/admin/backups", { method: "POST" }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["admin-backups"] }),
  });
}
