// Профиль курсанта: радар компетенций, история попыток, дельта между ними.
//
// «Контроль подготовленности» из ТЗ — не таблица оценок, а инструмент
// планирования подготовки. Работа над ошибками превращается в измеримый цикл:
// посмотрел разбор → попробовал снова → увидел дельту (docs/product/MODES.md).

import { useEffect, useState } from "react";

import { COMPETENCY_LABELS, Radar } from "@/features/debrief/Radar";
import { KioCard } from "@/features/kio-card/KioCard";
import { setNested } from "@/features/kio-card/merge";
import { useMe } from "@/features/auth/useAuth";
import { clock } from "@/features/timers/clock";
import {
  ApiError, useCreateScenarioSubmission, useProfile, useScenarioSubmissions, useTrainees,
} from "@/shared/api/http";
import type { Level } from "@/shared/types/generated";

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

const EMPTY_STUDENT_KIO: Record<string, unknown> = { language: "ru", signs: [], notify: [] };

export function Profile() {
  const url = new URLSearchParams(location.search);
  const [traineeId, setTraineeId] = useState<string | null>(url.get("trainee"));
  const [submissionTitle, setSubmissionTitle] = useState("");
  const [submissionLevel, setSubmissionLevel] = useState<Level>("L1");
  const [submissionKio, setSubmissionKio] = useState<Record<string, unknown>>(EMPTY_STUDENT_KIO);
  const me = useMe();
  const isTrainee = me.data?.role === "trainee";
  const trainees = useTrainees(!isTrainee);
  const profile = useProfile(traineeId);
  const submissions = useScenarioSubmissions(Boolean(isTrainee));
  const createSubmission = useCreateScenarioSubmission();
  const submissionErrorCode = createSubmission.error instanceof ApiError
    ? createSubmission.error.code
    : null;
  const submissionError = submissionErrorCode === "trainee_group_required_for_review"
    ? "Попросите преподавателя включить вас в свою учебную группу, затем отправьте КИО ещё раз."
    : submissionErrorCode === "instructor_group_required_for_review"
      ? "Для вашей группы пока не назначен преподаватель, который сможет проверить КИО."
      : createSubmission.error?.message;
  const submissionReady = typeof submissionKio.incident_type === "string"
    && typeof submissionKio.description === "string" && submissionKio.description.trim().length >= 20
    && Number.isInteger(submissionKio.incident_group)
    && Array.isArray(submissionKio.signs) && submissionKio.signs.length > 0
    && (typeof submissionKio.address === "string" && submissionKio.address.trim().length > 0
      || typeof submissionKio.street === "string" && submissionKio.street.trim().length > 0
        && typeof submissionKio.building === "string" && submissionKio.building.trim().length > 0);

  useEffect(() => {
    if (isTrainee && me.data?.trainee_id) setTraineeId(me.data.trainee_id);
  }, [isTrainee, me.data?.trainee_id]);

  return (
    <main className="page">
      <h1>Профиль курсанта</h1>

      <p><a href="/materials">Открыть назначенные учебные материалы →</a></p>

      {isTrainee && <section className="student-scenario-submit">
        <h2>Отправить КИО на проверку</h2>
        <p className="ref">Черновик увидит преподаватель вашей группы. Только после утверждения карточка появится в банке ДДС. Используйте вымышленные или обезличенные данные; не указывайте реальные имена и телефоны.</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          createSubmission.mutate({
            title: submissionTitle,
            level: submissionLevel,
            kio: submissionKio,
          }, { onSuccess: () => {
            setSubmissionTitle("");
            setSubmissionKio(EMPTY_STUDENT_KIO);
          } });
        }}>
          <label>Название<input required minLength={3} maxLength={200} value={submissionTitle}
            onChange={(event) => setSubmissionTitle(event.target.value)} /></label>
          <label>Сложность<select value={submissionLevel} onChange={(event) => setSubmissionLevel(event.target.value as Level)}>
            <option value="L1">L1</option><option value="L2">L2</option><option value="L3">L3</option>
          </select></label>
          <p className="ref">Заполните карточку КИО. Для отправки нужны описание от 20 символов, адрес, категория и хотя бы один признак происшествия. Код ЕКП и список служб рассчитываются автоматически; системные поля заполнит сервер.</p>
          <KioCard state={{ server: submissionKio, pending: {} }} referenceArm onChange={(path, value) =>
            setSubmissionKio((previous) => setNested(previous, path, value))} />
          <button type="submit" disabled={createSubmission.isPending || !submissionReady}>Сохранить черновик преподавателю</button>
          {createSubmission.isError && <p role="alert" className="state-violated">Не удалось отправить: {submissionError}</p>}
          {createSubmission.isSuccess && <p role="status" className="state-ok">Черновик КИО отправлен и ожидает проверки.</p>}
        </form>
        <h3>Мои предложения</h3>
        <ul>
          {submissions.data?.map((item) => <li key={item.id}>
            {item.title} · {item.level} — {item.status === "pending" ? "на проверке" : item.status === "approved" ? "утверждено, доступно в банке ДДС" : "нужно доработать"}
            {item.review_comment && <span className="ref"> · комментарий: {item.review_comment}</span>}
          </li>)}
          {!submissions.data?.length && <li className="ref">Предложений пока нет.</li>}
        </ul>
      </section>}

      {!isTrainee && <p>
        <select aria-label="Курсант для просмотра профиля" value={traineeId ?? ""} onChange={(event) => setTraineeId(event.target.value || null)}>
          <option value="">— выберите курсанта —</option>
          {trainees.data?.map((trainee) => (
            <option key={trainee.id} value={trainee.id}>
              {trainee.name}{trainee.group ? ` · ${trainee.group}` : ""}
            </option>
          ))}
        </select>
      </p>}

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
            <h3>Рекомендации к следующему занятию</h3>
            {profile.data.recommendations.length > 0 ? (
              <ul>
                {profile.data.recommendations.map((item) => (
                  <li key={item.code}>
                    <strong>{item.code} · {item.title}:</strong> {item.recommendation}
                    <span className="ref"> · в последней оценённой попытке: {item.occurrences}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="ref">Для последней оценённой попытки замечаний нет.</p>
            )}
            {profile.data.attempts.some((attempt) => attempt.score !== null) && (
              <p><a href={`/api/trainees/${profile.data.trainee.id}/certificate.pdf`}>
                Скачать сертификат PDF
              </a></p>
            )}

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
                      {attempt.recording_available && <>
                        {" · "}<a href={`/api/sessions/${attempt.session_id}/recording.wav`}>
                          запись WAV
                        </a>
                      </>}
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
