import { useEffect, useState } from "react";

import { useAssignTrainee, useCreateGroup, useGroupAnalytics, useGroupInsight, useGroups, useTrainees } from "@/shared/api/http";

export function Groups() {
  const groups = useGroups();
  const trainees = useTrainees();
  const create = useCreateGroup();
  const assign = useAssignTrainee();
  const [groupId, setGroupId] = useState<string | null>(null);
  const [groupName, setGroupName] = useState("");
  const [traineeId, setTraineeId] = useState("");
  const report = useGroupAnalytics(groupId);
  const insight = useGroupInsight();

  useEffect(() => {
    if (!groupId && groups.data?.length) setGroupId(groups.data[0].id);
  }, [groups.data, groupId]);

  return (
    <main className="page">
      <h1>Аналитика учебной группы</h1>
      <p><a href="/instructor">← к пульту преподавателя</a></p>
      <form onSubmit={(event) => {
        event.preventDefault();
        create.mutate(groupName.trim(), { onSuccess: (group) => {
          setGroupId(group.id);
          setGroupName("");
        } });
      }}>
        <label>Новая группа{" "}
          <input value={groupName} maxLength={120} onChange={(event) => setGroupName(event.target.value)} />
        </label>{" "}
        <button type="submit" disabled={!groupName.trim() || create.isPending}>Создать</button>
        {create.isError && <span className="warn"> Не удалось создать группу: возможно, имя занято.</span>}
      </form>
      <label>
        Группа{" "}
        <select value={groupId ?? ""} onChange={(event) => setGroupId(event.target.value || null)}>
          <option value="">— выберите группу —</option>
          {groups.data?.map((group) => (
            <option key={group.id} value={group.id}>{group.name}</option>
          ))}
        </select>
      </label>
      {groupId && <form onSubmit={(event) => {
        event.preventDefault();
        if (traineeId) assign.mutate({ groupId, traineeId }, { onSuccess: () => setTraineeId("") });
      }}>
        <label>Добавить курсанта{" "}
          <select value={traineeId} onChange={(event) => setTraineeId(event.target.value)}>
            <option value="">— выберите курсанта —</option>
            {trainees.data?.map((trainee) => (
              <option key={trainee.id} value={trainee.id}>{trainee.name}{trainee.group ? ` · ${trainee.group}` : ""}</option>
            ))}
          </select>
        </label>{" "}
        <button type="submit" disabled={!traineeId || assign.isPending}>Назначить</button>
        {assign.isError && <span className="warn"> Не удалось назначить курсанта.</span>}
      </form>}
      {groups.isError && <p className="warn">Не удалось загрузить группы.</p>}
      {groups.data?.length === 0 && <p>Учебные группы ещё не заведены.</p>}
      {report.isError && <p className="warn">Не удалось загрузить аналитику.</p>}
      {report.data && (
        <>
          <p>
            Курсанты: {report.data.active_trainees} занимались из {report.data.enrolled_trainees} ·
            оценённых попыток: {report.data.scored_attempts} ·
            средний балл: {report.data.average_score?.toFixed(1) ?? "—"}
          </p>
          <p className="ref">
            Доли ошибок считаются среди курсантов с оценёнными попытками; повторные попытки
            одного человека не увеличивают долю. Табличные рекомендации всегда формируются
            воспроизводимыми правилами.
          </p>
          <section className="group-insight">
            <button type="button" disabled={!report.data.scored_attempts || insight.isPending}
              onClick={() => groupId && insight.mutate(groupId)}>
              {insight.isPending ? "Qwen анализирует агрегаты…" : "Сформировать локальный ИИ-инсайт"}
            </button>
            <span className="ref">В модель передаются только агрегаты без ФИО и попыток отдельных курсантов.</span>
            {insight.data && <div>
              <strong>Вывод Qwen</strong>
              <p>{insight.data.summary}</p>
              <ol>{insight.data.priorities.map((item) => <li key={item}>{item}</li>)}</ol>
            </div>}
            {insight.isError && <p className="warn">ИИ-инсайт не сформирован; табличная аналитика остаётся доступна.</p>}
          </section>
          <table className="grid">
            <thead><tr><th>Типичная ошибка</th><th>Курсанты</th><th>Рекомендация</th></tr></thead>
            <tbody>
              {report.data.errors.map((item) => (
                <tr key={item.code}>
                  <th>{item.code} · {item.title}</th>
                  <td>{item.affected_trainees} · {item.rate_percent.toFixed(1)}%</td>
                  <td>{item.recommendation}</td>
                </tr>
              ))}
              {report.data.errors.length === 0 && (
                <tr><td colSpan={3}>Пока нет ошибок в оценённых занятиях.</td></tr>
              )}
            </tbody>
          </table>
        </>
      )}
    </main>
  );
}
