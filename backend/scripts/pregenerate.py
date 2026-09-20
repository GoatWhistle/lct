"""make pregen: таблица реплик звонящего для офлайна.

Зачем не «дерево диалога» в буквальном смысле: ветвление уже делает слот-автомат —
он решает, какой факт раскрыт, был ли повтор и уточнён ли адрес. Модели остаётся
дать формулировки, поэтому таблица индексируется парой «событие × настроение».

Сеть нужна только здесь. На занятии таблица читается с диска, и звонящий
отвечает мгновенно, без интернета.

**Один запрос на факт, а не на реплику.** В библиотеке 96 вызовов заказчика,
449 фактов и пять настроений: по реплике за запрос это шесть тысяч обращений
и часы ожидания. Модель отдаёт все настроения одного факта разом, а запросы
идут параллельно — это минуты.

Работа возобновляемая: уже заполненные реплики пропускаются, так что прерванный
прогон продолжается с места остановки.

    make pregen                        все сценарии
    make pregen s=t01-1-fire-container  один
    make pregen t=19                   все вызовы билета 19
    make pregen force=1                перегенерировать заполненное
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.dialog.caller import MOOD_WORDS, _prompt  # noqa: E402
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable  # noqa: E402
from app.scenarios.loader import load_library  # noqa: E402

LIBRARY = ROOT.parent / "scenarios"
OUTPUT = LIBRARY / "pregenerated"

#: Сколько филлеров «не понял вопрос» на каждое настроение: подряд одинаковая
#: фраза звучит как заевшая пластинка.
FILLERS_PER_MOOD = 3

#: Одновременных запросов к провайдеру. Восемь — компромисс: быстрее упираемся
#: в ограничение частоты у провайдера, медленнее ждём зря.
CONCURRENCY = int(os.environ.get("PREGEN_CONCURRENCY", "8"))


def moods_block() -> str:
    return "\n".join(f"  {mood.value} — {words}" for mood, words in MOOD_WORDS.items())


def parse(text: str) -> dict[str, str]:
    """JSON из ответа модели. Рассуждающие модели любят обернуть его в ограду
    или дописать фразу вокруг — вынимаем первый объект и не спорим."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return {
        key: value.strip()
        for key, value in data.items()
        if isinstance(value, str) and value.strip()
    }


async def ask(client: LlmClient, model: str, scenario_title: str, task: str) -> dict[str, str]:
    """Реплики на все настроения одним запросом."""
    prompt = _prompt("pregen.md").format(scenario=scenario_title, task=task, moods=moods_block())
    for attempt in range(1, 4):
        try:
            answer = await client.complete(
                LlmRequest(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    temperature=0.9,
                    max_tokens=700,
                ),
                use_cache=False,
            )
        except LlmUnavailable as exc:
            if attempt == 3:
                print(f"      не получено: {exc}", flush=True)
                return {}
            # 429 — не сбой, а просьба подождать. Три секунды провайдеру
            # ни о чём не говорят, и прогон сгорает на ровном месте.
            pause = (20 if "429" in str(exc) else 3) * attempt
            await asyncio.sleep(pause)
            continue
        lines = parse(answer)
        if lines:
            return lines
    return {}


def tasks_for(scenario, table: dict, force: bool) -> list[tuple[str, str, str]]:
    """Что осталось сгенерировать: (секция, ключ, задание для модели)."""
    todo: list[tuple[str, str, str]] = []
    known = set(MOOD_WORDS)

    def missing(section: str, key: str) -> bool:
        if force:
            return True
        have = table.get(section, {}).get(key) or {}
        return not all(have.get(mood.value) for mood in known)

    for fact in scenario.facts:
        if missing("reveal", fact.id):
            todo.append(("reveal", fact.id,
                         f"Оператор спросил, и ты отвечаешь ему вот этим фактом: «{fact.value}». "
                         "Скажи это своими словами."))
        if missing("repeat", fact.id):
            todo.append(("repeat", fact.id,
                         f"Оператор ПОВТОРНО спрашивает то, что ты уже говорил: «{fact.value}». "
                         "Ты раздражён: напомни, что уже сказал, и повтори коротко."))
        # Уточнение: заявитель назвал не тот адрес и теперь поправляется.
        # Без этой секции офлайн-звонящий на уточняющий вопрос ответит
        # заготовкой и уточнённого адреса не назовёт (tasks/lct-35).
        if fact.refined and missing("refine", fact.id):
            todo.append(("refine", fact.id,
                         f"Оператор переспросил, и выясняется, что ты назвал не то: на самом деле "
                         f"«{fact.refined}». Поправься — не раздражаясь, ты сам ошибся."))
    if force or not all(
        len(table.get("fillers", {}).get(mood.value) or []) >= FILLERS_PER_MOOD for mood in known
    ):
        for index in range(FILLERS_PER_MOOD):
            todo.append(("fillers", str(index),
                         "Оператор спросил о том, чего ты не знаешь, или ты не расслышал вопрос. "
                         "Переспроси или отмахнись — фактов не называй."))
    return todo


