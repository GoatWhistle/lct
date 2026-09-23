// Пульт преподавателя.
//
// Компоновка от того, по чему его оценит действующий преподаватель: задать
// сценарий и не вмешиваться, видеть работу курсанта вживую, получить готовый
// разбор, показать группе (docs/arch/FRONTEND.md).

import { useEffect, useRef, useState } from "react";

import { Debrief } from "@/features/debrief/Debrief";
import { Director } from "@/features/instructor/Director";
import { ScenarioEditor } from "@/features/instructor/ScenarioEditor";
import { useControl } from "@/features/instructor/useControl";
import { KioCard } from "@/features/kio-card/KioCard";
import { StandStatus } from "@/features/health/StandStatus";
import { ModeBanner } from "@/features/mode-banner/ModeBanner";
import { useObserve } from "@/features/observe/useObserve";
import { InterviewTimer, Timers } from "@/features/timers/Timers";
import { useIncidentGroups, useScenarios, useSessions, useTrainees } from "@/shared/api/http";
import { sessionIdFromUrl } from "@/shared/api/session";
import type { SessionMode, SessionReport } from "@/shared/types/generated";

const MODES: { value: SessionMode; label: string }[] = [
  { value: "training", label: "тренировочный" },
  { value: "exam", label: "контрольный" },
  { value: "self", label: "самостоятельный" },
];

