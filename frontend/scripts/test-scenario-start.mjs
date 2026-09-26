import assert from "node:assert/strict";

import { scenarioStartMessage } from "../src/features/instructor/start.mjs";

const minimal = JSON.parse(JSON.stringify(scenarioStartMessage({
  scenarioId: "s1", mode: "exam", trainee: "Иванов",
})));
assert.deepEqual(minimal, {
  type: "scenario.start", scenario_id: "s1", mode: "exam", trainee: "Иванов",
  group_id: null, trainee_id: null, dds_service: null, exercise: "dds", handoff_to_dds: false,
  dds_arrival_interval_seconds: 60, dds_max_waiting: 3,
}, "умолчания прежние, пустые списки и критерии в сокет не уходят");

const criteria = { allowed_errors: 2 };
const full = scenarioStartMessage({
  scenarioId: "s1", mode: "training", trainee: "Петров", exercise: "call", groupId: "g1",
  traineeId: "t1", ddsService: "01", scenarioIds: ["s1", "s2"], randomScenarioIds: ["s3"],
  ddsArrivalIntervalSeconds: 30, ddsMaxWaiting: 5, handoffToDds: true, criteria,
});
assert.deepEqual(full, {
  type: "scenario.start", scenario_id: "s1", mode: "training", trainee: "Петров",
  group_id: "g1", trainee_id: "t1", dds_service: "01", exercise: "call", handoff_to_dds: true,
  scenario_ids: ["s1", "s2"], random_scenario_ids: ["s3"],
  dds_arrival_interval_seconds: 30, dds_max_waiting: 5, criteria,
});

const empty = scenarioStartMessage({ scenarioId: "s1", mode: "exam", trainee: "И", scenarioIds: [], randomScenarioIds: [] });
assert.equal(empty.scenario_ids, undefined, "пустой список не отправляется");
assert.equal(empty.random_scenario_ids, undefined);

console.log("scenario.start: named options build the same socket message.");
