// Сгенерировано `make types` из backend/app/domain/events.py.
// Руками не править: правка уедет при следующей генерации.
// Контракт: docs/arch/CONTRACT.md


/** Аудио-фон происшествия. Файл лежит в сборке фронта, трафика фон не создаёт. */
export interface BgStart {
  type: "bg.start";
  loop: string;
  gain_db: number;
}

export interface BgStop {
  type: "bg.stop";
}

/** Снял гарнитуру. Останавливает норматив `answer`. */
export interface CallAnswer {
  type: "call.answer";
}

export type CallEndReason = "hangup" | "dropped" | "instructor";

export interface CallEnded {
  type: "call.ended";
  reason: CallEndReason;
}

export interface CallHangup {
  type: "call.hangup";
}

/** Обязательность полей приходит сценарием, а не моделью: без `required_fields` АРМ не может подсветить незаполненное обязательное поле, и курсант узнаёт о неполноте карточки только из разбора. */
export interface CallIncoming {
  type: "call.incoming";
  scenario_id: string;
  caller_number: string;
  level: Level;
  mode: SessionMode;
  required_fields?: Array<string>;
}

export interface CallStarted {
  type: "call.started";
  started_at: string;
}

/** Обратный дозвон после обрыва: 3 попытки по 10 с. */
export interface CallbackDial {
  type: "callback.dial";
}

export interface CallerUtterance {
  type: "caller.utterance";
  utterance_id: string;
  text: string;
  at: string;
  mood: Mood;
}

/** Останавливает норматив `dds_ack` (≤ 4 с). */
export interface CardAck {
  type: "card.ack";
}

/** Возврат на уточнение: неполнота КИО становится сорванным выездом. */
export interface CardBounce {
  type: "card.bounce";
  missing_fields: Array<string>;
  comment?: string;
}

/** Снимок КИО. После передачи не меняется — оператор не дописывает задним числом. */
export interface CardReceived {
  type: "card.received";
  card: KIO;
  from_operator: string;
  at: string;
}

/** Шесть осей радара. Проекция уже посчитанных метрик, без пересчёта весов. */
export type Competency = "intake" | "interview" | "card" | "routing" | "norms" | "communication";

export interface CompetencyScore {
  competency: string;
  value: number;
}

/** Координаты одним представлением на весь контракт. Именованные поля, а не кортеж: в `[55.75, 37.61]` невозможно увидеть, где широта, а где долгота, и ошибка всплывёт на карте, а не в типах. */
export interface Coords {
  lat: number;
  lon: number;
}

export interface CrewArrived {
  type: "crew.arrived";
  at: string;
}

export interface CrewDispatched {
  type: "crew.dispatched";
  at: string;
}

/** Дежурно-диспетчерская служба, в которую уходит карточка. */
export type DDSCode = "01" | "02" | "03" | "04" | "gkh";

/** Передача в ДДС. Замораживает карточку снимком и останавливает опрос (`interview`). */
export interface DdsDispatch {
  type: "dds.dispatch";
  service: DDSCode;
}

/** `immediate` — только для обрыва связи: рвёт TTS на полуслове. */
export type DirectiveMode = "next_turn" | "immediate";

/** Директива звонящему. Карточку курсанта не трогает ни одна команда. */
export interface DirectorInject {
  type: "director.inject";
  directive: string;
  mode?: DirectiveMode;
}

/** Автоданные ЭРА-ГЛОНАСС до соединения с водителем. */
export interface EraData {
  type: "era.data";
  vin: string;
  coords: Coords;
  passengers: number;
  impact_force: string;
}

export type ErrorCode = "E1" | "E2" | "E3" | "E4" | "E5" | "E6";

export interface ErrorEvent {
  type: "error";
  code: ErrorKind;
  message: string;
}

/** Коды канала `error`. Фронт разбирает код, а не текст сообщения: текст — для человека, код — для поведения интерфейса. */
export type ErrorKind = "session_not_found" | "call_not_started" | "hint_denied_in_exam" | "models_warming_up" | "directive_needs_network" | "scenario_invalid" | "unsupported_event" | "internal";

