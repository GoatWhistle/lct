// Таймеры ГОСТ Р 22.7.03-2021.
//
// Три состояния — единственное место, где цвет несёт смысл (docs/arch/FRONTEND.md).
// `interview` (75 с) — центральная метрика продукта: она крупная и видна курсанту,
// преподавателю и на внешнем мониторе одновременно (docs/arch/CONTRACT.md).

import type { TimerSnapshot } from "@/shared/types/generated";

import { clock } from "./clock";

const TITLES: Record<string, string> = {
  answer: "Ответ на звонок",
  interview: "Опрос",
  dds_notify: "Оповещение ДДС",
  dds_ack: "Подтверждение ДДС",
  zone_check: "Зона ответственности",
  callback: "Обратный дозвон",
  close: "Снятие с контроля",
};

/** Крупный индикатор опроса. `big` — для внешнего монитора: читается с последнего ряда. */
export function InterviewTimer({ timers, big = false }: { timers: TimerSnapshot[]; big?: boolean }) {
  const interview = timers.find((timer) => timer.code === "interview");
  if (!interview) return null;
  return (
    <div className={`interview state-${interview.state}${big ? " interview-big" : ""}`}>
      <span className="interview-label">Опрос</span>
      <span className="interview-value">{clock(interview.elapsed_ms)}</span>
      <span className="interview-limit">норматив {clock(interview.limit_ms)}</span>
    </div>
  );
}

export function Timers({ timers }: { timers: TimerSnapshot[] }) {
  if (timers.length === 0) return null;
  return (
    <table className="grid">
      <tbody>
        {timers.map((timer) => (
          <tr key={timer.code}>
            <th>
              {TITLES[timer.code] ?? timer.code}
              {(timer.attempt ?? 1) > 1 && ` · попытка ${timer.attempt}`}
            </th>
            <td className={`state-${timer.state}`}>
              {clock(timer.elapsed_ms)} из {clock(timer.limit_ms)}
              {timer.stopped && " · остановлен"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
