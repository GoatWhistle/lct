// Запросы АРМ администратора. Типы описаны руками, как и остальной HTTP:
// в generated.ts едут только события сокетов (docs/arch/CONTRACT.md).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Role } from "@/shared/types/generated";

export interface AdminUser {
  id: string;
  login: string;
  full_name: string;
  role: Role;
  auth_provider: "local" | "ldap";
  service: string | null;
  blocked: boolean;
  created_at: string;
}

export interface ServiceState { name: string; ok: boolean; detail: string }
export interface AuditRow {
  at: string; actor: string; role: string; action: string; object_id: string | null; detail: string;
}
export interface Backup { name: string; size_bytes: number; at: string }
export interface RuntimeMetrics {
  at: string;
  uptime_seconds: number;
  cpu_percent: number;
  load_1m_percent: number | null;
  cpu_cores: number;
  rss_bytes: number | null;
  memory_total_bytes: number | null;
  memory_available_bytes: number | null;
  disk_total_bytes: number;
  disk_free_bytes: number;
  threads: number;
  active_sessions: number;
  restored_sessions: number;
  completed_sessions_24h: number;
}
export interface DiagnosticEvent { at: string; level: string; source: string; message: string }
export interface FailureEvent {
  at: string; actor: string; action: string; object_id: string | null; detail: string;
}
export interface DiagnosticReport {
  generated_at: string;
  metrics: RuntimeMetrics;
  recent_system_events: DiagnosticEvent[];
  failed_actions_24h: FailureEvent[];
}

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

export const useDiagnostics = () =>
  useQuery({
    queryKey: ["admin-diagnostics"],
    queryFn: () => call<DiagnosticReport>("/api/admin/diagnostics"),
    refetchInterval: 10_000,
  });

export const useAudit = (action: string, actor: string, offset: number) =>
  useQuery({
    queryKey: ["admin-audit", action, actor, offset],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "201", offset: String(offset) });
      if (action) params.set("action", action);
      if (actor) params.set("actor", actor);
      return call<AuditRow[]>(`/api/admin/audit?${params.toString()}`);
    },
  });

export const useBackups = () =>
  useQuery({ queryKey: ["admin-backups"], queryFn: () => call<Backup[]>("/api/admin/backups") });

export function useCreateUser() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (body: Omit<AdminUser, "id" | "blocked" | "created_at" | "auth_provider"> & { password: string }) =>
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