/** Отметка в разборе. `fact` и `norm` — то самое обоснование. */
export interface Finding {
  code: ErrorCode;
  source: FindingSource;
  summary: string;
  fact: string;
  norm?: string | null;
  ref?: string | null;
  competency?: Competency | null;
  transcript_ref?: string | null;
  at?: string | null;
}

/** Кто выставил отметку. `judge` — единственный недетерминированный источник. */
export type FindingSource = "slots" | "ground_truth" | "timers" | "kio" | "chain" | "judge" | "instructor";

/** Уточняющие поля 01 — МЧС. */
export interface FireDetails {
  fire_nature?: string | null;
  object_kind?: string | null;
  floors?: number | null;
  gasified?: boolean | null;
  people_inside?: boolean | null;
  smoke_spread?: string | null;
}

/** В режиме `exam` сервер отвечает событием `error`. */
export interface HintRequest {
  type: "hint.request";
}

export interface HintShown {
  type: "hint.shown";
  checklist_id: string;
  question: string;
}

export interface HintUsage {
  checklist_id: string;
  question: string;
  at: string;
}

/** Тип происшествия. Совпадает с полем `type` сценария. */
export type IncidentType = "fire" | "medical" | "police" | "other" | "gkh" | "era_glonass";

export interface InstructorNoteAdd {
  type: "instructor_note.add";
  transcript_ref: string;
  text: string;
}

/** Пометка преподавателя видна всем, кому виден транскрипт. */
export interface InstructorNoteShown {
  type: "instructor_note.shown";
  transcript_ref: string;
  text: string;
  author: string;
}

/** Полная карточка. Наблюдателям уходит целиком (`kio.state`), курсанту — дельтой (`kio.patch`). Присваивание проверяется: без этого `card.dds = "03"` кладёт в карточку сырую строку вместо кода ДДС, и падает уже оценка, далеко от места ошибки. */
export interface KIO {
  card_id?: string;
  registered_at?: string | null;
  response_status?: ResponseStatus;
  caller_number?: string | null;
  caller_name?: string | null;
  caller_contact?: string | null;
  language?: string;
  okato?: string | null;
  address?: string | null;
  street?: string | null;
  building?: string | null;
  entrance?: string | null;
  floor?: string | null;
  intercom_code?: string | null;
  coords?: Coords | null;
  incident_type?: IncidentType | null;
  description?: string | null;
  victims_count?: number | null;
  is_emergency?: boolean;
  life_threat?: boolean;
  evacuation_needed?: boolean;
  dds?: DDSCode | null;
  dispatch_order_at?: string | null;
  arrival_at?: string | null;
  fire?: FireDetails | null;
  police?: PoliceDetails | null;
  medical?: MedicalDetails | null;
  utility?: UtilityDetails | null;
}

/** Правка карточки. Дебаунс 300 мс, шлётся только дельта. */
export interface KioPatchIn {
  type: "kio.patch";
  fields: Record<string, unknown>;
}

/** Сервер распознал факт из речи либо шлёт эхо чужой правки. */
export interface KioPatchOut {
  type: "kio.patch";
  fields: Record<string, unknown>;
  source: PatchSource;
}

/** Полная карточка, не дельта: наблюдателю проще, рассинхрон дороже килобайт. */
export interface KioState {
  type: "kio.state";
  kio: KIO;
}

/** Уровень сложности сценария. */
export type Level = "L1" | "L2" | "L3";

/** Уточняющие поля 03 — медицина. */
export interface MedicalDetails {
  reason?: string | null;
  conscious?: boolean | null;
  breathing?: boolean | null;
  can_move?: boolean | null;
  age?: number | null;
}

/** Метрика оценки: факт против норматива со ссылкой. Не балл, а обоснование. */
export interface Metric {
  key: string;
  title: string;
  fact: string;
  norm: string;
  ref?: string | null;
  passed: boolean;
  weight?: number;
}

