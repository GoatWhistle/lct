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

export const useSessions = (params: { trainee?: string; group?: string; mode?: SessionMode } = {}) => {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, value]) => value) as [string, string][],
  ).toString();
  return useQuery({
    queryKey: ["sessions", query],
    queryFn: () => request<SessionInfo[]>(`/api/sessions${query ? `?${query}` : ""}`),
    refetchInterval: 15_000,
  });
};

export interface TraineeInfo {
  id: string;
  name: string;
  group: string | null;
}

export interface Attempt {
  session_id: string;
  scenario_id: string;
  mode: SessionMode;
  attempt: number;
  created_at: string;
  score: number | null;
  interview_ms: number | null;
  facts_got: number | null;
  facts_required: number | null;
  hints: number | null;
  codes: Record<string, number>;
}

export interface Profile {
  trainee: TraineeInfo;
  attempts: Attempt[];
  competencies: Record<string, number>;
  deltas: {
    scenario_id: string;
    from_attempt: number;
    to_attempt: number;
    score: number | null;
    interview_ms: number | null;
    facts_got: number | null;
  }[];
  hints_total: number;
}

export const useTrainees = () =>
  useQuery({ queryKey: ["trainees"], queryFn: () => request<TraineeInfo[]>("/api/trainees") });

export const useProfile = (traineeId: string | null) =>
  useQuery({
    queryKey: ["profile", traineeId],
    queryFn: () => request<Profile>(`/api/trainees/${traineeId}/profile`),
    enabled: Boolean(traineeId),
  });

export const useCreateSession = () =>
  useMutation({
    mutationFn: (body: { scenario_id: string; mode: SessionMode; trainee?: string; group?: string }) =>
      request<SessionInfo>("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  });
