// Радар компетенций: проекция уже посчитанных метрик, без пересчёта весов.
// Преподаватель читает его за секунду: где провал — там тема следующего занятия
// (docs/product/METHODOLOGY.md).

import type { CompetencyScore } from "@/shared/types/generated";

export const COMPETENCY_LABELS: Record<string, string> = {
  intake: "Приём вызова",
  interview: "Опрос",
  card: "Карточка",
  routing: "Маршрутизация",
  norms: "Нормативы",
  communication: "Коммуникация",
};

export function Radar({ values, size = 220 }: { values: CompetencyScore[]; size?: number }) {
  if (values.length < 3) return null;
  const center = size / 2;
  const radius = center - 28;

  const point = (index: number, value: number) => {
    const angle = (Math.PI * 2 * index) / values.length - Math.PI / 2;
    return [center + radius * value * Math.cos(angle), center + radius * value * Math.sin(angle)];
  };
  const polygon = values.map((item, index) => point(index, item.value).join(",")).join(" ");

  return (
    <svg width={size} height={size} className="radar" role="img" aria-label="радар компетенций">
      {[0.25, 0.5, 0.75, 1].map((ring) => (
        <polygon
          key={ring}
          points={values.map((_, index) => point(index, ring).join(",")).join(" ")}
          fill="none"
          stroke="var(--line)"
        />
      ))}
      <polygon points={polygon} fill="rgba(31,111,61,0.18)" stroke="var(--ok)" strokeWidth={2} />
      {values.map((item, index) => {
        const [x, y] = point(index, 1.16);
        return (
          <text key={item.competency} x={x} y={y} textAnchor="middle" fontSize={10}>
            {COMPETENCY_LABELS[item.competency] ?? item.competency}
          </text>
        );
      })}
    </svg>
  );
}
