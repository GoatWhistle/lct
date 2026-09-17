// Профиль курсанта: радар компетенций, история попыток, дельта между ними.
//
// «Контроль подготовленности» из ТЗ — не таблица оценок, а инструмент
// планирования подготовки. Работа над ошибками превращается в измеримый цикл:
// посмотрел разбор → попробовал снова → увидел дельту (docs/product/MODES.md).

import { useState } from "react";

import { COMPETENCY_LABELS, Radar } from "@/features/debrief/Radar";
import { clock } from "@/features/timers/clock";
import { useProfile, useTrainees } from "@/shared/api/http";

const MODE_LABELS: Record<string, string> = {
  training: "тренировочный",
  exam: "контрольный",
  self: "самостоятельный",
};

function sign(value: number | null, format: (value: number) => string): string {
  if (value === null) return "—";
  if (value === 0) return "без изменений";
  return `${value > 0 ? "+" : "−"}${format(Math.abs(value))}`;
}

export function Profile() {
  const url = new URLSearchParams(location.search);
  const [traineeId, setTraineeId] = useState<string | null>(url.get("trainee"));
  const trainees = useTrainees();
  const profile = useProfile(traineeId);

  return (
    <main className="page">
      <h1>Профиль курсанта</h1>

      <p>
        <select value={traineeId ?? ""} onChange={(event) => setTraineeId(event.target.value || null)}>
          <option value="">— выберите курсанта —</option>
          {trainees.data?.map((trainee) => (
            <option key={trainee.id} value={trainee.id}>
              {trainee.name}{trainee.group ? ` · ${trainee.group}` : ""}
            </option>
          ))}
        </select>
      </p>

      {profile.data && (
        <div className="columns">
          <section>
            <h2>
              {profile.data.trainee.name}
              {profile.data.trainee.group && ` · группа ${profile.data.trainee.group}`}
            </h2>
            <Radar
              values={Object.entries(profile.data.competencies).map(([competency, value]) => ({
                competency,
                value,
              }))}
            />
            <p className="ref">
              радар — среднее по попыткам; подсказок использовано всего: {profile.data.hints_total}
            </p>

            <h3>Дельта попыток</h3>
            <table className="grid">
              <tbody>
                {profile.data.deltas.map((delta, index) => (
                  <tr key={index}>
                    <th>{delta.scenario_id} · {delta.from_attempt} → {delta.to_attempt}</th>
                    <td>
                      оценка {sign(delta.score, (value) => value.toFixed(0))}
                      {" · "}опрос {sign(delta.interview_ms, (value) => clock(value))}
                      {" · "}фактов {sign(delta.facts_got, (value) => String(value))}
                    </td>
                  </tr>
                ))}
                {profile.data.deltas.length === 0 && (
                  <tr><td colSpan={2}>повторных попыток по сценарию ещё не было</td></tr>
                )}
              </tbody>
            </table>
          </section>

          <section>
            <h2>История занятий</h2>
            <table className="grid">
              <tbody>
                {profile.data.attempts.map((attempt) => (
                  <tr key={attempt.session_id}>
                    <th>
                      {new Date(attempt.created_at).toLocaleString("ru-RU")}
                      <div className="ref">{MODE_LABELS[attempt.mode] ?? attempt.mode} · попытка {attempt.attempt}</div>
                    </th>
                    <td>
                      {attempt.scenario_id} · оценка {attempt.score?.toFixed(0) ?? "—"}
                      <div className="ref">
                        опрос {attempt.interview_ms === null ? "не завершён" : clock(attempt.interview_ms)}
                        {attempt.facts_required !== null && ` · факты ${attempt.facts_got}/${attempt.facts_required}`}
                        {attempt.hints ? ` · подсказок ${attempt.hints}` : ""}
                        {Object.keys(attempt.codes).length > 0 &&
                          ` · ${Object.entries(attempt.codes).map(([code, count]) => `${code}×${count}`).join(", ")}`}
                      </div>
                      <a href={`/wall?session=${attempt.session_id}`}>разбор</a>
                    </td>
                  </tr>
                ))}
                {profile.data.attempts.length === 0 && <tr><td colSpan={2}>занятий не было</td></tr>}
              </tbody>
            </table>
          </section>
        </div>
      )}

      {profile.isError && <p className="violated">Профиль не загрузился.</p>}
      {!traineeId && <p className="ref">Курсанты появляются после первого занятия.</p>}
      <p className="ref">
        Компетенции: {Object.values(COMPETENCY_LABELS).join(" · ")}
      </p>
    </main>
  );
}
