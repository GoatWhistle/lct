// Все поля карточки из контракта должны быть на форме.
//
// Домен живёт в backend/app/domain/kio.py и меняется: поле, добавленное в контракт
// и забытое в форме, — это пропущенный факт по вине интерфейса, а не курсанта.
// Проверка сверяет пути в features/kio-card/fields.ts с типами из generated.ts.

import { readFileSync } from "node:fs";

const types = readFileSync(new URL("../src/shared/types/generated.ts", import.meta.url), "utf8");
const spec = readFileSync(new URL("../src/features/kio-card/fields.ts", import.meta.url), "utf8");
const backendKio = readFileSync(new URL("../../backend/app/domain/kio.py", import.meta.url), "utf8");

// Поля, которых на форме нет намеренно.
const SKIP = new Map([
  ["card_id", "служебный идентификатор, человеку не нужен"],
  ["coords", "координаты приходят от ЭРА-ГЛОНАСС, отдельным экраном"],
]);

const NESTED = { fire: "FireDetails", police: "PoliceDetails", medical: "MedicalDetails", utility: "UtilityDetails" };

function fieldsOf(name) {
  const body = types.match(new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\n\\}`))?.[1];
  if (!body) throw new Error(`в generated.ts нет интерфейса ${name}`);
  return [...body.matchAll(/^\s{2}(\w+)\??:/gm)].map((m) => m[1]);
}

const expected = [];
for (const field of fieldsOf("KIO")) {
  if (SKIP.has(field)) continue;
  if (field in NESTED) {
    for (const nested of fieldsOf(NESTED[field])) expected.push(`${field}.${nested}`);
  } else {
    expected.push(field);
  }
}

const shown = new Set([...spec.matchAll(/path:\s*"([^"]+)"/g)].map((m) => m[1]));
const missing = expected.filter((path) => !shown.has(path));
const extra = [...shown].filter((path) => !expected.includes(path));

if (missing.length || extra.length) {
  if (missing.length) console.error("нет на форме КИО:", missing.join(", "));
  if (extra.length) console.error("на форме есть, а в контракте нет:", extra.join(", "));
  process.exit(1);
}

const editableBlock = backendKio.match(/EDITABLE_KIO_FIELDS:[\s\S]*?=\s*frozenset\(\{([\s\S]*?)\}\)/)?.[1];
if (!editableBlock) throw new Error("в backend/app/domain/kio.py нет EDITABLE_KIO_FIELDS");
const backendEditable = new Set([...editableBlock.matchAll(/"([^"]+)"/g)].map((match) => match[1]));
const formEditable = new Set([...spec.matchAll(/\{\s*path:\s*"([^"]+)"([^}]*)\}/gs)]
  .filter((match) => !/readOnly:\s*true/.test(match[2]))
  .map((match) => match[1]));
const unavailableInForm = [...backendEditable].filter((path) => !formEditable.has(path));
const notScorable = [...formEditable].filter((path) => !backendEditable.has(path));
if (unavailableInForm.length || notScorable.length) {
  if (unavailableInForm.length) console.error("required_fields, отсутствующие/только для чтения в форме:", unavailableInForm.join(", "));
  if (notScorable.length) console.error("редактируемые поля формы без backend scoring allowlist:", notScorable.join(", "));
  process.exit(1);
}
console.log(`карточка КИО: все ${expected.length} полей контракта на форме`);
console.log(`required_fields: backend allowlist совпадает с ${formEditable.size} редактируемыми путями формы`);
