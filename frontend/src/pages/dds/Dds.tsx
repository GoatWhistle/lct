// АРМ диспетчера ДДС.
//
// Аудио здесь нет вообще: приходит снимок карточки от оператора 112,
// дальше — подтверждение по нормативу, возврат на уточнение и отметки времени.
// Возврат карточки — самая ценная механика цепочки: неполнота КИО становится
// сорванным выездом с конкретной причиной (docs/arch/CONTRACT.md).

import { useEffect, useRef, useState } from "react";

import { GROUPS } from "@/features/kio-card/fields";
import { KioCard } from "@/features/kio-card/KioCard";
import { applyState, empty as emptyCard, type CardState } from "@/features/kio-card/merge";
import { stationChannel, type ChannelStatus } from "@/shared/api/ws";
import type { ServiceStatus, StationSnapshot } from "@/shared/types/generated";
import { SERVICE_STATUS_LABELS, CARD_STATUS_LABELS, ALARMING } from "./statuses";
import { sessionIdFromUrl } from "@/shared/api/session";

const ACK_LIMIT_S = 30; // норматив подтверждения, ПП РФ № 1931 и памятка АРМ-112

const FIELDS = GROUPS.flatMap((group) =>
  group.fields.filter((field) => !field.readOnly).map((field) => ({ path: field.path, label: field.label })),
);

