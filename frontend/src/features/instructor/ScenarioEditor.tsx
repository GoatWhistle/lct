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
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
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

function pretty(body: JsonObject): string {
  return JSON.stringify(body, null, 2);
}

function stringField(body: JsonObject, key: string): string {
  return typeof body[key] === "string" ? body[key] : "";
}

function generationLabel(generation: Draft["generation"]): string {
  if (generation === "ai_full") return "новый сюжет и эталон локальной модели";
  if (generation === "ai_variant") return "вариант локальной модели";
  return "копия шаблона";
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
  const [bodyText, setBodyText] = useState("");
  const [validation, setValidation] = useState<Validation | null>(null);
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
    setBodyText(pretty(value.body));
    setValidation(null);
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
    let parsed: unknown;
    try {
      parsed = JSON.parse(bodyText);
    } catch (cause) {
      throw new Error(`Некорректный JSON: ${cause instanceof Error ? cause.message : String(cause)}`);
    }
    if (!isObject(parsed)) throw new Error("Тело сценария должно быть JSON-объектом");
    if (parsed.id !== draft.id) throw new Error("id черновика менять нельзя");
    const patch = diff(draft.body, parsed);
    if (!Object.keys(patch).length) return draft;
    const updated = await api<Draft>(`/api/scenarios/drafts/${encodeURIComponent(draft.id)}`, "PATCH", patch);
    setDraft(updated);
    setBodyText(pretty(updated.body));
    setValidation(null);
    return updated;
  }

  async function validate() {
    await perform(async () => {
      const current = await save();
      const result = await api<Validation>(`/api/scenarios/drafts/${encodeURIComponent(current.id)}/validate`, "POST");
      setValidation(result);
      setNotice(result.valid ? "Черновик прошёл проверку и может быть утверждён." : "Найдены ошибки в сценарии.");
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
          <p className="ref">Черновик {draft.id} ({generationLabel(draft.generation)}). Факты и эталон доступны только преподавателю до утверждения. Поле score_weights позволяет задать веса метрик (0–10), например {"{\"address\": 3, \"dds_ack\": 2}"}.</p>
          <div className="scenario-editor-result">
            <strong>Предпросмотр: {stringField(draft.body, "title")}</strong>
            <p>Первая реплика: «{stringField(draft.body, "first_line")}»</p>
            <p className="ref">Сюжетные факты — проверьте, что они согласуются друг с другом и с эталоном:</p>
            <ul>{Array.isArray(draft.body.facts) && draft.body.facts.map((item, index) =>
              isObject(item) ? <li key={`${index}-${String(item.id)}`}>{String(item.id)}: {String(item.value ?? "")}</li> : null
            )}</ul>
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
          <label className="scenario-editor-json">JSON тела сценария
            <textarea value={bodyText} onChange={(event) => { setBodyText(event.target.value); setValidation(null); }} spellCheck={false} />
          </label>
          <div className="scenario-editor-actions">
            <button type="button" onClick={() => void perform(async () => { await save(); setNotice("Черновик сохранён."); })} disabled={busy}>Сохранить</button>
            <button type="button" onClick={validate} disabled={busy}>Проверить</button>
            <button type="button" className="start" onClick={approve} disabled={busy || !validation?.valid}>Утвердить</button>
          </div>
        </>
      )}
      {validation && <div className={validation.valid ? "scenario-editor-result" : "scenario-editor-result violated"}>
        {validation.valid ? <><strong>Проверка пройдена</strong><pre>{pretty(validation.ground_truth ?? {})}</pre></> :
          <><strong>Ошибки проверки</strong><ul>{validation.errors.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></>}
      </div>}
      {error && <p className="violated" role="alert">{error}</p>}
      {notice && <p className="state-ok" role="status">{notice}</p>}
    </section>
  );
}
