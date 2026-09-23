// HTTP-клиент и хуки. Типы ответов HTTP в generated.ts пока не попадают —
// там только события WS; здесь описаны руками по docs/arch/CONTRACT.md#http-api.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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
  sessions_restored?: number;
  offline: boolean;
  demo_no_db?: boolean;
}

export interface ScenarioSummary {
  id: string;
  title: string;
  type: string;
  level: Level;
  topics: string[];
  modes: SessionMode[];
  signs: string[];
  incident_code: string | null;
  dds: string | null;
  ticket: number | null;
  position: number | null;
  ekp_group: number | null;
  can_manage: boolean;
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
  service: string | null;
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
  recording_available: boolean;
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

/** Значения следующего признака происшествия при уже выбранных (ЕКП). */
export interface SignsPage {
  level: number;
  values: string[];
  incident: { code: string; type: string; notify: string[] } | null;
}

export interface IncidentGroupsPage {
  version: string;
  groups: { id: number; title: string }[];
}

export const useIncidentGroups = () => useQuery({
  queryKey: ["ekp-groups"], staleTime: Infinity,
  queryFn: () => request<IncidentGroupsPage>("/api/ekp/groups"),
});

export function useSigns(prefix: string[], group: number | null = null) {
  const params = new URLSearchParams();
  if (group !== null) params.set("group", String(group));
  for (const sign of prefix) params.append("prefix", sign);
  const query = params.toString();
  return useQuery({
    queryKey: ["ekp-signs", group, prefix],
    // Справочник не меняется между занятиями — перезапрашивать незачем.
    staleTime: Infinity,
    queryFn: () => request<SignsPage>(`/api/ekp/signs${query ? `?${query}` : ""}`),
  });
}

export const useTrainees = (enabled = true) =>
  useQuery({
    queryKey: ["trainees"],
    queryFn: () => request<TraineeInfo[]>("/api/trainees"),
    enabled,
  });

export interface GroupInfo {
  id: string;
  name: string;
}

export interface GroupAnalytics {
  group: GroupInfo;
  enrolled_trainees: number;
  active_trainees: number;
  scored_attempts: number;
  average_score: number | null;
  errors: {
    code: string;
    title: string;
    affected_trainees: number;
    occurrences: number;
    rate_percent: number;
    recommendation: string;
  }[];
}

export interface GroupInsight {
  summary: string;
  priorities: string[];
  source: "local_qwen";
  personal_data_sent: false;
}

export const useGroups = (enabled = true) =>
  useQuery({
    queryKey: ["groups"],
    queryFn: () => request<GroupInfo[]>("/api/groups"),
    enabled,
  });

export function useCreateGroup() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => request<GroupInfo>("/api/groups", {
      method: "POST", body: JSON.stringify({ name }),
    }),
    onSuccess: () => void cache.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useAssignTrainee() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, traineeId }: { groupId: string; traineeId: string }) =>
      request<GroupInfo>(`/api/groups/${groupId}/trainees/${traineeId}`, { method: "PUT" }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["trainees"] });
      void cache.invalidateQueries({ queryKey: ["group-analytics"] });
    },
  });
}

export const useGroupAnalytics = (groupId: string | null) =>
  useQuery({
    queryKey: ["group-analytics", groupId],
    queryFn: () => request<GroupAnalytics>(`/api/groups/${groupId}/analytics`),
    enabled: Boolean(groupId),
    refetchInterval: 15_000,
  });

export function useGroupInsight() {
  return useMutation({
    mutationFn: (groupId: string) => request<GroupInsight>(
      `/api/groups/${groupId}/analytics/insight`, { method: "POST" },
    ),
  });
}

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

export interface LearningMaterial {
  id: string;
  title: string;
  description: string;
  level: Level;
  kind: "text" | "file";
  body: string;
  scenario_id: string | null;
  file_name: string | null;
  media_type: string | null;
  file_size: number;
  file_sha256: string | null;
  active: boolean;
  created_by: string;
  created_at: string;
  assigned_at: string | null;
  completed_at: string | null;
  assignment_count: number;
}

export interface MaterialCreateInput {
  title: string;
  description: string;
  level: Level;
  kind: "text" | "file";
  body: string;
  scenario_id: string | null;
  file_name?: string;
  media_type?: string;
  content_base64?: string;
}

export const useMaterials = (includeArchived = false) => useQuery({
  queryKey: ["materials", includeArchived],
  queryFn: () => request<LearningMaterial[]>(
    `/api/materials${includeArchived ? "?include_archived=true" : ""}`,
  ),
});

function invalidateMaterials(cache: ReturnType<typeof useQueryClient>) {
  return cache.invalidateQueries({ queryKey: ["materials"] });
}

export function useCreateMaterial() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (body: MaterialCreateInput) => request<LearningMaterial>("/api/materials", {
      method: "POST", body: JSON.stringify(body),
    }),
    onSuccess: () => void invalidateMaterials(cache),
  });
}

export function useUpdateMaterial() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<LearningMaterial> }) =>
      request<LearningMaterial>(`/api/materials/${id}`, {
        method: "PATCH", body: JSON.stringify(patch),
      }),
    onSuccess: () => void invalidateMaterials(cache),
  });
}

export function useArchiveMaterial() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<LearningMaterial>(`/api/materials/${id}`, {
      method: "DELETE",
    }),
    onSuccess: () => void invalidateMaterials(cache),
  });
}

export function useAssignMaterial() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: ({ materialId, traineeId }: { materialId: string; traineeId: string }) =>
      request<LearningMaterial>(`/api/materials/${materialId}/assign/${traineeId}`, {
        method: "PUT",
      }),
    onSuccess: () => void invalidateMaterials(cache),
  });
}

export function useAssignMaterialGroup() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: ({ materialId, groupId }: { materialId: string; groupId: string }) =>
      request<{ material_id: string; assigned: number }>(
        `/api/materials/${materialId}/assign-group/${groupId}`, { method: "PUT" },
      ),
    onSuccess: () => void invalidateMaterials(cache),
  });
}

export function useCompleteMaterial() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => request<LearningMaterial>(`/api/materials/${id}/complete`, {
      method: "POST",
    }),
    onSuccess: () => void invalidateMaterials(cache),
  });
}

export function useStartMaterialPractice() {
  return useMutation({
    mutationFn: (id: string) => request<{
      session_id: string;
      scenario_id: string;
      mode: "self";
      exercise: "card";
      path: string;
    }>(`/api/materials/${id}/start`, { method: "POST" }),
  });
}
