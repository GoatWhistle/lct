import assert from "node:assert/strict";

import { ackDeadline } from "../src/pages/dds/ack.mjs";

const queueCard = (overrides) => ({ card_id: "c1", elapsed_ms: 12_000, limit_ms: 45_000,
  timer_stopped: false, active: true, ...overrides });
const ackTimer = (overrides) => ({ code: "dds_ack", elapsed_ms: 12_000, limit_ms: 45_000,
  state: "ok", stopped: false, ...overrides });

assert.equal(ackDeadline([], []), null, "до передачи карточки отсчёта нет");

assert.deepEqual(ackDeadline([], [queueCard({ active: false }), queueCard({ card_id: "c2", elapsed_ms: 3_000 })]),
  { elapsedMs: 3_000, limitMs: 45_000, violated: false },
  "срок берётся у активной карточки очереди, а не у первой");

assert.deepEqual(ackDeadline([ackTimer({ elapsed_ms: 99_000, limit_ms: 30_000 })], [queueCard()]),
  { elapsedMs: 12_000, limitMs: 45_000, violated: false },
  "снимок активной карточки важнее timer.tick, который мог остаться от прошлой карточки");

assert.deepEqual(ackDeadline([ackTimer()], []),
  { elapsedMs: 12_000, limitMs: 45_000, violated: false },
  "без очереди (передача из 112) — таймер dds_ack из timer.tick");

assert.equal(ackDeadline([ackTimer({ elapsed_ms: 45_000 })], []).violated, false,
  "ровно на лимите нарушения нет — как state_for на сервере");
assert.equal(ackDeadline([ackTimer({ elapsed_ms: 45_001 })], []).violated, true);
assert.equal(ackDeadline([], [queueCard({ elapsed_ms: 45_001 })]).violated, true);

assert.equal(ackDeadline([ackTimer({ stopped: true })], []), null, "сервер остановил таймер — отсчёт скрыт");
assert.equal(ackDeadline([ackTimer()], [queueCard({ timer_stopped: true })]), null);
assert.equal(ackDeadline([ackTimer({ code: "dds_work" })], []), null, "чужой таймер не подменяет dds_ack");

console.log("DDS ack deadline follows server snapshots: limit, violation and stop.");
