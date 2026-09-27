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

from pydantic import BaseModel, Field, field_validator

from app.domain.classifiers import DDSCode, Level, Outcome
from app.domain.kio import KIO, Coords
from app.domain.roles import Role
from app.domain.statuses import ServiceStatus, StationSnapshot
from app.domain.taxonomy import Finding
from app.domain.timers import TimerSnapshot
from app.scoring.taxonomy import INSTRUCTOR_METRIC, METRIC_MAP


class SessionMode(StrEnum):
    """Режим сессии. Меняет доступность подсказок и протоколирование,
    но не поведение звонящего (docs/product/MODES.md)."""

    TRAINING = "training"
    EXAM = "exam"
    SELF = "self"


class Exercise(StrEnum):
    CALL = "call"
    DDS = "dds"
    CARD = "card"


class LessonCriteria(BaseModel):
    """Настраиваемые преподавателем условия именно этого занятия.

    Нормативы ГОСТ для приёма вызова сюда не входят. Для занятия меняются
    учебные лимиты первичной реакции и полного цикла карточки ДДС, заполнения
    КИО, порог успешности и веса метрик; веса сценария остаются базовыми,
    настройки занятия могут их переопределить.
    """

    decision_time_limit_seconds: int = Field(default=30, ge=5, le=300)
    card_fill_time_limit_seconds: int = Field(default=180, ge=30, le=1800)
    dds_card_work_time_limit_seconds: int = Field(default=180, ge=30, le=1800)
    #: Сколько секунд после доклада бригады даётся на соответствующий статус.
    dds_report_reaction_limit_seconds: int = Field(default=45, ge=5, le=600)
    allowed_errors: int = Field(default=0, ge=0, le=50)
    require_correct_grammar: bool = True
    score_weights: dict[str, float] = Field(default_factory=dict)

    @field_validator("score_weights")
    @classmethod
    def validate_score_weights(cls, weights: dict[str, float]) -> dict[str, float]:
        unknown = weights.keys() - METRIC_MAP.keys() - {INSTRUCTOR_METRIC}
        if unknown:
            raise ValueError(f"неизвестные метрики весов: {', '.join(sorted(unknown))}")
        if any(not 0 <= weight <= 10 for weight in weights.values()):
            raise ValueError("каждый вес метрики должен быть от 0 до 10")
        return weights


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
    COMPLETE = "complete"


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
    #: Канал открыт не тем, кому он предназначен (lct-23). Фронт по этому коду
    #: отправляет на вход, а не показывает «занятие не найдено».
    FORBIDDEN = "forbidden"
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


class CardBriefing(BaseModel):
    """Текстовая вводная упражнения 112; эталонные значения сюда не входят."""

    type: Literal["card.briefing"] = "card.briefing"
    scenario_id: str
    mode: SessionMode
    text: str
    required_fields: list[str]
    card: KIO | None = None
    handoff_to_dds: bool = False


class TextTurnAccepted(BaseModel):
    type: Literal["text.turn.accepted"] = "text.turn.accepted"
    text: str
    at: datetime


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
    source: Literal["local_llm", "scenario"] = "scenario"


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


class CommandAck(BaseModel):
    """Durable confirmation for a station command, safe to replay by ID."""

    type: Literal["command.ack"] = "command.ack"
    command_id: UUID


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: ErrorKind
    message: str


