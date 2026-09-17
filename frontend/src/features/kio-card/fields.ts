// Поля карточки информационного обмена. Это не дизайн формы, это норматив:
// docs/spec/NORMATIVES.md#карточка-информационного-обмена-кио
//
// Описание полей отдельно от отрисовки: по нему же рисуется карточка на внешнем
// мониторе, на пульте и на АРМ ДДС — там она read-only.

import type { DDSCode, IncidentType } from "@/shared/types/generated";

export type FieldKind = "text" | "number" | "bool" | "select";

export interface Field {
  /** Путь в карточке: «floor», «fire.floors» — так же, как в `kio.patch`. */
  path: string;
  label: string;
  kind: FieldKind;
  options?: { value: string; label: string }[];
  /** Заполняется системой: номер определяется автоматически, время — сервером. */
  readOnly?: boolean;
}

export interface FieldGroup {
  title: string;
  fields: Field[];
  /** Группа показывается только для этой службы. */
  dds?: DDSCode;
}

const INCIDENT: { value: IncidentType; label: string }[] = [
  { value: "fire", label: "Пожар" },
  { value: "medical", label: "Медицинский вызов" },
  { value: "police", label: "Правонарушение" },
  { value: "gkh", label: "Авария ЖКХ" },
  { value: "era_glonass", label: "ДТП, автовызов" },
  { value: "other", label: "Прочее" },
];

const DDS: { value: DDSCode; label: string }[] = [
  { value: "01", label: "01 — МЧС" },
  { value: "02", label: "02 — полиция" },
  { value: "03", label: "03 — скорая помощь" },
  { value: "04", label: "04 — прочие службы" },
  { value: "gkh", label: "ЖКХ" },
];

export const GROUPS: FieldGroup[] = [
  {
    title: "Служебное",
    fields: [
      { path: "registered_at", label: "Время регистрации", kind: "text", readOnly: true },
      { path: "response_status", label: "Статус реагирования", kind: "text", readOnly: true },
    ],
  },
  {
    title: "Звонящий",
    fields: [
      { path: "caller_number", label: "Номер телефона", kind: "text", readOnly: true },
      { path: "caller_name", label: "ФИО", kind: "text" },
      { path: "caller_contact", label: "Контактный телефон", kind: "text" },
      { path: "language", label: "Язык общения", kind: "text" },
    ],
  },
  {
    title: "Место",
    fields: [
      { path: "okato", label: "ОКАТО", kind: "text" },
      { path: "address", label: "Адрес", kind: "text" },
      { path: "street", label: "Улица", kind: "text" },
      { path: "building", label: "Дом", kind: "text" },
      { path: "entrance", label: "Подъезд", kind: "text" },
      { path: "floor", label: "Этаж", kind: "text" },
      { path: "intercom_code", label: "Код домофона", kind: "text" },
    ],
  },
  {
    title: "Происшествие",
    fields: [
      { path: "incident_type", label: "Тип происшествия", kind: "select", options: INCIDENT },
      { path: "description", label: "Описание", kind: "text" },
      { path: "victims_count", label: "Пострадавших", kind: "number" },
      { path: "is_emergency", label: "Признак ЧС", kind: "bool" },
      { path: "life_threat", label: "Угроза жизни", kind: "bool" },
      { path: "evacuation_needed", label: "Нужна эвакуация", kind: "bool" },
    ],
  },
  {
    title: "Дежурно-диспетчерская служба",
    fields: [
      { path: "dds", label: "Служба", kind: "select", options: DDS },
      { path: "dispatch_order_at", label: "Приказ на выезд", kind: "text", readOnly: true },
      { path: "arrival_at", label: "Прибытие", kind: "text", readOnly: true },
    ],
  },
  {
    title: "Уточнения: пожар",
    dds: "01",
    fields: [
      { path: "fire.fire_nature", label: "Характер пожара", kind: "text" },
      { path: "fire.object_kind", label: "Объект", kind: "text" },
      { path: "fire.floors", label: "Этажность", kind: "number" },
      { path: "fire.gasified", label: "Газификация", kind: "bool" },
      { path: "fire.people_inside", label: "Люди в помещении", kind: "bool" },
      { path: "fire.smoke_spread", label: "Куда идёт дым", kind: "text" },
    ],
  },
  {
    title: "Уточнения: полиция",
    dds: "02",
    fields: [
      { path: "police.offence_kind", label: "Вид правонарушения", kind: "text" },
      { path: "police.suspects", label: "Подозреваемые, приметы", kind: "text" },
      { path: "police.suspect_fled", label: "Нарушитель скрылся", kind: "bool" },
      { path: "police.vehicle", label: "Транспортное средство", kind: "text" },
    ],
  },
  {
    title: "Уточнения: медицина",
    dds: "03",
    fields: [
      { path: "medical.reason", label: "Повод к вызову", kind: "text" },
      { path: "medical.conscious", label: "В сознании", kind: "bool" },
      { path: "medical.breathing", label: "Дышит", kind: "bool" },
      { path: "medical.can_move", label: "Может передвигаться", kind: "bool" },
      { path: "medical.age", label: "Возраст", kind: "number" },
    ],
  },
  {
    title: "Уточнения: ЖКХ и прочие",
    dds: "gkh",
    fields: [
      { path: "utility.failure_kind", label: "Тип аварии", kind: "text" },
      { path: "utility.scale", label: "Масштаб", kind: "text" },
      { path: "utility.threat_to_residents", label: "Угроза жильцам", kind: "bool" },
    ],
  },
];

/** Группы, которые показываются при выбранной службе. */
export function visibleGroups(dds: string | null | undefined): FieldGroup[] {
  return GROUPS.filter((group) => !group.dds || group.dds === dds);
}

export function readValue(card: Record<string, unknown>, path: string): unknown {
  return path.split(".").reduce<unknown>(
    (value, part) => (value && typeof value === "object" ? (value as Record<string, unknown>)[part] : undefined),
    card,
  );
}
