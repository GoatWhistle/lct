import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// Повторы запросов выключены: на занятии ошибка должна быть видна сразу,
// а не маскироваться тремя тихими попытками.
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

export function Providers({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
