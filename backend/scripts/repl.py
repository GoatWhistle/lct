"""make repl: текстовый диалог со звонящим, без голоса и без LLM.

Проверяет главную механику до сборки голосового контура: звонящий не выдаёт
данные сам, раскрывает факт на правильный вопрос и злится на повторы.

    make repl                         сценарий по умолчанию
    make repl s=fire-apartment-l2     конкретный сценарий

Команды: /подсказка, /факты, /итог, /выход
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.dialog.caller import TemplateCaller  # noqa: E402
from app.dialog.embeddings import E5Embedder  # noqa: E402
from app.dialog.persona import PersonaState  # noqa: E402
from app.dialog.slots import SlotMachine  # noqa: E402
from app.scenarios.loader import ScenarioError, load_library  # noqa: E402

LIBRARY = ROOT.parent / "scenarios"


def summary(slots: SlotMachine) -> None:
    required = slots.scenario.ground_truth.required_facts
    got = [fact_id for fact_id in required if fact_id in slots.revealed]
    print(f"\n  добыто обязательных фактов: {len(got)} из {len(required)}")
    for fact_id in slots.missing_required():
        item = next((i for i in slots.scenario.checklist if i.fact == fact_id), None)
        hint = f" — эталонный вопрос: «{item.question}»" if item else ""
        print(f"  E1 не добыт {fact_id}{hint}")


def main() -> None:
    scenario_id = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    try:
        library = {scenario.id: scenario for scenario in load_library(LIBRARY)}
    except ScenarioError as exc:
        raise SystemExit(f"библиотека не прошла проверку: {exc}") from exc

    scenario = library.get(scenario_id) if scenario_id else next(iter(library.values()))
    if scenario is None:
        raise SystemExit(f"нет сценария {scenario_id}; есть: {', '.join(library)}")

    try:
        embedder = E5Embedder(ROOT / get_settings().models_dir / "e5-small")
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    slots = SlotMachine(scenario, embedder)
    persona = PersonaState(scenario.persona)
    caller = TemplateCaller()

    print(f"── {scenario.title} ({scenario.level.value}) ──")
    print("Вы — оператор 112. Команды: /подсказка /факты /итог /выход\n")
    print(f"ЗВОНЯЩИЙ [{persona.mood.value}]: {scenario.first_line}")

    while True:
        try:
            line = input("ОПЕРАТОР: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line == "/выход":
            break
        if line == "/итог":
            summary(slots)
            continue
        if line == "/факты":
            for fact in slots.revealed_facts():
                print(f"  раскрыт {fact.id}: {fact.value}")
            continue
        if line == "/подсказка":
            unasked = slots.unasked()
            print(f"  подсказка: {unasked[0].question}" if unasked else "  все пункты чек-листа отработаны")
            continue

        turn = slots.hear(line)
        reply = caller.reply(turn, persona, slots)
        if turn.matched:
            print(f"  [понято: {', '.join(turn.matched)}]")
        print(f"ЗВОНЯЩИЙ [{reply.mood.value}]: {reply.text}")

    summary(slots)


if __name__ == "__main__":
    main()
