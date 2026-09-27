// Карточка КИО. Один и тот же компонент рисуется у курсанта, на пульте,
// на внешнем мониторе и на АРМ ДДС — там в режиме `readOnly`.

import { useState } from "react";

import { useIncidentGroups, useServiceCatalog, useSigns } from "@/shared/api/http";
import { type CardState, display } from "./merge";
import { type Field, type FieldGroup, visibleGroups } from "./fields";

interface Props {
  state: CardState;
  /** Обязательные поля приходят сценарием в `call.incoming`. */
  required?: string[];
  readOnly?: boolean;
  referenceArm?: boolean;
  onChange?: (path: string, value: unknown) => void;
}

export function KioCard({ state, required = [], readOnly = false, referenceArm = false, onChange }: Props) {
  const dds = display(state, "dds") as string | null;
  const incidentGroup = display(state, "incident_group") as number | null;
  const notify = (display(state, "notify") as string[] | null) ?? [];
  const groups = visibleGroups(dds);
  const section = (group: FieldGroup) => (
    <section className={`kio-arm-section kio-arm-${group.area}`} key={group.title}>
      {(!referenceArm || group.area !== "header") && <div className="kio-arm-section-title">
        {referenceArm && group.area === "incident" ? "Введите тип происшествия" : group.title}
      </div>}
      <div className="kio-arm-fields">
        {(referenceArm && group.area === "incident"
          ? [...group.fields].sort((a, b) => Number(b.path === "incident_type") - Number(a.path === "incident_type"))
          : group.fields).map((field) => (
          <Row
            key={field.path}
            field={field}
            value={display(state, field.path)}
            pending={field.path in state.pending}
            required={required.includes(field.path)}
            readOnly={readOnly || field.readOnly}
            referenceArm={referenceArm}
            incidentGroup={incidentGroup}
            notify={notify}
            onChange={onChange}
          />
        ))}
      </div>
    </section>
  );

  return (
    <div className={`kio-arm${referenceArm ? " kio-arm-ref" : ""}`}>
      <div className="kio-arm-top">
        {groups.filter((group) => group.title === "Звонящий").map(section)}
        {groups.filter((group) => group.title === "Служебное").map(section)}
      </div>
      <div className="kio-arm-main">
        <div className="kio-arm-left">{groups.filter((group) => group.area === "address" || group.area === "description").map(section)}</div>
        <div className="kio-arm-right">{groups.filter((group) => group.area === "incident" || group.area === "dispatch" || group.area === "details").map(section)}</div>
      </div>
    </div>
  );
}

function Row({
  field, value, pending, required, readOnly, referenceArm, incidentGroup, notify, onChange,
}: {
  field: Field;
  value: unknown;
  pending: boolean;
  required: boolean;
  readOnly?: boolean;
  referenceArm: boolean;
  incidentGroup: number | null;
  notify: string[];
  onChange?: (path: string, value: unknown) => void;
}) {
  const empty = value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0);
  return (
    <div className={`kio-arm-row kio-arm-row-${field.path.replaceAll(".", "-")}${required && empty ? " kio-arm-missing" : ""}${pending ? " kio-arm-pending" : ""}`}>
      <div className="kio-arm-label">
        {referenceArm && field.path === "incident_type" ? "Что случилось?"
          : referenceArm && field.path === "caller_number" ? "АОН"
            : referenceArm && field.path === "caller_contact" ? "Предоставленный"
              : referenceArm && field.path === "phone_on_scene" ? "Телефон на место"
                : referenceArm && field.path === "caller_name" ? "Фамилия и имя заявителя"
                : field.label}
        {required && <span title="обязательное поле" className="required"> ·</span>}
      </div>
      <div className="kio-arm-value">
        {readOnly ? (
          <span className="kio-arm-readonly">{formatValue(value, field)}</span>
        ) : (
          <Input field={field} value={value} referenceArm={referenceArm} incidentGroup={incidentGroup} notify={notify} onChange={onChange} />
        )}
      </div>
    </div>
  );
}

function Input({
  field, value, referenceArm, incidentGroup, notify, onChange,
}: {
  field: Field;
  value: unknown;
  referenceArm: boolean;
  incidentGroup: number | null;
  notify: string[];
  onChange?: (path: string, value: unknown) => void;
}) {
  const emit = (next: unknown) => onChange?.(field.path, next);

  if (field.kind === "list") {
    return <span className="kio-arm-readonly">{formatValue(value)}</span>;
  }
  if (field.kind === "group") {
    return <IncidentGroupPicker value={value as number | null} onChange={(next) => {
      onChange?.("incident_group", next);
      onChange?.("signs", []);
    }} />;
  }
  if (field.kind === "bool") {
    return (
      <div className="kio-arm-choice" role="group" aria-label={field.label}>
        <button type="button" aria-pressed={value === true} className={value === true ? "kio-arm-selected" : ""} onClick={() => emit(true)}>Да</button>
        <button type="button" aria-pressed={value === false} className={value === false ? "kio-arm-selected" : ""} onClick={() => emit(false)}>Нет</button>
      </div>
    );
  }
  if (field.kind === "select") {
    if (referenceArm && field.path === "incident_type") {
      return (
        <div className="kio-arm-type-picker" role="group" aria-label="Что случилось?">
          {field.options?.map((option) => (
            <button type="button" key={option.value} aria-pressed={value === option.value}
              className={value === option.value ? "kio-arm-selected" : ""}
              onClick={() => emit(option.value)}>{option.label}</button>
          ))}
        </div>
      );
    }
    return (
      <select aria-label={field.label} value={(value as string) ?? ""} onChange={(e) => emit(e.target.value || null)}>
        <option value="">—</option>
        {field.options?.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    );
  }
  if (field.kind === "services") {
    return <ServicesPicker value={(value as string[]) ?? []} notify={notify} onChange={(next) => emit(next)} />;
  }
  if (field.kind === "signs") {
    return <SignsPicker value={(value as string[]) ?? []} group={incidentGroup} onChange={(next) => emit(next)} />;
  }
  if (field.kind === "textarea") {
    return <textarea aria-label={field.label} rows={5} value={(value as string) ?? ""} placeholder="Введите сведения со слов заявителя" onChange={(e) => emit(e.target.value)} />;
  }
  if (field.kind === "number") {
    return (
      <input
        type="number"
        aria-label={field.label}
        min={0}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => {
          const text = e.target.value;
          if (text === "") return emit(null);
          const parsed = Number(text);
          // Нечисловое значение не уходит на сервер: карточка — норматив,
          // а не свободный текст.
          if (Number.isFinite(parsed) && parsed >= 0) emit(parsed);
        }}
      />
    );
  }
  return (
    <input type="text" aria-label={field.label} value={(value as string) ?? ""} onChange={(e) => emit(e.target.value)} />
  );
}

