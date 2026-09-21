/** Миллисекунды → мм:сс. Отдельным модулем без импортов: так проверяется в Node. */
export function clock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}
