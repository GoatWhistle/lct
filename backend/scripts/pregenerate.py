"""make pregen: построить таблицу реплик звонящего для офлайна.

Зачем не «дерево диалога» в буквальном смысле: ветвление уже делает слот-автомат —
он решает, какой факт раскрыт и был ли повтор. Модели остаётся дать формулировки,
поэтому таблица индексируется парой «событие × настроение» и получается небольшой:
для пожарного сценария — 62 реплики.

Результат кладётся рядом со сценариями, в `scenarios/pregenerated/<id>.yaml`:
это контент, и методист должен иметь возможность его прочитать и поправить.

Сеть нужна только здесь. На занятии таблица читается с диска, и звонящий
отвечает мгновенно, без интернета.

    make pregen                        все сценарии
    make pregen s=fire-apartment-l2    один
    make pregen force=1                перегенерировать уже готовое
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.dialog.caller import MOOD_WORDS, _prompt  # noqa: E402
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable  # noqa: E402
from app.domain.events import Mood  # noqa: E402
from app.scenarios.loader import load_library  # noqa: E402

LIBRARY = ROOT.parent / "scenarios"
OUTPUT = LIBRARY / "pregenerated"

#: Сколько филлеров «не понял вопрос» на каждое настроение: подряд одинаковая
#: фраза звучит как заевшая пластинка.
FILLERS_PER_MOOD = 3


async def ask(client: LlmClient, model: str, scenario_title: str, mood: Mood, task: str) -> str | None:
    prompt = _prompt("pregen.md").format(
        scenario=scenario_title, mood=MOOD_WORDS.get(mood, mood.value), task=task
    )
    for attempt in range(1, 4):
        try:
            return await client.complete(
                LlmRequest(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    temperature=0.9,
                ),
                use_cache=False,
            )
        except LlmUnavailable as exc:
            print(f"      попытка {attempt}: {exc}", flush=True)
            await asyncio.sleep(3 * attempt)
    return None


async def build(client: LlmClient, model: str, scenario, existing: dict, force: bool) -> dict:
    table = {
        "scenario": scenario.id,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "first_line": scenario.first_line,
        "reveal": dict(existing.get("reveal", {})),
        "repeat": dict(existing.get("repeat", {})),
        "fillers": dict(existing.get("fillers", {})),
    }
    moods = list(MOOD_WORDS)
    facts = {fact.id: fact.value for fact in scenario.facts}
    total = len(facts) * len(moods) * 2 + len(moods) * FILLERS_PER_MOOD
    done = 0

    for fact_id, value in facts.items():
        for section, task in (
            ("reveal", f"Оператор спросил, и ты отвечаешь ему вот этим фактом: «{value}». Скажи это своими словами."),
            ("repeat", f"Оператор ПОВТОРНО спрашивает то, что ты уже говорил: «{value}». "
                       f"Ты раздражён: напомни, что уже сказал, и повтори коротко."),
        ):
            table[section].setdefault(fact_id, {})
            for mood in moods:
                done += 1
                if not force and table[section][fact_id].get(mood.value):
                    continue
                line = await ask(client, model, scenario.title, mood, task)
                if line:
                    table[section][fact_id][mood.value] = line
                print(f"    [{done}/{total}] {section} {fact_id} {mood.value}: {line or 'НЕ ПОЛУЧЕНО'}", flush=True)

    for mood in moods:
        table["fillers"].setdefault(mood.value, [])
        for index in range(FILLERS_PER_MOOD):
            done += 1
            if not force and len(table["fillers"][mood.value]) > index:
                continue
            line = await ask(
                client, model, scenario.title, mood,
                "Оператор спросил о том, чего ты не знаешь, или ты не расслышал вопрос. "
                "Переспроси или отмахнись — фактов не называй.",
            )
            if line:
                table["fillers"][mood.value].append(line)
            print(f"    [{done}/{total}] filler {mood.value}: {line or 'НЕ ПОЛУЧЕНО'}", flush=True)

    return table


async def main() -> int:
    settings = get_settings()
    if not settings.llm_api_key:
        print("нет LLM_API_KEY в backend/.env — генерировать нечем")
        return 1

    only = os.environ.get("s") or (sys.argv[1] if len(sys.argv) > 1 else "")
    force = bool(os.environ.get("force"))
    scenarios = [s for s in load_library(LIBRARY) if not only or s.id == only]
    if not scenarios:
        print(f"нет сценария {only}")
        return 1

    print(f"модель: {settings.llm_model_caller}")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    client = LlmClient(timeout=180)  # рассуждающие модели отвечают долго
    try:
        for scenario in scenarios:
            path = OUTPUT / f"{scenario.id}.yaml"
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            print(f"\n{scenario.id} — {scenario.title}")
            table = await build(client, settings.llm_model_caller, scenario, existing or {}, force)
            path.write_text(
                yaml.safe_dump(table, allow_unicode=True, sort_keys=False, width=100),
                encoding="utf-8",
            )
            missing = sum(
                1
                for section in ("reveal", "repeat")
                for fact in table[section].values()
                for mood in MOOD_WORDS
                if not fact.get(mood.value)
            )
            print(f"  → {path.relative_to(ROOT.parent)}" + (f", не получено реплик: {missing}" if missing else ""))
    finally:
        await client.aclose()

    print("\nГотово. Занятие теперь идёт без сети: звонящий читает таблицу с диска.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