ServerToTrainee = Annotated[
    CallIncoming
    | CardBriefing
    | TextTurnAccepted
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


class CardSubmit(BaseModel):
    type: Literal["card.submit"] = "card.submit"


class TextTurn(BaseModel):
    type: Literal["text.turn"] = "text.turn"
    text: str = Field(min_length=1, max_length=1000)


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
    """Передача в ДДС. При маршрутизации по ЕКП служба уже есть в notify."""

    type: Literal["dds.dispatch"] = "dds.dispatch"
    service: DDSCode | None = None


class CallHangup(BaseModel):
    type: Literal["call.hangup"] = "call.hangup"


class CallbackDial(BaseModel):
    """Обратный дозвон после обрыва: 3 попытки по 10 с."""

    type: Literal["callback.dial"] = "callback.dial"


class CallResolve(BaseModel):
    """Курсант решил, что карточка здесь не заводится.

    Не «завершить звонок», а именно «этот вызов закрывается иначе»: справкой
    или передачей в другой регион. Решение оценивается наравне с выбором
    признаков — это та же классификация, только на шаг раньше.
    """

    type: Literal["call.resolve"] = "call.resolve"
    outcome: Outcome
    comment: str = ""


TraineeToServer = Annotated[
    CallAnswer
    | CardSubmit
    | TextTurn
    | KioPatchIn
    | HintRequest
    | SelfAssessmentSubmit
    | DdsDispatch
    | CallResolve
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
    exercise: Exercise = Exercise.CALL
    criteria: LessonCriteria = Field(default_factory=LessonCriteria)
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
    scenario_ids: list[str] | None = None
    random_scenario_ids: list[str] | None = None
    # Pace defaults to simultaneous for older clients; the instructor UI
    # explicitly sends its slower training default.
    dds_arrival_interval_seconds: int = Field(default=0, ge=0, le=300)
    dds_max_waiting: int = Field(default=3, ge=1, le=10)
    trainee: str
    trainee_id: UUID | None = None
    # Служба обучающегося определяет, чьи статусы он ведёт. В рабочем режиме
    # сервер заменяет это значение данными учётной записи; поле нужно demo без БД.
    dds_service: str | None = None
    group_id: str | None = None
    mode: SessionMode
    exercise: Exercise = Exercise.CALL
    handoff_to_dds: bool = False
    criteria: LessonCriteria = Field(default_factory=LessonCriteria)


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


class SessionStop(BaseModel):
    type: Literal["session.stop"] = "session.stop"


InstructorToServer = Annotated[
    ScenarioStart
    | DirectorInject
    | ReferencePlay
    | InstructorNoteAdd
    | ScoreOverride
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
    card_index: int = 1
    card_total: int = 1


class CardAck(BaseModel):
    """Останавливает норматив `dds_ack` (≤ 30 с)."""

    type: Literal["card.ack"] = "card.ack"
    comment: str = Field(min_length=1, max_length=1000)


class CardBounce(BaseModel):
    """Возврат на уточнение: неполнота КИО становится сорванным выездом."""

    type: Literal["card.bounce"] = "card.bounce"
    missing_fields: list[str]
    comment: str = ""


class ServiceStatusSet(BaseModel):
    """Диспетчер ставит статус реагирования своей службе.

    Последовательность жёсткая, комментарий к отказу обязателен — это не
    валидация формы, а то, чему учит второй режим занятия
    (docs/spec/DATASET.md#статусы-реагирования).
    """

    type: Literal["card.status"] = "card.status"
    service: str
    status: ServiceStatus
    comment: str = ""


class CrewSelect(BaseModel):
    type: Literal["crew.select"] = "crew.select"
    crew: str


class PhoneDial(BaseModel):
    type: Literal["phone.dial"] = "phone.dial"


class PhoneBrief(BaseModel):
    """Что диспетчер передал старшему группы при первом исходящем звонке."""

    type: Literal["phone.brief"] = "phone.brief"
    address: str = Field(min_length=3, max_length=300)
    incident: str = Field(min_length=3, max_length=500)
    request: str = Field(default="", max_length=500)


class PhoneCheck(BaseModel):
    """Запрос диспетчера об обстановке после предыдущего доклада."""

    type: Literal["phone.check"] = "phone.check"
    text: str = Field(min_length=8, max_length=500)


class PhoneHangup(BaseModel):
    type: Literal["phone.hangup"] = "phone.hangup"


class PhoneLine(BaseModel):
    type: Literal["phone.line"] = "phone.line"
    service: str
    crew: str
    speaker: Literal["dispatcher", "crew"]
    text: str
    at: datetime


class PhoneReport(BaseModel):
    type: Literal["phone.report"] = "phone.report"
    service: str
    crew: str
    phase: Literal["dispatched", "arrived", "working", "completed"]
    text: str
    at: datetime


class StationFinish(BaseModel):
    type: Literal["station.finish"] = "station.finish"


class CardReply(BaseModel):
    type: Literal["card.reply"] = "card.reply"
    card_id: UUID
    text: str = Field(max_length=2000)


class CardNext(BaseModel):
    type: Literal["card.next"] = "card.next"
    card_id: UUID


class CardOpen(BaseModel):
    """Открыть одну из уже выданных карточек, не меняя её таймер."""

    type: Literal["card.open"] = "card.open"
    card_id: UUID


class StationState(BaseModel):
    """Состояние АРМ ДДС после каждой отметки: что стоит и что доступно дальше."""

    type: Literal["station.state"] = "station.state"
    snapshot: StationSnapshot


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
    CardReceived | StationState | PhoneLine | PhoneReport | TimerTick | SessionEnded
    | ScoreReady | CommandAck | ErrorEvent,
    Field(discriminator="type"),
]

StationToServer = Annotated[
    CardAck | CardBounce | ServiceStatusSet | CrewSelect | PhoneDial | PhoneBrief | PhoneCheck
    | PhoneHangup | StationFinish
    | CardReply | CardNext | CardOpen
    | ZoneDecision | CrewDispatched | CrewArrived,
    Field(discriminator="type"),
]


# ─────────────────────────── отчёт по HTTP ───────────────────────────


class Metric(BaseModel):
    """Факт против норматива со ссылкой; `credit` задаёт частичный вклад времени."""

    key: str
    title: str
    fact: str
    norm: str
    ref: str | None = None
    passed: bool
    weight: float = 1.0
    credit: float | None = Field(default=None, ge=0, le=1)
    #: Номер карточки очереди ДДС (с 1) и служба — область метрики для разбора.
    card: int | None = None
    service: str | None = None
    #: Автоматический вердикт, если его изменило решение преподавателя по отметке.
    passed_auto: bool | None = None
    credit_auto: float | None = Field(default=None, ge=0, le=1)


class CompetencyScore(BaseModel):
    competency: str
    value: float


class AIRecommendation(BaseModel):
    metric_key: str
    text: str = Field(min_length=12, max_length=240)


class AICoaching(BaseModel):
    status: Literal["ready", "unavailable", "disabled", "not_needed"]
    model: str | None = None
    recommendations: list[AIRecommendation] = []


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


class DdsCardReport(BaseModel):
    card_id: UUID
    scenario_id: str
    score_auto: float
    #: Балл карточки после решений преподавателя по отметкам; None — решений не было.
    score_reviewed: float | None = None
    reply_text: str
    metrics: list[Metric]
    findings: list[Finding]
    actions: list[dict[str, Any]] = []
    duration_ms: int = 0
    title: str | None = None
    address: str | None = None
    description: str | None = None
    incident_type: str | None = None
    victims_count: int | None = None
    received_at: datetime | None = None
    managed_service: str | None = None
    recipient_services: list[str] = []


class SessionReport(BaseModel):
    """Единица истории: из отчётов складываются профиль, дельта попыток, аналитика."""

    session_id: UUID
    scenario_id: str
    mode: SessionMode
    exercise: Exercise = Exercise.CALL
    attempt: int = 1
    criteria: LessonCriteria
    failed_metrics: int
    passed: bool
    transcript: list[TranscriptEntry]
    findings: list[Finding]
    metrics: list[Metric]
    ai_coaching: AICoaching | None = None
    card_results: list[DdsCardReport] = []
    competencies: list[CompetencyScore]
    reference_questions: list[HintShown]
    missed_checklist: list[str]
    hints_used: list[HintUsage]
    self_assessment: SelfAssessment | None = None
    self_assessment_diff: SelfAssessmentDiff | None = None
    notes: list[InstructorNoteShown] = []
    score_auto: float
    #: Балл после решений преподавателя по отметкам; None — решений не было.
    score_reviewed: float | None = None
    score_final: float
    overridden_by: str | None = None
    override_comment: str | None = None


class Principal(BaseModel):
    """Кто вошёл. В событиях не участвует, но фронт разводит по роли экраны,
    и список ролей должен приезжать из домена, а не переписываться руками
    (lct-23). Полная модель — в `app/api/auth.py`.
    """

    login: str
    full_name: str
    role: Role
    service: str | None = None
    trainee_id: UUID | None = None


class EventCatalog(BaseModel):
    """Единственное назначение — собрать все союзы в одну JSON Schema для make types."""

    principal: Principal

    server_to_trainee: ServerToTrainee
    trainee_to_server: TraineeToServer
    server_to_observer: ServerToObserver
    instructor_to_server: InstructorToServer
    server_to_station: ServerToStation
    station_to_server: StationToServer
    session_report: SessionReport
