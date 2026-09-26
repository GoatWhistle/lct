export interface DdsHistoryCard {
  card_id: string;
  scenario_id: string;
  score_auto?: number;
  score_final?: number;
  title?: string | null;
  address?: string | null;
  description?: string | null;
  incident_type?: string | null;
  received_at?: string | null;
  ended_at?: string | null;
  managed_service?: string | null;
  recipient_services?: string[];
}

export function filterSortHistory<T extends DdsHistoryCard>(
  cards: T[],
  filters: { query: string; service: string; notification: string; descending: boolean },
): T[];
