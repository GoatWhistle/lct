// Учебное АРМ ДДС. Звонок бригаде имитируется внутри приложения, без SIP.
import { useEffect, useRef, useState } from "react";

import { GROUPS } from "@/features/kio-card/fields";
import { KioCard } from "@/features/kio-card/KioCard";
import { applyState, empty as emptyCard, readPath, type CardState } from "@/features/kio-card/merge";
import { sessionIdFromUrl } from "@/shared/api/session";
import { stationChannel, type ChannelStatus } from "@/shared/api/ws";
import type { ServiceStatus, SessionReport, StationSnapshot, StationToServer } from "@/shared/types/generated";
import { ALARMING, CARD_STATUS_LABELS, SERVICE_STATUS_LABELS } from "./statuses";

const ACK_LIMIT_S = 30;
type ReportPhase = "dispatched" | "arrived" | "working" | "completed";
interface PhoneReport { service: string; crew: string; phase: ReportPhase; text: string; at: string }
interface DdsSnapshot extends StationSnapshot {
  crew_options?: string[];
  crew_selected?: string | null;
  phone_reports?: PhoneReport[];
}
type DdsCommand = { type: "crew.select"; crew: string } | { type: "phone.dial" } | { type: "station.finish" };
const REPORT_LABELS: Record<ReportPhase, string> = {
  dispatched: "Доклад о выезде", arrived: "Доклад о прибытии",
  working: "Доклад о ходе работ", completed: "Доклад о завершении работ",
};
const FIELDS = GROUPS.flatMap((group) =>
  group.fields.filter((item) => !item.readOnly).map((item) => ({ path: item.path, label: item.label })),
);

function field(card: CardState, path: string): string {
  const value = readPath(card.server, path);
  return value === null || value === undefined || value === "" ? "—" : String(value);
}
function time(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString("ru-RU");
}