export interface ModeSet {
  type: "mode.set";
  mode: SessionMode;
}

/** Состояние звонящего. Фон подачи, не факты. */
export type Mood = "calm" | "worried" | "panic" | "aggressive" | "confused";

/** `auto` — сервер распознал факт из речи, `operator` — эхо правки курсанта. */
export type PatchSource = "auto" | "operator";

/** Уточняющие поля 02 — полиция. */
export interface PoliceDetails {
  offence_kind?: string | null;
  suspects?: string | null;
  suspect_fled?: boolean | null;
  vehicle?: string | null;
}

export interface ReferencePlay {
  type: "reference.play";
}

/** Автопроигрывание эталонного звонка на внешнем мониторе. */
export interface ReferenceStarted {
  type: "reference.started";
  scenario_id: string;
}

export type ResponseStatus = "registered" | "transferred" | "in_progress" | "closed";

export interface ScenarioPublish {
  type: "scenario.publish";
  scenario_id: string;
}

export interface ScenarioStart {
  type: "scenario.start";
  scenario_id: string;
  trainee: string;
  group_id?: string | null;
  mode: SessionMode;
}

/** Коррекция оценки. Автооценка сохраняется рядом. */
export interface ScoreOverride {
  type: "score.override";
  session_id: string;
  verdict: string;
  comment: string;
}

export interface ScoreReady {
  type: "score.ready";
  session_id: string;
}

export interface SelfAssessment {
  missed: Array<string>;
  comment?: string;
  submitted_at: string;
}

/** Расхождение самооценки с автооценкой — отдельный материал для преподавателя. Курсант, не заметивший, что пропустил вопрос о пострадавших, — более важный случай, чем сама ошибка (docs/product/DEBRIEF.md). */
export interface SelfAssessmentDiff {
  noticed?: Array<string>;
  unnoticed?: Array<string>;
  overcautious?: Array<string>;
}

/** Самооценка до показа автооценки. */
export interface SelfAssessmentSubmit {
  type: "self_assessment.submit";
  missed: Array<string>;
  comment?: string;
}

export interface SessionEnded {
  type: "session.ended";
  reason: CallEndReason;
}

/** Режим сессии. Меняет доступность подсказок и протоколирование, но не поведение звонящего (docs/product/MODES.md). */
export type SessionMode = "training" | "exam" | "self";

/** Единица истории: из отчётов складываются профиль, дельта попыток, аналитика. */
export interface SessionReport {
  session_id: string;
  scenario_id: string;
  mode: SessionMode;
  attempt?: number;
  transcript: Array<TranscriptEntry>;
  findings: Array<Finding>;
  metrics: Array<Metric>;
  competencies: Array<CompetencyScore>;
  reference_questions: Array<HintShown>;
  missed_checklist: Array<string>;
  hints_used: Array<HintUsage>;
  self_assessment?: SelfAssessment | null;
  self_assessment_diff?: SelfAssessmentDiff | null;
  notes?: Array<InstructorNoteShown>;
  score_auto: number;
  score_final: number;
  overridden_by?: string | null;
  override_comment?: string | null;
}

/** Обязательно при подключении: монитор в классе включают посреди занятия. */
export interface SessionSnapshot {
  type: "session.snapshot";
  session_id: string;
  scenario_id: string;
  scenario_title: string;
  level: Level;
  mode: SessionMode;
  trainee_name?: string | null;
  started_at?: string | null;
  kio: KIO;
  required_fields?: Array<string>;
  transcript: Array<TranscriptEntry>;
  timers: Array<TimerSnapshot>;
  hints_used?: number;
  ended?: boolean;
}

export interface SessionStop {
  type: "session.stop";
}

export type Speaker = "caller" | "operator";

export interface SttFinal {
  type: "stt.final";
  text: string;
  at: string;
}

export interface SttPartial {
  type: "stt.partial";
  text: string;
}

/** Коды одинаковы на бэке, фронте и в отчёте. */
export type TimerCode = "answer" | "interview" | "dds_notify" | "dds_ack" | "zone_check" | "callback" | "close";

