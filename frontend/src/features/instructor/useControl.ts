// Канал преподавателя: только передача.
//
// Преподаватель смотрит через `observe`, а пишет через `control`, и это разные
// сокеты. В `control` нет ни одной команды, меняющей карточку курсанта: он
// управляет ситуацией, а не работой обучаемого, иначе оценка перестаёт быть
// оценкой курсанта (docs/arch/CONTRACT.md).

import { useCallback, useEffect, useRef, useState } from "react";

import { controlChannel, type ChannelStatus } from "@/shared/api/ws";
import type { SessionMode } from "@/shared/types/generated";

export function useControl(sessionId: string | null) {
  const [status, setStatus] = useState<ChannelStatus>("closed");
  const channel = useRef<ReturnType<typeof controlChannel> | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    const ch = controlChannel(sessionId, { onStatus: setStatus }).connect();
    channel.current = ch;
    return () => {
      ch.close();
      channel.current = null;
    };
  }, [sessionId]);

  const start = useCallback(
    (scenarioId: string, mode: SessionMode, trainee: string, group?: string) =>
      channel.current?.send({
        type: "scenario.start",
        scenario_id: scenarioId,
        mode,
        trainee,
        group_id: group || null,
      }),
    [],
  );

  const note = useCallback(
    (transcriptRef: string, text: string) =>
      channel.current?.send({ type: "instructor_note.add", transcript_ref: transcriptRef, text }),
    [],
  );

  const stop = useCallback(() => channel.current?.send({ type: "session.stop" }), []);
  const playReference = useCallback(() => channel.current?.send({ type: "reference.play" }), []);

  return { status, start, note, stop, playReference };
}
