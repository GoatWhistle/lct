import assert from "node:assert/strict";

import { REPORT_RETRY_LIMIT, reportErrorMessage, reportRetryDelay, shouldRetryReport } from "../src/shared/api/report.mjs";

assert.equal(reportErrorMessage(403), "Отчёт недоступен: это занятие другого обучающегося.");
assert.equal(reportErrorMessage(409), "Оценка ещё не готова. Попробуйте получить разбор чуть позже.");
assert.equal(reportErrorMessage(500), "Не удалось получить отчёт (HTTP 500).");
assert.equal(reportErrorMessage(null), "Не удалось получить отчёт.", "сеть упала — статуса нет");

assert.equal(shouldRetryReport(0, 409), true, "оценка ещё считается — повторяем сами");
assert.equal(shouldRetryReport(REPORT_RETRY_LIMIT - 1, 409), true);
assert.equal(shouldRetryReport(REPORT_RETRY_LIMIT, 409), false, "число попыток ограничено");
assert.equal(shouldRetryReport(0, 403), false, "чужое занятие не станет своим — не повторяем");
assert.equal(shouldRetryReport(0, 404), false);
assert.equal(shouldRetryReport(0, 500), false);

assert.ok(reportRetryDelay(0) > 0);
assert.ok(reportRetryDelay(1) >= reportRetryDelay(0), "паузы не сокращаются");
assert.ok(reportRetryDelay(50) <= 5_000, "пауза ограничена сверху");

console.log("Session report: unified errors, 409 retried with a limit, 403 not retried.");
