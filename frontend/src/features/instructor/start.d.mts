import type { LessonCriteria, SessionMode } from "@/shared/types/generated";

export interface ScenarioStartOptions {
  scenarioId: string;
  mode: SessionMode;
  trainee: string;
  exercise?: "dds" | "call" | "card";
  groupId?: string;
  traineeId?: string;
  ddsService?: string;
  scenarioIds?: string[];
  randomScenarioIds?: string[];
  ddsArrivalIntervalSeconds?: number;
  ddsMaxWaiting?: number;
  handoffToDds?: boolean;
  criteria?: LessonCriteria;
}

export interface ScenarioStartMessage {
  type: "scenario.start";
  scenario_id: string;
  mode: SessionMode;
  trainee: string;
  group_id: string | null;
  trainee_id: string | null;
  dds_service: string | null;
  exercise: "dds" | "call" | "card";
  handoff_to_dds: boolean;
  scenario_ids: string[] | undefined;
  random_scenario_ids: string[] | undefined;
  dds_arrival_interval_seconds: number;
  dds_max_waiting: number;
  criteria: LessonCriteria | undefined;
}

export function scenarioStartMessage(options: ScenarioStartOptions): ScenarioStartMessage;
