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
    setReport(null);
    setError(null);
    if (!sessionId) return;
    let active = true;
    // Active monitors used to query this on every open. Almost all such requests
    // could only return score_not_ready, competing with the snapshot under load.
    // Ask for the report only after completion or when no live session exists
    // (e.g. history opened after a backend restart).
    let reportLoading = false;
    let reportLoaded = false;
    const loadReport = () => {
      if (reportLoading || reportLoaded) return;
      reportLoading = true;
      void fetch(`/api/sessions/${sessionId}/report`, { credentials: "same-origin" })
        .then((response) => (response.ok ? response.json() : null))
        .then((saved) => {
          if (!active) return;
          if (saved) {
            reportLoaded = true;
            setReport(saved as SessionReport);
          }
        })
        .catch(() => undefined)
        .finally(() => { reportLoading = false; });
    };
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
          case "error":
            setError(event.message);
            if (event.code === "session_not_found") loadReport();
            break;
          case "score.ready":
            loadReport();
            break;
        }
      },
    }).connect();
    return () => { active = false; channel.close(); };
  }, [sessionId]);

  return { status, title, mode, trainee, card, requiredFields, transcript, timers, hintsUsed, notes, ended, report, error };
}
