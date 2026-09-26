"""Локальная ИИ-вариация утверждённого сценария для проверки преподавателем.

Модель пишет только сюжетные поля. Адрес, число пострадавших, классификацию,
список оповещения и протокол оценки она менять не может. Публикация — отдельное
действие преподавателя после просмотра черновика и эталона.
"""

import json
import re

from app.config import get_settings
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable, is_loopback_url
from app.scenarios.editor import classifier_sign_gaps, template_copy, validate
from app.scenarios.loader import ScenarioError
from app.scenarios.schema import Scenario


class GenerationError(ValueError):
    pass


PROTECTED_IDS = ("address", "people", "victim", "floor", "location", "region")
PLACEHOLDER = re.compile(r"\{\{?\s*[a-zA-Z_][\w.-]*\s*\}?\}")
NUMBER_WORD = re.compile(
    r"\b(?:ноль|один|одна|одно|одного|одну|два|две|двое|двух|три|трое|тр[её]х|"
    r"четыре|четверо|четыр[её]х|пять|пятеро|пяти|шесть|шестеро|шести|семь|"
    r"семеро|семи|восемь|восьмеро|восьми|девять|девятеро|девяти|десять|"
    r"десятеро|десяти)\b",
    re.IGNORECASE,
)


def reveals_number(value: str) -> bool:
    return bool(re.search(r"\d", value) or NUMBER_WORD.search(value))


def editable_fact_ids(source: Scenario) -> list[str]:
    return [fact.id for fact in source.facts
            if not fact.refined and not fact.refine_on
            and not any(part in fact.id.casefold() for part in PROTECTED_IDS)]


def correction_target(source: Scenario, instruction: str) -> str | None:
    """Указать маленькой модели конкретный факт для правки при повторе."""
    eligible = [fact for fact in source.facts if fact.id in editable_fact_ids(source)]
    if not eligible:
        return None
    def stems(text: str) -> set[str]:
        return {word[:4] for word in re.findall(r"[а-яё]{3,}", text.casefold())}

    words = stems(instruction)
    questions = {item.fact: item.question or "" for item in source.checklist if item.fact}

    def relevance(fact) -> int:
        haystack = stems(f"{fact.value} {questions.get(fact.id, '')}")
        return len(words & haystack)

    _, best = max(enumerate(eligible), key=lambda pair: (relevance(pair[1]), pair[0]))
    return best.id if relevance(best) > 0 else None


def style_fallback(source: Scenario, instruction: str) -> dict | None:
    """Безопасно сократить реплику, если 1.7B дважды вернула копию."""
    if not re.search(r"\b(?:короч|кратк)", instruction.casefold()):
        return None
    chunks = re.findall(r"[^.!?…]+[.!?…]+|[^.!?…]+$", source.first_line)
    useful: list[str] = []
    for chunk in chunks:
        value = chunk.strip()
        letters = re.sub(r"[^а-яё]", "", value.casefold())
        if not letters or letters in {"алло", "ааа", "аа", "а"}:
            continue
        useful.append(value)
    if not useful:
        return None
    shortened = " ".join(useful[:2])[:300].strip()
    if shortened == source.first_line.strip():
        return None
    return {"title": source.title, "first_line": shortened, "facts": {}}


