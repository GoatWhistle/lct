"""Схема сценария. Формат: docs/spec/SCENARIO-FORMAT.md

Сценарий — контент, а не код: его пишет методист, и читается он глазами.
Поэтому схема строгая (`extra="forbid"`): опечатка в имени поля должна падать
на старте приложения, а не тихо игнорироваться и всплывать посреди занятия.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.classifiers import DDSCode, IncidentType, Level
from app.domain.events import Mood


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArcStage(Strict):
    stage: str
    mood: Mood


class Persona(Strict):
    base: str
    arc: list[ArcStage] = []


class Background(Strict):
    loop: str
    gain_db: float = -18


class RevealOn(Strict):
    """Два вида условий: вопрос из чек-листа либо подход оператора."""

    question: str | None = None
    approach: str | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if bool(self.question) == bool(self.approach):
            raise ValueError("reveal_on: ровно одно из `question` или `approach`")
        return self


class Fact(Strict):
    id: str
    value: str
    hidden: bool = False
    reveal_on: RevealOn | None = None

    @model_validator(mode="after")
    def hidden_needs_condition(self):
        if self.hidden and (self.reveal_on is None or not self.reveal_on.approach):
            raise ValueError(
                f"факт {self.id}: hidden требует reveal_on.approach — "
                "скрытый факт не раскрывается прямым вопросом"
            )
        return self


class ChecklistItem(Strict):
    """Пункт эталонного опроса.

    `examples` — другие формулировки того же вопроса. Без них матчинг
    по эмбеддингам не отличает вопрос от не-вопроса: на e5 «Оставайтесь
    на линии» ближе к пункту чек-листа, чем половина настоящих вопросов.
    """

    id: str
    question: str | None = None
    fact: str | None = None
    examples: list[str] = []


class EraGlonass(Strict):
    vin: str
    coords: dict
    passengers: int
    impact_force: str


class Tree(Strict):
    pregenerated: bool = False


class GroundTruth(Strict):
    """Выводится кодом. В YAML допускаются только нормализованные ожидания
    (адрес и число пострадавших): вывести «улица Ленина, 14» из фразы
    «улица Ленина, 14, квартира 47, 5-й этаж» кодом нельзя, а сверять оценку
    с сырым текстом факта — значит штрафовать курсанта за правильный ответ.

    Всё остальное загрузчик проставляет сам и запрещает писать руками —
    иначе генератор сценариев рассинхронизирует факты и эталон.
    """

    incident_type: IncidentType | None = None
    dds: DDSCode | None = None
    required_facts: list[str] = []
    address: str | None = None
    victims: int | None = None


class Scenario(Strict):
    id: str
    title: str
    type: IncidentType
    level: Level
    topics: list[str] = []
    modes: list[str] = ["training"]
    extends: str | None = None

    persona: Persona
    background: Background | None = None
    first_line: str

    facts: list[Fact] = []
    checklist: list[ChecklistItem] = []
    required_fields: list[str] = Field(default_factory=list)
    # Реплики оператора, которые вопросом не являются: «успокойтесь»,
    # «оставайтесь на линии». Общий список — checklists/common.yaml,
    # сценарий может дополнить своими.
    not_questions: list[str] = Field(default_factory=list)
    ground_truth: GroundTruth = GroundTruth()
    era_glonass: EraGlonass | None = None
    tree: Tree = Tree()

    @model_validator(mode="after")
    def era_only_for_era_type(self):
        if self.era_glonass is not None and self.type is not IncidentType.ERA_GLONASS:
            raise ValueError("era_glonass задан, но type не era_glonass")
        if self.type is IncidentType.ERA_GLONASS and self.era_glonass is None:
            raise ValueError("type era_glonass требует блок era_glonass")
        return self

    def fact_ids(self) -> set[str]:
        return {fact.id for fact in self.facts}
