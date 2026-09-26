/**
 * @typedef {Object} DdsHistoryCard
 * @property {string} card_id
 * @property {string} scenario_id
 * @property {number} [score_auto]
 * @property {number} [score_final]
 * @property {string|null} [title]
 * @property {string|null} [address]
 * @property {string|null} [description]
 * @property {string|null} [incident_type]
 * @property {string|null} [received_at]
 * @property {string|null} [ended_at]
 * @property {string|null} [managed_service]
 * @property {string[]} [recipient_services]
 */

/**
 * Filter and sort the archived DDS register without mutating the server result.
 * @param {DdsHistoryCard[]} cards
 * @param {{query: string, service: string, notification: string, descending: boolean}} filters
 * @returns {DdsHistoryCard[]}
 */
export function filterSortHistory(cards, { query, service, notification, descending }) {
  const normalizedQuery = query.trim().toLocaleLowerCase("ru-RU");
  return cards.filter((item) => {
    const textMatches = !normalizedQuery || [item.card_id, item.scenario_id, item.title ?? "", item.address ?? "",
      item.description ?? "", item.incident_type ?? ""].some((value) =>
      value.toLocaleLowerCase("ru-RU").includes(normalizedQuery));
    const serviceMatches = service === "all" || item.managed_service === service;
    const notificationMatches = notification === "all" || item.recipient_services?.includes(notification);
    return textMatches && serviceMatches && notificationMatches;
  }).sort((left, right) => {
    const leftAt = Date.parse(left.received_at ?? left.ended_at ?? "") || 0;
    const rightAt = Date.parse(right.received_at ?? right.ended_at ?? "") || 0;
    return (descending ? -1 : 1) * (leftAt - rightAt);
  });
}
