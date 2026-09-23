// АРМ администратора: учётные записи, состояние стенда, журнал аудита, копии.
//
// Того, чего администратору нельзя по ТЗ, здесь нет вовсе — ни правки оценок,
// ни вмешательства в занятие. Запрет выражен отсутствием кнопки, а не
// disabled-атрибутом (lct-24).

import { useState } from "react";

import { useLogout, useMe } from "@/features/auth/useAuth";
import type { Role } from "@/shared/types/generated";

import {
  useAudit, useBackups, useCreateBackup, useCreateUser, useDiagnostics, usePatchUser, useStatus,
  useUsers,
} from "./api";

const ROLE_LABELS: Record<Role, string> = {
  admin: "Администратор",
  instructor: "Преподаватель",
  trainee: "Обучающийся",
};

export function Admin() {
  const me = useMe();
  const logout = useLogout();

  return (
    <main className="page">
      <h1>АРМ администратора</h1>
      <p>
        {me.data?.full_name}{" "}
        <button type="button" onClick={() => logout.mutate()}>Выйти</button>
      </p>
      <Status />
      <Diagnostics />
      <p><a href="/api/admin/config.xml">Скачать XML-конфигурацию рабочих мест</a></p>
      <Users />
      <Backups />
      <Audit />
    </main>
  );
}

