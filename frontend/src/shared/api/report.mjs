// Правила чтения отчёта сессии — одни для пульта ДДС, звонка 112 и наблюдения.
// Отдельный .mjs, чтобы проверять их node-скриптом без сборки.

/** Сколько раз повторять 409: оценка к этому моменту обычно уже посчитана. */
export const REPORT_RETRY_LIMIT = 6;

/** @param {number | null} status HTTP-статус; `null` — ответа не было. */
export function reportErrorMessage(status) {
  if (status === 403) return "Отчёт недоступен: это занятие другого обучающегося.";
  if (status === 409) return "Оценка ещё не готова. Попробуйте получить разбор чуть позже.";
  if (status == null) return "Не удалось получить отчёт.";
  return `Не удалось получить отчёт (HTTP ${status}).`;
}

/**
 * 409 — оценка ещё считается, она появится сама. 403 и прочее от повтора
 * не изменятся.
 * @param {number} failureCount
 * @param {number | null} status
 */
export function shouldRetryReport(failureCount, status) {
  return status === 409 && failureCount < REPORT_RETRY_LIMIT;
}

/** @param {number} attempt */
export function reportRetryDelay(attempt) {
  return Math.min(1_000 * 2 ** attempt, 5_000);
}
