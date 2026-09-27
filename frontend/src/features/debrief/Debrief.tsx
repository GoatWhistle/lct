// Разбор после звонка — главный учебный момент занятия.
//
// Ни одной отметки без обоснования: у каждой есть код, факт и норматив.
// По каждому пропущенному пункту видно, каким был эталонный вопрос
// (docs/product/DEBRIEF.md).

import type { SessionReport } from "@/shared/types/generated";

import { Radar } from "./Radar";

export function Debrief({ report, big = false }: { report: SessionReport; big?: boolean }) {
  const questions = new Map(report.reference_questions.map((item) => [item.checklist_id, item.question]));
  const diff = report.self_assessment_diff;
  // Поля со значениями по умолчанию приходят из контракта необязательными.
  const unnoticed = diff?.unnoticed ?? [];
  const noticed = diff?.noticed ?? [];
  const overcautious = diff?.overcautious ?? [];
  const notes = report.notes ?? [];
  const passedMetrics = Math.max(0, report.metrics.length - report.failed_metrics);
  const improvementPoints = report.metrics.filter((metric) => !metric.passed).slice(0, 3);
  const metricTitles = new Map(report.metrics.map((metric) => [metric.key, metric.title]));
  const coaching = report.ai_coaching?.status === "ready" ? report.ai_coaching.recommendations ?? [] : [];

  return (
    <section className={big ? "debrief debrief-big" : "debrief"}>
      <h2>Результаты занятия</h2>
      <div className="debrief-summary" aria-label="Краткие итоги занятия">
        <article className={report.passed ? "debrief-stat is-passed" : "debrief-stat is-failed"}>
          <span>Итог</span><strong>{report.passed ? "Зачёт" : "Не зачтено"}</strong>
          <small>Критерии: выполнено {passedMetrics} из {report.metrics.length}</small>
          <small>Допустимо не выполнить: {report.criteria.allowed_errors}</small>
        </article>
        <article className="debrief-stat debrief-score">
          <span>Ваша итоговая оценка</span><strong>{report.score_final.toFixed(0)}<small> из 100</small></strong>
          {report.overridden_by && <small>Изменено: {report.overridden_by}</small>}
        </article>
        <article className="debrief-stat">
          <span>Автоматическая оценка</span><strong>{report.score_auto.toFixed(0)}<small> из 100</small></strong>
          <small>{report.overridden_by ? "до корректировки преподавателя" : "расчёт по критериям занятия"}</small>
          {report.score_reviewed != null && <small>после разбора отметок: {report.score_reviewed.toFixed(0)}</small>}
        </article>
        <article className="debrief-stat">
          <span>Упражнение</span><strong>{report.exercise === "dds" ? "ДДС" : report.exercise === "card" ? "КИО" : report.exercise === "call" ? "Вызов 112" : "112 → ДДС"}</strong>
          <small>{report.attempt ? `Попытка ${report.attempt}` : "Завершённое упражнение"}</small>
        </article>
      </div>
      <details className="debrief-tech-details">
        <summary>Служебные сведения</summary>
        <p>Идентификатор сценария: {report.scenario_id} · номер занятия: {report.session_id}</p>
      </details>
      <section className={`debrief-next-step ${report.passed ? "is-passed" : "is-failed"}`} aria-label="Что обсудить на разборе">
        <strong>{report.passed ? "Что получилось" : "На чём сосредоточиться"}</strong>
        {report.passed ? (
          <p>Курсант выполнил критерии зачёта. Разберите сильные решения и закрепите порядок действий.</p>
        ) : improvementPoints.length ? (
          <>
            <p>Начните с этих критериев — по ним нужно разобрать решение курсанта:</p>
            <ul>{improvementPoints.map((metric) => <li key={metric.key}>{metric.title} — {metric.fact}</li>)}</ul>
          </>
        ) : (
          <p>Зачёт не получен. Сверьте отметки и факты ниже с ходом занятия.</p>
        )}
        {coaching.length > 0 && <>
          <p>Рекомендации локальной модели (на балл не влияют):</p>
          <ul>{coaching.map((item) => <li key={item.metric_key}>
            {metricTitles.get(item.metric_key) ?? item.metric_key}: {item.text}
          </li>)}</ul>
        </>}
      </section>
      <p className="ref">
        Скачать отчёт: <a href={`/api/sessions/${report.session_id}/report.csv`}>таблица (CSV)</a>
        {" · "}<a href={`/api/sessions/${report.session_id}/report.pdf`}>документ (PDF)</a>
      </p>
      <p className={report.passed ? "state-ok" : "state-violated"}>
        {report.passed ? "Зачёт" : "Не зачтено"}: на разборе {report.failed_metrics} пунктов; допустимо пропустить {report.criteria.allowed_errors}. {report.exercise === "dds" && <>На первичное решение ДДС отводится
          {` ${report.criteria.decision_time_limit_seconds} с`}</>}
        {report.exercise === "dds" && <>; норматив отработки карточки ДДС —
          {` ${report.criteria.dds_card_work_time_limit_seconds ?? 180} с`}</>}
        {report.exercise === "card" && <>Норматив заполнения КИО —
          {` ${report.criteria.card_fill_time_limit_seconds ?? 180} с`}</>}
        {report.exercise && report.exercise !== "dds" && <>
          ; грамматика описания КИО {report.criteria.require_correct_grammar ? "входит" : "не входит"} в оценку
        </>}.
      </p>
      {Object.keys(report.criteria.score_weights ?? {}).length > 0 && <p className="ref">
        Преподаватель переопределил веса: {Object.entries(report.criteria.score_weights ?? {})
          .map(([key, weight]) => `${key} — ${weight}`).join("; ")}.
      </p>}

      {Boolean(report.card_results?.length) && <section className="debrief-card-results">
        <h3>Карточки занятия ({report.card_results!.length})</h3>
        {report.card_results!.map((card, index) => <details key={card.card_id}>
          <summary>Карточка {index + 1}: {card.title || card.scenario_id} · {(card.score_reviewed ?? card.score_auto).toFixed(0)} / 100
            {card.score_reviewed != null && ` после разбора (автооценка ${card.score_auto.toFixed(0)})`}
            {card.address ? ` · ${card.address}` : ""}</summary>
          {card.managed_service && <p><strong>Ответственная служба:</strong> {card.managed_service}</p>}
          <p><strong>Ответ диспетчера:</strong> {card.reply_text || "не внесён"}</p>
          {card.metrics.some((metric) => !metric.passed || (metric.credit != null && metric.credit < 1)) && <ul>
            {card.metrics.filter((metric) => !metric.passed || (metric.credit != null && metric.credit < 1)).map((metric) =>
              <li key={metric.key} className={metric.passed ? "ref" : "state-violated"}>
                <strong>{metric.title}:</strong> {metric.fact} · норматив {metric.norm}
                {metric.credit != null && ` · вклад ${Math.round(metric.credit * 100)}%`}
              </li>)}
          </ul>}
          {card.findings.map((finding, findingIndex) => <p className="state-violated" key={`${finding.code}-${findingIndex}`}>
            {finding.code}: {finding.summary}{finding.fact ? ` — ${finding.fact}` : ""}
          </p>)}
        </details>)}
      </section>}

      <div className="columns">
        <section>
          <h3>Разбор по критериям</h3>
          <p className="ref">Сравните, что сделал курсант, с ожидаемым порядком действий.</p>
          <table className="grid">
            <tbody>
              {report.metrics.map((metric) => (
                <tr key={metric.key}>
                  <th>{metric.title}</th>
                  <td className={metric.passed ? "state-ok" : "state-violated"}>
                    <strong className={`debrief-metric-status ${metric.passed ? "is-ok" : "is-needs-work"}`}>
                      {metric.passed ? "Выполнено" : "Разобрать"}
                    </strong>
                    <span>{metric.fact} · ожидалось: {metric.norm}</span>
                    {metric.ref && <span className="ref"> ({metric.ref})</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {report.metrics.some((metric) => metric.weight != null || metric.credit != null) && <details className="debrief-scoring-details">
            <summary>Как критерии повлияли на балл</summary>
            <ul>{report.metrics.filter((metric) => metric.weight != null || metric.credit != null).map((metric) => <li key={metric.key}>
              <strong>{metric.title}:</strong> вес {metric.weight ?? 1}
              {metric.credit != null && ` · вклад в оценку ${Math.round(metric.credit * 100)}%`}
            </li>)}</ul>
          </details>}

          <h3>Отметки</h3>
          <table className="grid">
            <tbody>
              {report.findings.map((finding, index) => (
                <tr key={index}>
                  <th>{finding.code}</th>
                  <td>
                    {finding.summary}
                    <div className="ref">{finding.fact}{finding.norm && ` · ${finding.norm}`}</div>
                    {finding.source === "instructor" && <div className="ref">отметка преподавателя {finding.author ?? ""}</div>}
                    {(finding.reviews ?? []).map((review, reviewIndex) => <div className="ref" key={reviewIndex}>
                      {review.decision === "dismissed" ? "снята" : "подтверждена"} преподавателем {review.author}: {review.reason}
                    </div>)}
                  </td>
                </tr>
              ))}
              {report.findings.length === 0 && <tr><td colSpan={2}>Замечаний по ходу занятия нет.</td></tr>}
            </tbody>
          </table>
        </section>

        <section>
          <h3>Компетенции</h3>
          <Radar values={report.competencies} size={big ? 320 : 220} />

          {report.missed_checklist.length > 0 && (
            <>
              <h3>Пропущенные вопросы</h3>
              <table className="grid">
                <tbody>
                  {report.missed_checklist.map((id) => (
                    <tr key={id}>
                      <th>{id}</th>
                      <td>эталонный вопрос: «{questions.get(id)}»</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {diff && (
            <>
              <h3>Самооценка</h3>
              <table className="grid">
                <tbody>
                  <tr>
                    <th>Не заметил, что пропустил</th>
                    <td className={unnoticed.length ? "state-violated" : ""}>
                      {unnoticed.map((id) => questions.get(id) ?? id).join("; ") || "—"}
                    </td>
                  </tr>
                  <tr>
                    <th>Заметил сам</th>
                    <td>{noticed.map((id) => questions.get(id) ?? id).join("; ") || "—"}</td>
                  </tr>
                  <tr>
                    <th>Отметил зря — спрашивал</th>
                    <td>{overcautious.map((id) => questions.get(id) ?? id).join("; ") || "—"}</td>
                  </tr>
                  {report.self_assessment?.comment && (
                    <tr><th>Комментарий курсанта</th><td>{report.self_assessment.comment}</td></tr>
                  )}
                </tbody>
              </table>
            </>
          )}

          {report.hints_used.length > 0 && (
            <>
              <h3>Использованные подсказки</h3>
              <table className="grid">
                <tbody>
                  {report.hints_used.map((hint, index) => (
                    <tr key={index}><th>{hint.checklist_id}</th><td>{hint.question}</td></tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </section>
      </div>

      <details className="debrief-transcript">
      <summary>Весь ход разговора ({report.transcript.length} реплик)</summary>
      <p className="ref">Отметки и комментарии преподавателя привязаны к соответствующим репликам.</p>
      <table className="grid">
        <tbody>
          {report.transcript.map((line) => {
            const marks = report.findings.filter((finding) => finding.transcript_ref === line.ref);
            const note = notes.find((item) => item.transcript_ref === line.ref);
            return (
              <tr key={line.ref}>
                <th>{line.speaker === "caller" ? "звонящий" : "оператор"}</th>
                <td>
                  {line.text}
                  {marks.map((mark, index) => (
                    <div key={index} className="state-violated" title={`${mark.fact} · ${mark.norm ?? ""}`}>
                      {mark.code}: {mark.summary}
                    </div>
                  ))}
                  {note && <div className="ref">пометка преподавателя: {note.text}</div>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </details>
    </section>
  );
}
