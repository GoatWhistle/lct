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
interface CompletedCard { card_id: string; scenario_id: string; score_auto: number }
interface DdsSnapshot extends StationSnapshot {
  crew_options?: string[];
  crew_selected?: string | null;
  phone_reports?: PhoneReport[];
  card_id?: string | null;
  card_index?: number;
  card_total?: number;
  reply_text?: string;
  completed_cards?: CompletedCard[];
}
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

function serviceTitle(service: string): string {
  return service.startsWith("Служба ") ? service : `Служба ${service}`;
}

export function Dds() {
  const sessionId = sessionIdFromUrl();
  const role = new URLSearchParams(location.search).get("role") ?? "dds_01";
  const [status, setStatus] = useState<ChannelStatus>("connecting");
  const [view, setView] = useState<"registry" | "card">("registry");
  const [pane, setPane] = useState<"status" | "phone" | "reply" | "full" | "journal" | "more" | null>(null);
  const [activeService, setActiveService] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [clockNow, setClockNow] = useState(() => new Date());
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
  const [reply, setReply] = useState({ text: "", dirty: false });
  const [advancePending, setAdvancePending] = useState(false);
  const channel = useRef<ReturnType<typeof stationChannel> | null>(null);
  const activeCardId = useRef<string | null>(null);

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
        if (event.type === "station.state") {
          const snapshot = event.snapshot as DdsSnapshot;
          setStation(snapshot);
          setActiveService((previous) => previous && snapshot.services.includes(previous)
            ? previous : snapshot.services[0] ?? null);
          setReply((previous) => {
            const saved = snapshot.reply_text ?? "";
            if (previous.text === saved) return { text: saved, dirty: false };
            return previous.dirty ? previous : { text: saved, dirty: false };
          });
          setError("");
        }
        else if (event.type === "card.received") {
          const nextId = String(event.card.card_id);
          if (activeCardId.current !== nextId) {
            activeCardId.current = nextId;
            setStation(null);
            setReply({ text: "", dirty: false });
            setLastReport(null);
            setDialing(false);
            setMissing([]);
            setBounceComment("");
            setBounced(false);
            setZone(null);
            setReport(null);
            setReportState("idle");
            setView("registry");
            setPane(null);
            setActiveService(null);
          }
          setCard(applyState(emptyCard, { ...event.card }));
          setFrom(event.from_operator);
          // Норматив идёт с передачи карточки сервером, в том числе если
          // диспетчер открыл АРМ уже после начала занятия.
          const transmittedAt = new Date(event.at).getTime();
          setReceivedAt(Number.isNaN(transmittedAt) ? Date.now() : transmittedAt);
          setEnded(false);
          setAdvancePending(false);
        } else if (event.type === "error") { setError(event.message); setDialing(false); setAdvancePending(false); }
        else if (event.type === "session.ended") { setEnded(true); setDialing(false); setAdvancePending(false); setPane(null); setView("registry"); }
        else if (event.type === "score.ready") { void loadReport(); }
        else if (event.type === "phone.report") {
          setLastReport(event);
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

  useEffect(() => {
    const timer = setInterval(() => setClockNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  const send = (command: StationToServer): boolean => channel.current?.send(command) ?? false;
  if (!sessionId) return <main className="page"><h1>АРМ диспетчера ДДС</h1>
    <p className="warn">Откройте ссылку занятия вида /dds?session=…&role=dds_01</p></main>;

  const reports = station?.phone_reports ?? [];
  const selectedCrew = station?.crew_selected ?? "";
  const latest = lastReport ?? reports.at(-1) ?? null;
  const isConnected = status === "open";
  const cardId = card ? String(readPath(card.server, "card_id") ?? "") : "";
  const cardIndex = station?.card_index ?? 1;
  const cardTotal = station?.card_total ?? 1;
  const completedCards = station?.completed_cards ?? [];
  const activeIsCompleted = completedCards.some((item) => item.card_id === cardId);
  const query = search.trim().toLocaleLowerCase("ru-RU");
  const visibleCompletedCards = completedCards.filter((item) =>
    !query || `${item.card_id} ${item.scenario_id}`.toLocaleLowerCase("ru-RU").includes(query));
  const showActive = card && !activeIsCompleted && (!query ||
    [cardId, field(card, "address"), field(card, "description"), field(card, "incident_type")]
      .some((value) => value.toLocaleLowerCase("ru-RU").includes(query)));
  const crewAccepted = Boolean(station?.services.some((service) =>
    selectedCrew.startsWith(`${service} —`) &&
    ["accepted", "responding", "arrived", "working"].includes(station.statuses[service])));

  return <main className="page dds-workspace">
    <header className="dds-app-top">
      <span className="dds-brand">112</span>
      <span className="dds-app-name">ГБУ Система 112 · учебное АРМ ДДС</span>
      <span className="dds-top-role">{role}</span>
      <span className={`dds-connection ${isConnected ? "state-ok" : "state-warn"}`}>
        {isConnected ? "● На связи" : `● ${status}`}</span>
    </header>
    {error && <div className="dds-alert" role="alert">Действие не принято: {error}</div>}
    {ended && <div className="dds-alert dds-done">Занятие завершено. Оценка появится после расчёта.</div>}
    {(ended || reportState !== "idle") && <section className="dds-panel" aria-live="polite">
      <div className="dds-panel-title"><h2>Итог упражнения</h2></div>
      {report ? <div style={{ padding: 12 }}>
        <p><strong>Итоговая оценка: {report.score_final.toFixed(0)} из 100</strong>
          {report.score_final !== report.score_auto && ` · автооценка ${report.score_auto.toFixed(0)}`}</p>
        <p className="ref">Скачать отчёт: <a href={`/api/sessions/${sessionId}/report.csv`}>CSV</a>
          {" · "}<a href={`/api/sessions/${sessionId}/report.pdf`}>PDF</a></p>
        {Boolean(report.card_results?.length) && <>
          <h3>Результаты по карточкам</h3>
          {report.card_results?.map((item, index) => <details className="dds-details" key={item.card_id}>
            <summary>Карточка {index + 1} · {item.scenario_id} · {item.score_auto.toFixed(0)} / 100</summary>
            <p><b>Ответ диспетчера:</b> {item.reply_text || "не внесён"}</p>
            {item.metrics.filter((metric) => !metric.passed).map((metric) =>
              <p key={metric.key} className="state-violated">{metric.title}: {metric.fact} · норматив {metric.norm}</p>)}
            {item.findings.map((finding, findingIndex) =>
              <p key={`${finding.code}-${findingIndex}`}>{finding.code}: {finding.summary}</p>)}
          </details>)}
        </>}
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

    {view === "registry" && <>
    <section className="dds-search">
      <div className="dds-search-main">
        <h1>Поиск происшествий</h1>
        <div className="dds-search-line"><label htmlFor="dds-search-input">Поиск по номеру, адресу или описанию</label>
          <input id="dds-search-input" type="search" value={search} onChange={(event) => setSearch(event.target.value)}
            placeholder="Введите номер или адрес" />
          <button type="button" onClick={() => setSearch("")}>сбросить</button></div>
      </div>
      <div className="dds-clock"><small>{clockNow.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</small>
        <strong>{clockNow.toLocaleTimeString("ru-RU")}</strong><span>Учебный контур</span></div>
    </section>
    <section className="dds-panel dds-register-panel"><div className="dds-panel-title"><h2>Список происшествий⌃</h2>
      <span>Карточка {cardIndex} из {cardTotal} · обработано {completedCards.length}</span></div>
      <div className="dds-table-wrap"><table className="dds-registry"><thead><tr>
        <th>Связи</th><th>ЧС</th><th>Опер.</th><th>АРМ</th><th>Номер</th><th>Дата</th><th>Время</th><th>Тип происшествия</th><th>Постр.</th><th>Адрес</th><th>Статус службы</th>
      </tr></thead><tbody>
        {visibleCompletedCards.map((item) => <tr key={item.card_id}>
          <td data-label="Связи">▾</td><td data-label="ЧС">—</td><td data-label="Опер.">0</td><td data-label="АРМ">ДДС</td>
          <td data-label="Номер">{item.card_id.slice(0, 8).toUpperCase()}</td><td data-label="Дата">—</td><td data-label="Время">—</td>
          <td data-label="Тип происшествия">Сценарий {item.scenario_id}</td><td data-label="Постр.">—</td><td data-label="Адрес">—</td>
          <td data-label="Статус службы">Отработана · {item.score_auto.toFixed(0)} / 100</td>
        </tr>)}
        {showActive && card && <tr className="dds-selected" aria-current="true">
          <td data-label="Связи"><button type="button" className="dds-row-open" onClick={() => setView("card")} aria-label="Открыть карточку">▾</button></td>
          <td data-label="ЧС">{field(card, "is_emergency") === "true" ? "Да" : "Нет"}</td>
          <td data-label="Опер.">0</td><td data-label="АРМ">ДДС</td>
          <td data-label="Номер"><button type="button" className="dds-row-link" onClick={() => setView("card")}>{cardId.slice(0, 8).toUpperCase()}</button></td>
          <td data-label="Дата">{receivedAt ? new Date(receivedAt).toLocaleDateString("ru-RU") : "—"}</td>
          <td data-label="Время">{receivedAt ? new Date(receivedAt).toLocaleTimeString("ru-RU") : "—"}</td>
          <td data-label="Тип происшествия">{field(card, "incident_code")} · {field(card, "incident_type")}
            <small>Описание: {field(card, "description")}</small></td>
          <td data-label="Постр.">{field(card, "victims_count")}</td>
          <td data-label="Адрес">{field(card, "address")}</td>
          <td data-label="Статус службы">{station?.services.map((service) => `${service}: ${SERVICE_STATUS_LABELS[station.statuses[service]]}`).join("; ") || "Добавлена"}</td>
        </tr>}
        {!showActive && !visibleCompletedCards.length && <tr><td colSpan={11} className="dds-empty">
          {query ? "По запросу ничего не найдено." : "Ожидание готовой карточки от преподавателя или оператора 112…"}</td></tr>}
      </tbody></table></div>
      {cardTotal > cardIndex && <p className="dds-hint">Остальные {cardTotal - cardIndex} карточки поступят после завершения текущей.</p>}
    </section>
    </>}

    {card && view === "card" && <div className="dds-case-layout">
      <div className="dds-case-top"><button type="button" onClick={() => { setView("registry"); setPane(null); }}>← Список происшествий</button>
        <span>Происшествие {cardId.slice(0, 8).toUpperCase()} · карточка {cardIndex} из {cardTotal}</span></div>
      <div className="dds-call-strip">
        <div className="dds-call-off"><span className="dds-call-icon">☎</span><strong>Отключение</strong><small>записи звонков · список SMS</small></div>
        <div><span className="dds-call-icon">☎</span><small>АОН</small><strong>{field(card, "caller_number")}</strong></div>
        <div><span className="dds-call-icon">☎</span><small>предоставленный</small><strong>{field(card, "caller_contact")}</strong></div>
        <div><span className="dds-call-icon">☎</span><small>телефон на место</small><strong>{field(card, "caller_contact")}</strong></div>
        <div className="dds-call-incident"><strong>Происшествие {cardId.slice(0, 8).toUpperCase()}</strong>
          <small>Сохр. {receivedAt ? new Date(receivedAt).toLocaleString("ru-RU") : "—"}</small>
          <small>Опер., АРМ ДДС</small></div>
        <div className="dds-case-top-buttons"><button type="button" onClick={() => setPane("full")}>просмотр</button>
          <button type="button" onClick={() => setPane("more")}>дополнение</button></div>
      </div>
      <div className="dds-case-meta"><span>ФИО заявителя: <b>{field(card, "caller_name")}</b></span>
        <span>Пострадавшие: {field(card, "victims_count")} &nbsp; ЧС: {field(card, "is_emergency") === "true" ? "да" : "нет"}</span>
        <span>✎</span></div>
      <div className="dds-case-summary">
        <div className="dds-case-address"><strong>{field(card, "address")}</strong><span>{field(card, "street")} {field(card, "building")}</span>
          <div><b>{receivedAt ? new Date(receivedAt).toLocaleString("ru-RU") : "—"}</b> &nbsp; {field(card, "description")}</div></div>
        <div className="dds-case-classification"><strong>Происшествие {field(card, "incident_code")}</strong>
          <div><b>{field(card, "description")}</b></div>
          <div>Класс: <b>{field(card, "incident_type")}</b></div>
          <div>Список оповещения: <b>{station?.services.join(", ") || field(card, "notify")}</b></div></div>
      </div>
      <div className="dds-case-stage">
      {pane && <div className={`dds-case-drawer dds-case-drawer-${pane}`}>
      <header className="dds-drawer-title"><strong>{({ status: "Статусы службы", phone: "IP-телефон", reply: "Ответ диспетчера",
        full: "Карточка информационного обмена", journal: "Журнал реагирования", more: "Дополнение" })[pane]}</strong>
        <button type="button" onClick={() => setPane(null)} aria-label="Закрыть панель">×</button></header>
      <div className="dds-main"><div className="dds-left">
      {pane === "full" && <section className="dds-panel"><div className="dds-panel-title"><h2>Карточка обращения</h2>
        <span>Источник: {from || "112"}</span></div>
        <div className="dds-summary">
          <div><small>Адрес</small><strong>{field(card, "address")}</strong></div>
          <div><small>Заявитель</small><strong>{field(card, "caller_name")}</strong></div>
          <div><small>Телефон</small><strong>{field(card, "caller_contact")}</strong></div>
          <div><small>Происшествие</small><strong>{field(card, "description")}</strong></div>
        </div><details className="dds-details"><summary>Полная карточка информационного обмена</summary><KioCard state={card} readOnly /></details>
      </section>}
      {pane === "status" && <section className="dds-panel"><div className="dds-panel-title"><h2>Реагирование служб</h2>
        <span className={station && ALARMING.includes(station.card) ? "state-violated" : "state-ok"}>
          {station ? CARD_STATUS_LABELS[station.card] : "—"}</span></div>
        {!primaryRecorded && receivedAt !== null && <div className={`dds-deadline ${elapsed > ACK_LIMIT_S ? "state-violated" : "state-ok"}`}>
          Первичное решение «Принята» / «Не принята»: {elapsed.toFixed(0)} из {ACK_LIMIT_S} секунд</div>}
        {station && station.services.length > 0 ? <>
          <div className="dds-status-history"><strong>Служба {activeService ?? station.services[0]}</strong>
            <button type="button" className="dds-status-close" onClick={() => setPane(null)} aria-label="Закрыть статусы">×</button>
            {station.log.filter((entry) => entry.service === (activeService ?? station.services[0])).map((entry, index) =>
              <div key={`${entry.at}-${index}`}><span>› {time(entry.at)} {SERVICE_STATUS_LABELS[entry.status]}</span>
                <span>{entry.comment || "—"}</span></div>)}
            {!station.log.some((entry) => entry.service === (activeService ?? station.services[0])) &&
              <div><span>› Карточка получена службой</span><span>Ожидается первичное решение</span></div>}
          </div>
          {station.services.filter((service) => service === (activeService ?? station.services[0])).map((service) =>
            <StatusRow key={`${cardId}:${service}`} service={service}
              current={station.statuses[service]} available={station.available[service] ?? []}
              disabled={!isConnected || ended} reports={reports.filter((report) => report.service === service)}
              phoneExercise={from === "учебный сценарий"}
              crewOptions={station.crew_options?.filter((crew) => crew.startsWith(`${service} —`)) ?? []}
              selectedCrew={selectedCrew.startsWith(`${service} —`) ? selectedCrew : ""}
              onCrew={(crew) => send({ type: "crew.select", crew })}
              onSet={(next, comment) => channel.current?.send({ type: "card.status", service, status: next, comment })} />)}
        </> : <p className="dds-empty">Ожидаем состав служб по карточке.</p>}
        <p className="dds-hint">Сначала примите карточку или укажите причину отказа. Последующие статусы в занятии ДДС подтверждаются докладами по телефону.</p>
      </section>}
      {pane === "journal" && station && station.log.length > 0 && <section className="dds-panel"><div className="dds-panel-title"><h2>Журнал реагирования</h2></div>
        <div className="dds-table-wrap"><table className="dds-registry"><thead><tr><th>Время</th><th>Служба</th><th>Статус</th><th>Комментарий</th></tr></thead>
          <tbody>{station.log.map((entry, index) => <tr key={`${entry.at}-${index}`}><td>{time(entry.at)}</td><td>{entry.service}</td>
            <td>{SERVICE_STATUS_LABELS[entry.status]}</td><td>{entry.comment || "—"}</td></tr>)}</tbody></table></div></section>}
    </div><aside className="dds-right">
      {pane === "phone" && <section className="dds-panel dds-phone"><div className="dds-panel-title"><h2>IP-телефон</h2><span>Учебная имитация</span></div>
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
      </section>}
      {pane === "reply" && <section className="dds-panel dds-reply">
        <div className="dds-panel-title"><h2>Ответ и комментарий диспетчера</h2>
          <span>{reply.dirty ? "Не сохранено" : "Сохранено"}</span></div>
        <label htmlFor="dds-reply-text">Свободный текст по текущей карточке</label>
        <textarea id="dds-reply-text" rows={7} value={reply.text}
          disabled={!isConnected || ended || advancePending}
          placeholder="Что сообщено старшему группы, какие сведения получены и почему принято решение"
          onChange={(event) => setReply({ text: event.target.value, dirty: true })} />
        <p className="dds-hint">Ответ сохраняется в журнале текущей карточки. Автооценка проверяет его наличие и минимальную полноту, но не грамотность или фактическую верность каждого предложения.</p>
        <button type="button" disabled={!cardId || !reply.dirty || !isConnected || ended || advancePending}
          onClick={() => { if (send({ type: "card.reply", card_id: cardId, text: reply.text })) setError(""); }}>
          Сохранить ответ</button>
      </section>}
      {pane === "more" && <section className="dds-panel dds-secondary"><div className="dds-panel-title"><h2>Дополнительные действия</h2></div>
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
        <button type="button" className="dds-finish" disabled={!cardId || !isConnected || ended || advancePending}
          onClick={() => {
            if (reply.dirty && !send({ type: "card.reply", card_id: cardId, text: reply.text })) return;
            if (send({ type: "card.next", card_id: cardId })) setAdvancePending(true);
          }}>
          {cardIndex < cardTotal ? "Завершить карточку и открыть следующую" : "Завершить карточку и занятие"}
        </button>
        {cardTotal > 1 && <button type="button" disabled={!isConnected || ended || advancePending}
          onClick={() => { if (window.confirm("Завершить занятие досрочно? Остальные карточки не будут пройдены.")) send({ type: "station.finish" }); }}>
          Досрочно завершить занятие</button>}
      </section>}
    </aside></div></div>}
      </div>
      <nav className="dds-service-dock" aria-label="Действия с карточкой">
        <span className="dds-dock-label">Службы:</span>
        {station?.services.map((service) => <button type="button" className={pane === "status" && activeService === service ? "active" : ""}
          key={service} onClick={() => { setActiveService(service); setPane("status"); }}>
          <b>{serviceTitle(service)}</b><small>{SERVICE_STATUS_LABELS[station.statuses[service]]}</small>
        </button>)}
        <button type="button" onClick={() => setPane("phone")}><b>☎ Связь</b><small>Бригада</small></button>
        <button type="button" onClick={() => setPane("reply")}><b>✎ Ответ</b><small>{reply.dirty ? "Не сохранён" : "Комментарий"}</small></button>
        <button type="button" onClick={() => setPane("journal")}><b>Журнал</b><small>Статусы</small></button>
        <button type="button" disabled={!cardId || !isConnected || ended || advancePending}
          onClick={() => {
            if (reply.dirty && !send({ type: "card.reply", card_id: cardId, text: reply.text })) return;
            if (send({ type: "card.next", card_id: cardId })) setAdvancePending(true);
          }}><b>✓ Завершить</b><small>{cardIndex < cardTotal ? "Следующая карточка" : "Занятие"}</small></button>
      </nav>
    </div>}
  </main>;
}

function StatusRow({ service, current, available, disabled, reports, phoneExercise, crewOptions, selectedCrew, onCrew, onSet }: {
  service: string; current: ServiceStatus; available: ServiceStatus[]; disabled: boolean;
  reports: PhoneReport[]; phoneExercise: boolean; crewOptions: string[]; selectedCrew: string;
  onCrew: (crew: string) => void; onSet: (status: ServiceStatus, comment: string) => void;
}) {
  const [chosen, setChosen] = useState<ServiceStatus | "">("");
  const [comment, setComment] = useState("");
  const needsComment = chosen === "declined" || chosen === "refused";
  const reportNeeded: Partial<Record<ServiceStatus, ReportPhase>> = {
    responding: "dispatched", arrived: "arrived", working: "working", completed: "completed",
  };
  const matchingReport = !chosen || !phoneExercise || !reportNeeded[chosen] ||
    reports.some((report) => report.phase === reportNeeded[chosen]);
  if (!available.length) return <p className="dds-hint">{serviceTitle(service)}: {SERVICE_STATUS_LABELS[current]}. Дальнейшие статусы недоступны.</p>;
  return <div className="dds-status-editor">
    <label>Статус
      <select value={chosen} disabled={disabled} aria-label={`Следующий статус ${service}`}
        onChange={(event) => setChosen(event.target.value as ServiceStatus)}>
        <option value="">{SERVICE_STATUS_LABELS[current]} →</option>
        {available.map((next) => <option key={next} value={next}>{SERVICE_STATUS_LABELS[next]}</option>)}
      </select>
    </label>
    <label>Номер наряда
      <select value={selectedCrew} disabled={disabled || !crewOptions.length} aria-label={`Наряд ${service}`}
        onChange={(event) => { if (event.target.value) onCrew(event.target.value); }}>
        <option value="">—</option>
        {crewOptions.map((crew) => <option value={crew} key={crew}>{crew}</option>)}
      </select>
    </label>
    <label>Комментарий
      <input type="text" value={comment} disabled={disabled} aria-label={`Комментарий к статусу ${service}`}
        placeholder={needsComment ? "Причина отказа и кому передана информация" : "Комментарий"}
        onChange={(event) => setComment(event.target.value)} />
    </label>
    <button type="button" className="dds-status-save" aria-label="Сохранить статус"
      disabled={disabled || !chosen || (needsComment && !comment.trim()) || !matchingReport}
      onClick={() => { if (chosen) { onSet(chosen, comment.trim()); setChosen(""); setComment(""); } }}>✓</button>
    <button type="button" className="dds-status-cancel" aria-label="Отменить изменение статуса"
      onClick={() => { setChosen(""); setComment(""); }}>×</button>
    {chosen && !matchingReport && <span className="dds-status-warning state-warn">Сначала получите доклад по телефону.</span>}
  </div>;
}
