import { useState } from "react";

import type { ScenarioSummary } from "@/shared/api/http";

type JsonObject = Record<string, unknown>;
type Draft = { id: string; status: "draft"; generation: "template_copy"; body: JsonObject };
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

export function ScenarioEditor({
  scenarios,
  onApproved,
}: {
  scenarios: ScenarioSummary[];
  onApproved: (id: string) => void | Promise<void>;
}) {
  const [sourceId, setSourceId] = useState("");
  const [title, setTitle] = useState("");
  const [draftId, setDraftId] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [bodyText, setBodyText] = useState("");
  const [validation, setValidation] = useState<Validation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

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
        source_id: sourceId || scenarios[0]?.id,
        ...(title.trim() ? { title: title.trim() } : {}),
      });
      openDraft(created);
      setNotice("Создан шаблонный черновик. Это копия сценария, не генерация ИИ.");
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

  return (
    <section className="scenario-editor">
      <h2>Редактор учебных сценариев</h2>
      <p className="ref">Создание работает локально: копия утверждённого шаблона, затем правка и проверка преподавателем.</p>
      <div className="scenario-editor-create">
        <label>Шаблон
          <select value={sourceId || scenarios[0]?.id || ""} onChange={(event) => setSourceId(event.target.value)}>
            {scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.title} · {scenario.level}</option>)}
          </select>
        </label>
        <label>Название нового сценария
          <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="необязательно" maxLength={200} />
        </label>
        <button type="button" onClick={create} disabled={busy || !scenarios.length}>Создать черновик</button>
      </div>
      <div className="scenario-editor-open">
        <label>Номер черновика
          <input value={draftId} onChange={(event) => setDraftId(event.target.value)} placeholder="draft-…" />
        </label>
        <button type="button" onClick={reopen} disabled={busy || !draftId.trim()}>Открыть</button>
      </div>
      {draft && (
        <>
          <p className="ref">Черновик {draft.id}. Факты и эталон доступны только преподавателю до утверждения. Поле score_weights позволяет задать веса метрик (0–10), например {"{\"address\": 3, \"dds_ack\": 2}"}.</p>
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
