// АРМ курсанта: приём вызова и разговор голосом.
// Карточка КИО — lct-10, таймеры — lct-11.

import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

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
  const [textTurn, setTextTurn] = useState("");
  const [resolveComment, setResolveComment] = useState("");
  const [resolved, setResolved] = useState<"consultation" | "transfer_region" | null>(null);
  const sessionId = sessionIdFromUrl();
  const call = useCall(sessionId);
  const location = useLocation();
  const navigate = useNavigate();
  const isCompletionScreen = location.pathname === "/trainee/completed";

  useEffect(() => {
    if (call.report && sessionId && !isCompletionScreen) {
      navigate(`/trainee/completed?session=${sessionId}`, { replace: true });
    }
  }, [call.report, isCompletionScreen, navigate, sessionId]);

  if (!sessionId) {
    return (
      <main className="page">
        <h1>АРМ курсанта</h1>
        <p className="warn">В адресе нет номера занятия: откройте ссылку вида /trainee?session=…</p>
      </main>
    );
  }

  if (isCompletionScreen) {
    if (!call.report) return <main className="page completion-page">
      <h1>Занятие завершено</h1>
      {call.reportError
        ? <p role="alert" className="state-violated">{call.reportError}</p>
        : <p role="status">Получаем итог занятия…</p>}
      <Link to="/profile">Вернуться к профилю</Link>
    </main>;
    const report = call.report;
    const nextSteps = report.metrics.filter((metric) => !metric.passed).slice(0, 3);
    const completedCount = Math.max(0, report.metrics.length - report.failed_metrics);
    return <main className="page completion-page">
      <header className="completion-heading">
        <span>Упражнение завершено</span>
        <h1>{report.passed ? "Зачёт" : "Не зачтено"}</h1>
        <p>{report.exercise === "card" ? "Заполнение карточки КИО" : report.exercise === "dds" ? "Работа диспетчера ДДС" : "Приём вызова 112"} · {report.scenario_id}</p>
      </header>
      <section className={`completion-score ${report.passed ? "is-passed" : "is-failed"}`} aria-label="Итог занятия">
        <div><span>Ваш результат</span><strong>{report.score_final.toFixed(0)}<small> / 100</small></strong></div>
        <p>{completedCount} критериев выполнено · {report.failed_metrics} требуют внимания</p>
      </section>
      {nextSteps.length > 0 && <section className="completion-focus">
        <h2>Что потренировать в следующий раз</h2>
        <ol>{nextSteps.map((metric) => <li key={metric.key}><strong>{metric.title}</strong><span>{metric.fact}</span></li>)}</ol>
      </section>}
      <details className="completion-details">
        <summary>Подробный разбор занятия</summary>
        <Debrief report={report} />
      </details>
      <nav className="completion-actions" aria-label="Действия после занятия">
        <Link to="/profile">Вернуться к профилю</Link>
        <a href={`/api/sessions/${report.session_id}/report.pdf`}>Скачать отчёт (PDF)</a>
      </nav>
    </main>;
  }

  if (call.briefing) {
    const services = display(call.card, "notify");
    const notifiedServices = Array.isArray(services) ? services.map(String) : [];
    // Some already-open demo backends still send the old multi-line briefing.
    // Keep only the caller's opening line; never dump the answer key into view.
    const openingLine = call.briefing.text.split(/\r?\n/)[1]
      ?? call.briefing.text.split(/\r?\n/)[0]
      ?? "Алло? Вы меня слышите?";
    return <main className="page arm-page">
      <ModeBanner mode={call.briefing.mode} />
      <header className="arm-topbar arm-topbar-text">
        <div className="arm-topbar-mark">112</div>
        <div className="arm-topbar-title"><strong>Карточка информационного обмена</strong><span>Текстовое упражнение · сценарий {call.briefing.scenario_id}</span></div>
        <div className="arm-topbar-state"><span className={call.status === "open" ? "state-ok" : "state-warn"}>● {call.status === "open" ? "АРМ подключён" : STATUS_LABEL[call.status]}</span><small>{call.cardSubmitted ? "Карточка отправлена" : "Карточка заполняется"}</small></div>
      </header>
      <div className="arm-text-layout">
        <section className="arm-text-dialogue" aria-label="Текстовый диалог с заявителем">
          <header><strong>Диалог с заявителем</strong><span>{call.cardSubmitted ? "Диалог завершён" : "Задайте вопрос"}</span></header>
          <div className="arm-text-lines" aria-live="polite">
            {!call.lines.length && <p className="arm-text-line caller"><b>Заявитель</b>{openingLine}</p>}
            {call.lines.map((line, index) => <p key={`${index}-${line.speaker}`} className={`arm-text-line ${line.speaker}`}>
              <b>{line.speaker === "caller" ? "Заявитель" : "Вы"}</b>{line.text}
              {line.speaker === "caller" && <small className="arm-line-source">{line.source === "local_llm" ? "Qwen · ответ модели" : "Сценарный ответ"}</small>}
            </p>)}
          </div>
          <form onSubmit={(event) => {
            event.preventDefault();
            if (call.sendText(textTurn)) setTextTurn("");
          }}>
            <input aria-label="Ваша реплика заявителю" value={textTurn} maxLength={1000} disabled={call.paused}
              placeholder="Ваша реплика…" onChange={(event) => setTextTurn(event.target.value)} />
            <button type="submit" disabled={call.paused || call.status !== "open" || !textTurn.trim() || call.cardSubmitted}>Отправить</button>
          </form>
          {call.paused && <p role="status" className="state-warn">Пауза, ждите преподавателя</p>}
          {call.error && <p role="alert" className="state-violated">{call.error}</p>}
        </section>
        <div className="arm-card-wrap">
          <KioCard state={call.card} required={call.briefing.required_fields}
            readOnly={call.paused || call.cardSubmitted} referenceArm onChange={call.patchKio} />
        </div>
      </div>
      <section className="arm-support" aria-label="Норматив заполнения карточки">
        <Timers timers={call.timers} />
      </section>
      <footer className="arm-footer">
        <span className="arm-footer-services">Службы:
          {notifiedServices.length
            ? notifiedServices.map((service) => <b key={service}>{service}</b>)
            : <em>появятся после выбора признаков</em>}
        </span>
        {!call.cardSubmitted && <button type="button" className="arm-primary" disabled={call.paused || call.status !== "open"}
          onClick={call.submitCard}>Сохранить и проверить</button>}
        {call.cardSubmitted && <strong>{call.briefing.handoff_to_dds ? "Карточка передана ДДС" : "Карточка отправлена"}</strong>}
      </footer>
      {call.error && <p className="state-violated arm-message" role="alert">{call.error}</p>}
      {call.cardSubmitted && !call.report && !call.reportError && <p className="arm-message">
        {call.briefing.handoff_to_dds
          ? <>Карточка передана в ДДС. <a href={`/dds?session=${sessionId}&role=dds_01`}>Открыть АРМ ДДС</a> и завершить реагирование для итоговой оценки.</>
          : "Карточка отправлена. Ожидаем итоговую оценку…"}
      </p>}
      {call.reportError && <p className="state-violated arm-message" role="alert">{call.reportError}</p>}
      {call.report && <Debrief report={call.report} />}
    </main>;
  }

  const service = ddsCode(display(call.card, "dds"));
  const notify = display(call.card, "notify");
  const autoRoute = Boolean(display(call.card, "incident_code")) && Array.isArray(notify) && notify.length > 0;

  return (
    <main className="page arm-page">
      <StandStatus />
      <ModeBanner mode={call.incoming?.mode} />
      <header className="arm-topbar">
        <div className="arm-topbar-mark">112</div>
        <div className="arm-phone-cell"><small>Канал связи</small><strong className={call.status === "open" ? "state-ok" : "state-warn"}>{STATUS_LABEL[call.status]}</strong><span>{call.micOn ? "● Микрофон включён" : "Микрофон выключен"}</span></div>
        <div className="arm-phone-cell arm-phone-main"><small>Входящий вызов</small><strong>{call.incoming?.caller_number || "Ожидание вызова"}</strong><span>{call.incoming ? `Сценарий ${call.incoming.scenario_id} · ${call.callerSpeaking ? "заявитель говорит" : "заявитель молчит"}` : "Карточка не назначена"}</span></div>
        <div className="arm-topbar-title"><strong>АРМ оператора 112</strong><span>Карточка информационного обмена</span></div>
        <div className="arm-topbar-clock"><InterviewTimer timers={call.timers} /></div>
      </header>

      <div className="arm-call-actions">
        {(call.phase === "incoming" || call.phase === "talking") && !call.micOn && <button type="button" className="arm-answer" disabled={call.paused} onClick={() => void call.answer()}>
          {call.phase === "talking" ? "☎ Продолжить разговор" : "☎ Ответить на вызов"}
        </button>}
        {call.micOn && <span className="arm-live">● Разговор идёт</span>}
        {call.micOn && hintsAllowed(call.incoming?.mode) && <button type="button" disabled={call.paused} onClick={call.hint}>Подсказка</button>}
        <span className="arm-call-actions-note">{call.phase === "ended"
          ? "Вызов завершён"
          : !call.micOn && call.phase === "incoming"
            ? "Поля КИО разблокируются после ответа на входящий вызов"
            : !call.micOn && call.phase === "talking"
              ? "Разговор продолжается. Нажмите кнопку, чтобы включить звук и микрофон на этом устройстве"
            : call.micOn
              ? "Заполните адрес и признаки происшествия со слов заявителя"
              : "Ожидайте входящего вызова"}</span>
      </div>

      {call.paused && <p className="state-warn arm-message" role="status">Пауза, ждите преподавателя</p>}
      {call.error && <p className="violated arm-message" role="alert">{call.error}</p>}

      <div className="arm-card-wrap">
          <KioCard
            state={call.card}
            required={call.incoming?.required_fields ?? []}
            readOnly={call.paused || !call.micOn}
            referenceArm
            onChange={call.patchKio}
          />
      </div>
      <footer className="arm-footer">
        <span>Службы · список оповещения рассчитывается по признакам</span>
        <div className="arm-footer-actions">
            <button
              type="button"
              className="arm-primary"
              disabled={call.paused || !call.micOn || (!service && !autoRoute)}
              onClick={() => call.dispatch(service)}
            >
              Передать в ДДС
            </button>
            {call.micOn && <button type="button" disabled={call.paused} onClick={call.hangup}>Завершить вызов</button>}
        </div>
      </footer>
      <div className="arm-secondary">
          {/* Не каждый вызов заканчивается карточкой: справка и чужой регион —
              отдельные исходы, и курсант выбирает их сам (docs/spec/TICKETS.md). */}
          <details className="arm-resolve">
            <summary>Закрыть вызов без карточки</summary>
            <p>
              <input
                type="text"
                aria-label="Комментарий при закрытии вызова без карточки"
                value={resolveComment}
                disabled={call.paused}
                placeholder="что сказали заявителю, куда передали"
                onChange={(event) => setResolveComment(event.target.value)}
              />
            </p>
            <p>
              <button
                type="button"
                disabled={call.paused || !call.micOn || resolved !== null}
                onClick={() => {
                  call.resolve("consultation", resolveComment);
                  setResolved("consultation");
                }}
              >
                Справка, выезд не нужен
              </button>{" "}
              <button
                type="button"
                disabled={call.paused || !call.micOn || resolved !== null}
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
        <section className="arm-support">
          <h2>Нормативы и разговор</h2>
          <Timers timers={call.timers} />
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
      {call.phase === "ended" && !call.selfAssessed && call.checklist.length > 0 && (
        <SelfAssessment checklist={call.checklist} onSubmit={call.submitSelfAssessment} />
      )}
      {call.selfAssessed && !call.report && <p className="arm-message">Самооценка принята, оценка считается…</p>}
      {call.report && <Debrief report={call.report} />}
    </main>
  );
}