export function Dds() {
  const sessionId = sessionIdFromUrl();
  const role = new URLSearchParams(location.search).get("role") ?? "dds_01";
  const [status, setStatus] = useState<ChannelStatus>("connecting");
  const [card, setCard] = useState<CardState | null>(null);
  const [from, setFrom] = useState("");
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [station, setStation] = useState<DdsSnapshot | null>(null);
  const [lastReport, setLastReport] = useState<PhoneReport | null>(null);
  const [dialing, setDialing] = useState(false);
  const [error, setError] = useState("");
  const [ended, setEnded] = useState(false);
  const [report, setReport] = useState<SessionReport | null>(null);
  const [reportState, setReportState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [reportError, setReportError] = useState("");
  const [missing, setMissing] = useState<string[]>([]);
  const [bounceComment, setBounceComment] = useState("");
  const [bounced, setBounced] = useState(false);
  const [zone, setZone] = useState<boolean | null>(null);
  const channel = useRef<ReturnType<typeof stationChannel> | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    async function loadReport() {
      setReportState("loading");
      setReportError("");
      try {
        const response = await fetch(`/api/sessions/${sessionId}/report`, {
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (!response.ok) {
          if (response.status === 403) throw new Error("Отчёт недоступен: это занятие другого обучающегося.");
          if (response.status === 409) throw new Error("Оценка ещё не готова. Попробуйте получить разбор чуть позже.");
          throw new Error(`Не удалось получить отчёт (HTTP ${response.status}).`);
        }
        setReport(await response.json() as SessionReport);
        setReportState("ready");
      } catch (cause) {
        if (controller.signal.aborted) return;
        setReportState("error");
        setReportError(cause instanceof Error ? cause.message : "Не удалось получить отчёт.");
      }
    }
    const ch = stationChannel(sessionId, role, {
      onStatus: (next) => { setStatus(next); if (next !== "open") setDialing(false); },
      onEvent: (event) => {
        if (event.type === "station.state") { setStation(event.snapshot as DdsSnapshot); setError(""); }
        else if (event.type === "card.received") {
          setCard(applyState(emptyCard, event.card as unknown as Record<string, unknown>));
          setFrom(event.from_operator);
          // Норматив идёт с передачи карточки сервером, в том числе если
          // диспетчер открыл АРМ уже после начала занятия.
          const transmittedAt = new Date(event.at).getTime();
          setReceivedAt(Number.isNaN(transmittedAt) ? Date.now() : transmittedAt);
          setEnded(false);
        } else if (event.type === "error") { setError(event.message); setDialing(false); }
        else if (event.type === "session.ended") { setEnded(true); setDialing(false); }
        else if (event.type === "score.ready") { void loadReport(); }
        else if ((event as { type: string }).type === "phone.report") {
          setLastReport(event as unknown as PhoneReport);
          setDialing(false);
          setError("");
        }
      },
    }).connect();
    channel.current = ch;
    return () => { controller.abort(); ch.close(); channel.current = null; };
  }, [sessionId, role]);

  const primaryRecorded = Boolean(station?.services.length && station.services.every((service) =>
    ["accepted", "declined", "responding", "arrived", "working", "completed", "refused"]
      .includes(station.statuses[service])));
  useEffect(() => {
    if (receivedAt === null || primaryRecorded) return;
    const timer = setInterval(() => setElapsed((Date.now() - receivedAt) / 1000), 200);
    return () => clearInterval(timer);
  }, [receivedAt, primaryRecorded]);

  const send = (command: DdsCommand): boolean => channel.current?.send(command as StationToServer) ?? false;
  if (!sessionId) return <main className="page"><h1>АРМ диспетчера ДДС</h1>
    <p className="warn">Откройте ссылку занятия вида /dds?session=…&role=dds_01</p></main>;

  const reports = station?.phone_reports ?? [];
  const selectedCrew = station?.crew_selected ?? "";
  const latest = lastReport ?? reports.at(-1) ?? null;
  const isConnected = status === "open";
  const crewAccepted = Boolean(station?.services.some((service) =>
    selectedCrew.startsWith(`${service} —`) &&
    ["accepted", "responding", "arrived", "working"].includes(station.statuses[service])));

  return <main className="page dds-workspace">
    <header className="dds-header"><div><div className="dds-eyebrow">Учебный контур · АРМ системы-112</div>
      <h1>Дежурно-диспетчерская служба <span>{role}</span></h1></div>
      <div className={`dds-connection ${isConnected ? "state-ok" : "state-warn"}`}>
        {isConnected ? "● Канал подключён" : `● ${status}`}</div></header>
    {error && <div className="dds-alert" role="alert">Действие не принято: {error}</div>}
    {ended && <div className="dds-alert dds-done">Занятие завершено. Оценка появится после расчёта.</div>}
    {(ended || reportState !== "idle") && <section className="dds-panel" aria-live="polite">
      <div className="dds-panel-title"><h2>Итог упражнения</h2></div>
      {report ? <div style={{ padding: 12 }}>
        <p><strong>Итоговая оценка: {report.score_final.toFixed(0)} из 100</strong>
          {report.score_final !== report.score_auto && ` · автооценка ${report.score_auto.toFixed(0)}`}</p>
        <p className="ref">Скачать отчёт: <a href={`/api/sessions/${sessionId}/report.csv`}>CSV</a>
          {" · "}<a href={`/api/sessions/${sessionId}/report.pdf`}>PDF</a></p>
        <h3>Не выполнены нормативы</h3>
        <table className="dds-registry"><tbody>
          {report.metrics.filter((metric) => !metric.passed).map((metric) => <tr key={metric.key}>
            <th>{metric.title}</th><td>{metric.fact} · норматив: {metric.norm}</td></tr>)}
          {report.metrics.every((metric) => metric.passed) && <tr><td>Все проверенные нормативы выполнены.</td></tr>}
        </tbody></table>
        <h3>Замечания</h3>
        <table className="dds-registry"><tbody>
          {report.findings.map((finding, index) => <tr key={`${finding.code}-${index}`}>
            <th>{finding.code}</th><td>{finding.summary}<div className="ref">{finding.fact}{finding.norm && ` · ${finding.norm}`}</div></td></tr>)}
          {report.findings.length === 0 && <tr><td>Замечаний нет.</td></tr>}
        </tbody></table>
      </div> : <div style={{ padding: 12 }}>
        {reportState === "loading" ? "Подготавливаем разбор…" : reportError || "Ожидаем итоговую оценку…"}
        {reportState === "error" && <button type="button" onClick={() => {
          setReportState("loading");
          void fetch(`/api/sessions/${sessionId}/report`, { credentials: "same-origin" }).then(async (response) => {
            if (response.status === 403) throw new Error("Отчёт недоступен: это занятие другого обучающегося.");
            if (response.status === 409) throw new Error("Оценка ещё не готова. Попробуйте получить разбор чуть позже.");
            if (!response.ok) throw new Error(`Не удалось получить отчёт (HTTP ${response.status}).`);
            setReport(await response.json() as SessionReport);
            setReportState("ready");
          }).catch((cause: unknown) => {
            setReportError(cause instanceof Error ? cause.message : "Не удалось получить отчёт.");
            setReportState("error");
          });
        }}>Повторить запрос</button>}
      </div>}
    </section>}

    <section className="dds-panel"><div className="dds-panel-title"><h2>Реестр обращений</h2><span>Входящие карточки</span></div>
      <div className="dds-table-wrap"><table className="dds-registry"><thead><tr>
        <th>№ карточки</th><th>Время</th><th>Адрес происшествия</th><th>Тип</th><th>Службы</th><th>Статус</th><th>Решение ДДС</th>
      </tr></thead><tbody>{card ? <tr className="dds-selected">
        <td>{sessionId.slice(0, 8).toUpperCase()}</td><td>{time(field(card, "registered_at"))}</td>
        <td>{field(card, "address")}</td><td>{field(card, "incident_type")}</td>
        <td>{station?.services.join(", ") || field(card, "notify")}</td>
        <td>{station ? CARD_STATUS_LABELS[station.card] : "Получена"}</td>
        <td>{station?.services.map((service) => `${service}: ${SERVICE_STATUS_LABELS[station.statuses[service]]}`).join("; ") || "ожидается"}</td>
      </tr> : <tr><td colSpan={7} className="dds-empty">Ожидание готовой карточки от преподавателя или оператора 112…</td></tr>}</tbody></table></div>
    </section>

    {card && <div className="dds-main"><div className="dds-left">
      <section className="dds-panel"><div className="dds-panel-title"><h2>Карточка обращения</h2>
        <span>Источник: {from || "112"}</span></div>
        <div className="dds-summary">
          <div><small>Адрес</small><strong>{field(card, "address")}</strong></div>
          <div><small>Заявитель</small><strong>{field(card, "caller_name")}</strong></div>
          <div><small>Телефон</small><strong>{field(card, "caller_contact")}</strong></div>
          <div><small>Происшествие</small><strong>{field(card, "description")}</strong></div>
        </div><details className="dds-details"><summary>Полная карточка информационного обмена</summary><KioCard state={card} readOnly /></details>
      </section>
      <section className="dds-panel"><div className="dds-panel-title"><h2>Реагирование служб</h2>
        <span className={station && ALARMING.includes(station.card) ? "state-violated" : "state-ok"}>
          {station ? CARD_STATUS_LABELS[station.card] : "—"}</span></div>
        {!primaryRecorded && receivedAt !== null && <div className={`dds-deadline ${elapsed > ACK_LIMIT_S ? "state-violated" : "state-ok"}`}>
          Первичное решение «Принята» / «Не принята»: {elapsed.toFixed(0)} из {ACK_LIMIT_S} секунд</div>}
        {station && station.services.length > 0 ? <div className="dds-table-wrap"><table className="dds-registry dds-status-table">
          <thead><tr><th>Служба</th><th>Текущий статус</th><th>Следующее действие</th></tr></thead><tbody>
            {station.services.map((service) => <StatusRow key={service} service={service}
              current={station.statuses[service]} available={station.available[service] ?? []}
              disabled={!isConnected || ended} reports={reports.filter((report) => report.service === service)}
              phoneExercise={from === "учебный сценарий"}
              onSet={(next, comment) => channel.current?.send({ type: "card.status", service, status: next, comment })} />)}
          </tbody></table></div> : <p className="dds-empty">Ожидаем состав служб по карточке.</p>}
        <p className="dds-hint">Сначала примите карточку или укажите причину отказа. Последующие статусы в занятии ДДС подтверждаются докладами по телефону.</p>
      </section>
      {station && station.log.length > 0 && <section className="dds-panel"><div className="dds-panel-title"><h2>Журнал реагирования</h2></div>
        <div className="dds-table-wrap"><table className="dds-registry"><thead><tr><th>Время</th><th>Служба</th><th>Статус</th><th>Комментарий</th></tr></thead>
          <tbody>{station.log.map((entry, index) => <tr key={`${entry.at}-${index}`}><td>{time(entry.at)}</td><td>{entry.service}</td>
            <td>{SERVICE_STATUS_LABELS[entry.status]}</td><td>{entry.comment || "—"}</td></tr>)}</tbody></table></div></section>}
    </div><aside className="dds-right">
      <section className="dds-panel dds-phone"><div className="dds-panel-title"><h2>IP-телефон</h2><span>Учебная имитация</span></div>
        <p className="dds-hint">Выберите бригаду вручную, свяжитесь со старшим группы и внесите его доклад в карточку.</p>
        <label htmlFor="dds-crew">Дежурная бригада</label>
        <select id="dds-crew" value={selectedCrew} disabled={!isConnected || ended || !station?.crew_options?.length}
          onChange={(event) => send({ type: "crew.select", crew: event.target.value })}>
          <option value="">Выберите бригаду</option>
          {station?.crew_options?.map((crew) => <option key={crew} value={crew}>{crew}</option>)}
        </select>
        <button type="button" className="dds-dial" disabled={!crewAccepted || !isConnected || ended || dialing}
          onClick={() => { if (send({ type: "phone.dial" })) setDialing(true); }}>
          ☎ {dialing ? "Соединение…" : "Позвонить старшему группы"}</button>
        {selectedCrew && !crewAccepted && <p className="dds-hint state-warn">Перед звонком поставьте статус «Принята» для службы этой бригады.</p>}
        {latest ? <div className="dds-report" aria-live="polite"><small>{REPORT_LABELS[latest.phase]} · {time(latest.at)}</small>
          <strong>{latest.crew}</strong><p>{latest.text}</p>
          <span>Прослушайте доклад и поставьте соответствующий статус реагирования.</span></div>
          : <div className="dds-phone-idle">Доклады поступят после исходящего учебного звонка.</div>}
        {reports.length > 0 && <details className="dds-details"><summary>История докладов ({reports.length})</summary>
          <ol className="dds-reports">{reports.map((report, index) => <li key={`${report.at}-${index}`}><b>{REPORT_LABELS[report.phase]}</b> · {time(report.at)}<br />{report.text}</li>)}</ol>
        </details>}
      </section>
      <section className="dds-panel dds-secondary"><div className="dds-panel-title"><h2>Дополнительные действия</h2></div>
        <div className="dds-zone">Зона ответственности: <button type="button" disabled={zone !== null || !isConnected || ended}
          onClick={() => { channel.current?.send({ type: "zone.decision", in_zone: true }); setZone(true); }}>Наша</button>
          <button type="button" disabled={zone !== null || !isConnected || ended}
            onClick={() => { channel.current?.send({ type: "zone.decision", in_zone: false }); setZone(false); }}>Не наша</button></div>
        <details className="dds-details"><summary>Вернуть карточку на уточнение</summary>
          <div className="bounce-fields">{FIELDS.map((item) => <label key={item.path}><input type="checkbox" checked={missing.includes(item.path)}
            onChange={() => setMissing((previous) => previous.includes(item.path) ? previous.filter((path) => path !== item.path) : [...previous, item.path])} /> {item.label}</label>)}</div>
          <input type="text" value={bounceComment} placeholder="Что необходимо уточнить" onChange={(event) => setBounceComment(event.target.value)} />
          <button type="button" disabled={!missing.length || bounced || !isConnected || ended} onClick={() => {
            channel.current?.send({ type: "card.bounce", missing_fields: missing, comment: bounceComment }); setBounced(true);
          }}>Вернуть оператору</button>
        </details>
        <button type="button" className="dds-finish" disabled={!isConnected || ended} onClick={() => send({ type: "station.finish" })}>Завершить упражнение ДДС</button>
      </section>
    </aside></div>}
  </main>;
}

function StatusRow({ service, current, available, disabled, reports, phoneExercise, onSet }: {
  service: string; current: ServiceStatus; available: ServiceStatus[]; disabled: boolean;
  reports: PhoneReport[]; phoneExercise: boolean; onSet: (status: ServiceStatus, comment: string) => void;
}) {
  const [chosen, setChosen] = useState<ServiceStatus | "">("");
  const [comment, setComment] = useState("");
  const needsComment = chosen === "declined" || chosen === "refused";
  const reportNeeded: Partial<Record<ServiceStatus, ReportPhase>> = {
    responding: "dispatched", arrived: "arrived", working: "working", completed: "completed",
  };
  const matchingReport = !chosen || !phoneExercise || !reportNeeded[chosen] ||
    reports.some((report) => report.phase === reportNeeded[chosen]);
  return <tr><td>{service}</td><td><strong>{SERVICE_STATUS_LABELS[current]}</strong></td><td>
    {available.length ? <div className="dds-status-action">
      <select value={chosen} disabled={disabled} aria-label={`Следующий статус ${service}`}
        onChange={(event) => setChosen(event.target.value as ServiceStatus)}>
        <option value="">Выберите статус</option>
        {available.map((next) => <option key={next} value={next}>{SERVICE_STATUS_LABELS[next]}</option>)}
      </select>
      {needsComment && <input type="text" value={comment} placeholder="Причина отказа и кому передана информация"
        onChange={(event) => setComment(event.target.value)} />}
      {chosen && !matchingReport && <span className="state-warn">Сначала получите доклад по телефону.</span>}
      <button type="button" disabled={disabled || !chosen || (needsComment && !comment.trim()) || !matchingReport}
        onClick={() => { if (chosen) { onSet(chosen, comment.trim()); setChosen(""); setComment(""); } }}>Сохранить статус</button>
    </div> : "Карточка закрыта для редактирования"}</td></tr>;
}
