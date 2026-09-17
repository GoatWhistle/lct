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

  return (
    <section className={big ? "debrief debrief-big" : "debrief"}>
      <h2>
        Разбор · оценка {report.score_final.toFixed(0)} из 100
        {report.overridden_by && ` · скорректировано (${report.overridden_by}), автооценка ${report.score_auto.toFixed(0)}`}
      </h2>

      <div className="columns">
        <section>
          <h3>Нормативы и факты</h3>
          <table className="grid">
            <tbody>
              {report.metrics.map((metric) => (
                <tr key={metric.key}>
                  <th>{metric.title}</th>
                  <td className={metric.passed ? "state-ok" : "state-violated"}>
                    {metric.fact} · норматив {metric.norm}
                    {metric.ref && <span className="ref"> ({metric.ref})</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3>Отметки</h3>
          <table className="grid">
            <tbody>
              {report.findings.map((finding, index) => (
                <tr key={index}>
                  <th>{finding.code}</th>
                  <td>
                    {finding.summary}
                    <div className="ref">{finding.fact}{finding.norm && ` · ${finding.norm}`}</div>
                  </td>
                </tr>
              ))}
              {report.findings.length === 0 && <tr><td colSpan={2}>ошибок не найдено</td></tr>}
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

      <h3>Транскрипт</h3>
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
    </section>
  );
}
