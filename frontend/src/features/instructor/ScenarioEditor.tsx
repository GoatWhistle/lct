import { useState } from "react";

import type { ScenarioSummary } from "@/shared/api/http";

type JsonObject = Record<string, unknown>;
type Draft = {
  id: string;
  status: "draft";
  generation: "template_copy" | "ai_variant" | "ai_full";
  body: JsonObject;
};
type Validation = { valid: boolean; errors: string[]; ground_truth?: JsonObject };
type GrammarCheck = { valid: boolean; checks: Array<{
  field: string; passed: boolean; errors: string[]; source: string;
}> };

async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : response.statusText;
    throw new Error(typeof detail === "string" ? detail : "Не удалось выполнить действие. Проверьте поля и повторите попытку.");
  }
  return data as T;
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Сервер принимает JSON Merge Patch, поэтому удалённое в редакторе поле
 *  передаётся как null, а неизменённые поля не отправляются. */
function diff(before: JsonObject, after: JsonObject): JsonObject {
  const patch: JsonObject = {};
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (!(key in after)) {
      patch[key] = null;
    } else if (!(key in before)) {
      patch[key] = after[key];
    } else if (isObject(before[key]) && isObject(after[key])) {
      const nested = diff(before[key], after[key]);
      if (Object.keys(nested).length) patch[key] = nested;
    } else if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
      patch[key] = after[key];
    }
  }
  return patch;
}

function stringField(body: JsonObject, key: string): string {
  return typeof body[key] === "string" ? body[key] : "";
}

function generationLabel(generation: Draft["generation"]): string {
  if (generation === "ai_full") return "новый сюжет и эталон локальной модели";
  if (generation === "ai_variant") return "вариант локальной модели";
  return "копия шаблона";
}

const SCENARIO_CONTENT_LABELS: Record<string, string> = {
  f_address: "Адрес", f_caller: "Данные заявителя", f_conscious: "Состояние сознания",
  f_fled: "Сведения о скрывшемся", f_gas: "Запах газа", f_observer: "Сведения очевидца",
  f_people: "Люди на месте", f_scale: "Масштаб происшествия", f_smoke: "Дым",
  f_suspects: "Сведения о подозреваемых", f_threat: "Угроза", f_vehicle: "Транспортное средство",
  f_victims: "Пострадавшие", f_what_burns: "Что горит", f_what_happened: "Что произошло",
  f_who: "Кто пострадал или участвует", q_address: "Уточнить адрес",
  q_address_check: "Проверить адрес", q_caller: "Уточнить данные заявителя",
  q_can_move: "Может ли человек передвигаться", q_conscious: "Проверить сознание",
  q_fled: "Уточнить сведения о скрывшемся", q_gas: "Уточнить запах газа",
  q_observer: "Уточнить сведения очевидца", q_people: "Спросить о людях на месте",
  q_scale: "Уточнить масштаб происшествия", q_smoke: "Уточнить сведения о дыме",
  q_suspects: "Уточнить сведения о подозреваемых", q_threat: "Уточнить угрозу",
  q_vehicle: "Уточнить сведения о транспорте", q_victims: "Спросить о пострадавших",
  q_what_burns: "Уточнить, что горит", q_what_happened: "Уточнить, что произошло",
  q_who: "Уточнить, кто пострадал или участвует",
};

function contentLabel(id: unknown, kind: "fact" | "question", index: number): string {
  const key = typeof id === "string" ? id : "";
  return SCENARIO_CONTENT_LABELS[key]
    ?? `${kind === "fact" ? "Дополнительный факт" : "Дополнительный вопрос"} ${index + 1}`;
}

function grammarFieldLabel(field: string): string {
  if (field === "first_line") return "Первая реплика звонящего";
  const fact = /^facts\.([^.]+)\.(value|refined)$/.exec(field);
  if (fact) {
    const subject = SCENARIO_CONTENT_LABELS[fact[1]] ?? "Факт сценария";
    return `${fact[2] === "refined" ? "Уточнённый ответ" : "Ответ звонящего"}: ${subject}`;
  }
  return "Текст сценария";
}

