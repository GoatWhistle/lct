// Звонок глазами курсанта: канал, микрофон, голос звонящего, перебивание.
//
// Фронт не хранит состояние сессии как истину: всё, что здесь лежит, — это то,
// что прислал сервер (docs/arch/CONTRACT.md).

import { useCallback, useEffect, useRef, useState } from "react";

import { type ChannelStatus, callChannel } from "@/shared/api/ws";
import { Ambience } from "@/shared/audio/ambience";
import { type Capture, startCapture } from "@/shared/audio/capture";
import { EnergyGate } from "@/shared/audio/levels";
import { CALLER_RATE, Playback } from "@/shared/audio/playback";
import { type CardState, applyPatch, applyState, edit, empty as emptyCard } from "@/features/kio-card/merge";
import type { ChecklistItem } from "@/features/self-assessment/SelfAssessment";
import type {
  Outcome,
  CallIncoming,
  CardBriefing,
  DDSCode,
  ServerToTrainee,
  SessionReport,
  TimerSnapshot,
} from "@/shared/types/generated";

/** Правки копятся и уходят одной дельтой: 300 мс тишины — и отправка. */
const PATCH_DEBOUNCE_MS = 300;

export interface Line {
  speaker: "caller" | "operator";
  text: string;
  source?: "local_llm" | "scenario";
  partial?: boolean;
}

export type CallPhase = "waiting" | "incoming" | "talking" | "ended";

