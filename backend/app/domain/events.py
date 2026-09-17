"""Схемы событий WebSocket — источник истины для make types.

Контракт целиком: docs/arch/CONTRACT.md
Каналы: /ws/call (курсант), /ws/station (ДДС), /ws/observe (монитор и пульт,
только приём), /ws/control (только преподаватель, только передача).

Правила, зашитые в схемы:
  * курсанту уходят дельты, наблюдателям — состояние целиком;
  * все `at` — серверное UTC, фронт время не считает;
  * ошибки идут одним каналом `error`, без HTTP-кодов внутри WS.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.classifiers import DDSCode, IncidentType, Level
from app.domain.kio import KIO, Coords
from app.domain.taxonomy import Finding
from app.domain.timers import TimerSnapshot


class SessionMode(StrEnum):
    """Режим сессии. Меняет доступность подсказок и протоколирование,
    но не поведение звонящего (docs/product/MODES.md)."""

    TRAINING = "training"
    EXAM = "exam"
    SELF = "self"


class Speaker(StrEnum):
    CALLER = "caller"
    OPERATOR = "operator"


class Mood(StrEnum):
    """Состояние звонящего. Фон подачи, не факты."""

    CALM = "calm"
    WORRIED = "worried"
    PANIC = "panic"
    AGGRESSIVE = "aggressive"
    CONFUSED = "confused"


class CallEndReason(StrEnum):
    HANGUP = "hangup"
    DROPPED = "dropped"
    INSTRUCTOR = "instructor"


class PatchSource(StrEnum):
    """`auto` — сервер распознал факт из речи, `operator` — эхо правки курсанта."""

    AUTO = "auto"
    OPERATOR = "operator"


class DirectiveMode(StrEnum):
    """`immediate` — только для обрыва связи: рвёт TTS на полуслове."""

    NEXT_TURN = "next_turn"
    IMMEDIATE = "immediate"


class ErrorKind(StrEnum):
    """Коды канала `error`. Фронт разбирает код, а не текст сообщения:
    текст — для человека, код — для поведения интерфейса."""

    SESSION_NOT_FOUND = "session_not_found"
    CALL_NOT_STARTED = "call_not_started"
    HINT_DENIED_IN_EXAM = "hint_denied_in_exam"
    MODELS_WARMING_UP = "models_warming_up"
    DIRECTIVE_NEEDS_NETWORK = "directive_needs_network"
    SCENARIO_INVALID = "scenario_invalid"
    UNSUPPORTED_EVENT = "unsupported_event"
    INTERNAL = "internal"


class TranscriptEntry(BaseModel):
    """Реплика в ленте. `ref` — якорь для пометок преподавателя и отметок разбора."""

    ref: str
    speaker: Speaker
    text: str
    at: datetime
    mood: Mood | None = None


# ─────────────────────────── сервер → курсант ───────────────────────────


class CallIncoming(BaseModel):
    """Обязательность полей приходит сценарием, а не моделью: без `required_fields`
    АРМ не может подсветить незаполненное обязательное поле, и курсант узнаёт
    о неполноте карточки только из разбора."""

    type: Literal["call.incoming"] = "call.incoming"
    scenario_id: str
    caller_number: str
    level: Level
    mode: SessionMode
    required_fields: list[str] = []


class CallStarted(BaseModel):
    type: Literal["call.started"] = "call.started"
    started_at: datetime


class SttPartial(BaseModel):
    type: Literal["stt.partial"] = "stt.partial"
    text: str


class SttFinal(BaseModel):
    type: Literal["stt.final"] = "stt.final"
    text: str
    at: datetime


class CallerUtterance(BaseModel):
    type: Literal["caller.utterance"] = "caller.utterance"
    utterance_id: UUID
    text: str
    at: datetime
    mood: Mood


class TtsBegin(BaseModel):
    type: Literal["tts.begin"] = "tts.begin"
    utterance_id: UUID


class TtsEnd(BaseModel):
    type: Literal["tts.end"] = "tts.end"
    utterance_id: UUID


class TtsCancel(BaseModel):
    """Оператор перебил. Фронт мгновенно чистит очередь воспроизведения."""

    type: Literal["tts.cancel"] = "tts.cancel"
    utterance_id: UUID
    reason: Literal["barge_in", "director"] = "barge_in"


class BgStart(BaseModel):
    """Аудио-фон происшествия. Файл лежит в сборке фронта, трафика фон не создаёт."""

    type: Literal["bg.start"] = "bg.start"
    loop: str
    gain_db: float


class BgStop(BaseModel):
    type: Literal["bg.stop"] = "bg.stop"


class EraData(BaseModel):
    """Автоданные ЭРА-ГЛОНАСС до соединения с водителем."""

    type: Literal["era.data"] = "era.data"
    vin: str
    coords: Coords
    passengers: int
    impact_force: str


class KioPatchOut(BaseModel):
    """Сервер распознал факт из речи либо шлёт эхо чужой правки."""

    type: Literal["kio.patch"] = "kio.patch"
    fields: dict[str, Any]
    source: PatchSource


class HintShown(BaseModel):
    type: Literal["hint.shown"] = "hint.shown"
    checklist_id: str
    question: str


class TimerTick(BaseModel):
    """Раз в секунду, не на каждое изменение: таймеров дюжина, UI рисует секунды."""

    type: Literal["timer.tick"] = "timer.tick"
    timers: list[TimerSnapshot]


class CallEnded(BaseModel):
    type: Literal["call.ended"] = "call.ended"
    reason: CallEndReason


class ScoreReady(BaseModel):
    type: Literal["score.ready"] = "score.ready"
    session_id: UUID


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: ErrorKind
    message: str


ServerToTrainee = Annotated[
    CallIncoming
    | CallStarted
    | SttPartial
    | SttFinal
    | CallerUtterance
    | TtsBegin
    | TtsEnd
    | TtsCancel
    | BgStart
    | BgStop
    | EraData
    | KioPatchOut
    | HintShown
    | TimerTick
    | CallEnded
    | ScoreReady
    | ErrorEvent,
    Field(discriminator="type"),
]


# ─────────────────────────── курсант → сервер ───────────────────────────


class CallAnswer(BaseModel):
    """Снял гарнитуру. Останавливает норматив `answer`."""

    type: Literal["call.answer"] = "call.answer"


class KioPatchIn(BaseModel):
    """Правка карточки. Дебаунс 300 мс, шлётся только дельта."""

    type: Literal["kio.patch"] = "kio.patch"
    fields: dict[str, Any]


class HintRequest(BaseModel):
    """В режиме `exam` сервер отвечает событием `error`."""

    type: Literal["hint.request"] = "hint.request"


class SelfAssessmentSubmit(BaseModel):
    """Самооценка до показа автооценки."""

    type: Literal["self_assessment.submit"] = "self_assessment.submit"
    missed: list[str]
    comment: str = ""


class DdsDispatch(BaseModel):
    """Передача в ДДС. Замораживает карточку снимком и останавливает опрос (`interview`)."""

    type: Literal["dds.dispatch"] = "dds.dispatch"
    service: DDSCode


class CallHangup(BaseModel):
    type: Literal["call.hangup"] = "call.hangup"


class CallbackDial(BaseModel):
    """Обратный дозвон после обрыва: 3 попытки по 10 с."""

    type: Literal["callback.dial"] = "callback.dial"


TraineeToServer = Annotated[
    CallAnswer
    | KioPatchIn
    | HintRequest
    | SelfAssessmentSubmit
    | DdsDispatch
    | CallHangup
    | CallbackDial,
    Field(discriminator="type"),
]


# ───────────────────────── сервер → наблюдателям ─────────────────────────


class SessionSnapshot(BaseModel):
    """Обязательно при подключении: монитор в классе включают посреди занятия."""

    type: Literal["session.snapshot"] = "session.snapshot"
    session_id: UUID
    scenario_id: str
    scenario_title: str
    level: Level
    mode: SessionMode
    trainee_name: str | None = None
    started_at: datetime | None = None
    kio: KIO
    required_fields: list[str] = []
    transcript: list[TranscriptEntry]
    timers: list[TimerSnapshot]
    hints_used: int = 0
    ended: bool = False


class TranscriptAppend(BaseModel):
    type: Literal["transcript.append"] = "transcript.append"
    entry: TranscriptEntry


class KioState(BaseModel):
    """Полная карточка, не дельта: наблюдателю проще, рассинхрон дороже килобайт."""

    type: Literal["kio.state"] = "kio.state"
    kio: KIO


class ModeSet(BaseModel):
    type: Literal["mode.set"] = "mode.set"
    mode: SessionMode


class SessionEnded(BaseModel):
    type: Literal["session.ended"] = "session.ended"
    reason: CallEndReason


class InstructorNoteShown(BaseModel):
    """Пометка преподавателя видна всем, кому виден транскрипт."""

    type: Literal["instructor_note.shown"] = "instructor_note.shown"
    transcript_ref: str
    text: str
    author: str


class ReferenceStarted(BaseModel):
    """Автопроигрывание эталонного звонка на внешнем мониторе."""

    type: Literal["reference.started"] = "reference.started"
    scenario_id: str


ServerToObserver = Annotated[
    SessionSnapshot
    | TranscriptAppend
    | KioState
    | TimerTick
    | ModeSet
    | HintShown
    | BgStart
    | BgStop
    | CallerUtterance
    | SessionEnded
    | ScoreReady
    | InstructorNoteShown
    | ReferenceStarted
    | ErrorEvent,
    Field(discriminator="type"),
]


# ──────────────────── преподаватель → сервер (control) ────────────────────


class ScenarioStart(BaseModel):
    type: Literal["scenario.start"] = "scenario.start"
    scenario_id: str
    trainee: str
    group_id: str | None = None
    mode: SessionMode


class DirectorInject(BaseModel):
    """Директива звонящему. Карточку курсанта не трогает ни одна команда."""

    type: Literal["director.inject"] = "director.inject"
    directive: str
    mode: DirectiveMode = DirectiveMode.NEXT_TURN


class ReferencePlay(BaseModel):
    type: Literal["reference.play"] = "reference.play"


class InstructorNoteAdd(BaseModel):
    type: Literal["instructor_note.add"] = "instructor_note.add"
    transcript_ref: str
    text: str


class ScoreOverride(BaseModel):
    """Коррекция оценки. Автооценка сохраняется рядом."""

    type: Literal["score.override"] = "score.override"
    session_id: UUID
    verdict: str
    comment: str


class ScenarioPublish(BaseModel):
    type: Literal["scenario.publish"] = "scenario.publish"
    scenario_id: str


class SessionStop(BaseModel):
    type: Literal["session.stop"] = "session.stop"


InstructorToServer = Annotated[
    ScenarioStart
    | DirectorInject
    | ReferencePlay
    | InstructorNoteAdd
    | ScoreOverride
    | ScenarioPublish
    | SessionStop,
    Field(discriminator="type"),
]


# ───────────────────────────── станция ДДС ─────────────────────────────


class CardReceived(BaseModel):
    """Снимок КИО. После передачи не меняется — оператор не дописывает задним числом."""

    type: Literal["card.received"] = "card.received"
    card: KIO
    from_operator: str
    at: datetime


class CardAck(BaseModel):
    """Останавливает норматив `dds_ack` (≤ 4 с)."""

    type: Literal["card.ack"] = "card.ack"


class CardBounce(BaseModel):
    """Возврат на уточнение: неполнота КИО становится сорванным выездом."""

    type: Literal["card.bounce"] = "card.bounce"
    missing_fields: list[str]
    comment: str = ""


class ZoneDecision(BaseModel):
    type: Literal["zone.decision"] = "zone.decision"
    in_zone: bool


class CrewDispatched(BaseModel):
    type: Literal["crew.dispatched"] = "crew.dispatched"
    at: datetime


class CrewArrived(BaseModel):
    type: Literal["crew.arrived"] = "crew.arrived"
    at: datetime


ServerToStation = Annotated[
    CardReceived | TimerTick | SessionEnded | ErrorEvent,
    Field(discriminator="type"),
]

StationToServer = Annotated[
    CardAck | CardBounce | ZoneDecision | CrewDispatched | CrewArrived,
    Field(discriminator="type"),
]


# ─────────────────────────── отчёт по HTTP ───────────────────────────


class Metric(BaseModel):
    """Метрика оценки: факт против норматива со ссылкой. Не балл, а обоснование."""

    key: str
    title: str
    fact: str
    norm: str
    ref: str | None = None
    passed: bool
    weight: float = 1.0


class CompetencyScore(BaseModel):
    competency: str
    value: float


class HintUsage(BaseModel):
    checklist_id: str
    question: str
    at: datetime


class SelfAssessment(BaseModel):
    missed: list[str]
    comment: str = ""
    submitted_at: datetime


class SelfAssessmentDiff(BaseModel):
    """Расхождение самооценки с автооценкой — отдельный материал для преподавателя.

    Курсант, не заметивший, что пропустил вопрос о пострадавших, — более важный
    случай, чем сама ошибка (docs/product/DEBRIEF.md).
    """

    #: Пропустил и сам это заметил.
    noticed: list[str] = []
    #: Пропустил и не заметил — самое ценное для разбора.
    unnoticed: list[str] = []
    #: Отметил как пропущенное, хотя на деле спросил.
    overcautious: list[str] = []


class SessionReport(BaseModel):
    """Единица истории: из отчётов складываются профиль, дельта попыток, аналитика."""

    session_id: UUID
    scenario_id: str
    mode: SessionMode
    attempt: int = 1
    transcript: list[TranscriptEntry]
    findings: list[Finding]
    metrics: list[Metric]
    competencies: list[CompetencyScore]
    reference_questions: list[HintShown]
    missed_checklist: list[str]
    hints_used: list[HintUsage]
    self_assessment: SelfAssessment | None = None
    self_assessment_diff: SelfAssessmentDiff | None = None
    notes: list[InstructorNoteShown] = []
    score_auto: float
    score_final: float
    overridden_by: str | None = None
    override_comment: str | None = None


class EventCatalog(BaseModel):
    """Единственное назначение — собрать все союзы в одну JSON Schema для make types."""

    server_to_trainee: ServerToTrainee
    trainee_to_server: TraineeToServer
    server_to_observer: ServerToObserver
    instructor_to_server: InstructorToServer
    server_to_station: ServerToStation
    station_to_server: StationToServer
    session_report: SessionReport
