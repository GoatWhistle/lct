import { useState, type FormEvent } from "react";

import { useMe } from "@/features/auth/useAuth";
import {
  useArchiveMaterial,
  useAssignMaterial,
  useAssignMaterialGroup,
  useCompleteMaterial,
  useCreateMaterial,
  useGroups,
  useMaterials,
  useScenarios,
  useStartMaterialPractice,
  useTrainees,
  useUpdateMaterial,
  type LearningMaterial,
} from "@/shared/api/http";
import type { Level } from "@/shared/types/generated";

async function encodeFile(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768));
  }
  return btoa(binary);
}

function size(value: number): string {
  if (value < 1024) return `${value} Б`;
  return `${(value / 1024).toFixed(value < 1024 * 100 ? 1 : 0)} КБ`;
}

export function Materials() {
  const me = useMe();
  const instructor = me.data?.role === "instructor";
  const trainee = me.data?.role === "trainee";
  const [showArchived, setShowArchived] = useState(false);
  const materials = useMaterials(showArchived && !trainee);
  const scenarios = useScenarios();
  const trainees = useTrainees(instructor);
  const groups = useGroups(instructor);
  const create = useCreateMaterial();
  const update = useUpdateMaterial();
  const archive = useArchiveMaterial();
  const assign = useAssignMaterial();
  const assignGroup = useAssignMaterialGroup();
  const complete = useCompleteMaterial();
  const startPractice = useStartMaterialPractice();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [level, setLevel] = useState<Level>("L1");
  const [kind, setKind] = useState<"text" | "file">("text");
  const [body, setBody] = useState("");
  const [scenarioId, setScenarioId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [traineeId, setTraineeId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<Pick<LearningMaterial,
    "id" | "title" | "description" | "level" | "body" | "scenario_id"> | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setNotice("");
    const content = file ? await encodeFile(file) : undefined;
    create.mutate({
      title,
      description,
      level,
      kind,
      body: kind === "text" ? body : "",
      scenario_id: scenarioId || null,
      file_name: file?.name,
      media_type: file?.type || "application/octet-stream",
      content_base64: content,
    }, {
      onSuccess: () => {
        setTitle(""); setDescription(""); setBody(""); setFile(null);
        setNotice("Материал сохранён локально.");
      },
    });
  }

  return <main className="page materials-page">
    <header className="materials-header">
      <div>
        <h1>Учебные материалы</h1>
        <p>Локальная справочная база и назначения курсантам без внешних сервисов.</p>
      </div>
      <a href={instructor ? "/instructor" : "/profile"}>← Назад к рабочему месту</a>
    </header>

    {instructor && <section className="material-create">
      <h2>Создать материал</h2>
      <form onSubmit={(event) => void submit(event)}>
        <label>Название<input value={title} required minLength={3}
          onChange={(event) => setTitle(event.target.value)} /></label>
        <label>Описание<input value={description}
          onChange={(event) => setDescription(event.target.value)} /></label>
        <div className="material-create-row">
          <label>Сложность<select value={level} onChange={(event) => setLevel(event.target.value as Level)}>
            <option value="L1">L1 · базовый</option><option value="L2">L2 · рабочий</option>
            <option value="L3">L3 · сложный</option>
          </select></label>
          <label>Связанная практика<select value={scenarioId} onChange={(event) => setScenarioId(event.target.value)}>
            <option value="">без сценария</option>
            {scenarios.data?.map((scenario) => <option key={scenario.id} value={scenario.id}>
              {scenario.title} · {scenario.level}
            </option>)}
          </select></label>
        </div>
        <div className="mode-row">
          <label className="mode-choice"><input type="radio" checked={kind === "text"}
            onChange={() => setKind("text")} /> Текстовая памятка</label>
          <label className="mode-choice"><input type="radio" checked={kind === "file"}
            onChange={() => setKind("file")} /> Локальный файл</label>
        </div>
        {kind === "text"
          ? <label>Содержание<textarea value={body} required rows={7}
              onChange={(event) => setBody(event.target.value)} /></label>
          : <label>Файл до 5 МБ<input type="file" required
              onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>}
        <button type="submit" disabled={create.isPending || !title || (kind === "text" ? !body : !file)}>
          {create.isPending ? "Сохраняем…" : "Сохранить материал"}
        </button>
        {notice && <span className="state-ok">{notice}</span>}
        {create.isError && <span className="violated">Не удалось сохранить материал.</span>}
      </form>
    </section>}

    <section>
      <div className="materials-list-head">
        <h2>{trainee ? "Назначено мне" : "Библиотека материалов"}</h2>
        {instructor && <label className="mode-choice"><input type="checkbox" checked={showArchived}
          onChange={(event) => setShowArchived(event.target.checked)} /> Показать архив</label>}
      </div>

      {instructor && <div className="material-assignment-bar">
        <label>Курсант<select value={traineeId} onChange={(event) => setTraineeId(event.target.value)}>
          <option value="">выберите курсанта</option>
          {trainees.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        <label>Группа<select value={groupId} onChange={(event) => setGroupId(event.target.value)}>
          <option value="">выберите группу</option>
          {groups.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        <span>Выберите получателя, затем нажмите «Назначить» у материала.</span>
      </div>}

      <div className="material-cards">
        {materials.data?.map((item) => <article key={item.id} className={!item.active ? "material-card archived" : "material-card"}>
          <header><span className="material-level">{item.level}</span><h3>{item.title}</h3>
            {item.completed_at && <strong className="state-ok">✓ изучено</strong>}</header>
          {editing?.id === item.id ? <div className="material-edit">
            <label>Название<input value={editing.title}
              onChange={(event) => setEditing({ ...editing, title: event.target.value })} /></label>
            <label>Описание<input value={editing.description}
              onChange={(event) => setEditing({ ...editing, description: event.target.value })} /></label>
            <div className="material-create-row">
              <label>Сложность<select value={editing.level}
                onChange={(event) => setEditing({ ...editing, level: event.target.value as Level })}>
                <option value="L1">L1</option><option value="L2">L2</option><option value="L3">L3</option>
              </select></label>
              <label>Практика<select value={editing.scenario_id ?? ""}
                onChange={(event) => setEditing({ ...editing, scenario_id: event.target.value || null })}>
                <option value="">без сценария</option>
                {scenarios.data?.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.title}</option>)}
              </select></label>
            </div>
            {item.kind === "text" && <label>Содержание<textarea rows={6} value={editing.body}
              onChange={(event) => setEditing({ ...editing, body: event.target.value })} /></label>}
            <div className="material-edit-actions">
              <button type="button" disabled={!editing.title.trim() || (item.kind === "text" && !editing.body.trim()) || update.isPending}
                onClick={() => update.mutate({ id: item.id, patch: {
                  title: editing.title, description: editing.description, level: editing.level,
                  body: editing.body, scenario_id: editing.scenario_id,
                } }, { onSuccess: () => setEditing(null) })}>
                Сохранить изменения
              </button>
              <button type="button" onClick={() => setEditing(null)}>Отмена</button>
            </div>
          </div> : <>
            {item.description && <p>{item.description}</p>}
            {item.kind === "text" && <div className="material-body">{item.body}</div>}
          </>}
          {item.kind === "file" && <p>
            <a href={`/api/materials/${item.id}/download`}>Скачать {item.file_name}</a>
            <span className="ref"> · {size(item.file_size)} · SHA-256 {item.file_sha256?.slice(0, 12)}…</span>
          </p>}
          {item.scenario_id && <p className="ref">Связанная практика: {item.scenario_id}</p>}
          {instructor && <footer>
            <span>Назначено: {item.assignment_count}</span>
            <button type="button" onClick={() => setEditing({
              id: item.id, title: item.title, description: item.description,
              level: item.level, body: item.body, scenario_id: item.scenario_id,
            })}>Редактировать</button>
            {item.active && <button type="button" disabled={!traineeId || assign.isPending}
              onClick={() => assign.mutate({ materialId: item.id, traineeId })}>Назначить курсанту</button>}
            {item.active && <button type="button" disabled={!groupId || assignGroup.isPending}
              onClick={() => assignGroup.mutate({ materialId: item.id, groupId })}>Назначить группе</button>}
            {item.active
              ? <button type="button" className="danger" onClick={() => archive.mutate(item.id)}>В архив</button>
              : <button type="button" onClick={() => update.mutate({ id: item.id, patch: { active: true } })}>Восстановить</button>}
          </footer>}
          {trainee && !item.completed_at && <footer>
            <button type="button" onClick={() => complete.mutate(item.id)} disabled={complete.isPending}>
              Отметить изученным
            </button>
          </footer>}
          {trainee && item.scenario_id && <footer>
            <button type="button" className="primary" disabled={startPractice.isPending}
              onClick={() => startPractice.mutate(item.id, {
                onSuccess: (started) => window.location.assign(started.path),
              })}>
              Начать самостоятельную практику
            </button>
            {startPractice.isError && <span className="violated">Практика сейчас недоступна.</span>}
          </footer>}
        </article>)}
      </div>
      {!materials.isLoading && materials.data?.length === 0 && <p className="ref">
        {trainee ? "Преподаватель пока не назначил материалы." : "Материалов пока нет."}
      </p>}
      {materials.isError && <p className="violated">Материалы не загрузились.</p>}
    </section>
  </main>;
}
