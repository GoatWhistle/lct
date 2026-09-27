"""Схема сценария. Формат: docs/spec/SCENARIO-FORMAT.md

Сценарий — контент, а не код: его пишет методист, и читается он глазами.
Поэтому схема строгая (`extra="forbid"`): опечатка в имени поля должна падать
на старте приложения, а не тихо игнорироваться и всплывать посреди занятия.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.classifiers import DDSCode, IncidentType, Level, Outcome
from app.domain.events import Mood
from app.domain.kio import EDITABLE_KIO_FIELDS, KIO
from app.scoring.address import address_matches
from app.scoring.taxonomy import METRIC_MAP


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
    """Факт открывает только вопрос, сопоставленный жёстким слот-протоколом."""

    question: str = Field(min_length=1)


class Fact(Strict):
    """Факт, который оператор должен добыть.

    `refined` — второе, настоящее значение. Нужно там, где заявитель называет
    ориентир или адрес, по которому стоит сам: «ул. Станционная, 28» оказывается
    Королёвом, «дом с библиотекой №193» — домом 11 по улице Грина. Памятка
    заказчика называет это обычным делом, а не краем сценария, и в билетах такой
    вызов не один (docs/spec/TICKETS.md).

    Пока оператор не переспросил, звонящий говорит `value`; после уточняющего
    вопроса — `refined`, и эталон сверяется уже с ним.
    """

    id: str
    value: str
    reveal_on: RevealOn | None = None
    refined: str | None = None
    refine_on: str | None = Field(
        default=None, description="Пункт чек-листа, уточняющий этот факт"
    )

    @model_validator(mode="after")
    def refinement_needs_both_halves(self):
        if bool(self.refined) != bool(self.refine_on):
            raise ValueError(
                f"факт {self.id}: refined и refine_on задаются вместе — "
                "иначе уточнение либо нечем вызвать, либо нечего сказать"
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
    incident_code: str | None = None
    notify: list[str] = []
    required_facts: list[str] = []
    address: str | None = None
    victims: int | None = None


class DdsDecision(Strict):
    """Эталон первичного решения службы по данной карточке.

    Профильность по ЕКП сама по себе не исключает дубль или территориальный
    отказ, поэтому такие исключения задаются явно в сценарии.
    """

    expected: str = Field(default="accept", pattern="^(accept|decline)$")
    reason: str | None = None

    @model_validator(mode="after")
    def decline_needs_reason(self):
        if self.expected == "decline" and not (self.reason and self.reason.strip()):
            raise ValueError("dds_decision.reason обязателен для эталонного отказа")
        return self


class DdsCardError(Strict):
    """Заложенное расхождение готовой карточки с эталоном для занятия ДДС."""

    field: Literal["address"]
    card_value: str = Field(min_length=3)

    @field_validator("card_value")
    @classmethod
    def nonempty_card_value(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("dds_card_error.card_value: укажите неверный адрес карточки")
        return value


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

    # Признаки происшествия по ЕКП — то, что курсант обязан проставить в карточке.
    # Из них загрузчик выводит код и список оповещения (domain/ekp.py).
    signs: list[str] = Field(default_factory=list, max_length=3)

    # Билет — единица занятия у заказчика: три вызова подряд, разные службы
    # (docs/spec/TICKETS.md). Преподаватель выбирает билет, а не сценарий.
    ticket: int | None = None
    position: int | None = Field(
        default=None, ge=1, le=3, description="Номер вызова в билете"
    )

    # Чем вызов заканчивается правильно. По умолчанию — карточка и выезд;
    # справка и передача в другой регион разбираются в lct-36.
    outcome: Outcome = Outcome.CARD
    dds_decision: DdsDecision = DdsDecision()
    dds_card_error: DdsCardError | None = None

    # Готовая КИО, сформированная курсантом и утверждённая преподавателем.
    # Для системных сценариев поле отсутствует; ДДС использует снимок как
    # исходную карточку вместо реконструкции её из кратких фактов.
    student_card: KIO | None = None

    facts: list[Fact] = []
    checklist: list[ChecklistItem] = []
    required_fields: list[str] = Field(default_factory=list)
    # Учебные веса задаёт преподаватель в утверждаемом сценарии. Ноль
    # отключает метрику из знаменателя; значения методики предварительные.
    score_weights: dict[str, float] = Field(default_factory=dict)
    # Реплики оператора, которые вопросом не являются: «успокойтесь»,
    # «оставайтесь на линии». Общий список — checklists/common.yaml,
    # сценарий может дополнить своими.
    not_questions: list[str] = Field(default_factory=list)
    ground_truth: GroundTruth = GroundTruth()
    era_glonass: EraGlonass | None = None
    tree: Tree = Tree()

    @model_validator(mode="after")
    def valid_score_weights(self):
        unknown = self.score_weights.keys() - METRIC_MAP.keys()
        if unknown:
            raise ValueError(
                f"неизвестные метрики score_weights: {', '.join(sorted(unknown))}"
            )
        if any(not 0 <= weight <= 10 for weight in self.score_weights.values()):
            raise ValueError("score_weights: каждый вес должен быть от 0 до 10")
        return self

    @model_validator(mode="after")
    def valid_required_fields(self):
        duplicates = sorted({path for path in self.required_fields
                             if self.required_fields.count(path) > 1})
        if duplicates:
            raise ValueError(
                f"required_fields: повторяются поля: {', '.join(duplicates)}"
            )
        unavailable = sorted(set(self.required_fields) - EDITABLE_KIO_FIELDS)
        if unavailable:
            raise ValueError(
                "required_fields: поля отсутствуют в форме КИО или заполняются системой: "
                + ", ".join(unavailable)
            )
        if self.outcome is not Outcome.CARD and self.required_fields:
            raise ValueError(
                "required_fields должны быть пустыми, если карточка КИО не создаётся"
            )
        return self

    @model_validator(mode="after")
    def valid_dds_card_error(self):
        error = self.dds_card_error
        if error is None:
            return self
        if not self.ground_truth.address or not self.ground_truth.address.strip():
            raise ValueError("dds_card_error: для ошибки адреса нужен ground_truth.address")
        if self.student_card is not None:
            raise ValueError("dds_card_error: используйте отдельный учебный сценарий без student_card")
        if address_matches(self.ground_truth.address, error.card_value):
            raise ValueError("dds_card_error: адрес карточки должен отличаться от эталона")
        return self

    @model_validator(mode="after")
    def unique_reference_ids(self):
        for label, values in (
            ("id фактов", [fact.id for fact in self.facts]),
            ("id пунктов чек-листа", [item.id for item in self.checklist]),
            ("признаки ЕКП", self.signs),
        ):
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(f"{label} должны быть уникальны: {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def ticket_needs_position(self):
        if (self.ticket is None) != (self.position is None):
            raise ValueError(
                "ticket и position задаются вместе: билет без номера вызова неполон"
            )
        return self

    @model_validator(mode="after")
    def era_only_for_era_type(self):
        if self.era_glonass is not None and self.type is not IncidentType.ERA_GLONASS:
            raise ValueError("era_glonass задан, но type не era_glonass")
        if self.type is IncidentType.ERA_GLONASS and self.era_glonass is None:
            raise ValueError("type era_glonass требует блок era_glonass")
        return self

    def fact_ids(self) -> set[str]:
        return {fact.id for fact in self.facts}
