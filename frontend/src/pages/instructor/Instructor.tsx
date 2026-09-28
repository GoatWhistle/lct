// Пульт преподавателя.
//
// Компоновка от того, по чему его оценит действующий преподаватель: задать
// сценарий и не вмешиваться, видеть работу курсанта вживую, получить готовый
// разбор, показать группе (docs/arch/FRONTEND.md).

import { useEffect, useRef, useState } from "react";

import { Debrief } from "@/features/debrief/Debrief";
import { FindingReview } from "@/features/debrief/FindingReview";
import { Director } from "@/features/instructor/Director";
import { ScoreWeightsEditor } from "@/features/instructor/ScoreWeightsEditor";
import { ScenarioEditor } from "@/features/instructor/ScenarioEditor";
import { useControl } from "@/features/instructor/useControl";
import { KioCard } from "@/features/kio-card/KioCard";
import { StandStatus } from "@/features/health/StandStatus";
import { ModeBanner } from "@/features/mode-banner/ModeBanner";
import { useObserve } from "@/features/observe/useObserve";
import { InterviewTimer, Timers } from "@/features/timers/Timers";
import { CARD_STATUS_LABELS, SERVICE_STATUS_LABELS } from "@/pages/dds/statuses";
import { DEMO_MODE } from "@/features/auth/useAuth";
import {
  useActiveSessions, useIncidentGroups, useReviewScenarioSubmission, useScenarioSubmissions,
  useScenarios, useSessions, useTrainees,
} from "@/shared/api/http";
import type { Signal } from "@/shared/api/http";
import { sessionIdFromUrl } from "@/shared/api/session";
import type { Level, SessionMode, SessionReport } from "@/shared/types/generated";

const MODES: { value: SessionMode; label: string; hint: string }[] = [
  { value: "training", label: "Тренировочный", hint: "Подсказки доступны" },
  { value: "exam", label: "Контрольный", hint: "Подсказки отключены" },
  { value: "self", label: "Самостоятельная работа", hint: "Курсант работает самостоятельно; подсказки доступны" },
];

function levelLabel(level: Level): string {
  const labels: Record<Level, string> = {
    L1: "L1 · простой вызов",
    L2: "L2 · заявитель в стрессе",
    L3: "L3 · обрыв связи / обратный вызов",
  };
  return labels[level];
}

function modeLabel(mode: SessionMode): string {
  return MODES.find((item) => item.value === mode)?.label ?? mode;
}

function worstSignalSeverity(signals: Signal[]): "warn" | "violated" | null {
  return signals.some((signal) => signal.severity === "violated")
    ? "violated"
    : signals.length ? "warn" : null;
}