/** Один таймер в событии `timer.tick`. */
export interface TimerSnapshot {
  code: TimerCode;
  elapsed_ms: number;
  limit_ms: number;
  state: TimerState;
  /** Номер попытки для callback */
  attempt?: number;
  /** Остановлен событием, не тикает */
  stopped?: boolean;
}

/** Единственное место, где цвет несёт смысл (docs/arch/FRONTEND.md). */
export type TimerState = "ok" | "warn" | "violated";

/** Раз в секунду, не на каждое изменение: таймеров дюжина, UI рисует секунды. */
export interface TimerTick {
  type: "timer.tick";
  timers: Array<TimerSnapshot>;
}

export interface TranscriptAppend {
  type: "transcript.append";
  entry: TranscriptEntry;
}

/** Реплика в ленте. `ref` — якорь для пометок преподавателя и отметок разбора. */
export interface TranscriptEntry {
  ref: string;
  speaker: Speaker;
  text: string;
  at: string;
  mood?: Mood | null;
}

export interface TtsBegin {
  type: "tts.begin";
  utterance_id: string;
}

/** Оператор перебил. Фронт мгновенно чистит очередь воспроизведения. */
export interface TtsCancel {
  type: "tts.cancel";
  utterance_id: string;
  reason?: "barge_in" | "director";
}

export interface TtsEnd {
  type: "tts.end";
  utterance_id: string;
}

/** Уточняющие поля 04 / ЖКХ. */
export interface UtilityDetails {
  failure_kind?: string | null;
  scale?: string | null;
  threat_to_residents?: boolean | null;
}

export interface ZoneDecision {
  type: "zone.decision";
  in_zone: boolean;
}

// Союзы по каналам — смотри docs/arch/CONTRACT.md#каналы

export type ServerToTrainee = CallIncoming | CallStarted | SttPartial | SttFinal | CallerUtterance | TtsBegin | TtsEnd | TtsCancel | BgStart | BgStop | EraData | KioPatchOut | HintShown | TimerTick | CallEnded | ScoreReady | ErrorEvent;

export type TraineeToServer = CallAnswer | KioPatchIn | HintRequest | SelfAssessmentSubmit | DdsDispatch | CallHangup | CallbackDial;

export type ServerToObserver = SessionSnapshot | TranscriptAppend | KioState | TimerTick | ModeSet | HintShown | BgStart | BgStop | CallerUtterance | SessionEnded | ScoreReady | InstructorNoteShown | ReferenceStarted | ErrorEvent;

export type InstructorToServer = ScenarioStart | DirectorInject | ReferencePlay | InstructorNoteAdd | ScoreOverride | ScenarioPublish | SessionStop;

export type ServerToStation = CardReceived | TimerTick | SessionEnded | ErrorEvent;

export type StationToServer = CardAck | CardBounce | ZoneDecision | CrewDispatched | CrewArrived;

export type EventName =
  "bg.start"
  | "bg.stop"
  | "call.answer"
  | "call.ended"
  | "call.hangup"
  | "call.incoming"
  | "call.started"
  | "callback.dial"
  | "caller.utterance"
  | "card.ack"
  | "card.bounce"
  | "card.received"
  | "crew.arrived"
  | "crew.dispatched"
  | "dds.dispatch"
  | "director.inject"
  | "era.data"
  | "error"
  | "hint.request"
  | "hint.shown"
  | "instructor_note.add"
  | "instructor_note.shown"
  | "kio.patch"
  | "kio.state"
  | "mode.set"
  | "reference.play"
  | "reference.started"
  | "scenario.publish"
  | "scenario.start"
  | "score.override"
  | "score.ready"
  | "self_assessment.submit"
  | "session.ended"
  | "session.snapshot"
  | "session.stop"
  | "stt.final"
  | "stt.partial"
  | "timer.tick"
  | "transcript.append"
  | "tts.begin"
  | "tts.cancel"
  | "tts.end"
  | "zone.decision";
