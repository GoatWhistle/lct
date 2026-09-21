// Баннер режима. Виден на всех четырёх экранах и определяет доступность подсказки:
// в контрольном режиме опоры нет, и это часть нормы контроля (docs/product/MODES.md).

import type { SessionMode } from "@/shared/types/generated";

const LABELS: Record<SessionMode, string> = {
  training: "Тренировочный режим — подсказки доступны",
  exam: "Контрольный режим — подсказки недоступны",
  self: "Самостоятельная работа — подсказки доступны",
};

export const hintsAllowed = (mode: SessionMode | undefined): boolean => mode !== "exam";

export function ModeBanner({ mode }: { mode: SessionMode | undefined }) {
  if (!mode) return null;
  return <div className={`mode-banner mode-${mode}`}>{LABELS[mode]}</div>;
}