def parse_proposal(
    raw: str, source: Scenario, *, require_fact_change: bool = True,
    instruction: str = "", restrict_first_line_facts: bool = False,
) -> dict:
    """Никакой произвольный JSON модели не попадает в схему сценария."""
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        proposal = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise GenerationError("модель не вернула корректный JSON") from exc
    if not isinstance(proposal, dict) or set(proposal) != {"title", "first_line", "facts"}:
        raise GenerationError("модель вернула поля вне разрешённого формата")
    title, first_line, facts = (proposal[key] for key in ("title", "first_line", "facts"))
    if not isinstance(title, str) or not 5 <= len(title.strip()) <= 200:
        raise GenerationError("название варианта пустое или слишком длинное")
    if not isinstance(first_line, str) or not 8 <= len(first_line.strip()) <= 300:
        raise GenerationError("первая реплика пустая или слишком длинная")
    if reveals_number(first_line):
        raise GenerationError("первая реплика раскрывает числовые детали до опроса")
    if instruction and restrict_first_line_facts:
        stop = {"есть", "если", "меня", "очень", "этот", "только", "сейчас", "помогите"}

        def stems(value: str) -> set[str]:
            return {word[:3] for word in re.findall(r"[а-яё]{3,}", value.casefold())
                    if word not in stop}

        fact_stems = set().union(*(stems(fact.value) for fact in source.facts))
        allowed_stems = stems(f"{source.first_line} {instruction}")
        leaked = (stems(first_line) & fact_stems) - allowed_stems
        if leaked:
            raise GenerationError(
                "первая реплика добавляет не запрошенные обстоятельства карточки"
            )
    if not isinstance(facts, dict):
        raise GenerationError("факты должны быть объектом")
    allowed = set(editable_fact_ids(source))
    if set(facts) - allowed:
        raise GenerationError("модель попыталась изменить защищённые или неизвестные факты")
    for fact_id, value in facts.items():
        if not isinstance(value, str) or not 3 <= len(value.strip()) <= 300:
            raise GenerationError(f"факт {fact_id}: значение пустое или слишком длинное")
        if PLACEHOLDER.search(value):
            raise GenerationError(f"факт {fact_id}: модель вернула плейсхолдер вместо ответа")
        if "?" in value:
            raise GenerationError(f"факт {fact_id}: модель вернула вопрос вместо ответа")
    source_values = {fact.id: fact.value for fact in source.facts}
    fact_changed = any(value.strip() != source_values[fact_id] for fact_id, value in facts.items())
    story_changed = title.strip() != source.title or first_line.strip() != source.first_line
    if require_fact_change and not fact_changed:
        raise GenerationError("модель не предложила новых обстоятельств происшествия")
    if not fact_changed and not story_changed:
        raise GenerationError("модель не изменила черновик по комментарию")
    return {"title": title.strip(), "first_line": first_line.strip(), "facts": facts}


def proposal_body(source: Scenario, draft_id: str, proposal: dict) -> dict:
    body = template_copy(source, draft_id, proposal["title"])
    by_id = proposal["facts"]
    for fact in body["facts"]:
        if fact["id"] in by_id:
            fact["value"] = by_id[fact["id"]].strip()
    body["first_line"] = proposal["first_line"]
    # Схема, связи фактов и эталон проверяются до сохранения черновика.
    validate(body)
    return body