async def build(client: LlmClient, model: str, scenario, existing: dict, force: bool,
                gate: asyncio.Semaphore) -> tuple[dict, int]:
    table = {
        "scenario": scenario.id,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "first_line": scenario.first_line,
        "reveal": dict(existing.get("reveal", {})),
        "repeat": dict(existing.get("repeat", {})),
        "refine": dict(existing.get("refine", {})),
        "fillers": dict(existing.get("fillers", {})),
    }
    todo = tasks_for(scenario, table, force)
    if not todo:
        return table, 0

    async def one(section: str, key: str, task: str):
        async with gate:
            return section, key, await ask(client, model, scenario.title, task)

    results = await asyncio.gather(*(one(*item) for item in todo))

    failed = 0
    for section, key, lines in results:
        if not lines:
            failed += 1
            continue
        if section == "fillers":
            for mood, line in lines.items():
                table["fillers"].setdefault(mood, []).append(line)
        else:
            table[section].setdefault(key, {}).update(lines)
    return table, failed


async def main() -> int:
    settings = get_settings()
    if not settings.llm_api_key:
        print("нет LLM_API_KEY в backend/.env — генерировать нечем")
        return 1

    only = os.environ.get("s") or ""
    ticket = os.environ.get("t") or ""
    force = bool(os.environ.get("force"))

    scenarios = load_library(LIBRARY)
    if only:
        scenarios = [s for s in scenarios if s.id == only]
    if ticket:
        scenarios = [s for s in scenarios if str(s.ticket) == ticket]
    if not scenarios:
        print(f"нет сценариев по условию s={only!r} t={ticket!r}")
        return 1

    # Прикидка до запуска: человек должен понимать, что он запускает —
    # тысячу обращений к платному провайдеру или десяток.
    planned = 0
    for scenario in scenarios:
        path = OUTPUT / f"{scenario.id}.yaml"
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        planned += len(tasks_for(scenario, existing or {}, force))
    # flush обязателен: вывод уходит в трубу, а человек должен увидеть оценку
    # объёма до того, как уйдёт первый запрос, а не после всего прогона.
    print(f"модель: {settings.llm_model_caller}, сценариев: {len(scenarios)}, "
          f"запросов: {planned}, параллельно: {CONCURRENCY}", flush=True)
    if not planned:
        print("всё уже сгенерировано (перегенерировать: make pregen force=1)", flush=True)
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    client = LlmClient(timeout=180)  # рассуждающие модели отвечают долго
    gate = asyncio.Semaphore(CONCURRENCY)
    started = datetime.now(timezone.utc)
    total_failed = 0

    try:
        for number, scenario in enumerate(scenarios, 1):
            path = OUTPUT / f"{scenario.id}.yaml"
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            table, failed = await build(client, settings.llm_model_caller, scenario,
                                        existing or {}, force, gate)
            total_failed += failed
            path.write_text(
                yaml.safe_dump(table, allow_unicode=True, sort_keys=False, width=100),
                encoding="utf-8",
            )
            mark = f", не получено: {failed}" if failed else ""
            print(f"  [{number}/{len(scenarios)}] {scenario.id}{mark}", flush=True)
    finally:
        await client.aclose()

    spent = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\nГотово за {spent / 60:.1f} мин. Занятие идёт без сети: "
          "звонящий читает таблицу с диска.")
    if total_failed:
        print(f"Не получено блоков: {total_failed}. Повторный запуск допишет только их.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
