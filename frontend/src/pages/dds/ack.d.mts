import type { DdsQueueCard, TimerSnapshot } from "@/shared/types/generated";

export interface AckDeadline {
  elapsedMs: number;
  limitMs: number;
  violated: boolean;
}

export function ackDeadline(timers: TimerSnapshot[], queueCards: DdsQueueCard[]): AckDeadline | null;
