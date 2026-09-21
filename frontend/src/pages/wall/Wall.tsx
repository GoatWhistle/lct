// Внешний монитор: группа смотрит, как идёт занятие, и разбирает вместе с ним.
//
// Это не отдельное приложение, а другой режим отрисовки тех же событий: крупный
// шрифт, высокий контраст, минимум деталей. Никакого взаимодействия — экран висит
// и показывает (docs/arch/FRONTEND.md).

import { Debrief } from "@/features/debrief/Debrief";
import { KioCard } from "@/features/kio-card/KioCard";
import { ModeBanner } from "@/features/mode-banner/ModeBanner";
import { InterviewTimer } from "@/features/timers/Timers";
import { useObserve } from "@/features/observe/useObserve";
import { sessionIdFromUrl } from "@/shared/api/session";

export function Wall() {
  const sessionId = sessionIdFromUrl();
  const session = useObserve(sessionId);

  if (!sessionId) {
    return (
      <main className="page wall">
        <h1>Внешний монитор</h1>
        <p className="warn">Откройте ссылку вида /wall?session=…</p>
      </main>
    );
  }

  if (session.report) {
    return (
      <main className="page wall">
        <ModeBanner mode={session.mode} />
        <h1>{session.title} · разбор</h1>
        <Debrief report={session.report} big />
      </main>
    );
  }

  return (
    <main className="page wall">
      <ModeBanner mode={session.mode} />
      <h1>
        {session.title ?? "Ожидание занятия"}
        {session.trainee && ` · ${session.trainee}`}
      </h1>
      <InterviewTimer timers={session.timers} big />

      <div className="columns">
        <section>
          <h2>Ход разговора</h2>
          <table className="grid">
            <tbody>
              {session.transcript.slice(-12).map((line) => (
                <tr key={line.ref}>
                  <th>{line.speaker === "caller" ? "звонящий" : "оператор"}</th>
                  <td>
                    {line.text}
                    {session.notes.has(line.ref) && (
                      <div className="ref">пометка: {session.notes.get(line.ref)}</div>
                    )}
                  </td>
                </tr>
              ))}
              {session.transcript.length === 0 && <tr><td colSpan={2}>—</td></tr>}
            </tbody>
          </table>
          <p className="ref">подсказок использовано: {session.hintsUsed}</p>
        </section>
        <section>
          <h2>Карточка</h2>
          <KioCard state={session.card} required={session.requiredFields} readOnly />
        </section>
      </div>
    </main>
  );
}
