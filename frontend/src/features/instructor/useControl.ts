// Канал преподавателя: только передача.
//
// Преподаватель смотрит через `observe`, а пишет через `control`, и это разные
// сокеты. В `control` нет ни одной команды, меняющей карточку курсанта: он
// управляет ситуацией, а не работой обучаемого, иначе оценка перестаёт быть
// оценкой курсанта (docs/arch/CONTRACT.md).

import { useCallback, useEffect, useRef, useState } from "react";

import { controlChannel, type ChannelStatus } from "@/shared/api/ws";
import type { LessonCriteria, SessionMode } from "@/shared/types/generated";

export function useControl(sessionId: string | null) {
  const [status, setStatus] = useState<ChannelStatus>("closed");
  const [openSessionId, setOpenSessionId] = useState<string | null>(null);
  const channel = useRef<{ sessionId: string; socket: ReturnType<typeof controlChannel> } | null>(null);

  useEffect(() => {
    channel.current = null;
    setOpenSessionId(null);
    if (!sessionId) {
      setStatus("closed");
      return;
    }
    let active = true;
    setStatus("connecting");
    const ch = controlChannel(sessionId, { onStatus: (next) => {
      if (!active) return;
      setStatus(next);
      setOpenSessionId(next === "open" ? sessionId : null);
    } }).connect();
    channel.current = { sessionId, socket: ch };
    return () => {
      active = false;
      ch.close();
      if (channel.current?.socket === ch) channel.current = null;
    };
  }, [sessionId]);

  const start = useCallback(
    (targetSessionId: string, scenarioId: string, mode: SessionMode, trainee: string, exercise: "dds" | "call" | "card" = "dds", group?: string, traineeId?: string, ddsService?: string, scenarioIds?: string[], ddsArrivalIntervalSeconds = 60, ddsMaxWaiting = 3, handoffToDds = false, criteria?: LessonCriteria, randomScenarioIds?: string[]) => {
      const message = {
        type: "scenario.start",
        scenario_id: scenarioId,
        mode,
        trainee,
        group_id: group || null,
        trainee_id: traineeId || null,
        dds_service: ddsService || null,
        exercise,
        handoff_to_dds: handoffToDds,
        scenario_ids: scenarioIds?.length ? scenarioIds : undefined,
        random_scenario_ids: randomScenarioIds?.length ? randomScenarioIds : undefined,
        dds_arrival_interval_seconds: ddsArrivalIntervalSeconds,
        dds_max_waiting: ddsMaxWaiting,
        criteria,
      } as const;
      const current = channel.current;
      if (!current || current.sessionId !== targetSessionId) return false;
      return current.socket.send(message);
    },
    [],
  );

  const note = useCallback(
    (transcriptRef: string, text: string) =>
      channel.current?.socket.send({ type: "instructor_note.add", transcript_ref: transcriptRef, text }),
    [],
  );

  const inject = useCallback(
    (directive: string, immediate: boolean) =>
      channel.current?.socket.send({
        type: "director.inject",
        directive,
        // `immediate` рвёт звук на полуслове и годится только для обрыва связи:
        // разговор не должен дёргаться от того, что преподаватель что-то нажал.
        mode: immediate ? "immediate" : "next_turn",
      }),
    [],
  );

  const stop = useCallback(() => channel.current?.socket.send({ type: "session.stop" }), []);
  const playReference = useCallback(() => channel.current?.socket.send({ type: "reference.play" }), []);

  return { status, openSessionId, start, note, inject, stop, playReference };
}
