// Сборка `scenario.start` из именованных параметров консоли преподавателя.
// Отдельный .mjs, чтобы состав сообщения проверялся node-скриптом без сборки.

/** @param {import("./start.d.mts").ScenarioStartOptions} options */
export function scenarioStartMessage({
  scenarioId, mode, trainee, exercise = "dds", groupId, traineeId, ddsService,
  scenarioIds, randomScenarioIds, ddsArrivalIntervalSeconds = 60, ddsMaxWaiting = 3,
  handoffToDds = false, criteria,
}) {
  return {
    type: "scenario.start",
    scenario_id: scenarioId,
    mode,
    trainee,
    group_id: groupId || null,
    trainee_id: traineeId || null,
    dds_service: ddsService || null,
    exercise,
    handoff_to_dds: handoffToDds,
    scenario_ids: scenarioIds?.length ? scenarioIds : undefined,
    random_scenario_ids: randomScenarioIds?.length ? randomScenarioIds : undefined,
    dds_arrival_interval_seconds: ddsArrivalIntervalSeconds,
    dds_max_waiting: ddsMaxWaiting,
    criteria,
  };
}
