// Заглушка экрана. Каждая ссылается на карточку, которая её закрывает.
export function Stub({ title, card }: { title: string; card: string }) {
  return (
    <main className="stub">
      <h1>{title}</h1>
      <p>
        Экран не реализован. Карточка: <code>tasks/{card}.md</code>
      </p>
    </main>
  );
}