/** Как `max_length` у `KIO.notify_extra`. */
const SERVICES_LIMIT = 20;

/** Ручное добавление служб из каталога — окно «Добавьте службы» боевого АРМ.
 * Список оповещения по признакам остаётся как есть: здесь только добавки. */
function ServicesPicker({ value, notify, onChange }: {
  value: string[]; notify: string[]; onChange: (next: string[]) => void;
}) {
  const catalog = useServiceCatalog();
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  // Лимит и службы из списка оповещения — те же правила, что на сервере:
  // лишняя служба отклонила бы весь пакет правок.
  const full = value.length >= SERVICES_LIMIT;
  const found = needle && !full
    ? (catalog.data?.services ?? [])
      .filter((item) => !value.includes(item.name) && !notify.includes(item.name)
        && item.name.toLowerCase().includes(needle))
      .slice(0, 8)
    : [];
  return (
    <div className="kio-services">
      {value.length > 0 && <div className="signs-selected" aria-label="Добавленные службы">
        {value.map((name) => <span key={name}>
          {name} <button type="button" aria-label={`Убрать ${name}`}
            onClick={() => onChange(value.filter((item) => item !== name))}>×</button>
        </span>)}
      </div>}
      <input type="search" aria-label="Поиск службы в каталоге"
        placeholder={full ? `Не больше ${SERVICES_LIMIT} служб` : "Поиск службы…"} disabled={full}
        value={query} onChange={(event) => setQuery(event.target.value)} />
      {found.length > 0 && <ul className="kio-services-found">
        {found.map((item) => <li key={item.id}>
          <button type="button" onClick={() => { onChange([...value, item.name]); setQuery(""); }}>{item.name}</button>
        </li>)}
      </ul>}
    </div>
  );
}

function IncidentGroupPicker({ value, onChange }: { value: number | null; onChange: (next: number | null) => void }) {
  const page = useIncidentGroups();
  return <select aria-label="Категория происшествия" value={value ?? ""}
    onChange={(event) => onChange(event.target.value ? Number(event.target.value) : null)}>
    <option value="">Все категории ЕКП</option>
    {page.data?.groups.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}
  </select>;
}

/** Три признака происшествия каскадом: выбор верхнего сужает нижний.
 *
 * Значения приходят с сервера, а не лежат на фронте: классификатор — 1283 строки,
 * и он обновляется файлом от заказчика (docs/spec/DATASET.md). Служба из выбора
 * не следует — её считает сервер, поэтому здесь её нет. */
function SignsPicker({
  value, group, onChange,
}: {
  value: string[];
  group: number | null;
  onChange: (next: string[]) => void;
}) {
  if (group === null) {
    return <p className="kio-signs-hint">Сначала выберите категорию происшествия — затем появятся признаки ЕКП.</p>;
  }
  const levels = [0, 1, 2];
  return (
    <div className="signs">
      {levels.map((level) => (
        <SignSelect
          key={level}
          level={level}
          prefix={value.slice(0, level)}
          group={group}
          chosen={value[level] ?? ""}
          onPick={(sign) => {
            // Смена верхнего признака обнуляет нижние: они относились
            // к другой ветке классификатора.
            const next = value.slice(0, level);
            if (sign) next.push(sign);
            onChange(next);
          }}
        />
      ))}
      {value.length > 0 && <div className="signs-selected" aria-label="Выбранные признаки">
        {value.map((sign, index) => <span key={`${index}-${sign}`}>{sign}</span>)}
      </div>}
    </div>
  );
}

function SignSelect({
  level, prefix, group, chosen, onPick,
}: {
  level: number;
  prefix: string[];
  group: number | null;
  chosen: string;
  onPick: (sign: string) => void;
}) {
  const page = useSigns(prefix, group);
  const values = page.data?.values ?? [];
  // Уровня может не быть: часть происшествий классифицируется двумя признаками.
  if (!page.isLoading && values.length === 0) return null;
  return (
    <select aria-label={`Признак происшествия ${level + 1}`} value={chosen} onChange={(e) => onPick(e.target.value)} disabled={page.isLoading}>
      <option value="">—</option>
      {values.map((sign) => (
        <option key={sign} value={sign}>{sign}</option>
      ))}
    </select>
  );
}

function formatValue(value: unknown, field?: Field): string {
  if (value === true) return "да";
  if (value === false) return "нет";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  if (value === null || value === undefined || value === "") return "—";
  if (field?.options) return field.options.find((option) => option.value === value)?.label ?? String(value);
  return String(value);
}
