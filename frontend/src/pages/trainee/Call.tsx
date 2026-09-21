// АРМ курсанта: приём вызова и разговор голосом.
// Карточка КИО — lct-10, таймеры — lct-11.

import { useState } from "react";

import { KioCard } from "@/features/kio-card/KioCard";
import { StandStatus } from "@/features/health/StandStatus";
import { ModeBanner, hintsAllowed } from "@/features/mode-banner/ModeBanner";
import { Debrief } from "@/features/debrief/Debrief";
import { SelfAssessment } from "@/features/self-assessment/SelfAssessment";
import { InterviewTimer, Timers } from "@/features/timers/Timers";
import { display } from "@/features/kio-card/merge";
import { useCall } from "@/features/call/useCall";
import { sessionIdFromUrl } from "@/shared/api/session";
import type { DDSCode } from "@/shared/types/generated";

const STATUS_LABEL: Record<string, string> = {
  connecting: "подключение",
  open: "на связи",
  reconnecting: "связь потеряна, переподключение",
  closed: "отключено",
};

const RESOLVE_LABELS = {
  consultation: "справочно-консультационная помощь",
  transfer_region: "передан в систему-112 другого региона",
} as const;

function ddsCode(value: unknown): DDSCode | undefined {
  return value === "01" || value === "02" || value === "03" || value === "04" || value === "gkh"
    ? value : undefined;
}

export function Call() {
  const [resolveComment, setResolveComment] = useState("");
  const [resolved, setResolved] = useState<"consultation" | "transfer_region" | null>(null);
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

  if (call.briefing) {
    return <main className="page">
      <ModeBanner mode={call.briefing.mode} />
      <h1>АРМ оператора 112 · учебная карточка</h1>
      <p className="ref">Текстовое упражнение: входящий голосовой вызов и микрофон не используются.</p>
      <table className="grid"><tbody>
        <tr><th>Канал</th><td className={call.status === "open" ? "state-ok" : "state-warn"}>{STATUS_LABEL[call.status]}</td></tr>
        <tr><th>Сценарий</th><td>{call.briefing.scenario_id}</td></tr>
        <tr><th>Состояние</th><td>{call.cardSubmitted ? "карточка отправлена на оценку" : "карточка заполняется"}</td></tr>
      </tbody></table>
      <section style={{ maxWidth: 900, marginTop: 16 }}>
        <h2>Вводная о происшествии</h2>
        <div style={{ background: "#fff", border: "1px solid var(--line)", padding: 16, whiteSpace: "pre-wrap", lineHeight: 1.5 }}>
          {call.briefing.text}
        </div>
        <p className="ref">Извлеките сведения из вводной, заполните поля карточки и проверьте предлагаемый список оповещения.</p>
        <h2>Карточка информационного обмена</h2>
        <KioCard state={call.card} required={call.briefing.required_fields}
          readOnly={call.cardSubmitted || call.status !== "open"} onChange={call.patchKio} />
        {!call.cardSubmitted && <p><button type="button" className="start" disabled={call.status !== "open"}
          onClick={call.submitCard}>Отправить карточку на проверку</button></p>}
        {call.error && <p className="state-violated" role="alert">{call.error}</p>}
        {call.cardSubmitted && !call.report && !call.reportError && <p>Карточка отправлена. Ожидаем итоговую оценку…</p>}
        {call.reportError && <p className="state-violated" role="alert">{call.reportError}</p>}
      </section>
      {call.report && <Debrief report={call.report} />}
    </main>;
  }

  const service = ddsCode(display(call.card, "dds"));
  const notify = display(call.card, "notify");
  const autoRoute = Boolean(display(call.card, "incident_code")) && Array.isArray(notify) && notify.length > 0;

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
              disabled={!call.micOn || (!service && !autoRoute)}
              onClick={() => call.dispatch(service)}
            >
              Передать в ДДС
            </button>
          </p>
          {/* Не каждый вызов заканчивается карточкой: справка и чужой регион —
              отдельные исходы, и курсант выбирает их сам (docs/spec/TICKETS.md). */}
          <details>
            <summary>Закрыть вызов без карточки</summary>
            <p>
              <input
                type="text"
                value={resolveComment}
                placeholder="что сказали заявителю, куда передали"
                onChange={(event) => setResolveComment(event.target.value)}
              />
            </p>
            <p>
              <button
                type="button"
                disabled={!call.micOn || resolved !== null}
                onClick={() => {
                  call.resolve("consultation", resolveComment);
                  setResolved("consultation");
                }}
              >
                Справка, выезд не нужен
              </button>{" "}
              <button
                type="button"
                disabled={!call.micOn || resolved !== null}
                onClick={() => {
                  call.resolve("transfer_region", resolveComment);
                  setResolved("transfer_region");
                }}
              >
                Передать в другой регион
              </button>
            </p>
            {resolved && <p className="warn">Вызов закрыт: {RESOLVE_LABELS[resolved]}</p>}
          </details>
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
