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
import { useScenarios, useSessions, useTrainees } from "@/shared/api/http";
import { sessionIdFromUrl } from "@/shared/api/session";
import type { SessionMode } from "@/shared/types/generated";

const MODES: { value: SessionMode; label: string }[] = [
  { value: "training", label: "тренировочный" },
  { value: "exam", label: "контрольный" },
  { value: "self", label: "самостоятельный" },
];

export function Instructor() {
  const [sessionId, setSessionId] = useState<string | null>(sessionIdFromUrl());
  const [scenarioId, setScenarioId] = useState("");
  const [extraScenarioIds, setExtraScenarioIds] = useState<string[]>([]);
  const [mode, setMode] = useState<SessionMode>("training");
  const [exercise, setExercise] = useState<"dds" | "call" | "card">("dds");
  const [trainee, setTrainee] = useState("");
  const [traineeId, setTraineeId] = useState("");
  const [noteFor, setNoteFor] = useState<string | null>(null);
  // Запускается только занятие, созданное этой кнопкой. Открытая из истории
  // ссылка на идущее занятие не должна запускать его заново и сбрасывать.
  const toStart = useRef<string | null>(null);
  const [noteText, setNoteText] = useState("");

  const scenarios = useScenarios();
  const history = useSessions();
  const trainees = useTrainees();
  const session = useObserve(sessionId);
  const control = useControl(sessionId);

  useEffect(() => {
    if (scenarios.data?.length && !scenarioId) setScenarioId(scenarios.data[0].id);
  }, [scenarios.data, scenarioId]);

  function startLesson() {
    if (trainees.data?.length && !traineeId) return;
    const id = crypto.randomUUID();
    // Номер занятия живёт в адресе: монитор и АРМ курсанта открываются ссылкой.
    window.history.replaceState(null, "", `/instructor?session=${id}`);
    toStart.current = id;
    setSessionId(id);
  }

  // Занятие запускается, когда канал управления открылся.
  useEffect(() => {
    if (!sessionId || control.status !== "open" || toStart.current !== sessionId) return;
    toStart.current = null;
    const selected = trainees.data?.find((item) => item.id === traineeId);
    const scenarioIds = exercise === "dds"
      ? [scenarioId, ...extraScenarioIds.filter((id) => id !== scenarioId)] : undefined;
    control.start(scenarioId, mode, selected?.name || trainee || "Демо-курсант", exercise,
      undefined, selected?.id, scenarioIds && scenarioIds.length > 1 ? scenarioIds : undefined);
  }, [sessionId, control, scenarioId, extraScenarioIds, mode, trainee, traineeId, trainees.data, exercise]);

  const links = sessionId
    ? {
        trainee: `${location.origin}/trainee?session=${sessionId}`,
        wall: `${location.origin}/wall?session=${sessionId}`,
        dds: `${location.origin}/dds?session=${sessionId}&role=dds_01`,
      }
    : null;

  return (
    <main className="page">
      <StandStatus />
      <ModeBanner mode={session.mode ?? mode} />
      <h1>Рабочее место преподавателя</h1>

      <div className="columns">
        <section style={{ flex: "0 0 280px" }}>
          <h2>Занятие</h2>
          <table className="grid">
            <tbody>
              <tr>
                <th>Сценарий</th>
                <td>
                  <select value={scenarioId} onChange={(event) => {
                    setScenarioId(event.target.value);
                    setExtraScenarioIds((previous) => previous.filter((id) => id !== event.target.value));
                  }}>
                    {scenarios.data?.map((scenario) => (
                      <option key={scenario.id} value={scenario.id}>
                        {scenario.title} · {scenario.level}
                      </option>
                    ))}
                  </select>
                  {exercise === "dds" && <details className="scenario-queue">
                    <summary>Дополнительные карточки ({extraScenarioIds.length})</summary>
                    <p className="ref">Карточки поступят последовательно после завершения текущей.</p>
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
                        <option key={item.id} value={item.id}>{item.name}{item.group ? ` · ${item.group}` : ""}</option>
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
            </tbody>
          </table>
          <p>
            <button type="button" className="start" onClick={startLesson}
              disabled={!scenarioId || trainees.isLoading || Boolean(trainees.data?.length && !traineeId)}>
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
          {links && (
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

        <section style={{ flex: "0 0 260px" }}>
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

      <ScenarioEditor scenarios={scenarios.data ?? []} onApproved={async (id) => {
        setScenarioId(id);
        await scenarios.refetch();
      }} />

      {session.report && <Debrief report={session.report} />}
    </main>
  );
}
