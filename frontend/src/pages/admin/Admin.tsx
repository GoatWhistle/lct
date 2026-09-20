// АРМ администратора: учётные записи, состояние стенда, журнал аудита, копии.
//
// Того, чего администратору нельзя по ТЗ, здесь нет вовсе — ни правки оценок,
// ни вмешательства в занятие. Запрет выражен отсутствием кнопки, а не
// disabled-атрибутом (lct-24).

import { useState } from "react";

import { useLogout, useMe } from "@/features/auth/useAuth";
import type { Role } from "@/shared/types/generated";

import {
  useAudit, useBackups, useCreateBackup, useCreateUser, usePatchUser, useStatus, useUsers,
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
      <Users />
      <Backups />
      <Audit />
    </main>
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
              <td><input value={form.login} onChange={(e) => setForm({ ...form, login: e.target.value })} /></td>
            </tr>
            <tr>
              <th>ФИО</th>
              <td><input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></td>
            </tr>
            <tr>
              <th>Пароль</th>
              <td>
                <input
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />{" "}
                не короче восьми знаков
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
  const audit = useAudit(action);
  return (
    <>
      <h2>Журнал действий</h2>
      <p>
        Фильтр по действию:{" "}
        <input value={action} placeholder="lesson.start" onChange={(e) => setAction(e.target.value)} />
      </p>
      <table className="grid">
        <thead>
          <tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Над чем</th><th>Подробности</th></tr>
        </thead>
        <tbody>
          {(audit.data ?? []).map((row, index) => (
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
    </>
  );
}