export function ScenarioEditor({
  scenarios,
  selectedScenarioId,
  onApproved,
  onLibraryChanged,
}: {
  scenarios: ScenarioSummary[];
  selectedScenarioId: string;
  onApproved: (id: string) => void | Promise<void>;
  onLibraryChanged: () => void | Promise<void>;
}) {
  const [sourceId, setSourceId] = useState("");
  const [title, setTitle] = useState("");
  const [instruction, setInstruction] = useState("");
  const [revisionComment, setRevisionComment] = useState("");
  const [draftId, setDraftId] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editorBody, setEditorBody] = useState<JsonObject | null>(null);
  const [validation, setValidation] = useState<Validation | null>(null);
  const [grammarCheck, setGrammarCheck] = useState<GrammarCheck | null>(null);
  const [manualEdits, setManualEdits] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [archiveCandidate, setArchiveCandidate] = useState("");
  const [lastArchived, setLastArchived] = useState<{ id: string; title: string } | null>(null);
  const effectiveSourceId = sourceId || selectedScenarioId || scenarios[0]?.id || "";
  const effectiveSource = scenarios.find((scenario) => scenario.id === effectiveSourceId);

  function openDraft(value: Draft) {
    setDraft(value);
    setDraftId(value.id);
    setEditorBody(value.body);
    setValidation(null);
    setGrammarCheck(null);
    setManualEdits(false);
    setError("");
    setNotice("");
  }

  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    await perform(async () => {
      const created = await api<Draft>("/api/scenarios/drafts/from-template", "POST", {
        source_id: effectiveSourceId,
        ...(title.trim() ? { title: title.trim() } : {}),
      });
      openDraft(created);
      setNotice("Создан шаблонный черновик. Это копия сценария, не генерация ИИ.");
    });
  }

  async function generate() {
    await perform(async () => {
      setNotice("Qwen формирует вариацию… Обычно это занимает несколько секунд.");
      const created = await api<Draft>("/api/scenarios/drafts/generate", "POST", {
        source_id: effectiveSourceId,
        instruction: instruction.trim(),
      });
      openDraft(created);
      setNotice("Локальная модель создала вариацию. Проверьте сюжет, факты и эталон перед утверждением.");
    });
  }

  async function generateFull() {
    await perform(async () => {
      setNotice("Qwen формирует новый сюжет по полям… Обычно это занимает 10–30 секунд.");
      const created = await api<Draft>("/api/scenarios/drafts/generate-from-description", "POST", {
        source_id: effectiveSourceId,
        description: instruction.trim(),
      });
      openDraft(created);
      setNotice("Локальная модель создала новый сюжет, все ответы и мягкий эталон. Класс ЕКП и структура опроса взяты из выбранного каркаса; проверьте результат.");
    });
  }

  async function reopen() {
    if (!draftId.trim()) return;
    await perform(async () => {
      openDraft(await api<Draft>(`/api/scenarios/drafts/${encodeURIComponent(draftId.trim())}`));
    });
  }

  async function save(): Promise<Draft> {
    if (!draft) throw new Error("Сначала создайте или откройте черновик");
    if (!editorBody) throw new Error("Откройте черновик заново.");
    const patch = diff(draft.body, editorBody);
    if (!Object.keys(patch).length) return draft;
    const updated = await api<Draft>(`/api/scenarios/drafts/${encodeURIComponent(draft.id)}`, "PATCH", patch);
    setDraft(updated);
    setEditorBody(updated.body);
    setValidation(null);
    return updated;
  }

  function editBodyField(key: string, value: unknown) {
    setEditorBody((previous) => previous ? { ...previous, [key]: value } : previous);
    setValidation(null);
    setGrammarCheck(null);
    setManualEdits(true);
  }

  function editArrayField(arrayKey: string, index: number, key: string, value: unknown) {
    setEditorBody((previous) => {
      if (!previous || !Array.isArray(previous[arrayKey])) return previous;
      const items = [...previous[arrayKey] as unknown[]];
      if (!isObject(items[index])) return previous;
      items[index] = { ...items[index], [key]: value };
      return { ...previous, [arrayKey]: items };
    });
    setValidation(null);
    setGrammarCheck(null);
    setManualEdits(true);
  }

  function editDdsDecision(key: "expected" | "reason", value: string) {
    setEditorBody((previous) => {
      if (!previous) return previous;
      const current = isObject(previous.dds_decision)
        ? previous.dds_decision : { expected: "accept" };
      const decision = { ...current, [key]: value };
      if (key === "expected" && value === "accept") delete decision.reason;
      if (key === "expected" && value === "decline" && typeof decision.reason !== "string") {
        decision.reason = "";
      }
      return { ...previous, dds_decision: decision };
    });
    setValidation(null);
  }

  async function validate() {
    await perform(async () => {
      const current = await save();
      const result = await api<Validation>(`/api/scenarios/drafts/${encodeURIComponent(current.id)}/validate`, "POST");
      setValidation(result);
      setNotice(result.valid ? "Черновик прошёл проверку и может быть утверждён." : "Найдены ошибки в сценарии.");
    });
  }

  async function checkGrammar() {
    await perform(async () => {
      const current = await save();
      const result = await api<GrammarCheck>(
        `/api/scenarios/drafts/${encodeURIComponent(current.id)}/grammar-check`, "POST",
      );
      setGrammarCheck(result);
      setManualEdits(!result.valid);
      setNotice(result.valid
        ? "Языковая проверка пройдена для первой реплики и текстовых фактов."
        : "Языковая проверка нашла замечания; исправьте их и запустите проверку повторно.");
    });
  }

  async function revise() {
    await perform(async () => {
      setNotice("Qwen перерабатывает черновик по комментарию…");
      const current = await save();
      const updated = await api<Draft>(
        `/api/scenarios/drafts/${encodeURIComponent(current.id)}/revise`, "POST",
        { comment: revisionComment.trim() },
      );
      openDraft(updated);
      setRevisionComment("");
      setNotice("Локальная модель переработала этот же черновик по комментарию. Проверьте изменения заново.");
    });
  }

  async function approve() {
    await perform(async () => {
      const current = await save();
      if (!validation?.valid) throw new Error("Сначала проверьте черновик кнопкой «Проверить»");
      await api(`/api/scenarios/drafts/${encodeURIComponent(current.id)}/approve`, "POST");
      setDraft(null);
      setValidation(null);
      setNotice("Сценарий утверждён и доступен для нового занятия.");
      await onApproved(current.id);
    });
  }

  async function archiveSelected() {
    await perform(async () => {
      const selected = scenarios.find((item) => item.id === effectiveSourceId);
      if (!selected) throw new Error("Выберите сценарий для архивирования");
      await api(`/api/scenarios/${encodeURIComponent(selected.id)}`, "DELETE");
      setLastArchived({ id: selected.id, title: selected.title });
      setArchiveCandidate("");
      setSourceId("");
      setNotice(`Сценарий «${selected.title}» перемещён в архив и больше не назначается.`);
      await onLibraryChanged();
    });
  }

  async function restoreLast() {
    if (!lastArchived) return;
    await perform(async () => {
      await api(`/api/scenarios/${encodeURIComponent(lastArchived.id)}/restore`, "POST");
      const restored = lastArchived;
      setLastArchived(null);
      setSourceId(restored.id);
      setNotice(`Сценарий «${restored.title}» восстановлен в учебной библиотеке.`);
      await onLibraryChanged();
    });
  }

  const workingBody = editorBody ?? draft?.body ?? {};
  const ddsDecision = isObject(workingBody.dds_decision) ? workingBody.dds_decision : {};
  const expectedDdsDecision = ddsDecision.expected === "decline" ? "decline" : "accept";
  const editableFacts = Array.isArray(workingBody.facts)
    ? workingBody.facts.map((item, index) => ({ item, index })).filter(({ item }) => isObject(item)) as Array<{ item: JsonObject; index: number }>
    : [];
  const editableChecklist = Array.isArray(workingBody.checklist)
    ? workingBody.checklist.map((item, index) => ({ item, index })).filter(({ item }) => isObject(item)) as Array<{ item: JsonObject; index: number }>
    : [];

  return (
    <section className="scenario-editor" id="ai-scenario-editor" aria-busy={busy}>
      <h2>ИИ-формирователь и редактор сценариев</h2>
      <p className="ref">Можно сделать безопасную вариацию либо новый сюжет из описания. Выбранный утверждённый сценарий задаёт класс ЕКП и структуру опроса; при новом сюжете Qwen заново создаёт все ответы, адрес и число пострадавших. Публикация — только после вашей проверки.</p>
      <div className="scenario-editor-create">
        <label>Каркас категории и опроса
          <select value={effectiveSourceId} onChange={(event) => setSourceId(event.target.value)}>
            {scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.title} · {scenario.level}</option>)}
          </select>
          {effectiveSource && <small className="ref">
            Фиксированный путь ЕКП: {effectiveSource.signs?.length
              ? effectiveSource.signs.join(" → ") : "без размеченных признаков"}
            {effectiveSource.incident_code ? ` · код ${effectiveSource.incident_code}` : ""}
          </small>}
        </label>
        <label>Название нового сценария
          <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="необязательно" maxLength={200} />
        </label>
        <button type="button" onClick={create} disabled={busy || !scenarios.length}>Создать черновик</button>
      </div>
      <div className="scenario-editor-create">
        <label>Указание для локальной модели
          <input value={instruction} onChange={(event) => setInstruction(event.target.value)}
            placeholder="Например: ночное возгорание, плотный дым, заявитель паникует"
            maxLength={1000} />
          <small className="ref">Описание меняет обстоятельства, но не показанный выше путь ЕКП.</small>
        </label>
        <button type="button" onClick={generate} disabled={busy || !scenarios.length || instruction.trim().length < 10}>
          Создать вариант ИИ
        </button>
        <button type="button" onClick={generateFull}
          disabled={busy || !scenarios.length || instruction.trim().length < 20}>
          Создать новый сюжет и эталон
        </button>
      </div>
      <div className="scenario-editor-open">
        <label>Номер черновика
          <input value={draftId} onChange={(event) => setDraftId(event.target.value)} placeholder="draft-…" />
        </label>
        <button type="button" onClick={reopen} disabled={busy || !draftId.trim()}>Открыть</button>
      </div>
      <div className="scenario-library-actions">
        {!archiveCandidate ? <button type="button" disabled={busy || !effectiveSourceId || !effectiveSource?.can_manage}
          onClick={() => setArchiveCandidate(effectiveSourceId)}>Архивировать выбранный сценарий</button> : <>
          <span>Сценарий исчезнет из назначения, но история занятий сохранится.</span>
          <button type="button" className="danger" disabled={busy}
            onClick={archiveSelected}>Подтвердить архивирование</button>
          <button type="button" disabled={busy} onClick={() => setArchiveCandidate("")}>Отмена</button>
        </>}
        {effectiveSource && !effectiveSource.can_manage && <small className="ref">
          Базовые сценарии и сценарии других преподавателей доступны только для чтения.
        </small>}
        {lastArchived && <button type="button" disabled={busy} onClick={restoreLast}>
          Вернуть «{lastArchived.title}» из архива
        </button>}
      </div>
      {draft && (
        <>
          <p className="ref">Черновик {draft.id} ({generationLabel(draft.generation)}). Факты и эталон доступны только преподавателю до утверждения. Веса критериев занятия настраиваются отдельно при запуске.</p>
          <div className="scenario-editor-result">
            <strong>Предпросмотр: {stringField(workingBody, "title")}</strong>
            <p>Первая реплика: «{stringField(workingBody, "first_line")}»</p>
            <p>Эталон первичного решения ДДС: {expectedDdsDecision === "decline"
              ? `отказать — ${stringField(ddsDecision, "reason") || "нужно указать причину"}`
              : "принять карточку"}</p>
            <p className="ref">Проверьте, что сюжетные факты и ответы согласуются друг с другом.</p>
            <ul>{editableFacts.map(({ item, index }) =>
              <li key={`${index}-${String(item.id)}`}><strong>{contentLabel(item.id, "fact", index)}:</strong> {String(item.value ?? "")}</li>
            )}</ul>
          </div>
          <div className="scenario-human-editor">
            <h3>Содержание сценария</h3>
            <label>Название занятия
              <input value={stringField(workingBody, "title")} maxLength={200}
                onChange={(event) => editBodyField("title", event.target.value)} />
            </label>
            <label>Первая реплика звонящего
              <textarea rows={3} value={stringField(workingBody, "first_line")}
                onChange={(event) => editBodyField("first_line", event.target.value)} />
            </label>
            <fieldset className="scenario-dds-decision">
              <legend>Эталон первичного решения ДДС</legend>
              <p className="ref">Оставьте «Принять», если карточка относится к компетенции службы. Выберите отказ только для подтверждённого исключения: например, дубль или неподведомственная территория.</p>
              <label>Какое решение считается правильным
                <select value={expectedDdsDecision}
                  onChange={(event) => editDdsDecision("expected", event.target.value)}>
                  <option value="accept">Принять карточку</option>
                  <option value="decline">Не принять карточку</option>
                </select>
              </label>
              {expectedDdsDecision === "decline" && <label>Почему отказ здесь правильный
                <input aria-label="Причина эталонного отказа ДДС" value={stringField(ddsDecision, "reason")}
                  maxLength={500} placeholder="Например: дубликат уже передан по карточке…"
                  onChange={(event) => editDdsDecision("reason", event.target.value)} />
                <small className="ref">Причина обязательна; она войдёт в норму оценки и отчёт курсанта.</small>
              </label>}
            </fieldset>
            <h4>Факты и ответы</h4>
            {editableFacts.map(({ item, index }) => <fieldset key={`${index}-${String(item.id)}`}>
              <legend>{contentLabel(item.id, "fact", index)}</legend>
              <label>Что сообщает звонящий
                <textarea rows={2} value={stringField(item, "value")}
                  onChange={(event) => editArrayField("facts", index, "value", event.target.value)} />
              </label>
              {typeof item.refined === "string" && <label>Уточнённый ответ после переспроса
                <input value={item.refined} onChange={(event) => editArrayField("facts", index, "refined", event.target.value)} />
              </label>}
            </fieldset>)}
            {editableChecklist.length > 0 && <>
              <h4>Контрольные вопросы и эталонные ответы</h4>
              {editableChecklist.map(({ item, index }) => <fieldset key={`${index}-${String(item.id)}`}>
                <legend>{contentLabel(item.id, "question", index)}</legend>
                <label>Ожидаемый вопрос оператора
                  <input value={stringField(item, "question")}
                    onChange={(event) => editArrayField("checklist", index, "question", event.target.value)} />
                </label>
                <label>Факт, который нужно выяснить
                  <textarea rows={2} value={stringField(item, "fact")}
                    onChange={(event) => editArrayField("checklist", index, "fact", event.target.value)} />
                </label>
              </fieldset>)}
            </>}
            <p className="ref">После правок сохраните черновик, проверьте сценарий и повторите языковую проверку перед утверждением.</p>
          </div>
          <div className="scenario-editor-create">
            <label>Комментарий для переработки
              <input value={revisionComment} onChange={(event) => setRevisionComment(event.target.value)}
                placeholder="Например: убери упоминание газа и сделай дым плотнее"
                maxLength={1000} />
            </label>
            <button type="button" onClick={revise}
              disabled={busy || revisionComment.trim().length < 10}>Переработать с ИИ</button>
          </div>
          <div className="scenario-editor-actions">
            <button type="button" onClick={() => void perform(async () => { await save(); setNotice("Черновик сохранён."); })} disabled={busy}>Сохранить</button>
            <button type="button" onClick={validate} disabled={busy}>Проверить</button>
            {manualEdits && <button type="button" onClick={checkGrammar} disabled={busy}>Проверить грамматику после правок</button>}
            <button type="button" className="start" onClick={approve}
              disabled={busy || !validation?.valid || manualEdits}>Утвердить</button>
          </div>
        </>
      )}
      {validation && <div className={validation.valid ? "scenario-editor-result" : "scenario-editor-result violated"}>
        {validation.valid ? <><strong>Проверка пройдена</strong><dl className="scenario-ground-truth">
          {typeof validation.ground_truth?.incident_type === "string" && <div><dt>Тип происшествия</dt><dd>{validation.ground_truth.incident_type}</dd></div>}
          {typeof validation.ground_truth?.dds === "string" && <div><dt>Ответственная служба</dt><dd>{validation.ground_truth.dds}</dd></div>}
          {typeof validation.ground_truth?.incident_code === "string" && <div><dt>Код ЕКП</dt><dd>{validation.ground_truth.incident_code}</dd></div>}
          {typeof validation.ground_truth?.address === "string" && <div><dt>Адрес</dt><dd>{validation.ground_truth.address}</dd></div>}
          {typeof validation.ground_truth?.victims === "number" && <div><dt>Пострадавших</dt><dd>{validation.ground_truth.victims}</dd></div>}
          {Array.isArray(validation.ground_truth?.notify) && <div><dt>Оповещаемые службы</dt><dd>{validation.ground_truth.notify.map(String).join(", ") || "не назначены"}</dd></div>}
          {Array.isArray(validation.ground_truth?.required_facts) && <div><dt>Обязательные факты</dt><dd>{validation.ground_truth.required_facts.map(String).join(", ") || "не заданы"}</dd></div>}
        </dl></> :
          <><strong>Ошибки проверки</strong><ul>{validation.errors.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></>}
      </div>}
      {grammarCheck && <div className={grammarCheck.valid ? "scenario-editor-result" : "scenario-editor-result violated"}>
        <strong>{grammarCheck.valid ? "Грамматика: замечаний нет" : "Замечания к грамматике"}</strong>
        <ul>{grammarCheck.checks.flatMap((item) => item.errors.map((message, index) =>
          <li key={`${item.field}-${index}`}><strong>{grammarFieldLabel(item.field)}:</strong> {message}</li>
        ))}</ul>
        <small className="ref">Проверены первая реплика и текстовые значения фактов; код не переписывает текст. Источник проверки для каждого поля: правила или Vikhr.</small>
      </div>}
      {error && <p className="violated" role="alert">{error}</p>}
      {notice && <p className="state-ok" role="status">{notice}</p>}
    </section>
  );
}