def parse_full_proposal(raw: str, source: Scenario) -> dict:
    """Проверить новый сюжет, где модель заполняет все ответы и мягкий эталон.

    Тип, признаки ЕКП, маршрутизация и чек-лист остаются структурным каркасом,
    но сами обстоятельства, адрес и число пострадавших создаются заново.
    """
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        proposal = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise GenerationError("модель не вернула корректный JSON нового сюжета") from exc
    if not isinstance(proposal, dict) or set(proposal) != {
        "title", "first_line", "facts", "ground_truth"
    }:
        raise GenerationError("новый сюжет содержит поля вне разрешённого формата")
    title = proposal["title"]
    first_line = proposal["first_line"]
    facts = proposal["facts"]
    truth = proposal["ground_truth"]
    if not isinstance(title, str) or not 5 <= len(title.strip()) <= 200:
        raise GenerationError("название нового сюжета пустое или слишком длинное")
    if not isinstance(first_line, str) or not 8 <= len(first_line.strip()) <= 300:
        raise GenerationError("первая реплика нового сюжета пустая или слишком длинная")
    if reveals_number(first_line):
        raise GenerationError("первая реплика раскрывает числовые детали до опроса")
    expected = {fact.id for fact in source.facts}
    if not isinstance(facts, dict) or set(facts) != expected:
        raise GenerationError("новый сюжет должен заполнить каждый факт каркаса ровно один раз")
    for fact_id, value in facts.items():
        if not isinstance(value, str) or not 3 <= len(value.strip()) <= 300:
            raise GenerationError(f"факт {fact_id}: значение пустое или слишком длинное")
        if PLACEHOLDER.search(value):
            raise GenerationError(f"факт {fact_id}: модель вернула плейсхолдер вместо ответа")
        if "?" in value:
            raise GenerationError(f"факт {fact_id}: модель вернула вопрос вместо ответа")
    if not isinstance(truth, dict) or set(truth) != {"address", "victims"}:
        raise GenerationError("ground_truth нового сюжета должен содержать address и victims")
    address, victims = truth["address"], truth["victims"]
    if not isinstance(address, str) or not 5 <= len(address.strip()) <= 200:
        raise GenerationError("эталонный адрес пустой или слишком длинный")
    if not isinstance(victims, int) or isinstance(victims, bool) or not 0 <= victims <= 20:
        raise GenerationError("число пострадавших должно быть целым от 0 до 20")
    address_id = next((fact.id for fact in source.facts if "address" in fact.id.casefold()), None)
    if address_id is None:
        raise GenerationError("в выбранном каркасе нет адресного факта")

    def normalized(value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip(" .,")

    if not normalized(facts[address_id]).startswith(normalized(address)):
        raise GenerationError("эталонный адрес не совпадает с началом адресного факта")
    evidence = " ".join(facts.values()).casefold().replace("ё", "е")
    count_words = {
        0: ("ноль",), 1: ("один", "одна", "одного"),
        2: ("два", "двое", "двух"), 3: ("три", "трое", "трех"),
        4: ("четыре", "четверо", "четырех"), 5: ("пять", "пятеро", "пяти"),
        6: ("шесть", "шестеро", "шести"), 7: ("семь", "семеро", "семи"),
        8: ("восемь", "восьмеро", "восьми"), 9: ("девять", "девятеро", "девяти"),
        10: ("десять", "десятеро", "десяти"),
    }
    numeric = re.search(rf"пострадавш\w*\s*[:—-]?\s*{victims}(?:\D|$)", evidence)
    worded = any(re.search(
        rf"(?:пострадавш\w*.{{0,25}}\b{word}\b|\b{word}\b.{{0,25}}пострада\w*)", evidence
    ) for word in count_words.get(victims, ()))
    none_hurt = victims == 0 and re.search(
        r"пострадавш(?:их|ие|их лиц)\s+нет|никто\s+не\s+пострадал|"
        r"все\s+(?:люди\s+)?(?:успели\s+)?(?:выйти|эвакуироваться)", evidence
    )
    if not (numeric or worded or none_hurt):
        raise GenerationError("в фактах нет явного подтверждения числа пострадавших")
    old_values = {fact.id: fact.value.strip() for fact in source.facts}
    changed = sum(value.strip() != old_values[fact_id] for fact_id, value in facts.items())
    if changed < max(1, (len(facts) + 1) // 2):
        raise GenerationError("новый сюжет слишком близко копирует исходный каркас")
    if normalized(address) in normalized(first_line):
        raise GenerationError("первая реплика раскрывает адрес до опроса")
    return {
        "title": title.strip(), "first_line": first_line.strip(),
        "facts": {key: value.strip() for key, value in facts.items()},
        "ground_truth": {"address": address.strip(), "victims": victims},
    }


def full_proposal_body(source: Scenario, draft_id: str, proposal: dict) -> dict:
    """Собрать самостоятельный сценарий из проверенного нового сюжета."""
    body = template_copy(source, draft_id, proposal["title"])
    for fact in body["facts"]:
        fact["value"] = proposal["facts"][fact["id"]]
        # Старое уточнение относится к старому сюжету и не переносится.
        fact["refined"] = None
        fact["refine_on"] = None
    body["first_line"] = proposal["first_line"]
    body["ground_truth"] = dict(proposal["ground_truth"])
    validate(body)
    return body


async def generate_from_description(source: Scenario, description: str) -> dict:
    """Создать все обстоятельства и мягкий эталон внутри выбранной категории ЕКП.

    Qwen 1.7B заметно надёжнее отвечает на один вопрос за раз. Поэтому вместо
    одного длинного JSON, где малая модель копировала вопросы и старую карточку,
    используются короткие строгие запросы. Итог всё равно проходит единую
    сквозную проверку непротиворечивости.
    """
    settings = get_settings()
    if (not is_loopback_url(
            settings.llm_base_url,
            allow_docker_host=settings.allow_docker_host_models,
        ) or not settings.llm_model_caller):
        raise LlmUnavailable("для генерации нужна разрешённая локальная модель")
    questions = {item.fact: item.question for item in source.checklist if item.fact}
    address_id = next((fact.id for fact in source.facts if "address" in fact.id.casefold()), None)
    if address_id is None:
        raise GenerationError("в выбранном каркасе нет адресного факта")

    def victim_relevance(fact_id: str) -> int:
        text = f"{fact_id} {questions.get(fact_id, '')}".casefold()
        return sum(word in text for word in (
            "people", "victim", "injur", "who", "люд", "пострад", "кто", "кому"
        ))

    victim_id = max((fact.id for fact in source.facts), key=victim_relevance)
    client = LlmClient(base_url=settings.llm_base_url, timeout=45)

    def clean_answer(value: object) -> str:
        """Нормализовать пробелы и ограничить ответ без порчи `ул.`/`д.`."""
        text = re.sub(r"\s+", " ", str(value)).strip()
        for marker in (", но", ". В данном", ". Однако", ". Необходимо",
                       ". Рекомендуется", ". Следует"):
            text = text.split(marker, 1)[0]
        return text[:300].strip()

    def validate_value(data: dict, *, extra_key: str | None = None) -> None:
        value = clean_answer(data.get("value", ""))
        if not 3 <= len(value) <= 300:
            raise GenerationError("значение пустое или слишком длинное")
        if PLACEHOLDER.search(value):
            raise GenerationError("модель вернула плейсхолдер вместо ответа")
        if "?" in value:
            raise GenerationError("модель вернула вопрос вместо ответа")
        if extra_key is not None and extra_key not in data:
            raise GenerationError(f"нет обязательного поля {extra_key}")

    def validate_headline(data: dict) -> None:
        title = clean_answer(data.get("title", ""))
        first_line = clean_answer(data.get("first_line", ""))
        if not 5 <= len(title) <= 200:
            raise GenerationError("название пустое или слишком длинное")
        if not 8 <= len(first_line) <= 300:
            raise GenerationError("первая реплика пустая или слишком длинная")
        if reveals_number(first_line):
            raise GenerationError("первая реплика раскрывает числовые детали")

    def validate_victims(data: dict) -> None:
        validate_value(data, extra_key="victims")
        victims = data["victims"]
        if not isinstance(victims, int) or isinstance(victims, bool) or not 0 <= victims <= 20:
            raise GenerationError("victims должен быть целым от 0 до 20")

    def victim_answer(count: int) -> str:
        """Число модели сохраняем, формулировку делаем воспроизводимой и грамотной."""
        if count == 0:
            return "Никто не пострадал. Пострадавших: 0"
        if count == 1:
            return "Пострадал один человек. Пострадавших: 1"
        noun = "человека" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "человек"
        return f"Пострадали {count} {noun}. Пострадавших: {count}"

    async def ask(
        system: str, payload: dict, schema: dict, *, max_tokens: int = 180,
        validator=None,
    ) -> dict:
        last_error: Exception | None = None
        raw = ""
        for attempt in range(3):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            if attempt:
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": (
                        f"Ответ отклонён: {last_error}. Верни исправленный JSON. "
                        "Нужен конкретный ответ, не повторяй вопрос."
                    )},
                ]
            try:
                raw = await client.complete(LlmRequest(
                    messages=messages, model=settings.llm_model_caller,
                    temperature=0.25, max_tokens=max_tokens,
                    response_format={"type": "json_object", "schema": schema},
                    strip_reasoning=True,
                ), use_cache=False)
                data = json.loads(raw)
                if not isinstance(data, dict) or set(data) != set(schema["required"]):
                    raise GenerationError("неверный набор полей")
                if validator is not None:
                    validator(data)
                return data
            except (ValueError, TypeError, GenerationError, LlmUnavailable) as exc:
                last_error = exc
                if isinstance(exc, LlmUnavailable) and not any(marker in str(exc) for marker in (
                    "служебные токены", "пустой ответ модели"
                )):
                    raise
        raise GenerationError(f"локальная модель не заполнила часть сценария: {last_error}")

    value_schema = {
        "type": "object", "properties": {"value": {"type": "string"}},
        "required": ["value"], "additionalProperties": False,
    }
    try:
        # Заголовочный запрос не получает числовые детали даже словами: малая
        # модель склонна повторять их в первой реплике, раскрывая ответ до
        # обязательного вопроса оператора. Полное описание остаётся у запросов
        # фактов ниже, поэтому эталон числа пострадавших не теряется.
        headline_description = re.sub(r"\d+", "", description)
        headline_description = re.sub(r"\s+", " ", NUMBER_WORD.sub("", headline_description)).strip()
        headline = await ask(
            "Создай название и первую эмоциональную реплику заявителя для учебного вызова 112. "
            "Верни JSON title и first_line. first_line без цифр, адреса и служебных пояснений. "
            "classifier_signs — обязательный путь классификатора ЕКП: сюжет не должен ему "
            "противоречить. Если свободное description конфликтует с classifier_signs, "
            "приоритет всегда у classifier_signs.",
            {"incident_type": source.type.value, "classifier_signs": source.signs,
             "level": source.level.value, "description": headline_description},
            {"type": "object", "properties": {
                "title": {"type": "string"}, "first_line": {"type": "string"},
            }, "required": ["title", "first_line"], "additionalProperties": False},
            max_tokens=220,
            validator=validate_headline,
        )
        facts: dict[str, str] = {}
        address = ""
        victims: int | None = None
        for fact in source.facts:
            question = questions.get(fact.id, fact.id)
            base_payload = {
                "description": description, "incident_type": source.type.value,
                "classifier_signs": source.signs,
                "question": question, "already_known_answers": facts,
            }
            if fact.id == address_id:
                data = await ask(
                    "Придумай конкретный вымышленный московский адрес для учебного сценария. "
                    "Верни JSON value и address. value должно начинаться дословно с address, "
                    "после него можно добавить квартиру, этаж или ориентир. Это ответ, не вопрос. "
                    "Адрес и тип объекта не должны противоречить classifier_signs.",
                    base_payload,
                    {"type": "object", "properties": {
                        "value": {"type": "string"}, "address": {"type": "string"},
                    }, "required": ["value", "address"], "additionalProperties": False},
                    validator=lambda data: validate_value(data, extra_key="address"),
                )
                facts[fact.id] = clean_answer(data["value"])
                # Эталон выводится из факта, а не из второго пересказа модели:
                # малая Qwen иногда меняет пунктуацию или номер между полями.
                address = facts[fact.id][:200].rstrip(" ,.;")
            elif fact.id == victim_id:
                data = await ask(
                    "Ответь на вопрос заявителя конкретным фактом по описанию. Верни JSON value "
                    "и victims. victims — число пострадавших от 0 до 20. value обязательно "
                    "заканчивается точной фразой `Пострадавших: N`, где N равно victims. "
                    "Не пиши вопрос и не считай всех присутствующих пострадавшими. "
                    "Ответ не должен противоречить classifier_signs.",
                    base_payload,
                    {"type": "object", "properties": {
                        "value": {"type": "string"},
                        "victims": {"type": "integer", "minimum": 0, "maximum": 20},
                    }, "required": ["value", "victims"], "additionalProperties": False},
                    validator=validate_victims,
                )
                victims = data["victims"] if isinstance(data["victims"], int) else None
                if victims is not None:
                    facts[fact.id] = victim_answer(victims)
            else:
                data = await ask(
                    "Ответь по-русски конкретным обстоятельством заявителя на question, используя "
                    "description, classifier_signs и already_known_answers. classifier_signs — "
                    "обязательный путь ЕКП и важнее конфликтующих деталей description. Верни JSON "
                    "только с ключом value. value — ответ, не вопрос; не используй знак вопроса "
                    "и плейсхолдеры.",
                    base_payload, value_schema, validator=validate_value,
                )
                facts[fact.id] = clean_answer(data["value"])
        if victims is None:
            raise GenerationError("модель не указала число пострадавших")
        gaps = classifier_sign_gaps(
            source.signs,
            [str(headline["title"]), str(headline["first_line"]), *facts.values()],
        )
        if gaps:
            target = next(
                fact for fact in source.facts
                if fact.id not in {address_id, victim_id}
            )

            def validate_alignment(data: dict) -> None:
                validate_value(data)
                candidate = dict(facts)
                candidate[target.id] = clean_answer(data["value"])
                missing = classifier_sign_gaps(
                    source.signs,
                    [str(headline["title"]), str(headline["first_line"]), *candidate.values()],
                )
                if missing:
                    raise GenerationError(
                        "ответ не подтверждает признаки ЕКП: " + ", ".join(missing)
                    )

            aligned = await ask(
                "Перепиши один ответ заявителя так, чтобы он оставался конкретным ответом на "
                "question и дословно, осмысленно подтверждал все missing_classifier_signs. "
                "Это обязательные признаки ЕКП, их нельзя заменять другим объектом или событием. "
                "Верни JSON только с ключом value, без вопроса и служебных пояснений.",
                {
                    "description": description,
                    "question": questions.get(target.id, target.id),
                    "current_answer": facts[target.id],
                    "missing_classifier_signs": gaps,
                    "all_classifier_signs": source.signs,
                    "already_known_answers": facts,
                },
                value_schema,
                max_tokens=220,
                validator=validate_alignment,
            )
            facts[target.id] = clean_answer(aligned["value"])
        first_line = str(headline["first_line"]).strip()
        if first_line and first_line[-1] not in ".!?…":
            first_line += "!"
        proposal = {
            "title": headline["title"], "first_line": first_line,
            "facts": facts, "ground_truth": {"address": address, "victims": victims},
        }
        proposal = parse_full_proposal(json.dumps(proposal, ensure_ascii=False), source)
        full_proposal_body(source, "ai-full-validation", proposal)
        return proposal
    finally:
        await client.aclose()


