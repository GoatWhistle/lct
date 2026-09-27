// Разбор по отметкам: преподаватель подтверждает, снимает и добавляет отметку.
//
// Балл пересчитывает сервер теми же метриками и весами (lct-35): здесь только
// решение и его причина. Причина обязательна — она остаётся в отчёте и в CSV/PDF.

import { useState } from "react";

import type { ErrorCode, FindingDecision, SessionReport } from "@/shared/types/generated";

const DDS_CODES: ErrorCode[] = ["D1", "D2", "D3", "D4", "D5", "D6"];
const CALL_CODES: ErrorCode[] = ["E1", "E2", "E3", "E4", "E5", "E6"];

async function send(url: string, body: unknown): Promise<SessionReport> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : response.statusText;
    throw new Error(typeof detail === "string" ? detail : "Не удалось сохранить решение.");
  }
  return data as SessionReport;
}

export function FindingReview({ report, onChange }: {
  report: SessionReport;
  onChange: (report: SessionReport) => void;
}) {
  const cards = report.card_results ?? [];
  const codes = report.exercise === "dds" ? DDS_CODES
    : cards.length ? [...CALL_CODES, ...DDS_CODES] : CALL_CODES;
  const [reasons, setReasons] = useState<Record<number, string>>({});
  const [code, setCode] = useState<ErrorCode>(codes[0]);
  const [card, setCard] = useState(cards.length ? "1" : "");
  const [fact, setFact] = useState("");
  const [norm, setNorm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const base = `/api/sessions/${report.session_id}/findings`;

  async function run(action: () => Promise<SessionReport>) {
    setBusy(true);
    setError("");
    try {
      onChange(await action());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось сохранить решение.");
    } finally {
      setBusy(false);
    }
  }

  function decide(index: number, decision: FindingDecision) {
    const reason = (reasons[index] ?? "").trim();
    if (!reason) {
      setError("Укажите причину решения по отметке.");
      return;
    }
    void run(async () => {
      const updated = await send(`${base}/${index}/review`, { decision, reason });
      setReasons((current) => ({ ...current, [index]: "" }));
      return updated;
    });
  }

  function addFinding() {
    if (!fact.trim() || !norm.trim()) {
      setError("Для отметки нужны факт и норма.");
      return;
    }
    void run(async () => {
      const updated = await send(base, {
        code, fact: fact.trim(), norm: norm.trim(), card: card ? Number(card) : null,
      });
      setFact("");
      setNorm("");
      return updated;
    });
  }

  return (
    <section className="debrief finding-review">
      <h2>Разбор по отметкам</h2>
      <p className="ref">
        Снятая отметка не штрафует свою метрику, добавленная — штрафует. Балл пересчитывается
        по тем же критериям и весам; автоматическая оценка ({report.score_auto.toFixed(1)}) сохраняется рядом.
        {report.score_reviewed != null && ` После разбора: ${report.score_reviewed.toFixed(1)}.`}
      </p>
      <table className="grid">
        <tbody>
          {report.findings.map((finding, index) => (
            <tr key={index}>
              <th>{finding.code}</th>
              <td>
                {finding.summary}
                <div className="ref">{finding.fact}{finding.norm && ` · ${finding.norm}`}</div>
                {finding.review && <div className={finding.review.decision === "dismissed" ? "ref" : "state-violated"}>
                  {finding.review.decision === "dismissed" ? "Снята" : "Подтверждена"}: {finding.review.reason}
                </div>}
                <div className="finding-review-actions">
                  <input aria-label={`Причина решения по отметке ${index + 1}`} maxLength={1000}
                    placeholder="Причина решения" value={reasons[index] ?? ""}
                    onChange={(event) => setReasons((current) => ({ ...current, [index]: event.target.value }))} />
                  <button type="button" disabled={busy} onClick={() => decide(index, "confirmed")}>Подтвердить</button>
                  <button type="button" disabled={busy} onClick={() => decide(index, "dismissed")}>Снять</button>
                </div>
              </td>
            </tr>
          ))}
          {report.findings.length === 0 && <tr><td colSpan={2}>Автоматических отметок нет.</td></tr>}
        </tbody>
      </table>

      <h3>Добавить отметку</h3>
      <div className="finding-review-form">
        <label>Код
          <select value={code} onChange={(event) => setCode(event.target.value as ErrorCode)}>
            {codes.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        {cards.length > 0 && <label>Карточка
          <select value={card} onChange={(event) => setCard(event.target.value)}>
            {cards.map((item, index) => <option key={item.card_id} value={String(index + 1)}>
              {index + 1}: {item.title || item.scenario_id}
            </option>)}
          </select>
        </label>}
        <label>Факт
          <input maxLength={1000} value={fact} onChange={(event) => setFact(event.target.value)}
            placeholder="Что сделал курсант" />
        </label>
        <label>Норма
          <input maxLength={1000} value={norm} onChange={(event) => setNorm(event.target.value)}
            placeholder="Как требовалось" />
        </label>
        <button type="button" disabled={busy || !fact.trim() || !norm.trim()} onClick={addFinding}>
          {busy ? "Сохраняем…" : "Добавить отметку"}
        </button>
      </div>
      {error && <p className="violated" role="alert">{error}</p>}
    </section>
  );
}
