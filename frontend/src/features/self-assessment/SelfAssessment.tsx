// Самооценка курсанта — до показа автооценки.
//
// Педагогический смысл — рефлексия: курсант сверяет своё ощущение с объективной
// оценкой, а расхождение — отдельный материал для преподавателя. Курсант,
// не заметивший, что пропустил вопрос о пострадавших, — более важный случай,
// чем сама ошибка (docs/product/DEBRIEF.md).

import { useState } from "react";

export interface ChecklistItem {
  id: string;
  question: string;
}

export function SelfAssessment({
  checklist,
  onSubmit,
}: {
  checklist: ChecklistItem[];
  onSubmit: (missed: string[], comment: string) => void;
}) {
  const [missed, setMissed] = useState<string[]>([]);
  const [comment, setComment] = useState("");

  const toggle = (id: string) =>
    setMissed((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));

  return (
    <section>
      <h2>Самооценка</h2>
      <p>Отметьте вопросы, которые, по вашему мнению, вы пропустили. Оценка откроется после.</p>
      <table className="grid">
        <tbody>
          {checklist.map((item) => (
            <tr key={item.id}>
              <th>
                <label>
                  <input type="checkbox" checked={missed.includes(item.id)} onChange={() => toggle(item.id)} />{" "}
                  пропустил
                </label>
              </th>
              <td>{item.question}</td>
            </tr>
          ))}
          <tr>
            <th>Комментарий</th>
            <td>
              <input
                type="text"
                value={comment}
                placeholder="например: запнулся на адресе, растерялся"
                onChange={(event) => setComment(event.target.value)}
              />
            </td>
          </tr>
        </tbody>
      </table>
      <p>
        <button type="button" onClick={() => onSubmit(missed, comment)}>
          Отправить самооценку
        </button>
      </p>
    </section>
  );
}