export function Dds() {
  const sessionId = sessionIdFromUrl();
  const role = new URLSearchParams(location.search).get("role") ?? "dds_01";

  const [status, setStatus] = useState<ChannelStatus>("connecting");
  const [card, setCard] = useState<CardState | null>(null);
  const [from, setFrom] = useState<string>("");
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [acked, setAcked] = useState(false);
  const [bounced, setBounced] = useState<string[] | null>(null);
  const [missing, setMissing] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [zone, setZone] = useState<boolean | null>(null);
  const [marks, setMarks] = useState<string[]>([]);
  const [station, setStation] = useState<StationSnapshot | null>(null);
  const channel = useRef<ReturnType<typeof stationChannel> | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    const ch = stationChannel(sessionId, role, {
      onStatus: setStatus,
      onEvent: (event) => {
        if (event.type === "station.state") setStation(event.snapshot);
        if (event.type === "error") mark(`отклонено: ${event.message}`);
        if (event.type === "card.received") {
          setCard(applyState(emptyCard, event.card as unknown as Record<string, unknown>));
          setFrom(event.from_operator);
          setReceivedAt(Date.now());
        }
      },
    }).connect();
    channel.current = ch;
    return () => ch.close();
  }, [sessionId, role]);

  // Обратный отсчёт норматива: пока карточка не подтверждена, счёт идёт.
  useEffect(() => {
    if (receivedAt === null || acked) return;
    const timer = setInterval(() => setElapsed((Date.now() - receivedAt) / 1000), 200);
    return () => clearInterval(timer);
  }, [receivedAt, acked]);

  if (!sessionId) {
    return (
      <main className="page">
        <h1>АРМ диспетчера ДДС</h1>
        <p className="warn">Откройте ссылку вида /dds?session=…&role=dds_01</p>
      </main>
    );
  }

  const mark = (label: string) => setMarks((prev) => [`${new Date().toLocaleTimeString("ru-RU")} — ${label}`, ...prev]);

  return (
    <main className="page">
      <h1>АРМ диспетчера ДДС · {role}</h1>
      <table className="grid">
        <tbody>
          <tr><th>Канал</th><td className={status === "open" ? "" : "warn"}>{status}</td></tr>
          <tr><th>Карточка</th><td>{card ? `от оператора ${from}` : "ожидание карточки от 112"}</td></tr>
          {card && !acked && (
            <tr>
              <th>Подтверждение</th>
              <td className={elapsed > ACK_LIMIT_S ? "state-violated" : "state-ok"}>
                {elapsed.toFixed(1)} с из {ACK_LIMIT_S} с
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {card && (
        <>
          <p>
            <button type="button" disabled={acked} onClick={() => {
              channel.current?.send({ type: "card.ack" });
              setAcked(true);
              mark("карточка принята");
            }}>Принять карточку</button>{" "}
            <button type="button" disabled={Boolean(bounced)} onClick={() => {
              if (!missing.length) return;
              channel.current?.send({ type: "card.bounce", missing_fields: missing, comment });
              setBounced(missing);
              mark(`возврат на уточнение: ${missing.join(", ")}`);
            }}>Вернуть на уточнение</button>
          </p>

          {!bounced && (
            <details>
              <summary>Чего не хватает для выезда</summary>
              <div className="bounce-fields">
                {FIELDS.map((field) => (
                  <label key={field.path}>
                    <input type="checkbox" checked={missing.includes(field.path)}
                      onChange={() => setMissing((prev) =>
                        prev.includes(field.path) ? prev.filter((item) => item !== field.path) : [...prev, field.path])} />{" "}
                    {field.label}
                  </label>
                ))}
              </div>
              <p>
                <input type="text" value={comment} placeholder="куда ехать без этажа"
                  onChange={(event) => setComment(event.target.value)} />
              </p>
            </details>
          )}

          <p>
            Зона ответственности:{" "}
            <button type="button" disabled={zone !== null} onClick={() => {
              channel.current?.send({ type: "zone.decision", in_zone: true });
              setZone(true); mark("наша зона");
            }}>наша</button>{" "}
            <button type="button" disabled={zone !== null} onClick={() => {
              channel.current?.send({ type: "zone.decision", in_zone: false });
              setZone(false); mark("не наша зона");
            }}>не наша</button>
          </p>
          <p>
            <button type="button" onClick={() => {
              channel.current?.send({ type: "crew.dispatched", at: new Date().toISOString() });
              mark("приказ на выезд");
            }}>Приказ на выезд</button>{" "}
            <button type="button" onClick={() => {
              channel.current?.send({ type: "crew.arrived", at: new Date().toISOString() });
              mark("прибытие на место");
            }}>Прибытие</button>
          </p>

          {station && station.services.length > 0 && (
            <>
              <h2>Статусы реагирования</h2>
              <p>
                Статус карточки:{" "}
                <b className={ALARMING.includes(station.card) ? "state-violated" : "state-ok"}>
                  {CARD_STATUS_LABELS[station.card]}
                </b>
              </p>
              <table className="grid">
                <thead>
                  <tr><th>Служба</th><th>Статус</th><th>Отметить</th></tr>
                </thead>
                <tbody>
                  {station.services.map((service) => (
                    <StatusRow
                      key={service}
                      service={service}
                      current={station.statuses[service]}
                      available={station.available[service] ?? []}
                      onSet={(status, comment) => {
                        channel.current?.send({ type: "card.status", service, status, comment });
                        mark(`${service}: ${SERVICE_STATUS_LABELS[status]}`);
                      }}
                    />
                  ))}
                </tbody>
              </table>
            </>
          )}

          <h2>Карточка от оператора 112</h2>
          {/* Снимок: после передачи он не меняется. */}
          <KioCard state={card} readOnly />
        </>
      )}

      {station && station.log.length > 0 && (
        <>
          <h2>Журнал отметок</h2>
          <table className="grid">
            <tbody>
              {station.log.map((entry, index) => (
                <tr key={index}>
                  <th>{entry.service}</th>
                  <td>
                    {SERVICE_STATUS_LABELS[entry.status]}
                    {entry.comment && <> — {entry.comment}</>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {marks.length > 0 && (
        <>
          <h2>Отметки времени</h2>
          <table className="grid">
            <tbody>{marks.map((line, index) => <tr key={index}><td>{line}</td></tr>)}</tbody>
          </table>
        </>
      )}
    </main>
  );
}

/** Одна служба списка оповещения.
 *
 * Доступные статусы приходят с сервера: автомат переходов живёт там, и
 * дублировать его на фронте нельзя — разъедется. Поле комментария появляется
 * у отказов, потому что без комментария сервер такую отметку не примет. */
function StatusRow({
  service, current, available, onSet,
}: {
  service: string;
  current: ServiceStatus;
  available: ServiceStatus[];
  onSet: (status: ServiceStatus, comment: string) => void;
}) {
  const [chosen, setChosen] = useState<ServiceStatus | "">("");
  const [comment, setComment] = useState("");
  const needsComment = chosen === "declined" || chosen === "refused";

  if (available.length === 0) {
    return (
      <tr>
        <th>{service}</th>
        <td>{SERVICE_STATUS_LABELS[current]}</td>
        <td>карточка закрыта для редактирования</td>
      </tr>
    );
  }
  return (
    <tr>
      <th>{service}</th>
      <td>{SERVICE_STATUS_LABELS[current]}</td>
      <td>
        <select value={chosen} onChange={(event) => setChosen(event.target.value as ServiceStatus)}>
          <option value="">—</option>
          {available.map((status) => (
            <option key={status} value={status}>{SERVICE_STATUS_LABELS[status]}</option>
          ))}
        </select>{" "}
        {needsComment && (
          <input
            type="text"
            value={comment}
            placeholder="причина и куда передана информация"
            onChange={(event) => setComment(event.target.value)}
          />
        )}{" "}
        <button
          type="button"
          disabled={!chosen || (needsComment && !comment.trim())}
          onClick={() => {
            if (!chosen) return;
            onSet(chosen, comment);
            setChosen("");
            setComment("");
          }}
        >
          Поставить
        </button>
      </td>
    </tr>
  );
}
