// Наблюдатель: внешний монитор и пульт преподавателя.
//
// Снимок при подключении обязателен — монитор в классе включают посреди занятия,
// и он должен показать текущее состояние, а не ждать событий. Отправлять в этот
// канал нечего и нельзя: тип `Out` — `never` (docs/arch/CONTRACT.md).

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { type CardState, applyState, empty as emptyCard } from "@/features/kio-card/merge";
import { useSessionReport } from "@/shared/api/http";
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
  paused: boolean;
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
  const [paused, setPaused] = useState(false);
  // Сессия, для которой отчёт уже стоит просить. Привязка к id, а не флаг:
  // при переключении истории старый флаг не должен дёрнуть запрос новой сессии.
  const [reportFor, setReportFor] = useState<string | null>(null);
  // Ошибку отчёта наблюдение не показывает: пока оценки нет, это не ошибка.
  const { report } = useSessionReport(sessionId, reportFor === sessionId);
  const queries = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setStatus(sessionId ? "connecting" : "closed");
    setTitle(null);
    setMode(undefined);
    setTrainee(null);
    setCard({ server: { ...emptyCard.server }, pending: { ...emptyCard.pending } });
    setRequiredFields([]);
    setTranscript([]);
    setTimers([]);
    setHintsUsed(0);
    setNotes(new Map());
    setEnded(false);
    setPaused(false);
    setError(null);
    if (!sessionId) return;
    let active = true;
    // Active monitors used to query this on every open. Almost all such requests
    // could only return score_not_ready, competing with the snapshot under load.
    // Ask for the report only after completion or when no live session exists
    // (e.g. history opened after a backend restart).
    const loadReport = () => setReportFor(sessionId);
    const channel = observeChannel(sessionId, {
      onStatus: (next) => { if (active) setStatus(next); },
      onEvent: (event) => {
        if (!active) return;
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
            setPaused(event.paused ?? false);
            if (event.ended) loadReport();
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
            loadReport();
            break;
          case "session.paused":
            setPaused(event.paused);
            break;
          case "error":
            setError(event.message);
            if (event.code === "session_not_found") loadReport();
            break;
          case "score.ready":
            loadReport();
            // После session.ended hook мог исчерпать повторы 409 до готовности оценки.
            void queries.invalidateQueries({ queryKey: ["session-report", sessionId] });
            break;
        }
      },
    }).connect();
    return () => { active = false; channel.close(); };
  }, [sessionId, queries]);

  return { status, title, mode, trainee, card, requiredFields, transcript, timers, hintsUsed, notes, ended, paused, report, error };
}