function bytes(value: number | null) {
  if (value === null) return "нет данных ОС";
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} ГБ`;
  return `${Math.round(value / 1024 ** 2)} МБ`;
}

function Diagnostics() {
  const report = useDiagnostics();
  const metrics = report.data?.metrics;
  const systemEvents = report.data?.recent_system_events ?? [];
  const failedActions = report.data?.failed_actions_24h ?? [];
  return (
    <>
      <h2>Нагрузка и диагностика</h2>
      {metrics && (
        <table className="grid">
          <tbody>
            <tr><th>CPU backend</th><td>{metrics.cpu_percent.toFixed(1)}% · {metrics.cpu_cores} ядер на сервере</td></tr>
            <tr><th>Нагрузка сервера, 1 мин</th><td>{metrics.load_1m_percent === null ? "нет счётчика ОС" : `${metrics.load_1m_percent.toFixed(1)}%`}</td></tr>
            <tr><th>Память backend</th><td>{bytes(metrics.rss_bytes)}</td></tr>
            <tr><th>Свободная память сервера</th><td>{bytes(metrics.memory_available_bytes)} из {bytes(metrics.memory_total_bytes)}</td></tr>
            <tr><th>Свободно на диске</th><td>{bytes(metrics.disk_free_bytes)} из {bytes(metrics.disk_total_bytes)}</td></tr>
            <tr><th>Занятия</th><td>активно {metrics.active_sessions}; завершено за 24 ч {metrics.completed_sessions_24h}; восстановлено после запуска {metrics.restored_sessions}</td></tr>
            <tr><th>Процесс</th><td>работает {Math.floor(metrics.uptime_seconds / 60)} мин; потоков {metrics.threads}</td></tr>
          </tbody>
        </table>
      )}
      {report.isError && <p className="warn">Диагностика недоступна: {String(report.error)}</p>}
      <p><a href="/api/admin/diagnostics.json">Скачать отчёт об ошибках и сбоях JSON</a></p>
      <h3>Сбои действий за 24 часа</h3>
      {failedActions.length === 0 ? <p>Зафиксированных сбоев действий нет.</p> : (
        <table className="grid">
          <thead><tr><th>Когда</th><th>Действие</th><th>Кто</th><th>Подробности</th></tr></thead>
          <tbody>{failedActions.map((event, index) => (
            <tr key={`${event.at}-${index}`}>
              <th>{new Date(event.at).toLocaleString("ru-RU")}</th>
              <td>{event.action}</td><td>{event.actor}</td><td>{event.detail}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
      <details>
        <summary>Предупреждения процесса ({systemEvents.length})</summary>
        {systemEvents.length === 0 ? <p>Предупреждений после запуска нет.</p> : (
          <table className="grid">
            <thead><tr><th>Когда</th><th>Уровень</th><th>Источник</th><th>Сообщение</th></tr></thead>
            <tbody>{systemEvents.map((event, index) => (
              <tr key={`${event.at}-${index}`}>
                <th>{new Date(event.at).toLocaleString("ru-RU")}</th>
                <td>{event.level}</td><td>{event.source}</td><td>{event.message}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </details>
    </>
  );
}

function Status() {
  const status = useStatus();
  return (
    <>
      <h2>Состояние стенда</h2>
      <table className="grid">
        <tbody>
          {(status.data ?? []).map((item) => (
            <tr key={item.name}>
              <th>{item.name}</th>
              <td className={item.ok ? "state-ok" : "state-violated"}>{item.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function Users() {
  const users = useUsers();
  const create = useCreateUser();
  const patch = usePatchUser();
  const [form, setForm] = useState({ login: "", full_name: "", password: "", role: "trainee" as Role, service: "" });

  return (
    <>
      <h2>Учётные записи</h2>
      <table className="grid">
        <thead>
          <tr><th>Логин</th><th>ФИО</th><th>Роль</th><th>Служба</th><th>Состояние</th></tr>
        </thead>
        <tbody>
          {(users.data ?? []).map((user) => (
            <tr key={user.id}>
              <th>{user.login}</th>
              <td>{user.full_name}</td>
              <td>{ROLE_LABELS[user.role]}</td>
              <td>{user.service ?? "—"}</td>
              <td>
                <button
                  type="button"
                  onClick={() => patch.mutate({ id: user.id, body: { blocked: !user.blocked } })}
                >
                  {user.blocked ? "Разблокировать" : "Заблокировать"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Завести запись</h3>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(
            { ...form, service: form.service || null },
            { onSuccess: () => setForm({ ...form, login: "", full_name: "", password: "" }) },
          );
        }}
      >
        <table className="grid">
          <tbody>
            <tr>
              <th>Логин</th>
              <td><input value={form.login} maxLength={80} onChange={(e) => setForm({ ...form, login: e.target.value })} /></td>
            </tr>
            <tr>
              <th>ФИО</th>
              <td><input value={form.full_name} maxLength={120} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></td>
            </tr>
            <tr>
              <th>Пароль</th>
              <td>
                <input
                  type="password"
                  maxLength={1024}
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />{" "}
                от восьми до 1024 знаков
              </td>
            </tr>
            <tr>
              <th>Роль</th>
              <td>
                <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
                  {Object.entries(ROLE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </td>
            </tr>
            {/* Служба осмысленна только у обучающегося: по ней в его ленту
                попадают профильные события (docs/spec/DATASET.md). */}
            {form.role === "trainee" && (
              <tr>
                <th>Служба ДДС</th>
                <td><input value={form.service} onChange={(e) => setForm({ ...form, service: e.target.value })} /></td>
              </tr>
            )}
          </tbody>
        </table>
        <p>
          <button type="submit" disabled={!form.login || !form.full_name || form.password.length < 8}>
            Завести
          </button>
          {create.isError && <span className="warn"> Логин уже занят</span>}
        </p>
      </form>
    </>
  );
}

function Backups() {
  const backups = useBackups();
  const create = useCreateBackup();
  return (
    <>
      <h2>Резервные копии</h2>
      <p>
        <button type="button" disabled={create.isPending} onClick={() => create.mutate()}>
          Снять копию сейчас
        </button>
        {create.isError && <span className="warn"> {String(create.error)}</span>}
      </p>
      <table className="grid">
        <tbody>
          {(backups.data ?? []).map((item) => (
            <tr key={item.name}>
              <th>{item.name}</th>
              <td>{Math.round(item.size_bytes / 1024)} КБ</td>
              <td>{new Date(item.at).toLocaleString("ru-RU")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(backups.data ?? []).length === 0 && <p>Копий пока нет.</p>}
    </>
  );
}

function Audit() {
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [offset, setOffset] = useState(0);
  const audit = useAudit(action, actor, offset);
  const pageSize = 200;
  const rows = audit.data ?? [];
  return (
    <>
      <h2>Журнал действий</h2>
      <p>
        Действие: <input value={action} placeholder="login.failed" onChange={(e) => { setAction(e.target.value); setOffset(0); }} />
        {" "}Пользователь: <input value={actor} placeholder="логин" onChange={(e) => { setActor(e.target.value); setOffset(0); }} />
      </p>
      <table className="grid">
        <thead>
          <tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Над чем</th><th>Подробности</th></tr>
        </thead>
        <tbody>
          {rows.slice(0, pageSize).map((row, index) => (
            <tr key={index}>
              <th>{new Date(row.at).toLocaleString("ru-RU")}</th>
              <td>{row.actor}</td>
              <td>{row.action}</td>
              <td>{row.object_id ?? "—"}</td>
              <td>{row.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Назад</button>{" "}
        <span>Записи {rows.length ? offset + 1 : 0}–{offset + Math.min(rows.length, pageSize)}</span>{" "}
        <button type="button" disabled={rows.length <= pageSize} onClick={() => setOffset(offset + pageSize)}>Раньше</button>
      </p>
    </>
  );
}
