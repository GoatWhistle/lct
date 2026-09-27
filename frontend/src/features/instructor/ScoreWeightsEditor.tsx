import { useState } from "react";
import type { Exercise } from "@/shared/types/generated";

type LessonExercise = Exercise | "card_dds";

type MetricOption = { key: string; label: string; hint: string };

const OPERATOR_METRICS: MetricOption[] = [
  { key: "answer_time", label: "Ответ на вызов", hint: "Скорость принятия звонка." },
  { key: "interview_time", label: "Длительность опроса", hint: "Соблюдение норматива опроса." },
  { key: "checklist_completeness", label: "Полнота опроса", hint: "Получение обязательных сведений из сценария." },
  { key: "incident_signs", label: "Признаки происшествия", hint: "Верность признаков и кода ЕКП." },
  { key: "incident_type", label: "Тип происшествия", hint: "Верность типа, если код ЕКП не задан." },
  { key: "dds_choice", label: "Выбор службы", hint: "Маршрутизация для сценариев без кода ЕКП." },
  { key: "outcome", label: "Исход вызова", hint: "Карточка, справка или иной предусмотренный итог." },
  { key: "address_refined", label: "Уточнение адреса", hint: "Подтверждение уточняемых адресных ориентиров." },
  { key: "address", label: "Адрес в карточке", hint: "Совпадение адреса с эталоном по компонентам." },
  { key: "victims_count", label: "Пострадавшие", hint: "Точность числа, если оно известно из сценария." },
  { key: "required_fields", label: "Обязательные поля КИО", hint: "Полнота заполнения полей карточки." },
  { key: "dds_chain", label: "Передача карточки в ДДС", hint: "Успешная передача, если она предусмотрена." },
  { key: "callback", label: "Обратный дозвон", hint: "Норматив повторного вызова при обрыве." },
  { key: "description_grammar", label: "Грамматика описания", hint: "Учитывается, если включён критерий занятия." },
  { key: "card_fill_time", label: "Время заполнения КИО", hint: "Соблюдение заданного преподавателем норматива." },
];

const DDS_METRICS: MetricOption[] = [
  { key: "dds_primary", label: "Первичное решение", hint: "Зафиксирован ли статус «Принята» или «Не принята»." },
  { key: "dds_ack", label: "Открытие карточки", hint: "Карточка открыта в пределах лимита после появления в строке сообщений." },
  { key: "dds_decision", label: "Решение по эталону", hint: "Соответствие принятию или обоснованному отказу." },
  { key: "dds_crew", label: "Назначение бригады", hint: "Выбор необходимой бригады ДДС." },
  { key: "dds_progress", label: "Ход реагирования", hint: "Статусы по фактическому ходу работ или отказу." },
  { key: "dds_completion", label: "Завершение работ", hint: "Фиксация завершения или отказа от работ." },
  { key: "dds_reply", label: "Основание и сведения", hint: "Комментарии к статусам и содержание полученных сведений." },
  { key: "dds_work_time", label: "Первая запись", hint: "Первый статус с текстом в пределах лимита после открытия карточки." },
];

const CARD_METRICS = OPERATOR_METRICS.filter(({ key }) => [
  "incident_signs", "incident_type", "dds_choice", "address", "victims_count",
  "required_fields", "description_grammar", "card_fill_time",
].includes(key));
const CALL_METRICS = OPERATOR_METRICS.filter(({ key }) => key !== "card_fill_time");

export function ScoreWeightsEditor({
  exercise,
  values,
  onChange,
}: {
  exercise: LessonExercise;
  values: Record<string, number>;
  onChange: (weights: Record<string, number>) => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const groups: [string, MetricOption[]][] = exercise === "dds"
    ? [["Реагирование ДДС", DDS_METRICS]]
    : exercise === "card"
      ? [["Карточка КИО", CARD_METRICS]]
      : exercise === "card_dds"
        ? [["Карточка КИО", CARD_METRICS], ["Реагирование ДДС", DDS_METRICS]]
        : [["Опрос 112", CALL_METRICS], ["Реагирование ДДС", DDS_METRICS]];

  function setWeight(key: string, raw: string) {
    const next = { ...values };
    if (raw === "") delete next[key];
    else {
      const value = Number(raw);
      if (!Number.isFinite(value)) return;
      next[key] = Math.max(0, Math.min(10, value));
    }
    onChange(next);
  }

  return <details className="lesson-score-weight-editor">
    <summary>Настроить веса оценки (необязательно)</summary>
    <p className="ref">
      Пустое поле сохраняет вес сценария или стандартный вес. Значение 0 исключает
      метрику из расчёта; допустимый диапазон — от 0 до 10.
    </p>
    {groups.map(([group, metrics]) => <fieldset key={group}>
      <legend>{group}</legend>
      {metrics.map((metric) => <label className="lesson-criterion" key={metric.key}>
        <span title={metric.hint}>{metric.label}
          <small className="weight-metric-hint" id={`weight-hint-${metric.key}`}>{metric.hint}</small>
        </span>
        <input type="number" min={0} max={10} step={0.5}
          aria-label={`Вес метрики «${metric.label}»`}
          aria-describedby={`weight-hint-${metric.key}`}
          value={drafts[metric.key] ?? values[metric.key] ?? ""} placeholder="—"
          onChange={(event) => setDrafts((previous) => ({ ...previous, [metric.key]: event.target.value }))}
          onBlur={(event) => {
            const raw = event.target.value.trim();
            if (!raw) {
              setDrafts((previous) => ({ ...previous, [metric.key]: "" }));
              setWeight(metric.key, "");
              return;
            }
            const parsed = Number(raw);
            if (!Number.isFinite(parsed)) return;
            const normalized = Math.max(0, Math.min(10, parsed));
            setDrafts((previous) => ({ ...previous, [metric.key]: String(normalized) }));
            setWeight(metric.key, String(normalized));
          }} />
      </label>)}
    </fieldset>)}
  </details>;
}
