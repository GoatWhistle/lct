// Экран входа. Ставится перед всеми остальными: номер занятия перестал быть
// пропуском, по ссылке пускает роль, а не знание адреса (lct-23).

import { useState, type ReactNode } from "react";

import { SCREENS, useLogin, useLogout, useMe } from "./useAuth";

export function RequireAuth({ screen, children }: { screen: string; children: ReactNode }) {
  const me = useMe();
  const logout = useLogout();

  if (me.isLoading) {
    return <main className="page"><p>Проверяем доступ…</p></main>;
  }
  if (!me.data) return <Login />;

  const allowed = SCREENS[me.data.role] ?? [];
  if (!allowed.includes(screen)) {
    return (
      <main className="page">
        <h1>Экран недоступен</h1>
        <p className="warn">
          Вы вошли как {me.data.full_name} ({me.data.role}). Этот экран открыт другой роли.
        </p>
        <p>Доступно: {allowed.join(", ") || "ничего"}</p>
        <p><button type="button" onClick={() => logout.mutate()}>Выйти</button></p>
      </main>
    );
  }
  return <>{children}</>;
}

function Login() {
  const login = useLogin();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");

  return (
    <main className="page">
      <h1>Учебный симулятор системы-112</h1>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          login.mutate({ login: name, password });
        }}
      >
        <table className="grid">
          <tbody>
            <tr>
              <th>Логин</th>
              <td>
                <input value={name} autoFocus onChange={(event) => setName(event.target.value)} />
              </td>
            </tr>
            <tr>
              <th>Пароль</th>
              <td>
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </td>
            </tr>
          </tbody>
        </table>
        <p>
          <button type="submit" disabled={!name || !password || login.isPending}>
            Войти
          </button>
        </p>
      </form>
      {/* Причину отказа не уточняем: «нет такого логина» — это список
          действующих учётных записей для того, кто подбирает. */}
      {login.isError && <p className="warn">Логин или пароль неверны</p>}
    </main>
  );
}
