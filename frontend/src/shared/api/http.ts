// HTTP-клиент и хуки. Типы ответов HTTP в generated.ts пока не попадают —
// там только события WS; здесь описаны руками по docs/arch/CONTRACT.md#http-api.

import { useMutation, useQuery } from "@tanstack/react-query";

import type { Level, SessionMode } from "@/shared/types/generated";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(`${status} ${code}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

export interface Health {
  status: string;
  models_ready: boolean;
  embeddings_ready: boolean;
  scenarios_loaded: number;
  offline: boolean;
}

export interface ScenarioSummary {
  id: string;
  title: string;
  type: string;
  level: Level;
  topics: string[];
  modes: SessionMode[];
  dds: string | null;
}

export interface SessionInfo {
  session_id: string;
  scenario_id: string;
  mode: SessionMode;
  attempt: number;
  trainee_id: string | null;
  group_id: string | null;
}

export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: () => request<Health>("/api/health"), refetchInterval: 5_000 });

export const useScenarios = () =>
  useQuery({ queryKey: ["scenarios"], queryFn: () => request<ScenarioSummary[]>("/api/scenarios") });

export const useCreateSession = () =>
  useMutation({
    mutationFn: (body: { scenario_id: string; mode: SessionMode; trainee?: string; group?: string }) =>
      request<SessionInfo>("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  });