export function useCall(sessionId: string | null) {
  const [status, setStatus] = useState<ChannelStatus>("connecting");
  const [phase, setPhase] = useState<CallPhase>("waiting");
  const [incoming, setIncoming] = useState<CallIncoming | null>(null);
  const [briefing, setBriefing] = useState<CardBriefing | null>(null);
  const [cardSubmitted, setCardSubmitted] = useState(false);
  const [lines, setLines] = useState<Line[]>([]);
  const [timers, setTimers] = useState<TimerSnapshot[]>([]);
  const [callerSpeaking, setCallerSpeaking] = useState(false);
  const [micOn, setMicOn] = useState(false);
  const [card, setCard] = useState<CardState>(emptyCard);
  const [checklist, setChecklist] = useState<ChecklistItem[]>([]);
  const [selfAssessed, setSelfAssessed] = useState(false);
  const [scoreReady, setScoreReady] = useState(false);
  const [report, setReport] = useState<SessionReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const channel = useRef<ReturnType<typeof callChannel> | null>(null);
  const capture = useRef<Capture | null>(null);
  const audio = useRef<{ context: AudioContext; playback: Playback; ambience: Ambience } | null>(null);
  const gate = useRef(new EnergyGate());
  const outbox = useRef<Record<string, unknown>>({});
  const patchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const textMode = useRef(false);

  const append = useCallback((line: Line) => {
    setLines((prev) => {
      const withoutPartial = prev.filter((item) => !item.partial);
      return [...withoutPartial, line].slice(-50);
    });
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    const ch = callChannel(sessionId, {
      onStatus: (next) => {
        setStatus(next);
        if (next === "open") {
          // Браузер не подтверждает доставку WebSocket-кадра. Всё, что ещё
          // отмечено pending и не пришло эхом от сервера, повторяем после
          // reconnect: kio.patch идемпотентен и не создаёт двойных действий.
          queueMicrotask(() => setCard((current) => {
            if (Object.keys(current.pending).length) {
              channel.current?.send({ type: "kio.patch", fields: current.pending });
            }
            return current;
          }));
        }
      },
      onBinary: (frame) => audio.current?.playback.enqueue(frame),
      onEvent: (event: ServerToTrainee) => {
        if (event.type === "text.turn.accepted") {
          append({ speaker: "operator", text: event.text });
          return;
        }
        if (event.type === "card.briefing") {
          const briefing = event;
          textMode.current = true;
          setBriefing(briefing);
          setIncoming(null);
          setCard(applyState(emptyCard, { ...briefing.card }));
          setCardSubmitted(false);
          setError(null);
          return;
        }
        switch (event.type) {
          case "call.incoming":
            setIncoming(event);
            setPhase("incoming");
            break;
          case "call.started":
            setPhase("talking");
            break;
          case "stt.partial":
            if (event.text) append({ speaker: "operator", text: event.text, partial: true });
            break;
          case "stt.final":
            append({ speaker: "operator", text: event.text });
            break;
          case "caller.utterance":
            append({ speaker: "caller", text: event.text, source: event.source });
            break;
          case "tts.begin":
            setCallerSpeaking(true);
            break;
          case "tts.end":
            setCallerSpeaking(false);
            break;
          case "tts.cancel":
            // Сервер подтвердил перебивание: выбросить всё, что не доиграло.
            audio.current?.playback.flush();
            setCallerSpeaking(false);
            break;
          case "bg.start":
            void audio.current?.ambience.start(event.loop, event.gain_db);
            break;
          case "bg.stop":
            audio.current?.ambience.stop();
            break;
          case "kio.patch":
            setCard((prev) => applyPatch(prev, event.fields, event.source));
            break;
          case "timer.tick":
            setTimers(event.timers);
            break;
          case "call.ended":
            setPhase("ended");
            audio.current?.ambience.stop();
            if (textMode.current) { setCardSubmitted(true); break; }
            // Чек-лист открывается только после звонка: во время него это
            // содержимое подсказок.
            void fetch(`/api/sessions/${sessionId}/checklist`)
              .then((response) => (response.ok ? response.json() : []))
              .then(setChecklist)
              .catch(() => setChecklist([]));
            break;
          case "score.ready":
            setScoreReady(true);
            setReportError(null);
            void fetch(`/api/sessions/${sessionId}/report`)
              .then((response) => {
                if (response.status === 403) throw new Error("Отчёт недоступен: занятие привязано к другому обучающемуся.");
                if (response.status === 409) throw new Error("Оценка ещё не готова. Обновите страницу позже.");
                if (!response.ok) throw new Error(`Не удалось загрузить отчёт (HTTP ${response.status}).`);
                return response.json();
              })
              .then(setReport)
              .catch((cause: unknown) => setReportError(cause instanceof Error ? cause.message : "Не удалось загрузить отчёт."));
            break;
          case "error":
            setError(event.message);
            break;
        }
      },
    }).connect();
    channel.current = ch;
    return () => {
      ch.close();
      void capture.current?.stop();
      audio.current?.ambience.stop();
      void audio.current?.context.close();
      audio.current = null;
    };
  }, [sessionId, append]);

  const answer = useCallback(async () => {
    try {
      setError(null);
      // Контекст создаётся по нажатию: браузер не даёт играть звук без действия человека.
      const context = new AudioContext({ sampleRate: CALLER_RATE });
      audio.current = { context, playback: new Playback(context), ambience: new Ambience(context) };

      capture.current = await startCapture((frame) => {
        const playback = audio.current?.playback;
        // Перебивание: фронт гасит звук мгновенно, сервер подтвердит `tts.cancel`.
        if (gate.current.push(frame) && playback?.speaking) {
          playback.flush();
          setCallerSpeaking(false);
        }
        channel.current?.sendBinary(frame);
      });
      setMicOn(true);
      channel.current?.send({ type: "call.answer" });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const hangup = useCallback(() => {
    channel.current?.send({ type: "call.hangup" });
    void capture.current?.stop();
    capture.current = null;
    setMicOn(false);
  }, []);

  const hint = useCallback(() => channel.current?.send({ type: "hint.request" }), []);

  const patchKio = useCallback((path: string, value: unknown) => {
    setCard((prev) => edit(prev, path, value));
    outbox.current[path] = value;
    if (patchTimer.current) clearTimeout(patchTimer.current);
    patchTimer.current = setTimeout(() => {
      const fields = outbox.current;
      outbox.current = {};
      if (Object.keys(fields).length) channel.current?.send({ type: "kio.patch", fields });
    }, PATCH_DEBOUNCE_MS);
  }, []);

  const dispatch = useCallback((service?: DDSCode) => {
    channel.current?.send(service ? { type: "dds.dispatch", service } : { type: "dds.dispatch" });
  }, []);

  const sendText = useCallback((text: string) => {
    const clean = text.trim();
    if (!clean || clean.length > 1000) return false;
    return Boolean(channel.current?.send({ type: "text.turn", text: clean }));
  }, []);

  const submitCard = useCallback(() => {
    if (patchTimer.current) clearTimeout(patchTimer.current);
    patchTimer.current = null;
    const fields = outbox.current;
    outbox.current = {};
    if (Object.keys(fields).length && !channel.current?.send({ type: "kio.patch", fields })) {
      setError("Канал отключён: карточка не отправлена.");
      return;
    }
    if (channel.current?.send({ type: "card.submit" })) {
      setCardSubmitted(true);
      setError(null);
    } else {
      setError("Канал отключён: карточка не отправлена.");
    }
  }, []);

  /** Закрыть вызов не карточкой: справкой или передачей в другой регион.
   *  Отдельное действие, а не «положил трубку»: система должна отличить
   *  осознанное решение от брошенного вызова (docs/spec/TICKETS.md). */
  const resolve = useCallback((outcome: Outcome, comment: string) => {
    channel.current?.send({ type: "call.resolve", outcome, comment });
  }, []);

  const submitSelfAssessment = useCallback((missed: string[], comment: string) => {
    channel.current?.send({ type: "self_assessment.submit", missed, comment });
    setSelfAssessed(true);
  }, []);

  return {
    resolve,
    status, phase, incoming, briefing, cardSubmitted, lines, timers, callerSpeaking, micOn, error, card,
    checklist, selfAssessed, scoreReady, report, reportError,
    answer, hangup, hint, patchKio, dispatch, submitCard, submitSelfAssessment, sendText,
  };
}
