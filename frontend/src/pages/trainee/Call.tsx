// АРМ курсанта: приём вызова и разговор голосом.
// Карточка КИО — lct-10, таймеры — lct-11.

import { KioCard } from "@/features/kio-card/KioCard";
import { StandStatus } from "@/features/health/StandStatus";
import { ModeBanner, hintsAllowed } from "@/features/mode-banner/ModeBanner";
import { Debrief } from "@/features/debrief/Debrief";
import { SelfAssessment } from "@/features/self-assessment/SelfAssessment";
import { InterviewTimer, Timers } from "@/features/timers/Timers";
import { display } from "@/features/kio-card/merge";
import { useCall } from "@/features/call/useCall";
import { sessionIdFromUrl } from "@/shared/api/session";

const STATUS_LABEL: Record<string, string> = {
  connecting: "подключение",
  open: "на связи",
  reconnecting: "связь потеряна, переподключение",
  closed: "отключено",
};

export function Call() {
  const sessionId = sessionIdFromUrl();
  const call = useCall(sessionId);

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
      <StandStatus />
      <ModeBanner mode={call.incoming?.mode} />
      <h1>АРМ оператора 112</h1>
      <InterviewTimer timers={call.timers} />
      <table className="grid">
        <tbody>
          <tr><th>Канал</th><td className={call.status === "open" ? "" : "warn"}>{STATUS_LABEL[call.status]}</td></tr>
          <tr><th>Вызов</th><td>{call.incoming ? `${call.incoming.caller_number} · сценарий ${call.incoming.scenario_id} · режим ${call.incoming.mode}` : "ожидание"}</td></tr>
          <tr><th>Гарнитура</th><td>{call.micOn ? "микрофон включён" : "выключен"}</td></tr>
          <tr><th>Звонящий</th><td>{call.callerSpeaking ? "говорит" : "молчит"}</td></tr>
        </tbody>
      </table>

      <p>
        {call.phase === "incoming" && !call.micOn && (
          <button type="button" onClick={() => void call.answer()}>Ответить</button>
        )}
        {call.micOn && <button type="button" onClick={call.hangup}>Завершить вызов</button>}{" "}
        {call.micOn && hintsAllowed(call.incoming?.mode) && (
          <button type="button" onClick={call.hint}>Подсказка</button>
        )}
      </p>

      {call.error && <p className="violated">{call.error}</p>}

      {call.phase === "ended" && !call.selfAssessed && call.checklist.length > 0 && (
        <SelfAssessment checklist={call.checklist} onSubmit={call.submitSelfAssessment} />
      )}
      {call.selfAssessed && !call.report && <p>Самооценка принята, оценка считается…</p>}
      {call.report && <Debrief report={call.report} />}

      <div className="columns">
        <section>
          <h2>Карточка информационного обмена</h2>
          <KioCard
            state={call.card}
            required={call.incoming?.required_fields ?? []}
            readOnly={!call.micOn}
            onChange={call.patchKio}
          />
          <p>
            <button
              type="button"
              disabled={!call.micOn || !display(call.card, "dds")}
              onClick={() => call.dispatch(display(call.card, "dds") as never)}
            >
              Передать в ДДС
            </button>
          </p>
        </section>
        <section>
          <h2>Нормативы</h2>
          <Timers timers={call.timers} />
      <h2>Разговор</h2>
      <table className="grid">
        <tbody>
          {call.lines.map((line, index) => (
            <tr key={index}>
              <th>{line.speaker === "caller" ? "звонящий" : "оператор"}</th>
              <td className={line.partial ? "warn" : ""}>{line.text}</td>
            </tr>
          ))}
          {call.lines.length === 0 && <tr><td colSpan={2}>—</td></tr>}
        </tbody>
      </table>
        </section>
      </div>
    </main>
  );
}
