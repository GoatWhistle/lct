// Пульт преподавателя.
//
// Компоновка от того, по чему его оценит действующий преподаватель: задать
// сценарий и не вмешиваться, видеть работу курсанта вживую, получить готовый
// разбор, показать группе (docs/arch/FRONTEND.md).

import { useEffect, useRef, useState } from "react";

import { Debrief } from "@/features/debrief/Debrief";
import { useControl } from "@/features/instructor/useControl";
import { KioCard } from "@/features/kio-card/KioCard";
import { ModeBanner } from "@/features/mode-banner/ModeBanner";
import { useObserve } from "@/features/observe/useObserve";
import { InterviewTimer, Timers } from "@/features/timers/Timers";
import { useScenarios, useSessions } from "@/shared/api/http";
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
  const [mode, setMode] = useState<SessionMode>("training");
  const [trainee, setTrainee] = useState("");
  const [noteFor, setNoteFor] = useState<string | null>(null);
  // Запускается только занятие, созданное этой кнопкой. Открытая из истории
  // ссылка на идущее занятие не должна запускать его заново и сбрасывать.
  const toStart = useRef<string | null>(null);
  const [noteText, setNoteText] = useState("");

  const scenarios = useScenarios();
  const history = useSessions();
  const session = useObserve(sessionId);
  const control = useControl(sessionId);

  useEffect(() => {
    if (scenarios.data?.length && !scenarioId) setScenarioId(scenarios.data[0].id);
  }, [scenarios.data, scenarioId]);

  function startLesson() {
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
    control.start(scenarioId, mode, trainee || "Курсант");
  }, [sessionId, control, scenarioId, mode, trainee]);

  const links = sessionId
    ? {
        trainee: `${location.origin}/trainee?session=${sessionId}`,
        wall: `${location.origin}/wall?session=${sessionId}`,
      }
    : null;

  return (
    <main className="page">
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
                  <select value={scenarioId} onChange={(event) => setScenarioId(event.target.value)}>
                    {scenarios.data?.map((scenario) => (
                      <option key={scenario.id} value={scenario.id}>
                        {scenario.title} · {scenario.level}
                      </option>
                    ))}
                  </select>
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
                  <input type="text" value={trainee} placeholder="фамилия"
                    onChange={(event) => setTrainee(event.target.value)} />
                </td>
              </tr>
            </tbody>
          </table>
          <p>
            <button type="button" className="start" onClick={startLesson} disabled={!scenarioId}>
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
          {links && (
            <table className="grid">
              <tbody>
                <tr><th>АРМ курсанта</th><td><a href={links.trainee}>открыть</a></td></tr>
                <tr><th>Внешний монитор</th><td><a href={links.wall}>открыть</a></td></tr>
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

      {session.report && <Debrief report={session.report} />}
    </main>
  );
}
