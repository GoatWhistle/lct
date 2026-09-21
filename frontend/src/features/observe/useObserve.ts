// Наблюдатель: внешний монитор и пульт преподавателя.
//
// Снимок при подключении обязателен — монитор в классе включают посреди занятия,
// и он должен показать текущее состояние, а не ждать событий. Отправлять в этот
// канал нечего и нельзя: тип `Out` — `never` (docs/arch/CONTRACT.md).

import { useEffect, useState } from "react";

import { type CardState, applyState, empty as emptyCard } from "@/features/kio-card/merge";
import { observeChannel, type ChannelStatus } from "@/shared/api/ws";
import type {
  SessionMode,
  SessionReport,
  TimerSnapshot,
  TranscriptEntry,
} from "@/shared/types/generated";

export interface ObservedSession {
  status: ChannelStatus;
  title: string | null;
  mode: SessionMode | undefined;
  trainee: string | null;
  card: CardState;
  requiredFields: string[];
  transcript: TranscriptEntry[];
  timers: TimerSnapshot[];
  hintsUsed: number;
  notes: Map<string, string>;
  ended: boolean;
  report: SessionReport | null;
  error: string | null;
}

export function useObserve(sessionId: string | null): ObservedSession {
  const [status, setStatus] = useState<ChannelStatus>("connecting");
  const [title, setTitle] = useState<string | null>(null);
  const [mode, setMode] = useState<SessionMode | undefined>(undefined);
  const [trainee, setTrainee] = useState<string | null>(null);
  const [card, setCard] = useState<CardState>(emptyCard);
  const [requiredFields, setRequiredFields] = useState<string[]>([]);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [timers, setTimers] = useState<TimerSnapshot[]>([]);
  const [hintsUsed, setHintsUsed] = useState(0);
  const [notes, setNotes] = useState<Map<string, string>>(new Map());
  const [ended, setEnded] = useState(false);
  const [report, setReport] = useState<SessionReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    const channel = observeChannel(sessionId, {
      onStatus: setStatus,
      onEvent: (event) => {
        switch (event.type) {
          case "session.snapshot":
            setTitle(event.scenario_title);
            setMode(event.mode);
            setTrainee(event.trainee_name ?? null);
            setCard((prev) => applyState(prev, event.kio as unknown as Record<string, unknown>));
            setRequiredFields(event.required_fields ?? []);
            setTranscript(event.transcript);
            setTimers(event.timers);
            setHintsUsed(event.hints_used ?? 0);
            setEnded(event.ended ?? false);
            break;
          case "kio.state":
            setCard((prev) => applyState(prev, event.kio as unknown as Record<string, unknown>));
            break;
          case "transcript.append":
            setTranscript((prev) => [...prev, event.entry]);
            break;
          case "timer.tick":
            setTimers(event.timers);
            break;
          case "mode.set":
            setMode(event.mode);
            break;
          case "hint.shown":
            setHintsUsed((count) => count + 1);
            break;
          case "instructor_note.shown":
            setNotes((prev) => new Map(prev).set(event.transcript_ref, event.text));
            break;
          case "session.ended":
            setEnded(true);
            break;
          case "error":
            setError(event.message);
            break;
          case "score.ready":
            void fetch(`/api/sessions/${sessionId}/report`)
              .then((response) => (response.ok ? response.json() : null))
              .then(setReport)
              .catch(() => setReport(null));
            break;
        }
      },
    }).connect();
    return () => channel.close();
  }, [sessionId]);

  return { status, title, mode, trainee, card, requiredFields, transcript, timers, hintsUsed, notes, ended, report, error };
}
