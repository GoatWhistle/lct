// Кто выигрывает спор за поле карточки.
//
// Истина — на сервере: он распознаёт факты из речи и присылает `kio.patch`
// с `source: "auto"`. Локально хранится только то, что курсант печатает
// прямо сейчас, — до подтверждения сервером. Иначе автозаполнение и ручной
// ввод дерутся за одно поле, и это худший класс багов для отладки в последнюю
// ночь (docs/arch/FRONTEND.md).

export interface CardState {
  /** Состояние с сервера — единственная истина. */
  server: Record<string, unknown>;
  /** Правки курсанта, ещё не подтверждённые сервером: путь → значение. */
  pending: Record<string, unknown>;
}

export const empty: CardState = { server: {}, pending: {} };

export function setNested(card: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> {
  const [head, ...rest] = path.split(".");
  if (rest.length === 0) return { ...card, [head]: value };
  const nested = (card[head] as Record<string, unknown>) ?? {};
  return { ...card, [head]: setNested(nested, rest.join("."), value) };
}

/** Курсант ввёл значение: показываем сразу, отправку делает вызывающий. */
export function edit(state: CardState, path: string, value: unknown): CardState {
  return { ...state, pending: { ...state.pending, [path]: value } };
}

/**
 * Пришёл `kio.patch` от сервера.
 *
 * `auto` — сервер распознал факт из речи: он побеждает, локальная правка снимается.
 * `operator` — эхо правки: снимаем ожидание, если сервер подтвердил то же значение.
 */
export function applyPatch(
  state: CardState,
  fields: Record<string, unknown>,
  source: "auto" | "operator",
): CardState {
  let server = state.server;
  const pending = { ...state.pending };
  for (const [path, value] of Object.entries(fields)) {
    server = setNested(server, path, value);
    if (source === "auto" || pending[path] === value) delete pending[path];
  }
  return { server, pending };
}

/** Пришло состояние карточки целиком (`kio.state`, `session.snapshot`). */
export function applyState(state: CardState, card: Record<string, unknown>): CardState {
  const pending = { ...state.pending };
  for (const path of Object.keys(pending)) {
    if (readPath(card, path) === pending[path]) delete pending[path];
  }
  return { server: card, pending };
}

export function readPath(card: Record<string, unknown>, path: string): unknown {
  return path.split(".").reduce<unknown>(
    (value, part) => (value && typeof value === "object" ? (value as Record<string, unknown>)[part] : undefined),
    card,
  );
}

/** Что показать в поле: своя неподтверждённая правка либо значение сервера. */
export function display(state: CardState, path: string): unknown {
  return path in state.pending ? state.pending[path] : readPath(state.server, path);
}
