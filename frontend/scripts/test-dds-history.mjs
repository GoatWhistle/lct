import assert from "node:assert/strict";

import { filterSortHistory } from "../src/pages/dds/history.mjs";

const cards = [
  { card_id: "36814001", scenario_id: "t01-1", title: "Медицинская помощь",
    received_at: "2026-09-24T14:55:00+03:00", managed_service: "01", recipient_services: ["01"] },
  { card_id: "36814002", scenario_id: "t01-2", title: "Пожар в квартире",
    received_at: "2026-09-25T14:50:00+03:00", managed_service: "01", recipient_services: ["01", "02"] },
  { card_id: "36814003", scenario_id: "t01-3", title: "Старая запись без снимка адресатов",
    ended_at: "2026-09-23T14:50:00+03:00", managed_service: null },
];

const recipients02 = filterSortHistory(cards, {
  query: "", service: "all", notification: "02", descending: true,
});
assert.deepEqual(recipients02.map((item) => item.card_id), ["36814002"]);

const descending = filterSortHistory(cards, {
  query: "", service: "all", notification: "all", descending: true,
});
assert.deepEqual(descending.map((item) => item.card_id), ["36814002", "36814001", "36814003"]);

const ascending = filterSortHistory(cards, {
  query: "", service: "all", notification: "all", descending: false,
});
assert.deepEqual(ascending.map((item) => item.card_id), ["36814003", "36814001", "36814002"]);

const combined = filterSortHistory(cards, {
  query: "ПОЖАР", service: "01", notification: "02", descending: true,
});
assert.deepEqual(combined.map((item) => item.card_id), ["36814002"]);
assert.deepEqual(cards.map((item) => item.card_id), ["36814001", "36814002", "36814003"],
  "filter/sort must not mutate the source archive list");

console.log("DDS archive recipient filters, combined search, sorting, and immutability passed.");
