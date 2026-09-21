// Подписи статусов реагирования и статусов карточки.
//
// Формулировки — из памятки ГБУ «Система 112» дословно (docs/spec/DATASET.md).
// Переводить их на «свой» язык нельзя: диспетчер узнаёт эти слова на своём
// боевом АРМ, и тренажёр должен говорить ими же.
//
// Сами значения приходят из домена через generated.ts — здесь только подписи,
// иначе список статусов разъедется с сервером.

import type { CardStatus, ServiceStatus } from "@/shared/types/generated";

export const SERVICE_STATUS_LABELS: Record<ServiceStatus, string> = {
  added: "Добавлена",
  received: "Получена службой",
  accepted: "Принята",
  declined: "Не принята",
  responding: "Начало реагирования",
  arrived: "Прибытие",
  working: "Проведение работ",
  completed: "Работы завершены",
  refused: "Отказ от выполнения работ",
};

export const CARD_STATUS_LABELS: Record<CardStatus, string> = {
  registered: "Зарегистрирована",
  processed: "Отработана",
  checked: "Проверена",
  not_notified: "Не оповещено",
  refused: "Отказ",
  not_completed: "Не завершено",
  completed: "Завершена",
};

/** Три статуса, которые в боевой системе красные и попадают в раздел отдела
 *  контроля реагирования. Единственное место, где цвет несёт смысл. */
export const ALARMING: CardStatus[] = ["not_notified", "refused", "not_completed"];
