// Карточка КИО. Один и тот же компонент рисуется у курсанта, на пульте,
// на внешнем мониторе и на АРМ ДДС — там в режиме `readOnly`.

import { type CardState, display } from "./merge";
import { type Field, visibleGroups } from "./fields";

interface Props {
  state: CardState;
  /** Обязательные поля приходят сценарием в `call.incoming`. */
  required?: string[];
  readOnly?: boolean;
  onChange?: (path: string, value: unknown) => void;
}

export function KioCard({ state, required = [], readOnly = false, onChange }: Props) {
  const dds = display(state, "dds") as string | null;

  return (
    <div className="kio">
      {visibleGroups(dds).map((group) => (
        <table className="grid" key={group.title}>
          <thead>
            <tr>
              <th colSpan={2}>{group.title}</th>
            </tr>
          </thead>
          <tbody>
            {group.fields.map((field) => (
              <Row
                key={field.path}
                field={field}
                value={display(state, field.path)}
                pending={field.path in state.pending}
                required={required.includes(field.path)}
                readOnly={readOnly || field.readOnly}
                onChange={onChange}
              />
            ))}
          </tbody>
        </table>
      ))}
    </div>
  );
}

function Row({
  field, value, pending, required, readOnly, onChange,
}: {
  field: Field;
  value: unknown;
  pending: boolean;
  required: boolean;
  readOnly?: boolean;
  onChange?: (path: string, value: unknown) => void;
}) {
  const empty = value === null || value === undefined || value === "";
  return (
    <tr>
      <th>
        {field.label}
        {required && <span title="обязательное поле" className="required"> ·</span>}
      </th>
      <td className={required && empty ? "warn" : pending ? "pending" : ""}>
        {readOnly ? (
          <span>{formatValue(value)}</span>
        ) : (
          <Input field={field} value={value} onChange={onChange} />
        )}
      </td>
    </tr>
  );
}

function Input({
  field, value, onChange,
}: {
  field: Field;
  value: unknown;
  onChange?: (path: string, value: unknown) => void;
}) {
  const emit = (next: unknown) => onChange?.(field.path, next);

  if (field.kind === "bool") {
    return (
      <input type="checkbox" checked={value === true} onChange={(e) => emit(e.target.checked)} />
    );
  }
  if (field.kind === "select") {
    return (
      <select value={(value as string) ?? ""} onChange={(e) => emit(e.target.value || null)}>
        <option value="">—</option>
        {field.options?.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    );
  }
  if (field.kind === "number") {
    return (
      <input
        type="number"
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
    <input type="text" value={(value as string) ?? ""} onChange={(e) => emit(e.target.value)} />
  );
}

function formatValue(value: unknown): string {
  if (value === true) return "да";
  if (value === false) return "нет";
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}
