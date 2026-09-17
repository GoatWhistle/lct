// Пульт директив: преподаватель ведёт ситуацию по ходу разговора.
//
// За этим пультом будет сидеть человек, который видит его впервые и не может
// ошибиться: кнопки крупные, подписаны человеческим языком, нажатие сразу видно
// в журнале — даже если эффект применится со следующей реплики
// (docs/arch/FRONTEND.md).

import { useState } from "react";

const SOFT: { key: string; label: string }[] = [
  { key: "panic_rises", label: "Паника нарастает" },
  { key: "screaming", label: "Переходит на крик" },
  { key: "turns_aggressive", label: "Переходит в агрессию" },
  { key: "distracted", label: "Звонящий отвлёкся" },
];

const HARD: { key: string; label: string; danger?: boolean }[] = [
  { key: "line_dropped", label: "Связь обрывается", danger: true },
  { key: "second_victim", label: "Появился второй пострадавший" },
  { key: "address_wrong", label: "Адрес оказался неточным" },
];

export function Director({
  onInject,
  disabled,
  error,
}: {
  onInject: (directive: string, immediate: boolean) => void;
  disabled?: boolean;
  error?: string | null;
}) {
  const [free, setFree] = useState("");
  const [log, setLog] = useState<string[]>([]);

  const fire = (label: string, key: string, immediate = false) => {
    onInject(key, immediate);
    const time = new Date().toLocaleTimeString("ru-RU");
    setLog((prev) => [`${time} — ${label}`, ...prev].slice(0, 6));
  };

  return (
    <section className="director">
      <h3>Ведение ситуации</h3>
      <div className="director-buttons">
        {SOFT.map((item) => (
          <button key={item.key} type="button" disabled={disabled}
            onClick={() => fire(item.label, item.key)}>
            {item.label}
          </button>
        ))}
        {HARD.map((item) => (
          <button key={item.key} type="button" disabled={disabled}
            className={item.danger ? "danger" : ""}
            onClick={() => fire(item.label, item.key, item.key === "line_dropped")}>
            {item.label}
          </button>
        ))}
      </div>
      <p className="ref">
        Мягкие применяются со следующей реплики — разговор не дёргается. «Связь обрывается»
        рвёт звук немедленно.
      </p>
      <p>
        <input type="text" value={free} placeholder="своя реплика — нужна сеть и LLM"
          onChange={(event) => setFree(event.target.value)} />{" "}
        <button type="button" disabled={disabled || !free.trim()}
          onClick={() => { fire(`своя реплика: ${free.trim()}`, free.trim()); setFree(""); }}>
          Отправить
        </button>
      </p>
      {error && <p className="violated">{error}</p>}
      {log.length > 0 && (
        <table className="grid">
          <tbody>
            {log.map((line, index) => (
              <tr key={index}><td>{line}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