async def generate(
    source: Scenario, instruction: str, *, require_fact_change: bool = True
) -> dict:
    settings = get_settings()
    if (not is_loopback_url(
            settings.llm_base_url,
            allow_docker_host=settings.allow_docker_host_models,
        ) or not settings.llm_model_caller):
        raise LlmUnavailable("для генерации нужна разрешённая локальная модель")
    all_editable = editable_fact_ids(source)
    target = correction_target(source, instruction)
    style_only = target is None and not require_fact_change
    # Для 1.7B один комментарий = один изменяемый факт. Это не даёт модели
    # «заодно» испортить газ, пока преподаватель просит сделать плотнее дым.
    editable = [] if style_only else ([target] if target else all_editable)
    fact_questions = {item.fact: item.question for item in source.checklist
                      if item.fact and item.question}
    context = {
        "incident_type": source.type.value,
        "source_title": source.title,
        "editable_facts": {
            fact.id: {"question": fact_questions.get(fact.id, fact.id), "old_answer": fact.value}
            for fact in source.facts if fact.id in editable
        },
    }
    if not require_fact_change:
        context["current_first_line"] = source.first_line
    change_rule = (
        "Измени обстоятельства хотя бы одного факта: дословная копия недопустима. "
        if require_fact_change else
        "Измени черновик по комментарию: допустимо менять только заголовок или first_line; "
        "полностью прежняя версия недопустима. "
    )
    if style_only:
        change_rule += (
            "Это стилистическая правка: не добавляй в first_line новые обстоятельства, "
            "которых нет в current_first_line или комментарии. facts оставь пустым. "
        )
    system = (
        "Сочини НОВУЮ вариацию учебного вызова 112 на русском языке. "
        "Верни ровно JSON с тремя ключами: title, first_line, facts. "
        "facts содержит только id из editable_facts. " + change_rule +
        "Значения facts — НОВЫЕ ответы "
        "заявителя на question, не повторение вопроса. Не добавляй ключи. "
        "first_line — прямая речь заявителя без цифр и адреса. "
        "Тип происшествия, адрес и люди не меняются; их не включай в facts. "
        "Не придумывай службы, коды и регламенты."
    )
    request = LlmRequest(
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps(
                      {"source": context, "request": instruction}, ensure_ascii=False)}],
        model=settings.llm_model_caller, temperature=0.7, max_tokens=500,
        response_format={"type": "json_object", "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "first_line": {"type": "string"},
                "facts": {"type": "object", "properties": {
                    fact_id: {"type": "string"} for fact_id in editable
                }, "additionalProperties": False},
            },
            "required": ["title", "first_line", "facts"],
            "additionalProperties": False,
        }},
        strip_reasoning=True,
    )
    client = LlmClient(base_url=settings.llm_base_url, timeout=45)

    async def complete_safely(candidate: LlmRequest) -> str:
        last_error: LlmUnavailable | None = None
        for _ in range(2):
            try:
                return await client.complete(candidate, use_cache=False)
            except LlmUnavailable as exc:
                last_error = exc
                if not any(marker in str(exc) for marker in (
                    "служебные токены", "пустой ответ модели"
                )):
                    raise
        assert last_error is not None
        raise last_error

    try:
        raw = await complete_safely(request)
        try:
            proposal = parse_proposal(
                raw, source, require_fact_change=require_fact_change, instruction=instruction,
                restrict_first_line_facts=style_only,
            )
        except GenerationError as first_error:
            feedback = (
                f"Предыдущий ответ отклонён: {first_error}. "
                "Верни исправленный JSON той же схемы. "
                "Значения facts должны быть ответами заявителя, не вопросами. "
                "Запрещены плейсхолдеры вида {fact_id}: пиши конкретный русский текст. "
            )
            if target:
                old = next(fact.value for fact in source.facts if fact.id == target)
                feedback += (
                    f"Обязательно замени факт {target}. Старый ответ: {old}. "
                    f"Новый ответ должен учитывать запрос: {instruction}. "
                    "Не повторяй старый ответ дословно."
                )
            retry = LlmRequest(
                messages=[*request.messages, {"role": "assistant", "content": raw},
                          {"role": "user", "content": feedback}],
                model=request.model, temperature=0.8, max_tokens=request.max_tokens,
                response_format=request.response_format,
                strip_reasoning=True,
            )
            raw = await complete_safely(retry)
            try:
                proposal = parse_proposal(
                    raw, source, require_fact_change=require_fact_change, instruction=instruction,
                    restrict_first_line_facts=style_only,
                )
            except GenerationError:
                fallback = style_fallback(source, instruction) if not require_fact_change else None
                if fallback is None:
                    raise
                # Даже детерминированный fallback проходит те же запреты утечек.
                proposal = parse_proposal(
                    json.dumps(fallback, ensure_ascii=False), source,
                    require_fact_change=False, instruction=instruction,
                    restrict_first_line_facts=style_only,
                )
    finally:
        await client.aclose()
    # Проверка на фиксированном ID гарантирует, что генерация не выдаст
    # черновик, который потом невозможно утвердить из-за сломанной схемы.
    try:
        proposal_body(source, "ai-validation", proposal)
    except ScenarioError as exc:
        raise GenerationError(f"сгенерированный сценарий не прошёл проверку: {exc}") from exc
    return proposal
