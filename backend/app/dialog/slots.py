"""Слот-автомат: какие факты раскрыты, какие вопросы заданы.

**Звонящий не выдаёт информацию сам.** Автомат держит состояние явно и отдаёт
звонящему только раскрытые факты — иначе LLM услужливо назовёт адрес без вопроса
(docs/product/CALL-SIM.md).

Тот же автомат детерминированно отвечает на вопрос «спросил ли оператор про этаж»:
на нём стоят подсказки, полнота опроса в оценке (E1) и аналитика группы.
"""

import re
from dataclasses import dataclass, field

import numpy as np

from app.dialog.embeddings import Embedder
from app.scenarios.schema import ChecklistItem, Fact, Scenario

#: Как решается, что реплика — вопрос пункта чек-листа.
#:
#: Порог по косинусной близости не работает: на multilingual-e5-small настоящие
#: вопросы дают 0.79–0.95, а не-вопросы вроде «Оставайтесь на линии» — до 0.89.
#: Диапазоны перекрываются, и любой порог либо выдаёт адрес на «успокойтесь»,
#: либо не слышит «где вы находитесь?».
#:
#: Работает сравнение с ближайшим соседом: реплика засчитывается пункту, если
#: она ближе к одной из его формулировок, чем к любому не-вопросу из
#: checklists/common.yaml. На отложенных фразах (tests/test_slots_e5.py):
#: 19 из 20 вопросов распознано, 0 из 8 ложных срабатываний.
#:
#: MATCH_MARGIN — насколько ближе к пункту, чем к не-вопросу. Ноль даёт лучшее
#: распознавание при нуле ложных срабатываний; 0.03 теряет уже 5 вопросов из 20.
#: MATCH_FLOOR — нижняя граница на случай сценария без не-вопросов.
MATCH_MARGIN = 0.0
MATCH_FLOOR = 0.75

#: Реплика режется только по границам предложений, и знак остаётся при части.
#: «?» для e5 — сильный признак вопроса: без него «Куда ехать» ближе
#: к «Оставайтесь на линии», чем к пункту про адрес. По запятым и «и» не режем:
#: «Улица, дом?» распадается на обрывки, которые не похожи ни на что.
_SENTENCES = re.compile(r"(?<=[?!.;])\s+")


@dataclass
class TurnResult:
    """Что произошло в одной реплике оператора."""

    text: str
    matched: list[str] = field(default_factory=list)   # пункты чек-листа
    revealed: list[str] = field(default_factory=list)  # факты, раскрытые впервые
    repeated: list[str] = field(default_factory=list)  # факты, спрошенные повторно
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def understood(self) -> bool:
        return bool(self.matched)


class SlotMachine:
    def __init__(
        self,
        scenario: Scenario,
        embedder: Embedder,
        floor: float = MATCH_FLOOR,
        margin: float = MATCH_MARGIN,
    ) -> None:
        self.scenario = scenario
        self.floor = floor
        self.margin = margin
        self._embedder = embedder

        self._facts: dict[str, Fact] = {fact.id: fact for fact in scenario.facts}
        self._items: list[ChecklistItem] = [item for item in scenario.checklist if item.question]

        # Каждая формулировка — отдельная строка; `_owner` помнит, чей это пункт.
        texts: list[str] = []
        self._owner: list[int] = []
        for index, item in enumerate(self._items):
            for text in [item.question, *item.examples]:
                texts.append(text)
                self._owner.append(index)
        self._anchors = embedder.embed(texts)
        self._not_questions = (
            embedder.embed(scenario.not_questions) if scenario.not_questions else None
        )

        # Какой пункт чек-листа какие факты раскрывает. Скрытые факты вопросом
        # не раскрываются никогда — только подходом.
        self._reveals: dict[str, list[str]] = {}
        for item in self._items:
            if item.fact and not self._facts[item.fact].hidden:
                self._reveals.setdefault(item.id, []).append(item.fact)
        for fact in scenario.facts:
            question = fact.reveal_on.question if fact.reveal_on else None
            if question and not fact.hidden and fact.id not in self._reveals.get(question, []):
                self._reveals.setdefault(question, []).append(fact.id)

        self.asked: list[str] = []
        self.revealed: list[str] = []

    # ── реплика оператора ──

    def _clauses(self, text: str) -> list[str]:
        parts = [part.strip() for part in _SENTENCES.split(text.strip()) if part.strip()]
        return parts or [text]

    def hear(self, text: str) -> TurnResult:
        """Сопоставить реплику с чек-листом и раскрыть заслуженные факты.

        Реплика режется на предложения: «Где вы? Есть кто внутри?» — два вопроса,
        и оба должны засчитаться.
        """
        result = TurnResult(text=text)
        clauses = self._embedder.embed(self._clauses(text))
        by_anchor = self._anchors @ clauses.T  # формулировки × части реплики

        # Лучшая близость каждого пункта к каждой части реплики.
        owner = np.array(self._owner)
        item_scores = np.stack(
            [by_anchor[owner == index].max(axis=0) for index in range(len(self._items))]
        )  # пункты × части

        # Насколько каждая часть похожа на не-вопрос.
        if self._not_questions is not None:
            not_question = (self._not_questions @ clauses.T).max(axis=0)
        else:
            not_question = np.full(clauses.shape[0], -1.0)

        # Часть реплики засчитывается только одному, ближайшему пункту:
        # «адрес» не должен заодно раскрыть «кто в квартире».
        winners: dict[int, float] = {}
        for part in range(clauses.shape[0]):
            best = int(np.argmax(item_scores[:, part]))
            score = float(item_scores[best, part])
            if score >= self.floor and score > not_question[part] + self.margin:
                winners[best] = max(score, winners.get(best, -1.0))

        for index, item in enumerate(self._items):
            result.scores[item.id] = float(item_scores[index].max())
            if index not in winners:
                continue
            result.matched.append(item.id)
            if item.id not in self.asked:
                self.asked.append(item.id)
            for fact_id in self._reveals.get(item.id, []):
                if fact_id in self.revealed:
                    if fact_id not in result.repeated:
                        result.repeated.append(fact_id)
                else:
                    self.revealed.append(fact_id)
                    result.revealed.append(fact_id)
        return result

    def reveal_by_approach(self, fact_id: str) -> bool:
        """Скрытый факт раскрывается подходом оператора, а не вопросом.
        Решение «создал ли оператор подход» принимает LLM (temperature=0) —
        автомат только фиксирует результат."""
        fact = self._facts.get(fact_id)
        if fact is None or fact_id in self.revealed:
            return False
        self.revealed.append(fact_id)
        return True

    def invalidate(self, fact_id: str) -> None:
        """Директива «адрес оказался неточным»: оператор обязан переспросить."""
        if fact_id in self.revealed:
            self.revealed.remove(fact_id)

    # ── что видят другие ──

    def revealed_facts(self) -> list[Fact]:
        """Единственное, что уходит в контекст звонящего."""
        return [self._facts[fact_id] for fact_id in self.revealed]

    def unasked(self) -> list[ChecklistItem]:
        """Неотработанные пункты по порядку чек-листа — источник подсказки."""
        return [item for item in self._items if item.id not in self.asked]

    def missing_required(self) -> list[str]:
        """Обязательные факты, которые оператор так и не добыл — основание E1."""
        return [
            fact_id
            for fact_id in self.scenario.ground_truth.required_facts
            if fact_id not in self.revealed
        ]
