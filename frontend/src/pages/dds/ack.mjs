/**
 * @typedef {Object} AckDeadline
 * @property {number} elapsedMs
 * @property {number} limitMs
 * @property {boolean} violated
 */

/**
 * Срок первичного решения «Принята» / «Не принята» только по снимкам сервера.
 *
 * Лимит настраивает преподаватель, остановку фиксирует сервер на первом статусе
 * из `PRIMARY`: своя константа или свой список статусов на пульте расходятся
 * с тем, по чему ставится оценка. Активная карточка очереди важнее `timer.tick` —
 * после переключения карточки тик до следующей секунды ещё несёт таймер прежней.
 *
 * @param {Array<{code: string, elapsed_ms: number, limit_ms: number, stopped?: boolean}>} timers
 * @param {Array<{elapsed_ms: number, limit_ms: number, timer_stopped: boolean, active: boolean}>} queueCards
 * @returns {AckDeadline | null} null — отсчёт не показывать
 */
export function ackDeadline(timers, queueCards) {
  const active = queueCards.find((item) => item.active);
  if (active) return active.timer_stopped ? null : deadline(active.elapsed_ms, active.limit_ms);
  const timer = timers.find((item) => item.code === "dds_ack");
  if (!timer || timer.stopped) return null;
  return deadline(timer.elapsed_ms, timer.limit_ms);
}

/** Граница как у `state_for` на сервере: ровно на лимите нарушения ещё нет. */
function deadline(elapsedMs, limitMs) {
  return { elapsedMs, limitMs, violated: elapsedMs > limitMs };
}
