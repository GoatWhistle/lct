// Учебное АРМ ДДС. Проверяемый текстовый протокол дополняется настоящим
// локальным SIP/WebRTC-телефоном; оба контура явно подписаны в интерфейсе.
import { useEffect, useRef, useState } from "react";

import { KioCard } from "@/features/kio-card/KioCard";
import { applyState, empty as emptyCard, readPath, type CardState } from "@/features/kio-card/merge";
import { SipPhone } from "@/features/sip/SipPhone";
import { sessionIdFromUrl } from "@/shared/api/session";
import { stationChannel, type ChannelStatus } from "@/shared/api/ws";
import type { DdsQueueCard, ServiceStatus, SessionReport, StationSnapshot, StationToServer } from "@/shared/types/generated";
import { ALARMING, CARD_STATUS_LABELS, SERVICE_STATUS_LABELS } from "./statuses";

const ACK_LIMIT_S = 30;
type ReportPhase = "dispatched" | "arrived" | "working" | "completed";
interface PhoneReport { service: string; crew: string; phase: ReportPhase; text: string; at: string }
interface PhoneLine { service: string; crew: string; speaker: "dispatcher" | "crew"; text: string; at: string }
interface PhonePending { service: string; crew: string; phase: ReportPhase }
interface CompletedCard { card_id: string; scenario_id: string; score_auto: number }
interface DdsSnapshot extends StationSnapshot {
  recipient_services?: string[];
  managed_service?: string | null;
  crew_options?: string[];
  crew_selected?: string | null;
  phone_reports?: PhoneReport[];
  phone_lines?: PhoneLine[];
  phone_pending?: PhonePending | null;
  card_id?: string | null;
  card_index?: number;
  card_total?: number;
  reply_text?: string;
  completed_cards?: CompletedCard[];
  queue_cards?: DdsQueueCard[];
}
const REPORT_LABELS: Record<ReportPhase, string> = {
  dispatched: "Доклад о выезде", arrived: "Доклад о прибытии",
  working: "Доклад о ходе работ", completed: "Доклад о завершении работ",
};
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
  const [brief, setBrief] = useState({ address: "", incident: "", request: "" });
  const [checkText, setCheckText] = useState("");
  const [error, setError] = useState("");
  const [ended, setEnded] = useState(false);
  const [report, setReport] = useState<SessionReport | null>(null);
  const [reportState, setReportState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [reportError, setReportError] = useState("");
  const [zone, setZone] = useState<boolean | null>(null);
  const [reply, setReply] = useState({ text: "", dirty: false });
  const [advancePending, setAdvancePending] = useState(false);
  const channel = useRef<ReturnType<typeof stationChannel> | null>(null);
  const activeCardId = useRef<string | null>(null);
  const openCardAfterSwitch = useRef(false);

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
          if (snapshot.card_id == null && snapshot.pending_cards_count > 0) {
            activeCardId.current = null;
            setCard(null);
            setView("registry");
            setPane(null);
            setAdvancePending(false);
          }
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
            setBrief({ address: "", incident: "", request: "" });
            setCheckText("");
            setZone(null);
            setReport(null);
            setReportState("idle");
            setView(openCardAfterSwitch.current ? "card" : "registry");
            openCardAfterSwitch.current = false;
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
          setBrief({ address: "", incident: "", request: "" });
          setCheckText("");
          setError("");
        }
        else if (event.type === "phone.line") setDialing(false);
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
  const latest = (lastReport?.crew === selectedCrew ? lastReport : null) ??
    reports.filter((item) => item.crew === selectedCrew).at(-1) ?? null;
  const phoneCall = station?.phone_pending ?? null;
  const phoneLines = (station?.phone_lines ?? []).filter((line) => line.crew === selectedCrew);
  const isConnected = status === "open";
  const cardId = card ? String(readPath(card.server, "card_id") ?? "") : "";
  const cardIndex = station?.card_index ?? 1;
  const cardTotal = station?.card_total ?? 1;
  const completedCards = station?.completed_cards ?? [];
  const queueCards = station?.queue_cards ?? [];
  const query = search.trim().toLocaleLowerCase("ru-RU");
  const visibleCompletedCards = completedCards.filter((item) =>
    !query || `${item.card_id} ${item.scenario_id}`.toLocaleLowerCase("ru-RU").includes(query));
  const visibleQueueCards = queueCards.filter((item) => !query ||
    [item.card_id, item.address ?? "", item.description ?? "", item.incident_type ?? "", item.title]
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
        {visibleQueueCards.map((item) => {
          const received = new Date(item.received_at);
          const liveElapsed = item.timer_stopped ? item.elapsed_ms / 1000 : Math.max(
            item.elapsed_ms / 1000, (clockNow.getTime() - received.getTime()) / 1000,
          );
          const openCard = () => {
            if (item.active) {
              setView("card");
              return;
            }
            openCardAfterSwitch.current = true;
            if (!send({ type: "card.open", card_id: item.card_id })) {
              openCardAfterSwitch.current = false;
            }
          };
          return <tr key={item.card_id} className={item.active ? "dds-selected" : ""}
            aria-current={item.active ? "true" : undefined}>
          <td data-label="Связи"><button type="button" className="dds-row-open" onClick={openCard} aria-label="Открыть карточку">▾</button></td>
          <td data-label="ЧС">—</td>
          <td data-label="Опер.">0</td><td data-label="АРМ">ДДС</td>
          <td data-label="Номер"><button type="button" className="dds-row-link" onClick={openCard}>{item.card_id.slice(0, 8).toUpperCase()}</button></td>
          <td data-label="Дата">{received.toLocaleDateString("ru-RU")}</td>
          <td data-label="Время">{received.toLocaleTimeString("ru-RU")}</td>
          <td data-label="Тип происшествия"><span className="dds-cell-value">
            <span>{item.title} · {item.incident_type || "тип не указан"}</span>
            <small>Описание: {item.description || "—"}</small>
          </span></td>
          <td data-label="Постр.">{item.victims_count ?? "—"}</td>
          <td data-label="Адрес">{item.address || "—"}</td>
          <td data-label="Статус службы"><span className="dds-cell-value">
            <span>{item.managed_service || "ДДС"}: {SERVICE_STATUS_LABELS[item.service_status]}</span>
            <small className={!item.timer_stopped && liveElapsed * 1000 > item.limit_ms ? "state-violated" : ""}>
              {item.timer_stopped ? "решение за" : "до первичного решения"} {liveElapsed.toFixed(0)} с
            </small>
          </span></td>
        </tr>})}
        {!visibleQueueCards.length && !visibleCompletedCards.length && <tr><td colSpan={11} className="dds-empty">
          {query ? "По запросу ничего не найдено." : "Ожидание готовой карточки от преподавателя или оператора 112…"}</td></tr>}
      </tbody></table></div>
      {queueCards.length > 1 && <p className="dds-hint">Одновременно в работе {queueCards.length} карточек. Таймер каждой идёт независимо от того, какая строка открыта.</p>}
      {station && station.pending_cards_count > 0 && <p className="dds-hint" role="status" aria-live="polite">
        {station.next_arrival_in_seconds === null
          ? `Осталось карточек: ${station.pending_cards_count}. Все карточки уже поступили; необработанные находятся в реестре.`
          : station.next_arrival_in_seconds > 0
          ? `Осталось карточек: ${station.pending_cards_count}. Следующее поступление примерно через ${station.next_arrival_in_seconds} с.`
          : queueCards.filter((item) => !item.active).length >= station.max_waiting_cards
            ? `Осталось карточек: ${station.pending_cards_count}. Очередь ожидания заполнена (${station.max_waiting_cards}); новые поступят, когда освободится место.`
            : `Осталось карточек: ${station.pending_cards_count}. Ожидается следующее поступление.`}
      </p>}
    </section>
    </>}

    {card && view === "card" && <div className="dds-case-layout">
      <div className="dds-case-top"><button type="button" onClick={() => { setView("registry"); setPane(null); }}>← Список происшествий</button>
        <span>Происшествие {cardId.slice(0, 8).toUpperCase()} · карточка {cardIndex} из {cardTotal}</span></div>
      <div className="dds-call-strip">
        <div className="dds-call-off"><span className="dds-call-icon">☎</span><strong>Отключение</strong><small>записи звонков · список SMS</small></div>
        <div><span className="dds-call-icon">☎</span><small>АОН</small><strong>{field(card, "caller_number")}</strong></div>
        <div><span className="dds-call-icon">☎</span><small>предоставленный</small><strong>{field(card, "caller_contact")}</strong></div>
        <div><span className="dds-call-icon">☎</span><small>телефон на место</small><strong>{field(card, "phone_on_scene")}</strong></div>
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
          <div>Получатели карточки: <b>{station?.recipient_services?.join(", ") || field(card, "notify")}</b></div>
          <div>Ваша ДДС: <b>{station?.managed_service || "служба не назначена"}</b></div></div>
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
              disabled={!isConnected || ended}
              crewOptions={station.crew_options?.filter((crew) => crew.startsWith(`${service} —`)) ?? []}
              selectedCrew={selectedCrew.startsWith(`${service} —`) ? selectedCrew : ""}
              onCrew={(crew) => send({ type: "crew.select", crew })}
              onSet={(next, comment) => channel.current?.send({ type: "card.status", service, status: next, comment })} />)}
        </> : <p className="dds-empty">Ожидаем состав служб по карточке.</p>}
        <p className="dds-hint">Сначала примите карточку или укажите причину отказа. Последующие статусы ставятся по фактически полученной информации; телефон — один из возможных каналов.</p>
      </section>}
      {pane === "journal" && station && station.log.length > 0 && <section className="dds-panel"><div className="dds-panel-title"><h2>Журнал реагирования</h2></div>
        <div className="dds-table-wrap"><table className="dds-registry"><thead><tr><th>Время</th><th>Служба</th><th>Статус</th><th>Комментарий</th></tr></thead>
          <tbody>{station.log.map((entry, index) => <tr key={`${entry.at}-${index}`}><td>{time(entry.at)}</td><td>{entry.service}</td>
            <td>{SERVICE_STATUS_LABELS[entry.status]}</td><td>{entry.comment || "—"}</td></tr>)}</tbody></table></div></section>}
    </div><aside className="dds-right">
      {pane === "phone" && <section className="dds-panel dds-phone"><div className="dds-panel-title"><h2>IP-телефон</h2><span>SIP/WebRTC + учебный протокол</span></div>
        <SipPhone defaultExtension="6101" defaultTarget="6102" title="Реальный локальный канал ДДС" />
        <p className="dds-hint">На втором рабочем месте откройте <a href="/phone" target="_blank" rel="noreferrer">телефон старшего группы</a> и зарегистрируйте 6102. Пароли 6101/6102 выдаёт локальный администратор командой <code>make sip-credentials</code>.</p>
        <h3 className="dds-protocol-title">Учебный протокол доклада</h3>
        <p className="dds-hint">Связь используется при необходимости. Разговор и учебный доклад сохраняются в журнале, но не блокируют изменение статуса.</p>
        <label htmlFor="dds-crew">Дежурная бригада</label>
        <select id="dds-crew" value={selectedCrew} disabled={!isConnected || ended || !station?.crew_options?.length}
          onChange={(event) => send({ type: "crew.select", crew: event.target.value })}>
          <option value="">Выберите бригаду</option>
          {station?.crew_options?.map((crew) => <option key={crew} value={crew}>{crew}</option>)}
        </select>
        <button type="button" className="dds-dial" disabled={!crewAccepted || !isConnected || ended || dialing || Boolean(phoneCall)}
          onClick={() => { if (send({ type: "phone.dial" })) setDialing(true); }}>
          ☎ {dialing ? "Соединение…" : "Позвонить старшему группы"}</button>
        {selectedCrew && !crewAccepted && <p className="dds-hint state-warn">Перед звонком поставьте статус «Принята» для службы этой бригады.</p>}
        {phoneCall && <div className="dds-phone-active" aria-live="polite">
          <strong>● Соединение установлено · {phoneCall.crew}</strong>
          {phoneCall.phase === "dispatched" ? <div className="dds-phone-form">
            <p>Передайте сведения из карточки и задачу бригаде. Доклад о выезде появится только после передачи.</p>
            <label htmlFor="dds-phone-address">Полный адрес из карточки</label>
            <input id="dds-phone-address" value={brief.address} onChange={(event) => setBrief({ ...brief, address: event.target.value })} placeholder="Улица, дом, при наличии квартира и этаж" />
            <label htmlFor="dds-phone-incident">Характер происшествия</label>
            <textarea id="dds-phone-incident" rows={2} value={brief.incident} onChange={(event) => setBrief({ ...brief, incident: event.target.value })} placeholder="Что случилось и важные обстоятельства" />
            <label htmlFor="dds-phone-request">Что требуется от бригады</label>
            <input id="dds-phone-request" value={brief.request} onChange={(event) => setBrief({ ...brief, request: event.target.value })} placeholder="Подтвердите выезд и доложите о прибытии" />
            <button type="button" disabled={!isConnected || ended || !brief.address.trim() || !brief.incident.trim() || !brief.request.trim()}
              onClick={() => send({ type: "phone.brief", ...brief })}>Передать сообщение</button>
          </div> : <div className="dds-phone-form">
            <p>Уточните обстановку у старшего группы. Ответ станет основанием для следующего статуса.</p>
            <label htmlFor="dds-phone-check">Ваш вопрос старшему</label>
            <textarea id="dds-phone-check" rows={2} value={checkText} onChange={(event) => setCheckText(event.target.value)} placeholder="Сообщите, пожалуйста, текущее состояние работ по карточке" />
            <button type="button" disabled={!isConnected || ended || checkText.trim().length < 8}
              onClick={() => send({ type: "phone.check", text: checkText })}>Спросить обстановку</button>
          </div>}
          <button type="button" className="dds-phone-hangup" onClick={() => send({ type: "phone.hangup" })}>Завершить без доклада</button>
        </div>}
        {phoneLines.length > 0 && <div className="dds-phone-transcript" aria-live="polite"><strong>Учебный разговор</strong>
          {phoneLines.map((line, index) => <p key={`${line.at}-${index}`}><small>{time(line.at)} · {line.speaker === "crew" ? "Старший группы" : "Диспетчер"}</small><br />{line.text}</p>)}
        </div>}
        {latest ? <div className="dds-report" aria-live="polite"><small>{REPORT_LABELS[latest.phase]} · {time(latest.at)}</small>
          <strong>{latest.crew}</strong><p>{latest.text}</p>
          <span>Прослушайте доклад и поставьте соответствующий статус реагирования.</span></div>
          : <div className="dds-phone-idle">Доклад появится после передачи сведений и ответа старшего группы.</div>}
        {reports.length > 0 && <details className="dds-details"><summary>История докладов ({reports.length})</summary>
          <ol className="dds-reports">{reports.map((report, index) => <li key={`${report.at}-${index}`}><b>{REPORT_LABELS[report.phase]}</b> · {time(report.at)}<br />{report.text}</li>)}</ol>
        </details>}
      </section>}
      {pane === "reply" && <section className="dds-panel dds-reply">
        <div className="dds-panel-title"><h2>Рабочая заметка диспетчера</h2>
          <span>{reply.dirty ? "Не сохранено" : "Сохранено"}</span></div>
        <label htmlFor="dds-reply-text">Необязательная заметка по текущей карточке</label>
        <textarea id="dds-reply-text" rows={7} value={reply.text}
          disabled={!isConnected || ended || advancePending}
          placeholder="Что сообщено старшему группы, какие сведения получены и почему принято решение"
          onChange={(event) => setReply({ text: event.target.value, dirty: true })} />
        <p className="dds-hint">Заметка сохраняется в журнале, но не влияет на оценку. Обязательный комментарий вводится непосредственно при статусе отказа.</p>
        <button type="button" disabled={!cardId || !reply.dirty || !isConnected || ended || advancePending}
          onClick={() => { if (send({ type: "card.reply", card_id: cardId, text: reply.text })) setError(""); }}>
          Сохранить ответ</button>
      </section>}
      {pane === "more" && <section className="dds-panel dds-secondary"><div className="dds-panel-title"><h2>Дополнительные действия</h2></div>
        <div className="dds-zone">Зона ответственности: <button type="button" disabled={zone !== null || !isConnected || ended}
          onClick={() => { channel.current?.send({ type: "zone.decision", in_zone: true }); setZone(true); }}>Наша</button>
          <button type="button" disabled={zone !== null || !isConnected || ended}
            onClick={() => { channel.current?.send({ type: "zone.decision", in_zone: false }); setZone(false); }}>Не наша</button></div>
        <p className="dds-hint">ДДС не исправляет и не возвращает заполненную карточку: её качество контролируют оператор и служба контроля 112.</p>
        <button type="button" className="dds-finish" disabled={!cardId || !isConnected || ended || advancePending}
          onClick={() => {
            if (reply.dirty && !send({ type: "card.reply", card_id: cardId, text: reply.text })) return;
            if (send({ type: "card.next", card_id: cardId })) setAdvancePending(true);
          }}>
          {queueCards.length > 1 ? "Закрыть карточку" : "Закрыть карточку и занятие"}
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
          }}><b>✓ Завершить</b><small>{queueCards.length > 1 ? "Закрыть карточку" : "Занятие"}</small></button>
      </nav>
    </div>}
  </main>;
}

function StatusRow({ service, current, available, disabled, crewOptions, selectedCrew, onCrew, onSet }: {
  service: string; current: ServiceStatus; available: ServiceStatus[]; disabled: boolean;
  crewOptions: string[]; selectedCrew: string;
  onCrew: (crew: string) => void; onSet: (status: ServiceStatus, comment: string) => void;
}) {
  const [chosen, setChosen] = useState<ServiceStatus | "">("");
  const [comment, setComment] = useState("");
  const needsComment = chosen === "declined" || chosen === "refused";
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
      disabled={disabled || !chosen || (needsComment && !comment.trim())}
      onClick={() => { if (chosen) { onSet(chosen, comment.trim()); setChosen(""); setComment(""); } }}>✓</button>
    <button type="button" className="dds-status-cancel" aria-label="Отменить изменение статуса"
      onClick={() => { setChosen(""); setComment(""); }}>×</button>
  </div>;
}