function sessionDateLabel(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function LessonNumberInput({
  label, value, min, max, fallback, onCommit,
}: {
  label: string; value: number; min: number; max: number; fallback: number;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  function commit() {
    const parsed = draft.trim() ? Number(draft) : fallback;
    const normalized = Number.isFinite(parsed) ? Math.max(min, Math.min(max, parsed)) : fallback;
    setDraft(String(normalized));
    onCommit(normalized);
  }
  return <input type="number" min={min} max={max} aria-label={label} value={draft}
    onChange={(event) => setDraft(event.target.value)} onBlur={commit} />;
}

export function Instructor() {
  const [sessionId, setSessionId] = useState<string | null>(sessionIdFromUrl());
  const [scenarioId, setScenarioId] = useState("");
  const [ticketFilter, setTicketFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [levelFilter, setLevelFilter] = useState<Level | "">("");
  const [scenarioSearch, setScenarioSearch] = useState("");
  const [cardSource, setCardSource] = useState<"system" | "trainee" | "mixed">("system");
  const [submissionReviewComments, setSubmissionReviewComments] = useState<Record<string, string>>({});
  const [extraScenarioIds, setExtraScenarioIds] = useState<string[]>([]);
  const [randomScenario, setRandomScenario] = useState(false);
  const [mode, setMode] = useState<SessionMode>("training");
  const [exercise, setExercise] = useState<"dds" | "call" | "card" | "card_dds">("dds");
  const [decisionTimeLimit, setDecisionTimeLimit] = useState(30);
  const [cardFillTimeLimit, setCardFillTimeLimit] = useState(180);
  const [ddsCardWorkTimeLimit, setDdsCardWorkTimeLimit] = useState(180);
  const [ddsReportReactionEnabled, setDdsReportReactionEnabled] = useState(false);
  const [ddsReportReactionLimit, setDdsReportReactionLimit] = useState(45);
  const [ddsAdjacentLimit, setDdsAdjacentLimit] = useState(90);
  const [allowedErrors, setAllowedErrors] = useState(0);
  const [requireCorrectGrammar, setRequireCorrectGrammar] = useState(true);
  const [lessonScoreWeights, setLessonScoreWeights] = useState<Record<string, number>>({});
  const [ddsArrivalIntervalSeconds, setDdsArrivalIntervalSeconds] = useState(60);
  const [ddsMaxWaiting, setDdsMaxWaiting] = useState(3);
  const [trainee, setTrainee] = useState("");
  const [traineeId, setTraineeId] = useState("");
  const [noteFor, setNoteFor] = useState<string | null>(null);
  // Запускается только занятие, созданное этой кнопкой. Открытая из истории
  // ссылка на идущее занятие не должна запускать его заново и сбрасывать.
  const toStart = useRef<string | null>(null);
  const [startPendingId, setStartPendingId] = useState<string | null>(null);
  const startPendingRef = useRef<string | null>(null);
  const [startError, setStartError] = useState("");
  const startTimeout = useRef<number | null>(null);
  const [noteText, setNoteText] = useState("");
  const [overrideReport, setOverrideReport] = useState<SessionReport | null>(null);
  const [overrideScore, setOverrideScore] = useState("");
  const [overrideComment, setOverrideComment] = useState("");
  const [overrideError, setOverrideError] = useState("");
  const [overrideBusy, setOverrideBusy] = useState(false);

  const scenarios = useScenarios();
  const scenarioSubmissions = useScenarioSubmissions();
  const reviewScenarioSubmission = useReviewScenarioSubmission();
  const incidentGroups = useIncidentGroups();
  const history = useSessions();
  const activeSessions = useActiveSessions();
  const trainees = useTrainees();
  const session = useObserve(sessionId);
  const control = useControl(sessionId);
  const pendingSubmissions = (scenarioSubmissions.data ?? []).filter((item) => item.status === "pending");
  const ddsExcludedScenarios = (scenarios.data ?? []).filter((item) => item.outcome !== "card");

  useEffect(() => {
    if (!session.report) return;
    setOverrideReport(null);
    setOverrideScore(String(session.report.score_final));
    setOverrideComment(session.report.override_comment ?? "");
  }, [session.report?.session_id, session.report?.score_final, session.report?.override_comment]);

  const visibleScenarios = (scenarios.data ?? []).filter((item) =>
    (exercise !== "dds" || item.outcome === "card") &&
    (cardSource === "mixed" || item.source === cardSource) &&
    (!ticketFilter || cardSource === "trainee" || item.source === "trainee" || item.ticket === Number(ticketFilter)) &&
    (!categoryFilter || item.source === "trainee" || item.ekp_group === Number(categoryFilter)) &&
    (!levelFilter || item.level === levelFilter) &&
    (!scenarioSearch.trim() || `${item.title} ${item.id}`.toLocaleLowerCase("ru-RU")
      .includes(scenarioSearch.trim().toLocaleLowerCase("ru-RU")))
  ).sort((a, b) => (a.ticket ?? 99) - (b.ticket ?? 99) || (a.position ?? 0) - (b.position ?? 0));
  const selectedScenario = visibleScenarios.find((item) => item.id === scenarioId);
  const hasScenarioFilters = Boolean(ticketFilter || categoryFilter || levelFilter || scenarioSearch.trim() || cardSource !== "system");
  function clearScenarioFilters() {
    setTicketFilter("");
    setCategoryFilter("");
    setLevelFilter("");
    setScenarioSearch("");
    setCardSource("system");
    setWholeTicket(false);
  }
  const selectedTicketScenarios = ticketFilter
    ? (scenarios.data ?? []).filter((item) => item.ticket === Number(ticketFilter))
        .filter((item) => item.source === "system")
        .filter((item) => exercise !== "dds" || item.outcome === "card")
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    : [];
  const [wholeTicket, setWholeTicket] = useState(false);
  const canStartSelectedExercise = exercise === "dds" && wholeTicket
    ? selectedTicketScenarios.length > 0
    : Boolean(scenarioId);
  const startDisabledReason = scenarios.isLoading || trainees.isLoading
      ? "Загружаются сценарии и список курсантов…"
      : !canStartSelectedExercise
        ? "Выберите подходящий сценарий или билет перед запуском."
      : Boolean(trainees.data?.length && (!traineeId || (traineeId === "__demo__" && !DEMO_MODE)))
        ? DEMO_MODE ? "Выберите курсанта из списка или пункт «Демо-курсант»." : "Сначала выберите курсанта из списка."
        : "";
  const selectedTraineeLabel = trainees.data?.find((item) => item.id === traineeId)?.name
    ?? (traineeId === "__demo__" ? trainee.trim() || "Демо-курсант" : trainee.trim());
  const exerciseLabel = exercise === "dds" ? "Диспетчер ДДС"
    : exercise === "call" ? "Оператор 112 · звонок"
      : exercise === "card" ? "Оператор 112 · текстовая вводная" : "Полный цикл 112 → ДДС";

  useEffect(() => () => {
    if (startTimeout.current !== null) window.clearTimeout(startTimeout.current);
  }, []);

  useEffect(() => {
    if (!sessionId || sessionId !== startPendingId) return;
    if (session.title) {
      if (startTimeout.current !== null) window.clearTimeout(startTimeout.current);
      startTimeout.current = null;
      startPendingRef.current = null;
      setStartPendingId(null);
      setStartError("");
      return;
    }
    // observe may connect just before scenario.start and report a transient
    // session_not_found. Do not present that expected startup race as failure.
    if (session.error && session.error !== "Занятие не запущено") {
      if (startTimeout.current !== null) window.clearTimeout(startTimeout.current);
      startTimeout.current = null;
      toStart.current = null;
      startPendingRef.current = null;
      setStartPendingId(null);
      setStartError(session.error);
    }
  }, [sessionId, startPendingId, session.title, session.error]);

  useEffect(() => {
    if (!visibleScenarios.some((item) => item.id === scenarioId)) {
      setScenarioId(visibleScenarios[0]?.id ?? "");
    }
  }, [scenarios.data, ticketFilter, categoryFilter, levelFilter, scenarioSearch, scenarioId, exercise, cardSource]);

  function startLesson() {
    if (startPendingId || toStart.current || startDisabledReason) return;
    if (trainees.data?.length && (!traineeId || (traineeId === "__demo__" && !DEMO_MODE))) return;
    const id = crypto.randomUUID();
    // Номер занятия живёт в адресе: монитор и АРМ курсанта открываются ссылкой.
    window.history.replaceState(null, "", `/instructor?session=${id}`);
    toStart.current = id;
    startPendingRef.current = id;
    setStartPendingId(id);
    setStartError("");
    setSessionId(id);
    startTimeout.current = window.setTimeout(() => {
      if (startPendingRef.current !== id) return;
      startPendingRef.current = null;
      if (toStart.current === id) toStart.current = null;
      startTimeout.current = null;
      setStartPendingId(null);
      setStartError("Не удалось запустить занятие. Проверьте подключение и нажмите «Повторить запуск».");
    }, 15_000);
  }

  async function saveScoreOverride(report: SessionReport) {
    const score = Number(overrideScore);
    if (!overrideScore.trim() || !Number.isFinite(score) || score < 0 || score > 100 || !overrideComment.trim()) {
      setOverrideError("Укажите оценку от 0 до 100 и обязательное обоснование.");
      return;
    }
    setOverrideBusy(true);
    setOverrideError("");
    try {
      const response = await fetch(`/api/sessions/${report.session_id}/report`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ score_final: score, comment: overrideComment.trim() }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data && typeof data === "object" && "detail" in data ? data.detail : response.statusText;
        throw new Error(typeof detail === "string" ? detail : "Не удалось сохранить корректировку.");
      }
      const updated = data as SessionReport;
      setOverrideReport(updated);
      setOverrideScore(String(updated.score_final));
      setOverrideComment(updated.override_comment ?? "");
    } catch (cause) {
      setOverrideError(cause instanceof Error ? cause.message : "Не удалось сохранить корректировку.");
    } finally {
      setOverrideBusy(false);
    }
  }

  function openActiveSession(item: NonNullable<typeof activeSessions.data>[number]) {
    toStart.current = null;
    startPendingRef.current = null;
    if (startTimeout.current !== null) window.clearTimeout(startTimeout.current);
    startTimeout.current = null;
    setStartPendingId(null);
    window.history.replaceState(null, "", `/instructor?session=${item.session_id}`);
    setSessionId(item.session_id);
    setScenarioId(item.scenario_id);
    setExercise(item.exercise);
    setMode(item.mode);
  }

  // Занятие запускается, когда канал управления открылся.
  useEffect(() => {
    if (!sessionId || control.openSessionId !== sessionId || toStart.current !== sessionId) return;
    const selected = trainees.data?.find((item) => item.id === traineeId);
    const primaryScenarioId = exercise === "dds" && wholeTicket
      ? selectedTicketScenarios[0]?.id ?? scenarioId : scenarioId;
    const scenarioIds = exercise === "dds" || exercise === "card_dds"
      ? (exercise === "dds" && wholeTicket
          ? selectedTicketScenarios.map((item) => item.id)
          : [scenarioId, ...extraScenarioIds.filter((id) => id !== scenarioId &&
            visibleScenarios.some((item) => item.id === id))])
          .filter((id) => scenarios.data?.find((item) => item.id === id)?.outcome === "card")
          .filter((id, index, all) => id && all.indexOf(id) === index)
      : undefined;
    const randomScenarioIds = randomScenario && exercise !== "call"
      && !(exercise === "dds" && wholeTicket)
      ? visibleScenarios.filter((item) => item.outcome === "card").map((item) => item.id)
      : undefined;
    const reportReactionEnabled = (exercise === "dds" || exercise === "card_dds") && ddsReportReactionEnabled;
    const sent = control.start(sessionId, {
      scenarioId: primaryScenarioId,
      mode,
      trainee: selected?.name || trainee || "Демо-курсант",
      exercise: exercise === "card_dds" ? "card" : exercise,
      traineeId: selected?.id,
      ddsService: selected?.service ?? undefined,
      scenarioIds: scenarioIds && scenarioIds.length > 1 ? scenarioIds : undefined,
      randomScenarioIds,
      ddsArrivalIntervalSeconds,
      ddsMaxWaiting,
      handoffToDds: exercise === "card_dds",
      criteria: {
        decision_time_limit_seconds: decisionTimeLimit,
        card_fill_time_limit_seconds: cardFillTimeLimit,
        dds_card_work_time_limit_seconds: ddsCardWorkTimeLimit,
        dds_report_reaction_enabled: reportReactionEnabled,
        dds_report_reaction_limit_seconds: ddsReportReactionLimit,
        dds_adjacent_limit_seconds: ddsAdjacentLimit,
        allowed_errors: allowedErrors,
        require_correct_grammar: requireCorrectGrammar,
        score_weights: reportReactionEnabled
          ? lessonScoreWeights
          : Object.fromEntries(Object.entries(lessonScoreWeights).filter(([key]) => key !== "dds_report_reaction")),
      },
    });
    if (sent) toStart.current = null;
  }, [sessionId, control, scenarioId, extraScenarioIds, mode, trainee, traineeId, trainees.data,
    exercise, decisionTimeLimit, cardFillTimeLimit, ddsCardWorkTimeLimit, ddsReportReactionEnabled, ddsReportReactionLimit, ddsAdjacentLimit, allowedErrors, requireCorrectGrammar, wholeTicket, selectedTicketScenarios,
    ddsArrivalIntervalSeconds, ddsMaxWaiting, lessonScoreWeights, scenarios.data,
    randomScenario, visibleScenarios]);

  const links = sessionId
    ? {
        trainee: `${location.origin}/trainee?session=${sessionId}`,
        wall: `${location.origin}/wall?session=${sessionId}`,
        dds: `${location.origin}/dds?session=${sessionId}&role=dds_01`,
      }
    : null;

  return (
    <main className={`page instructor-page${session.report ? " has-report" : ""}`}>
      <StandStatus />
      <ModeBanner mode={session.mode ?? mode} />
      <h1>Рабочее место преподавателя</h1>
      <p className="workspace-links">
        <a href="#ai-scenario-editor">Перейти к ИИ-формирователю сценария ↓</a>
        <a href="/materials">Учебные материалы и назначения →</a>
      </p>

      <details className="instructor-live-group instructor-secondary-panel" open={Boolean(activeSessions.data?.length)}>
        <summary>
          <span>Занятия группы в реальном времени</span>
          <small>{activeSessions.isLoading ? "Обновляем список…" : activeSessions.data?.length
            ? `${activeSessions.data.length} активных` : "Сейчас нет активных занятий"}</small>
        </summary>
        <p className="ref" role="status" aria-live="polite">
          Список ваших активных сессий обновляется автоматически каждые 2 секунды.
        </p>
        <div className="dds-table-wrap"><table className="grid">
          <thead><tr><th>Курсант</th><th>Упражнение · карточка</th><th>Ход</th><th>ДДС</th><th>Задержки</th><th>Сигналы</th><th></th></tr></thead>
          <tbody>
            {(activeSessions.data ?? []).map((item) => (
              <tr key={item.session_id}>
                <td>{item.trainee_name || "Демо-курсант"}<small className="ref">{modeLabel(item.mode)} · {Math.floor(item.elapsed_seconds / 60)} мин {item.elapsed_seconds % 60} с</small></td>
                <td>{item.scenario_title}<small className="ref">{item.scenario_id}{item.exercise === "dds" && ` · ${item.dds_open_cards}/${item.dds_card_total} карточек открыто`}</small></td>
                <td>{Object.entries(item.dds_statuses).map(([service, status]) =>
                  <span key={service}>{service}: {status}<br /></span>)}
                  {!Object.keys(item.dds_statuses).length && (item.exercise === "dds" ? "Ожидает первичного решения" : "Упражнение выполняется")}
                  {item.dds_snapshot && <details className="active-session-details">
                    <summary>Карточки и доклады ({item.dds_snapshot.queue_cards?.length ?? 0})</summary>
                    {item.dds_snapshot.crew_selected && <p><strong>Бригада:</strong> {item.dds_snapshot.crew_selected}</p>}
                    {item.dds_snapshot.phone_pending && <p className="ref" role="status">
                      Идёт связь с бригадой {item.dds_snapshot.phone_pending.crew}.
                    </p>}
                    {item.dds_snapshot.queue_cards?.map((card) => <article className="active-session-card" key={card.card_id}>
                      <strong>{card.active ? "Сейчас в работе: " : "Ожидает: "}{card.title}</strong>
                      {card.address && <span>{card.address}</span>}
                      <small>{card.managed_service ? `${card.managed_service} · ` : ""}{SERVICE_STATUS_LABELS[card.service_status]} · {CARD_STATUS_LABELS[card.card_status]}</small>
                      <small>{card.timer_stopped ? `Открыта за ${Math.ceil(card.elapsed_ms / 1000)} с` :
                        card.elapsed_ms > card.limit_ms ? `Просрочено открытие · ${Math.ceil((card.elapsed_ms - card.limit_ms) / 1000)} с` :
                          `До открытия осталось ${Math.ceil((card.limit_ms - card.elapsed_ms) / 1000)} с`}</small>
                    </article>)}
                    {item.dds_snapshot.pending_cards_count ? <p className="ref">Ещё поступят по сценарию: {item.dds_snapshot.pending_cards_count}</p> : null}
                    {item.dds_snapshot.log.length > 0 && <div className="active-session-log">
                      <strong>Последние отметки служб</strong>
                      {item.dds_snapshot.log.slice(-4).reverse().map((entry, index) => <p key={`${entry.at}-${index}`}>
                        <span>{entry.service} · {SERVICE_STATUS_LABELS[entry.status]}</span>
                        {entry.comment && <small>{entry.comment}</small>}
                      </p>)}
                    </div>}
                    {item.dds_snapshot.phone_reports?.length ? <div className="active-session-log">
                      <strong>Доклады бригады</strong>
                      {item.dds_snapshot.phone_reports.map((report, index) => <p key={`${report.at}-${index}`}>
                        <span>{report.crew} · {report.phase === "dispatched" ? "выезд" : report.phase === "arrived" ? "прибытие" : report.phase === "working" ? "ход работ" : report.phase === "completed" ? "завершение" : report.phase}</span>
                        <small>{report.text}</small>
                      </p>)}
                    </div> : null}
                  </details>}</td>
                <td>{item.exercise === "dds" ? `Открытых карточек: ${item.dds_open_cards}` : "—"}</td>
                <td className={item.dds_overdue_cards ? "state-violated" : "state-ok"}>
                  {item.dds_overdue_cards || item.dds_work_overdue_cards
                    ? `Просрочено: открытие ${item.dds_overdue_cards}; первая запись ${item.dds_work_overdue_cards}`
                    : "Нормативы не нарушены"}
                </td>
                <td className={worstSignalSeverity(item.signals) ? `state-${worstSignalSeverity(item.signals)}` : item.presence_known ? "state-ok" : undefined}>
                  {item.signals.length
                    ? item.signals.map((signal) => <span key={signal.kind}>{signal.text}<br /></span>)
                    : item.presence_known ? "Сигналов нет" : null}
                  {!item.presence_known && <span>Связь курсанта не проверена</span>}
                </td>
                <td><button type="button" onClick={() => openActiveSession(item)}>
                  {sessionId === item.session_id ? "Открыто" : "Наблюдать"}
                </button></td>
              </tr>
            ))}
            {!activeSessions.data?.length && <tr><td colSpan={7}>
              {activeSessions.isLoading ? "Загружаем активные занятия…" : "Активных занятий пока нет."}
            </td></tr>}
          </tbody>
        </table></div>
      </details>

      <details className="student-scenario-review instructor-secondary-panel" open={pendingSubmissions.length > 0}>
        <summary>
          <span>Карточки курсантов на проверку</span>
          <small>{scenarioSubmissions.isLoading ? "Проверяем очередь…" : pendingSubmissions.length
            ? `${pendingSubmissions.length} ожидают решения` : "Новых карточек нет"}</small>
        </summary>
        <p className="ref">Только ваши учебные группы. Утверждение создаёт карточку для очереди ДДС; преподаватель проверяет её факты до публикации.</p>
        {pendingSubmissions.map((item) => (
          <article className="student-scenario-proposal" key={item.id}>
            <header><strong>{item.title}</strong><span>{item.author_name} · {item.level} · {item.incident_type}</span></header>
            {item.kio ? <details><summary>Проверить КИО целиком</summary>
              <KioCard state={{ server: item.kio, pending: {} }} readOnly />
            </details> : <><p>{item.description}</p>
              {item.address && <p><strong>Адрес:</strong> {item.address}</p>}
              {item.victims !== null && <p><strong>Пострадавших:</strong> {item.victims}</p>}
            </>}
            <label>Комментарий преподавателя<textarea maxLength={1000} value={submissionReviewComments[item.id] ?? ""}
              onChange={(event) => setSubmissionReviewComments((previous) => ({ ...previous, [item.id]: event.target.value }))} /></label>
            <footer>
              <button type="button" disabled={reviewScenarioSubmission.isPending}
                onClick={() => reviewScenarioSubmission.mutate({ id: item.id, decision: "approve", comment: submissionReviewComments[item.id] })}>
                Проверил(а), утвердить в банк ДДС
              </button>
              <button type="button" disabled={reviewScenarioSubmission.isPending || !(submissionReviewComments[item.id] ?? "").trim()}
                onClick={() => reviewScenarioSubmission.mutate({ id: item.id, decision: "reject", comment: submissionReviewComments[item.id] })}>
                Вернуть на доработку
              </button>
              {reviewScenarioSubmission.isError && <span role="alert" className="state-violated">{reviewScenarioSubmission.error.message}</span>}
            </footer>
          </article>
        ))}
        {!scenarioSubmissions.isLoading && !pendingSubmissions.length &&
          <p className="ref">Предложения курсантов из ваших групп появятся здесь.</p>}
      </details>

      <div className={`instructor-layout${sessionId ? " has-session" : " is-idle"}`}>
        <section>
          <h2>Настроить занятие</h2>
          <p className="lesson-setup-intro"><strong>Четыре шага до запуска</strong><span>Выберите сценарий, упражнение, режим и курсанта. Остальные настройки уже подготовлены — при необходимости их можно раскрыть ниже.</span></p>
          <table className="grid">
            <tbody>
              <tr>
                <th><span className="lesson-step-title"><i>1</i> Сценарий</span></th>
                <td>
                  <details className="scenario-filters-details">
                    <summary>Фильтры сценариев <span>{hasScenarioFilters ? "изменены · нажмите, чтобы настроить" : "необязательно · нажмите, чтобы открыть"}</span></summary>
                    <div className="instructor-scenario-filters">
                    <label><span>Источник карточек</span><select aria-label="Источник карточек" value={cardSource} onChange={(event) => {
                      setCardSource(event.target.value as "system" | "trainee" | "mixed");
                      setWholeTicket(false);
                    }}>
                      <option value="system">Системные карточки</option>
                      <option value="trainee">Сформированные курсантами</option>
                      <option value="mixed">Системные + курсанты</option>
                    </select></label>
                    <label><span>Категория ЕКП</span><select aria-label="Категория происшествия" value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)}>
                      <option value="">Все 23 категории ЕКП</option>
                      {incidentGroups.data?.groups.map((group) =>
                        <option key={group.id} value={group.id}>{group.title}</option>)}
                    </select></label>
                    <label><span>Билет</span><select aria-label="Билет заказчика" value={ticketFilter} onChange={(event) => {
                      setTicketFilter(event.target.value);
                      setWholeTicket(false);
                    }}>
                      <option value="">Все 32 билета</option>
                      {Array.from({ length: 32 }, (_, index) => <option key={index} value={index + 1}>Билет {index + 1}</option>)}
                    </select></label>
                    <label><span>Сложность</span><select aria-label="Уровень сложности" value={levelFilter}
                      onChange={(event) => setLevelFilter(event.target.value as Level | "")}>
                      <option value="">Все уровни сложности</option>
                      <option value="L1">L1 · простой вызов</option>
                      <option value="L2">L2 · заявитель в стрессе</option>
                      <option value="L3">L3 · обрыв связи / обратный вызов</option>
                    </select></label>
                    </div>
                    {hasScenarioFilters && <button className="scenario-filters-reset" type="button" onClick={clearScenarioFilters}>Сбросить фильтры</button>}
                  </details>
                  <div className="instructor-scenario-filters scenario-search-only">
                    <label className="instructor-filter-search"><span>Найти карточку</span><input type="search" aria-label="Поиск сценария" value={scenarioSearch}
                      placeholder="Название или номер" onChange={(event) => setScenarioSearch(event.target.value)} /></label>
                  </div>
                  <label className="instructor-primary-scenario"><span>Сценарий занятия</span>
                    <select aria-label="Сценарий занятия" value={scenarioId} disabled={exercise === "dds" && wholeTicket} onChange={(event) => {
                      setScenarioId(event.target.value);
                      setExtraScenarioIds((previous) => previous.filter((id) => id !== event.target.value));
                    }}>
                      {!visibleScenarios.length && <option value="">Карточки не найдены</option>}
                      {visibleScenarios.map((scenario) => (
                        <option key={scenario.id} value={scenario.id}>
                          {scenario.source === "trainee" ? "От курсанта · " : scenario.ticket ? `Билет ${scenario.ticket}, вызов ${scenario.position} · ` : ""}{scenario.title} · {levelLabel(scenario.level)}
                        </option>
                      ))}
                    </select>
                  </label>
                  {!scenarios.isLoading && visibleScenarios.length === 0 && <div className="scenario-empty-state" role="status">
                    <strong>Подходящих сценариев не найдено</strong>
                    <span>Измените поиск или фильтры. Можно быстро вернуться к системным карточкам.</span>
                    {hasScenarioFilters && <button type="button" onClick={clearScenarioFilters}>Сбросить фильтры</button>}
                  </div>}
                  {selectedScenario && <div className="selected-scenario-summary" aria-live="polite">
                    <span>Для занятия выбрано</span>
                    <strong>{selectedScenario.title}</strong>
                    <small>{selectedScenario.source === "trainee" ? "Предложение курсанта" : selectedScenario.ticket ? `Билет ${selectedScenario.ticket}` : "Учебный сценарий"} · {levelLabel(selectedScenario.level)}</small>
                    {(selectedScenario.dds || selectedScenario.topics?.length || selectedScenario.outcome) && <div className="selected-scenario-details">
                      {selectedScenario.dds && <span><b>Для ДДС:</b> {selectedScenario.dds}</span>}
                      {Boolean(selectedScenario.topics?.length) && <span><b>Темы:</b> {selectedScenario.topics!.join(" · ")}</span>}
                      {selectedScenario.outcome && <span><b>Тип:</b> {selectedScenario.outcome === "card" ? "карточка происшествия" : selectedScenario.outcome === "consultation" ? "консультация" : "передача в другой регион"}</span>}
                    </div>}
                  </div>}
                  <small className="ref scenario-count">
                    Найдено: {visibleScenarios.length} из {scenarios.data?.length ?? 0} сценариев
                    {exercise === "dds" && ddsExcludedScenarios.length > 0 &&
                      ` · ${ddsExcludedScenarios.length} не являются карточками ДДС`}
                  </small>
                  {exercise !== "call" && !(exercise === "dds" && wholeTicket) && <label className="mode-choice">
                    <input type="checkbox" checked={randomScenario}
                      disabled={!visibleScenarios.some((item) => item.outcome === "card")}
                      onChange={(event) => setRandomScenario(event.target.checked)} />
                    {" Случайный порядок карточек из отфильтрованного набора"}
                    <small className="ref"> — порядок формирует сервер; новые карточки поступают по заданному темпу, занятие можно завершить в любой момент</small>
                  </label>}
                  {exercise === "dds" && ticketFilter && cardSource === "system" && <label className="mode-choice">
                    <input type="checkbox" checked={wholeTicket && selectedTicketScenarios.length > 0}
                      disabled={selectedTicketScenarios.length === 0}
                      onChange={(event) => {
                        if (event.target.checked && selectedTicketScenarios[0]) {
                          setScenarioId(selectedTicketScenarios[0].id);
                          setExtraScenarioIds([]);
                        }
                        setWholeTicket(event.target.checked);
                      }} />
                    {` Запустить из билета ${ticketFilter} карточки для ДДС (${selectedTicketScenarios.length})`}
                    {exercise === "dds" && selectedTicketScenarios.length < 3 &&
                      <small className="ref"> — вызовы без карточки в ДДС не включаются в очередь</small>}
                    {selectedTicketScenarios.length === 0 && <small className="ref"> — нет карточек для упражнения ДДС</small>}
                  </label>}
                  {(exercise === "dds" || exercise === "card_dds") && !wholeTicket && <details className="scenario-queue">
                    <summary>{exercise === "card_dds" ? "Готовые карточки после КИО курсанта" : "Дополнительные карточки"} ({extraScenarioIds.length})</summary>
                    <p className="ref">{exercise === "card_dds" ? "Сначала курсант заполнит и передаст КИО, затем карточка попадёт в ДДС первой; отмеченные сценарии поступят следом." : "Интервал и максимум ожидающих задаются в критериях занятия. Таймер каждой карточки начинается при поступлении и идёт, пока курсант занят другой."}</p>
                    {visibleScenarios.filter((scenario) => scenario.id !== scenarioId && scenario.outcome === "card").map((scenario) =>
                      <label key={scenario.id} className="mode-choice">
                        <input type="checkbox" checked={extraScenarioIds.includes(scenario.id)} onChange={() =>
                          setExtraScenarioIds((previous) => previous.includes(scenario.id)
                            ? previous.filter((id) => id !== scenario.id) : [...previous, scenario.id])} />
                        {" "}{scenario.title} · {scenario.level}
                      </label>)}
                  </details>}
                </td>
              </tr>
              <tr>
                <th><span className="lesson-step-title"><i>2</i> Упражнение</span></th>
                <td>
                  <div className="exercise-options">
                    <label className="exercise-option"><input type="radio" checked={exercise === "dds"}
                      onChange={() => setExercise("dds")} /><span><strong>Диспетчер ДДС</strong><small>Разобрать готовую карточку и передать её службам.</small></span></label>
                    <label className="exercise-option"><input type="radio" checked={exercise === "call"}
                      onChange={() => setExercise("call")} /><span><strong>Оператор 112 · звонок</strong><small>Принять учебный вызов и заполнить карточку.</small></span></label>
                    <label className="exercise-option"><input type="radio" checked={exercise === "card"}
                      onChange={() => setExercise("card")} /><span><strong>Оператор 112 · текстовая вводная</strong><small>Отработать заполнение КИО без аудиозвонка.</small></span></label>
                    <label className="exercise-option"><input type="radio" checked={exercise === "card_dds"}
                      onChange={() => setExercise("card_dds")} /><span><strong>Полный цикл 112 → ДДС</strong><small>Курсант заполнит КИО, затем отработает готовые карточки в ДДС.</small></span></label>
                  </div>
                  {exercise === "call" && <p className="ref">В режиме звонка поля КИО откроются после ответа на вызов; браузеру понадобятся разрешения на микрофон и звук. Для демонстрации без аудио выберите «Оператор 112 · текстовая вводная» — её поля доступны сразу.</p>}
                </td>
              </tr>
              <tr>
                <th><span className="lesson-step-title"><i>3</i> Режим</span></th>
                <td>
                  {MODES.map((item) => (
                      <label key={item.value} className="mode-choice lesson-mode-choice">
                        <input
                          type="radio"
                          checked={mode === item.value}
                          onChange={() => setMode(item.value)}
                        />{" "}
                        <span><strong>{item.label}</strong><small>{item.hint}</small></span>
                    </label>
                  ))}
                </td>
              </tr>
              <tr>
                <th><span className="lesson-step-title"><i>4</i> Курсант</span></th>
                <td>
                  {trainees.data?.length ? (
                    <>
                      <select aria-label="Учётная запись курсанта" value={traineeId} onChange={(event) => setTraineeId(event.target.value)}>
                        <option value="">Выберите учётную запись</option>
                        {trainees.data.map((item) => (
                          <option key={item.id} value={item.id}>{item.name}{item.group ? ` · ${item.group}` : ""}{item.service ? ` · ${item.service}` : " · служба не назначена"}</option>
                        ))}
                        {DEMO_MODE && <option value="__demo__">Демо-курсант без учётной записи</option>}
                      </select>
                      {DEMO_MODE && traineeId === "__demo__" && <label className="demo-trainee-name">
                        Имя для этого занятия
                        <input type="text" aria-label="Имя демо-курсанта" value={trainee}
                          placeholder="Например, Иван Петров" onChange={(event) => setTrainee(event.target.value)} />
                        <small className="ref">Учётная запись и персональный отчёт не создаются; это безопасный режим для демонстрации.</small>
                      </label>}
                    </>
                  ) : (
                    <>
                      <input type="text" aria-label="Имя демо-курсанта" value={trainee} placeholder="имя для демо"
                        onChange={(event) => setTrainee(event.target.value)} />
                      <small className="ref">Без учётной записи: личный отчёт курсанту недоступен.</small>
                    </>
                  )}
                </td>
              </tr>
              <tr>
                <th>Оценивание</th>
                <td>
                  <details className="lesson-criteria-details">
                    <summary>Настроить нормативы и критерии оценки</summary>
                    <p className="ref">
                      {exercise === "dds"
                        ? `По умолчанию: открытие карточки за ${decisionTimeLimit} с, первая запись за ${ddsCardWorkTimeLimit} с; новые карточки поступают каждые ${ddsArrivalIntervalSeconds} с.`
                        : exercise === "card_dds"
                          ? `По умолчанию: заполнение КИО за ${cardFillTimeLimit} с, первая запись ДДС за ${ddsCardWorkTimeLimit} с.`
                          : exercise === "card"
                            ? `По умолчанию: заполнение КИО за ${cardFillTimeLimit} с.`
                            : "Для звонка действуют нормативы приёма вызова по ГОСТ."}
                    </p>
                  {exercise === "dds" && <>
                    <label className="lesson-criterion">
                      Интервал поступления карточек, секунд (0 — без задержки; действует лимит ожидания)
                      <LessonNumberInput label="Интервал поступления карточек ДДС, секунд"
                        value={ddsArrivalIntervalSeconds} min={0} max={300} fallback={0}
                        onCommit={setDdsArrivalIntervalSeconds} />
                    </label>
                    <label className="lesson-criterion">
                      Максимум ожидающих карточек
                      <LessonNumberInput label="Максимум ожидающих карточек ДДС"
                        value={ddsMaxWaiting} min={1} max={10} fallback={1} onCommit={setDdsMaxWaiting} />
                    </label>
                  </>}
                  {(exercise === "dds" || exercise === "card_dds") && <label className="lesson-criterion">
                    Открытие карточки ДДС, секунд
                    <LessonNumberInput label="Лимит открытия карточки ДДС, секунд" value={decisionTimeLimit}
                      min={5} max={300} fallback={30} onCommit={setDecisionTimeLimit} />
                  </label>}
                  {(exercise === "card" || exercise === "card_dds") && <label className="lesson-criterion">
                    Норматив заполнения карточки КИО, секунд
                    <LessonNumberInput label="Норматив заполнения карточки КИО, секунд"
                      value={cardFillTimeLimit} min={30} max={1800} fallback={180} onCommit={setCardFillTimeLimit} />
                    <small className="ref">По умолчанию 180 секунд; в отчёте будут факт и отклонение от норматива.</small>
                  </label>}
                  {(exercise === "dds" || exercise === "card_dds") && <label className="lesson-criterion">
                    Первая запись по карточке ДДС, секунд
                    <LessonNumberInput label="Норматив первой записи по карточке ДДС, секунд"
                      value={ddsCardWorkTimeLimit} min={30} max={1800} fallback={180}
                      onCommit={setDdsCardWorkTimeLimit} />
                    <small className="ref">По умолчанию 180 секунд от открытия до первого статуса с текстом; дальше работы по времени не нормируются.</small>
                  </label>}
                  {(exercise === "dds" || exercise === "card_dds") && <>
                    <label className="mode-choice">
                      <input type="checkbox" checked={ddsReportReactionEnabled}
                        onChange={(event) => setDdsReportReactionEnabled(event.target.checked)} />
                      Оценивать срок отметки статуса после доклада бригады
                    </label>
                    {ddsReportReactionEnabled && <label className="lesson-criterion">
                      Лимит реакции на доклад бригады, секунд
                      <LessonNumberInput label="Лимит реакции на доклад бригады, секунд"
                        value={ddsReportReactionLimit} min={5} max={600} fallback={45}
                        onCommit={setDdsReportReactionLimit} />
                      <small className="ref">Учебный лимит при включённом критерии; исходное значение — 45 секунд.</small>
                    </label>}
                    <label className="lesson-criterion">
                      Лимит вводной «бригада просит смежников», секунд
                      <LessonNumberInput label="Лимит вводной о смежной службе, секунд"
                        value={ddsAdjacentLimit} min={5} max={600} fallback={90}
                        onCommit={setDdsAdjacentLimit} />
                      <small className="ref">Действует в сценариях с вводной: за это время нужно ответить бригаде, а при молчании смежной службы — позвонить ей.</small>
                    </label>
                  </>}
                  <label className="lesson-criterion">
                    Допустимо нарушенных метрик
                    <LessonNumberInput label="Допустимое число ошибок" value={allowedErrors}
                      min={0} max={50} fallback={0} onCommit={setAllowedErrors} />
                  </label>
                  {exercise !== "dds" && <label className="mode-choice">
                    <input type="checkbox" checked={requireCorrectGrammar}
                      onChange={(event) => setRequireCorrectGrammar(event.target.checked)} />
                    Грамматика карточки оператора 112 входит в оценку
                  </label>}
                  <ScoreWeightsEditor exercise={exercise === "card_dds" ? "card_dds" : exercise}
                    reportReactionEnabled={(exercise === "dds" || exercise === "card_dds") && ddsReportReactionEnabled}
                    values={lessonScoreWeights} onChange={setLessonScoreWeights} />
                  <small className="ref">Нормативы ГОСТ для приёма вызова не изменяются.</small>
                  </details>
                </td>
              </tr>
            </tbody>
          </table>
          <div className="lesson-launch-summary" aria-label="Проверка настроек занятия">
            <strong>Перед запуском</strong>
            <ul>
              <li className={canStartSelectedExercise ? "is-ready" : "is-missing"}>
                <span aria-hidden="true">{canStartSelectedExercise ? "✓" : "1"}</span>
                <span><b>Сценарий</b><small>{selectedScenario?.title ?? (wholeTicket ? `Билет ${ticketFilter}` : "Выберите сценарий выше")}</small></span>
              </li>
              <li className="is-ready">
                <span aria-hidden="true">✓</span><span><b>Упражнение и режим</b><small>{exerciseLabel} · {MODES.find((item) => item.value === mode)?.label}</small></span>
              </li>
              <li className={selectedTraineeLabel ? "is-ready" : "is-missing"}>
                <span aria-hidden="true">{selectedTraineeLabel ? "✓" : "3"}</span>
                <span><b>Курсант</b><small>{selectedTraineeLabel || "Выберите участника выше"}</small></span>
              </li>
            </ul>
          </div>
          <div className="lesson-start-bar">
            <button type="button" className="start" onClick={startLesson}
              disabled={Boolean(startDisabledReason) || Boolean(startPendingId)}>
              {startPendingId ? "Создаём занятие…" : session.title ? "Начать новое занятие" : "Начать занятие"}
            </button>
            {startDisabledReason && <small className="ref" role="status">{startDisabledReason}</small>}
            {startPendingId && <small className="ref" role="status">Подключаемся к занятию. Не нажимайте повторно — кнопка станет доступна после запуска.</small>}
            {startError && <small className="ref" role="alert">{startError} <button type="button" onClick={startLesson}>Повторить запуск</button></small>}
            {links && session.title && <small className="ref">После запуска появятся отдельные ссылки для курсанта, ДДС и монитора.</small>}
          </div>
          {sessionId && session.title && (
            <p>
              {session.ended ? "занятие завершено" : (
                <>
                  <button type="button" onClick={control.stop}>Завершить занятие</button>{" "}
                  {session.paused ? (
                    <button type="button" onClick={control.resume}>Возобновить</button>
                  ) : (
                    <button type="button" onClick={control.pause}>Пауза</button>
                  )}
                  {session.paused && <small className="ref" role="status">Занятие на паузе — время не идёт</small>}
                </>
              )}
            </p>
          )}
          {sessionId && session.title && !session.ended && (
            exercise === "call" && <Director onInject={control.inject} disabled={control.status !== "open" || session.paused} error={session.error} />
          )}
          {links && startPendingId && (
            <p className="ref" role="status">Создаём занятие. Ссылки появятся автоматически после подключения.</p>
          )}
          {links && session.title && (
            <table className="grid">
              <tbody>
                <tr><th>АРМ курсанта</th><td><a href={exercise === "dds" ? links.dds : links.trainee}>открыть {exercise === "dds" ? "ДДС" : "112"}</a></td></tr>
                <tr><th>Внешний монитор</th><td><a href={links.wall}>открыть</a></td></tr>
                <tr><th>АРМ ДДС</th><td><a href={links.dds}>открыть</a></td></tr>
              </tbody>
            </table>
          )}
        </section>

        <section>
          <h2>Ход занятия</h2>
          {!sessionId && <div className="lesson-empty-state">
            <strong>Здесь появится ход занятия</strong>
            <span>После запуска здесь будут диалог, таймеры и карточка курсанта.</span>
          </div>}
          {sessionId && (
            <>
              <InterviewTimer timers={session.timers} />
              <p className="ref">
                канал: {session.status} · подсказок использовано: {session.hintsUsed}
              </p>
              <h3>Транскрипт — клик по реплике добавляет пометку</h3>
              <table className="grid">
                <tbody>
                  {session.transcript.map((line) => (
                    <tr key={line.ref} onClick={() => setNoteFor(line.ref)} className="clickable">
                      <th>{line.speaker === "caller" ? "звонящий" : "оператор"}</th>
                      <td>
                        {line.text}
                        {session.notes.has(line.ref) && (
                          <div className="ref">пометка: {session.notes.get(line.ref)}</div>
                        )}
                        {noteFor === line.ref && (
                          <div>
                            <input type="text" aria-label="Пометка преподавателя к реплике" value={noteText} autoFocus
                              placeholder="здесь надо было предупредить о прибытии"
                              onChange={(event) => setNoteText(event.target.value)} />{" "}
                            <button type="button" onClick={() => {
                              if (noteText.trim()) control.note(line.ref, noteText.trim());
                              setNoteText("");
                              setNoteFor(null);
                            }}>Сохранить</button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                  {session.transcript.length === 0 && <tr><td colSpan={2}>—</td></tr>}
                </tbody>
              </table>
              <h3>Нормативы</h3>
              <Timers timers={session.timers} />
              <h3>Карточка курсанта</h3>
              <p className="ref instructor-readonly-note">Это просмотр преподавателя: редактировать карточку здесь нельзя. После запуска откройте отдельную ссылку «АРМ курсанта» — ввод выполняется там.</p>
              {/* Только чтение: преподаватель управляет ситуацией, а не работой обучаемого. */}
              <KioCard state={session.card} required={session.requiredFields} readOnly />
            </>
          )}
        </section>

        <section>
          <h2>История группы</h2>
          <p><a href="/profile">профили курсантов</a> · <a href="/groups">аналитика группы</a></p>
          {history.data?.length ? <div className="lesson-history-list">
            {history.data.slice(0, 15).map((item) => {
              const scenario = scenarios.data?.find((candidate) => candidate.id === item.scenario_id);
              const completed = Boolean(item.ended_at);
              const dateLabel = sessionDateLabel(item.started_at ?? item.ended_at);
              return <a key={item.session_id} href={`/instructor?session=${item.session_id}`}>
                <strong>{scenario?.title ?? "Учебное занятие"}</strong>
                <span>{modeLabel(item.mode)} · попытка {item.attempt}{dateLabel && ` · ${dateLabel}`}</span>
                <small className={completed ? "lesson-history-complete" : "lesson-history-active"}>
                  {completed ? "Завершено · открыть результаты →" : "Занятие ещё идёт · открыть ход занятия →"}
                </small>
              </a>;
            })}
          </div> : <div className="lesson-history-empty">
            <strong>История занятий пока пуста</strong>
            <span>Запущенные занятия и их результаты появятся здесь автоматически.</span>
          </div>}
        </section>
      </div>

      <ScenarioEditor scenarios={scenarios.data ?? []} selectedScenarioId={scenarioId} onApproved={async (id) => {
        setTicketFilter("");
        setCategoryFilter("");
        setScenarioSearch("");
        await scenarios.refetch();
        setScenarioId(id);
      }} onLibraryChanged={async () => { await scenarios.refetch(); }} />

      {session.report && <>
        <Debrief report={overrideReport?.session_id === session.report.session_id
          ? overrideReport : session.report} />
        <FindingReview report={overrideReport?.session_id === session.report.session_id
          ? overrideReport : session.report} onChange={setOverrideReport} />
        <section className="debrief debrief-score-override">
          <h2>Ручная корректировка оценки</h2>
          <p className="ref">Автоматическая оценка: {session.report.score_auto.toFixed(1)} из 100. Меняйте её только при необходимости: причина сохранится в журнале вместе с вашим логином.</p>
          <label>Итоговая оценка, 0–100
            <input type="number" min={0} max={100} step={0.1}
              value={overrideScore}
              onChange={(event) => setOverrideScore(event.target.value)} />
          </label>
          <label>Обязательное обоснование
            <textarea rows={3} maxLength={2000} value={overrideComment}
              onChange={(event) => setOverrideComment(event.target.value)}
              placeholder="Почему итоговая оценка отличается от автоматической" />
          </label>
          <button type="button" disabled={overrideBusy || !overrideComment.trim()}
            onClick={() => void saveScoreOverride(session.report!)}>
            {overrideBusy ? "Сохраняем…" : "Сохранить оценку и обоснование"}
          </button>
          {overrideError && <p className="violated" role="alert">{overrideError}</p>}
        </section>
      </>}
    </main>
  );
}
