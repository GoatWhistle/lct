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
import { type CardState, applyPatch, edit, empty as emptyCard } from "@/features/kio-card/merge";
import type { ChecklistItem } from "@/features/self-assessment/SelfAssessment";
import type {
  CallIncoming,
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
  partial?: boolean;
}

export type CallPhase = "waiting" | "incoming" | "talking" | "ended";

export function useCall(sessionId: string | null) {
  const [status, setStatus] = useState<ChannelStatus>("connecting");
  const [phase, setPhase] = useState<CallPhase>("waiting");
  const [incoming, setIncoming] = useState<CallIncoming | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [timers, setTimers] = useState<TimerSnapshot[]>([]);
  const [callerSpeaking, setCallerSpeaking] = useState(false);
  const [micOn, setMicOn] = useState(false);
  const [card, setCard] = useState<CardState>(emptyCard);
  const [checklist, setChecklist] = useState<ChecklistItem[]>([]);
  const [selfAssessed, setSelfAssessed] = useState(false);
  const [scoreReady, setScoreReady] = useState(false);
  const [report, setReport] = useState<SessionReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const channel = useRef<ReturnType<typeof callChannel> | null>(null);
  const capture = useRef<Capture | null>(null);
  const audio = useRef<{ context: AudioContext; playback: Playback; ambience: Ambience } | null>(null);
  const gate = useRef(new EnergyGate());
  const outbox = useRef<Record<string, unknown>>({});
  const patchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const append = useCallback((line: Line) => {
    setLines((prev) => {
      const withoutPartial = prev.filter((item) => !item.partial);
      return [...withoutPartial, line].slice(-50);
    });
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    const ch = callChannel(sessionId, {
      onStatus: setStatus,
      onBinary: (frame) => audio.current?.playback.enqueue(frame),
      onEvent: (event: ServerToTrainee) => {
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
            append({ speaker: "caller", text: event.text });
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
            // Чек-лист открывается только после звонка: во время него это
            // содержимое подсказок.
            void fetch(`/api/sessions/${sessionId}/checklist`)
              .then((response) => (response.ok ? response.json() : []))
              .then(setChecklist)
              .catch(() => setChecklist([]));
            break;
          case "score.ready":
            setScoreReady(true);
            void fetch(`/api/sessions/${sessionId}/report`)
              .then((response) => (response.ok ? response.json() : null))
              .then(setReport)
              .catch(() => setReport(null));
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

  const dispatch = useCallback((service: DDSCode) => {
    channel.current?.send({ type: "dds.dispatch", service });
  }, []);

  const submitSelfAssessment = useCallback((missed: string[], comment: string) => {
    channel.current?.send({ type: "self_assessment.submit", missed, comment });
    setSelfAssessed(true);
  }, []);

  return {
    status, phase, incoming, lines, timers, callerSpeaking, micOn, error, card,
    checklist, selfAssessed, scoreReady, report,
    answer, hangup, hint, patchKio, dispatch, submitSelfAssessment,
  };
}
