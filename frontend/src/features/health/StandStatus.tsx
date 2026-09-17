// Готовность стенда. Модели грузятся несколько секунд на старте, и курсант
// не должен догадываться, почему звонящий молчит (docs/arch/FRONTEND.md).

import { useHealth } from "@/shared/api/http";

export function StandStatus() {
  const health = useHealth();

  if (health.isError) {
    return <div className="stand stand-bad">Сервер не отвечает — занятие не запустить.</div>;
  }
  if (!health.data) return null;

  const troubles: string[] = [];
  if (!health.data.models_ready) troubles.push("голосовой контур выключен: звонок будет без звука");
  if (!health.data.embeddings_ready) troubles.push("нет модели эмбеддингов: подсказки идут по порядку чек-листа");
  if (!health.data.scenarios_loaded) troubles.push("библиотека сценариев пуста");

  if (troubles.length === 0) return null;
  return <div className="stand stand-warn">Стенд не в полной готовности — {troubles.join("; ")}.</div>;
}