export function Instructor() {
  const [sessionId, setSessionId] = useState<string | null>(sessionIdFromUrl());
  const [scenarioId, setScenarioId] = useState("");
  const [ticketFilter, setTicketFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [scenarioSearch, setScenarioSearch] = useState("");
  const [extraScenarioIds, setExtraScenarioIds] = useState<string[]>([]);
  const [mode, setMode] = useState<SessionMode>("training");
  const [exercise, setExercise] = useState<"dds" | "call" | "card" | "card_dds">("dds");
  const [decisionTimeLimit, setDecisionTimeLimit] = useState(30);
  const [allowedErrors, setAllowedErrors] = useState(0);
  const [requireCorrectGrammar, setRequireCorrectGrammar] = useState(true);
  const [scoreWeightsText, setScoreWeightsText] = useState("{}");
  const [scoreWeightsError, setScoreWeightsError] = useState("");
  const [lessonScoreWeights, setLessonScoreWeights] = useState<Record<string, number>>({});
  const [ddsArrivalIntervalSeconds, setDdsArrivalIntervalSeconds] = useState(60);
  const [ddsMaxWaiting, setDdsMaxWaiting] = useState(3);
  const [trainee, setTrainee] = useState("");
  const [traineeId, setTraineeId] = useState("");
  const [noteFor, setNoteFor] = useState<string | null>(null);
  // Запускается только занятие, созданное этой кнопкой. Открытая из истории
  // ссылка на идущее занятие не должна запускать его заново и сбрасывать.
  const toStart = useRef<string | null>(null);
  const [noteText, setNoteText] = useState("");
  const [overrideReport, setOverrideReport] = useState<SessionReport | null>(null);
  const [overrideScore, setOverrideScore] = useState("");
  const [overrideComment, setOverrideComment] = useState("");
  const [overrideError, setOverrideError] = useState("");
  const [overrideBusy, setOverrideBusy] = useState(false);

  const scenarios = useScenarios();
  const incidentGroups = useIncidentGroups();
  const history = useSessions();
  const trainees = useTrainees();
  const session = useObserve(sessionId);
  const control = useControl(sessionId);

  useEffect(() => {
    if (!session.report) return;
    setOverrideReport(null);
    setOverrideScore(String(session.report.score_final));
    setOverrideComment(session.report.override_comment ?? "");
  }, [session.report?.session_id, session.report?.score_final, session.report?.override_comment]);

  const visibleScenarios = (scenarios.data ?? []).filter((item) =>
    (!ticketFilter || item.ticket === Number(ticketFilter)) &&
    (!categoryFilter || item.ekp_group === Number(categoryFilter)) &&
    (!scenarioSearch.trim() || `${item.title} ${item.id}`.toLocaleLowerCase("ru-RU")
      .includes(scenarioSearch.trim().toLocaleLowerCase("ru-RU")))
  ).sort((a, b) => (a.ticket ?? 99) - (b.ticket ?? 99) || (a.position ?? 0) - (b.position ?? 0));
  const selectedTicketScenarios = ticketFilter
    ? (scenarios.data ?? []).filter((item) => item.ticket === Number(ticketFilter))
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    : [];
  const [wholeTicket, setWholeTicket] = useState(false);
  const canStartSelectedExercise = exercise === "dds" && wholeTicket
    ? selectedTicketScenarios.length === 3
    : Boolean(scenarioId);

  useEffect(() => {
    if (!visibleScenarios.some((item) => item.id === scenarioId)) {
      setScenarioId(visibleScenarios[0]?.id ?? "");
    }
  }, [scenarios.data, ticketFilter, categoryFilter, scenarioSearch, scenarioId]);

  function startLesson() {
    if (trainees.data?.length && !traineeId) return;
    let parsedWeights: unknown;
    try {
      parsedWeights = JSON.parse(scoreWeightsText);
    } catch {
      setScoreWeightsError("Введите корректный JSON, например {\"dds_ack\": 2}.");
      return;
    }
    if (!parsedWeights || typeof parsedWeights !== "object" || Array.isArray(parsedWeights) ||
        Object.values(parsedWeights).some((value) => typeof value !== "number" ||
          !Number.isFinite(value) || value < 0 || value > 10)) {
      setScoreWeightsError("Задайте объект «ключ метрики: вес от 0 до 10».");
      return;
    }
    setScoreWeightsError("");
    setLessonScoreWeights(parsedWeights as Record<string, number>);
    const id = crypto.randomUUID();
    // Номер занятия живёт в адресе: монитор и АРМ курсанта открываются ссылкой.
    window.history.replaceState(null, "", `/instructor?session=${id}`);
    toStart.current = id;
    setSessionId(id);
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

  // Занятие запускается, когда канал управления открылся.
  useEffect(() => {
    if (!sessionId || control.status !== "open" || toStart.current !== sessionId) return;
    toStart.current = null;
    const selected = trainees.data?.find((item) => item.id === traineeId);
    const primaryScenarioId = exercise === "dds" && wholeTicket
      ? selectedTicketScenarios[0]?.id ?? scenarioId : scenarioId;
    const scenarioIds = exercise === "dds"
      ? (wholeTicket
          ? selectedTicketScenarios.map((item) => item.id)
          : [scenarioId, ...extraScenarioIds.filter((id) => id !== scenarioId)])
          .filter((id, index, all) => id && all.indexOf(id) === index)
      : undefined;
    control.start(primaryScenarioId, mode, selected?.name || trainee || "Демо-курсант",
      exercise === "card_dds" ? "card" : exercise,
      undefined, selected?.id, selected?.service ?? undefined,
      scenarioIds && scenarioIds.length > 1 ? scenarioIds : undefined,
      ddsArrivalIntervalSeconds, ddsMaxWaiting,
      exercise === "card_dds", {
        decision_time_limit_seconds: decisionTimeLimit,
        allowed_errors: allowedErrors,
        require_correct_grammar: requireCorrectGrammar,
        score_weights: lessonScoreWeights,
      });
  }, [sessionId, control, scenarioId, extraScenarioIds, mode, trainee, traineeId, trainees.data,
    exercise, decisionTimeLimit, allowedErrors, requireCorrectGrammar, wholeTicket, selectedTicketScenarios,
    ddsArrivalIntervalSeconds, ddsMaxWaiting, lessonScoreWeights]);

  const links = sessionId
    ? {
        trainee: `${location.origin}/trainee?session=${sessionId}`,
        wall: `${location.origin}/wall?session=${sessionId}`,
        dds: `${location.origin}/dds?session=${sessionId}&role=dds_01`,
      }
    : null;

  return (
    <main className="page instructor-page">
      <StandStatus />
      <ModeBanner mode={session.mode ?? mode} />
      <h1>Рабочее место преподавателя</h1>
      <p className="workspace-links">
        <a href="#ai-scenario-editor">Перейти к ИИ-формирователю сценария ↓</a>
        <a href="/materials">Учебные материалы и назначения →</a>
      </p>

      <div className="instructor-layout">
        <section>
          <h2>Занятие</h2>
          <table className="grid">
            <tbody>
              <tr>
                <th>Сценарий</th>
                <td>
                  <div className="instructor-scenario-filters">
                    <select aria-label="Категория происшествия" value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)}>
                      <option value="">Все 23 категории ЕКП</option>
                      {incidentGroups.data?.groups.map((group) =>
                        <option key={group.id} value={group.id}>{group.title}</option>)}
                    </select>
                    <select aria-label="Билет заказчика" value={ticketFilter} onChange={(event) => {
                      setTicketFilter(event.target.value);
                      setWholeTicket(false);
                    }}>
                      <option value="">Все 32 билета</option>
                      {Array.from({ length: 32 }, (_, index) => <option key={index} value={index + 1}>Билет {index + 1}</option>)}
                    </select>
                    <input type="search" aria-label="Поиск сценария" value={scenarioSearch}
                      placeholder="Название или ID карточки" onChange={(event) => setScenarioSearch(event.target.value)} />
                  </div>
                  <select value={scenarioId} disabled={exercise === "dds" && wholeTicket} onChange={(event) => {
                    setScenarioId(event.target.value);
                    setExtraScenarioIds((previous) => previous.filter((id) => id !== event.target.value));
                  }}>
                    {!visibleScenarios.length && <option value="">Карточки не найдены</option>}
                    {visibleScenarios.map((scenario) => (
                      <option key={scenario.id} value={scenario.id}>
                        {scenario.ticket ? `Билет ${scenario.ticket}, вызов ${scenario.position} · ` : ""}{scenario.title} · {scenario.level}
                      </option>
                    ))}
                  </select>
                  <small className="ref">{visibleScenarios.length} из {scenarios.data?.length ?? 0} карточек (96 — билеты заказчика)</small>
                  {exercise === "dds" && ticketFilter && <label className="mode-choice">
                    <input type="checkbox" checked={wholeTicket && selectedTicketScenarios.length === 3}
                      disabled={selectedTicketScenarios.length !== 3}
                      onChange={(event) => {
                        if (event.target.checked && selectedTicketScenarios[0]) {
                          setScenarioId(selectedTicketScenarios[0].id);
                          setExtraScenarioIds([]);
                        }
                        setWholeTicket(event.target.checked);
                      }} />
                    {` Запустить весь билет ${ticketFilter} целиком (3 вызова)`}
                    {selectedTicketScenarios.length !== 3 && <small className="ref"> — в библиотеке найдено {selectedTicketScenarios.length} из 3 вызовов</small>}
                  </label>}
                  {exercise === "dds" && !wholeTicket && <details className="scenario-queue">
                    <summary>Дополнительные карточки ({extraScenarioIds.length})</summary>
                    <p className="ref">Интервал и максимум ожидающих задаются в критериях занятия. Таймер каждой карточки начинается при поступлении и идёт, пока курсант занят другой.</p>
                    {scenarios.data?.filter((scenario) => scenario.id !== scenarioId).map((scenario) =>
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
                <th>Упражнение</th>
                <td>
                  <label className="mode-choice"><input type="radio" checked={exercise === "dds"}
                    onChange={() => setExercise("dds")} /> Диспетчер ДДС · готовая карточка</label>
                  <label className="mode-choice"><input type="radio" checked={exercise === "call"}
                    onChange={() => setExercise("call")} /> Оператор 112 · звонок</label>
                  <label className="mode-choice"><input type="radio" checked={exercise === "card"}
                    onChange={() => setExercise("card")} /> Оператор 112 · текстовая вводная</label>
                  <label className="mode-choice"><input type="radio" checked={exercise === "card_dds"}
                    onChange={() => setExercise("card_dds")} /> Связка 112 → ДДС · одна карточка</label>
                </td>
              </tr>
              <tr>
                <th>Режим</th>
                <td>
                  {MODES.map((item) => (
                    <label key={item.value} className="mode-choice">
                      <input
                        type="radio"
                        checked={mode === item.value}
                        onChange={() => setMode(item.value)}
                      />{" "}
                      {item.label}
                    </label>
                  ))}
                </td>
              </tr>
              <tr>
                <th>Курсант</th>
                <td>
                  {trainees.data?.length ? (
                    <select value={traineeId} onChange={(event) => setTraineeId(event.target.value)}>
                      <option value="">Выберите учётную запись</option>
                      {trainees.data.map((item) => (
                        <option key={item.id} value={item.id}>{item.name}{item.group ? ` · ${item.group}` : ""}{item.service ? ` · ${item.service}` : " · служба не назначена"}</option>
                      ))}
                    </select>
                  ) : (
                    <>
                      <input type="text" value={trainee} placeholder="имя для демо"
                        onChange={(event) => setTrainee(event.target.value)} />
                      <small className="ref">Без учётной записи: личный отчёт курсанту недоступен.</small>
                    </>
                  )}
                </td>
              </tr>
              <tr>
                <th>Критерии занятия</th>
                <td>
                  {exercise === "dds" && <>
                    <label className="lesson-criterion">
                      Интервал поступления карточек, секунд (0 — без задержки; действует лимит ожидания)
                      <input aria-label="Интервал поступления карточек ДДС, секунд" type="number" min={0} max={300}
                        value={ddsArrivalIntervalSeconds} onChange={(event) => setDdsArrivalIntervalSeconds(
                          Math.max(0, Math.min(300, Number(event.target.value) || 0)))} />
                    </label>
                    <label className="lesson-criterion">
                      Максимум ожидающих карточек
                      <input aria-label="Максимум ожидающих карточек ДДС" type="number" min={1} max={10}
                        value={ddsMaxWaiting} onChange={(event) => setDdsMaxWaiting(
                          Math.max(1, Math.min(10, Number(event.target.value) || 1)))} />
                    </label>
                  </>}
                  <label className="lesson-criterion">
                    Решение ДДС, секунд
                    <input aria-label="Лимит решения ДДС, секунд" type="number" min={5} max={300}
                      value={decisionTimeLimit} onChange={(event) => setDecisionTimeLimit(
                        Math.max(5, Math.min(300, Number(event.target.value) || 30)))} />
                  </label>
                  <label className="lesson-criterion">
                    Допустимо нарушенных метрик
                    <input aria-label="Допустимое число ошибок" type="number" min={0} max={50}
                      value={allowedErrors} onChange={(event) => setAllowedErrors(
                        Math.max(0, Math.min(50, Number(event.target.value) || 0)))} />
                  </label>
                  {exercise !== "dds" && <label className="mode-choice">
                    <input type="checkbox" checked={requireCorrectGrammar}
                      onChange={(event) => setRequireCorrectGrammar(event.target.checked)} />
                    Грамматика карточки оператора 112 входит в оценку
                  </label>}
                  <label className="lesson-criterion">
                    Переопределения весов метрик (JSON; 0 отключает метрику)
                    <textarea aria-label="Переопределения весов метрик" rows={3}
                      value={scoreWeightsText}
                      onChange={(event) => { setScoreWeightsText(event.target.value); setScoreWeightsError(""); }}
                      placeholder={'{"address": 3, "dds_ack": 2}'} spellCheck={false} />
                    <small className="ref">Оставьте {"{}"} для стандартной шкалы. Диапазон каждого веса — 0–10; допустимые ключи проверяются сервером.</small>
                  </label>
                  {scoreWeightsError && <p className="violated" role="alert">{scoreWeightsError}</p>}
                  <small className="ref">Нормативы ГОСТ для приёма вызова не изменяются.</small>
                </td>
              </tr>
            </tbody>
          </table>
          <p>
            <button type="button" className="start" onClick={startLesson}
              disabled={!canStartSelectedExercise || trainees.isLoading || Boolean(trainees.data?.length && !traineeId)}>
              Начать занятие
            </button>
          </p>
          {sessionId && (
            <p>
              {session.ended ? "занятие завершено" : (
                <button type="button" onClick={control.stop}>Завершить занятие</button>
              )}
            </p>
          )}
          {sessionId && !session.ended && (
            exercise === "call" && <Director onInject={control.inject} disabled={control.status !== "open"} error={session.error} />
          )}
          {links && !session.title && !session.error && (
            <p className="ref" role="status">Запускаем занятие и готовим ссылки на рабочие места…</p>
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
          {!sessionId && <p>Занятие не запущено.</p>}
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
                            <input type="text" value={noteText} autoFocus
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
              {/* Только чтение: преподаватель управляет ситуацией, а не работой обучаемого. */}
              <KioCard state={session.card} required={session.requiredFields} readOnly />
            </>
          )}
        </section>

        <section>
          <h2>История группы</h2>
          <p><a href="/profile">профили курсантов</a> · <a href="/groups">аналитика группы</a></p>
          <table className="grid">
            <tbody>
              {history.data?.slice(0, 15).map((item) => (
                <tr key={item.session_id}>
                  <th>{item.mode} · попытка {item.attempt}</th>
                  <td><a href={`/instructor?session=${item.session_id}`}>{item.scenario_id}</a></td>
                </tr>
              ))}
              {!history.data?.length && <tr><td colSpan={2}>занятий ещё не было</td></tr>}
            </tbody>
          </table>
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
        <section className="debrief">
          <h2>Корректировка итоговой оценки</h2>
          <p className="ref">Автооценка {session.report.score_auto.toFixed(1)} / 100. Изменение сохраняется в журнале с вашим логином и обоснованием.</p>
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
            {overrideBusy ? "Сохраняем…" : "Сохранить оценку с аудитом"}
          </button>
          {overrideError && <p className="violated" role="alert">{overrideError}</p>}
        </section>
        <Debrief report={overrideReport?.session_id === session.report.session_id
          ? overrideReport : session.report} />
      </>}
    </main>
  );
}
