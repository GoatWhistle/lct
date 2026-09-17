// Проверка сквозного пути звука до сервера — веха карточки lct-08.
// Полный АРМ курсанта собирается поверх этого в lct-09 и lct-10.

import { useEffect, useRef, useState } from "react";

import { type Capture, startCapture } from "@/shared/audio/capture";
import { type ChannelStatus, callChannel } from "@/shared/api/ws";
import { sessionIdFromUrl } from "@/shared/api/session";
import type { ServerToTrainee } from "@/shared/types/generated";

const STATUS_LABEL: Record<ChannelStatus, string> = {
  connecting: "подключение",
  open: "на связи",
  reconnecting: "связь потеряна, переподключение",
  closed: "отключено",
};

export function MicCheck() {
  const sessionId = sessionIdFromUrl();
  const [status, setStatus] = useState<ChannelStatus>("connecting");
  const [events, setEvents] = useState<ServerToTrainee[]>([]);
  const [framesSent, setFramesSent] = useState(0);
  const [micRate, setMicRate] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const channel = useRef<ReturnType<typeof callChannel> | null>(null);
  const capture = useRef<Capture | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    const ch = callChannel(sessionId, {
      onStatus: setStatus,
      onEvent: (event) => setEvents((prev) => [event, ...prev].slice(0, 12)),
    }).connect();
    channel.current = ch;
    return () => {
      ch.close();
      void capture.current?.stop();
    };
  }, [sessionId]);

  async function toggleMic() {
    if (capture.current) {
      await capture.current.stop();
      capture.current = null;
      setMicRate(null);
      return;
    }
    try {
      setError(null);
      capture.current = await startCapture((frame) => {
        if (channel.current?.sendBinary(frame)) setFramesSent((n) => n + 1);
      });
      setMicRate(capture.current.sampleRate);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (!sessionId) {
    return (
      <main className="page">
        <h1>АРМ курсанта</h1>
        <p className="warn">В адресе нет номера занятия: откройте ссылку вида /trainee?session=…</p>
      </main>
    );
  }

  return (
    <main className="page">
      <h1>АРМ курсанта — проверка звука</h1>
      <table className="grid">
        <tbody>
          <tr><th>Занятие</th><td><code>{sessionId}</code></td></tr>
          <tr><th>Канал</th><td className={status === "open" ? "" : "warn"}>{STATUS_LABEL[status]}</td></tr>
          <tr><th>Микрофон</th><td>{micRate ? `включён, ${micRate} Гц → 16000 Гц` : "выключен"}</td></tr>
          <tr><th>Кадров отправлено</th><td>{framesSent} ({(framesSent * 0.02).toFixed(1)} с звука)</td></tr>
        </tbody>
      </table>
      <p>
        <button type="button" onClick={toggleMic} disabled={status !== "open"}>
          {micRate ? "Выключить микрофон" : "Включить микрофон"}
        </button>
      </p>
      {error && <p className="violated">Ошибка микрофона: {error}</p>}
      <h2>События сервера</h2>
      <table className="grid">
        <tbody>
          {events.map((event, index) => (
            <tr key={index}><th>{event.type}</th><td><code>{JSON.stringify(event).slice(0, 140)}</code></td></tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
