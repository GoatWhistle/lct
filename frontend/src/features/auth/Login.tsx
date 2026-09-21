// Экран входа. Ставится перед всеми остальными: номер занятия перестал быть
// пропуском, по ссылке пускает роль, а не знание адреса (lct-23).

import { useState, type MouseEvent, type ReactNode } from "react";

import { DEMO_MODE, SCREENS, useDemoLogin, useLogin, useLogout, useMe } from "./useAuth";

function DemoLinks() {
  const openScreen = (event: MouseEvent<HTMLAnchorElement>, path: string) => {
    event.preventDefault();
    const current = new URLSearchParams(window.location.search);
    const session = current.get("session") || window.sessionStorage.getItem("demo_session");
    if (session) window.sessionStorage.setItem("demo_session", session);
    const target = new URL(path, window.location.origin);
    if (session) target.searchParams.set("session", session);
    if (path === "/dds") target.searchParams.set("role", current.get("role") || "dds_01");
    window.location.assign(target.pathname + target.search);
  };

  return <nav className="demo-switcher" aria-label="Демо-переходы между АРМ">
    <details><summary>Демо · экраны</summary>
      <div>
        <a href="/instructor" onClick={(event) => openScreen(event, "/instructor")}>Преподаватель</a>
        <a href="/trainee" onClick={(event) => openScreen(event, "/trainee")}>Оператор 112</a>
        <a href="/dds" onClick={(event) => openScreen(event, "/dds")}>ДДС</a>
      </div>
    </details>
  </nav>;
}

export function RequireAuth({ screen, children }: { screen: string; children: ReactNode }) {
  const me = useMe();
  const logout = useLogout();

  if (me.isLoading) {
    return <main className="page"><p>Проверяем доступ…</p></main>;
  }
  if (!me.data) return <Login />;

  const demoUser = DEMO_MODE && ["dev", "demo-instructor"].includes(me.data.login)
    && me.data.role === "instructor";
  const allowed = [...(SCREENS[me.data.role] ?? []), ...(demoUser ? ["/trainee"] : [])];
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
  return <>{children}{demoUser && <DemoLinks />}</>;
}

function Login() {
  const login = useLogin();
  const demoLogin = useDemoLogin();
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
      {DEMO_MODE && <div className="demo-login">
        <p>Локальный деморежим: вход без учётной записи доступен только при включённом обходе авторизации на backend.</p>
        <button type="button" disabled={demoLogin.isPending} onClick={() => demoLogin.mutate()}>
          {demoLogin.isPending ? "Входим…" : "Войти в демо одним нажатием"}
        </button>
        {demoLogin.isError && <p className="warn" role="alert">
          {demoLogin.error instanceof Error && demoLogin.error.message === "404"
            ? "Демо-вход выключен на сервере: запустите backend с DEV_AUTH_BYPASS=true."
            : "Не удалось выполнить демо-вход. Проверьте подключение к локальному backend."}
        </p>}
      </div>}
      {/* Причину отказа не уточняем: «нет такого логина» — это список
          действующих учётных записей для того, кто подбирает. */}
      {login.isError && <p className="warn">Логин или пароль неверны</p>}
    </main>
  );
}
