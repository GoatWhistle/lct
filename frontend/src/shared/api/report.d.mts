export const REPORT_RETRY_LIMIT: number;
export function reportErrorMessage(status: number | null): string;
export function shouldRetryReport(failureCount: number, status: number | null): boolean;
export function reportRetryDelay(attempt: number): number;
