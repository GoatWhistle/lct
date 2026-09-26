"""Состояние звонящего: базовый профиль, эмоциональная дуга, раздражение.

Дуга — фон, а не пьеса: она меняет подачу, а не факты. Директивы преподавателя
перекрывают дугу на время действия, повторные вопросы копят раздражение
(docs/product/CALL-SIM.md).
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.domain.events import Mood
from app.scenarios.schema import Persona

#: Настроение базового профиля, если дуга не задана.
BASE_MOOD: dict[str, Mood] = {
    "calm": Mood.CALM,
    "worried": Mood.WORRIED,
    "panic": Mood.PANIC,
    "aggressive": Mood.AGGRESSIVE,
    "elderly": Mood.CONFUSED,
    "drunk": Mood.CONFUSED,
    "evasive": Mood.WORRIED,
    "child": Mood.PANIC,
    "foreigner": Mood.WORRIED,
}

#: С какого числа повторов звонящий срывается в агрессию.
#: Первый повтор — «Я же сказал!», второй и дальше — уже крик.
AGGRESSION_AFTER_REPEATS = 2

#: Директивы, меняющие подачу. Остальные (обрыв связи, второй пострадавший)
#: правят факты и таймеры, а не настроение.
DIRECTIVE_MOOD: dict[str, Mood] = {
    "panic_rises": Mood.PANIC,
    "screaming": Mood.PANIC,
    "turns_aggressive": Mood.AGGRESSIVE,
    "distracted": Mood.CONFUSED,
}


class PersonaProgress(BaseModel):
    """Где звонящий по дуге и что накопил; профиль берётся из сценария."""

    stage: str = "registration"
    repeats: int = 0
    directive: str | None = None
    history: list[Mood] = Field(default_factory=list)


@dataclass
class PersonaState:
    persona: Persona
    progress: PersonaProgress = field(default_factory=PersonaProgress)

    @property
    def stage(self) -> str:
        return self.progress.stage

    @stage.setter
    def stage(self, value: str) -> None:
        self.progress.stage = value

    @property
    def repeats(self) -> int:
        return self.progress.repeats

    @repeats.setter
    def repeats(self, value: int) -> None:
        self.progress.repeats = value

    @property
    def directive(self) -> str | None:
        return self.progress.directive

    @directive.setter
    def directive(self, value: str | None) -> None:
        self.progress.directive = value

    @property
    def history(self) -> list[Mood]:
        return self.progress.history

    @property
    def base(self) -> str:
        return self.persona.base

    def _arc_mood(self) -> Mood:
        for step in self.persona.arc:
            if step.stage == self.stage:
                return step.mood
        return BASE_MOOD.get(self.persona.base, Mood.CALM)

    @property
    def mood(self) -> Mood:
        """Приоритет: директива преподавателя → раздражение → дуга → база."""
        if self.directive in DIRECTIVE_MOOD:
            return DIRECTIVE_MOOD[self.directive]
        if self.repeats >= AGGRESSION_AFTER_REPEATS:
            return Mood.AGGRESSIVE
        return self._arc_mood()

    def on_repeat(self) -> None:
        """Повтор не штрафуется в оценке (иногда он оправдан), но звонящий
        его запоминает — и это попадает в разбор."""
        self.repeats += 1

    def advance(self, stage: str) -> None:
        self.stage = stage

    def remember(self) -> Mood:
        mood = self.mood
        self.history.append(mood)
        return mood
