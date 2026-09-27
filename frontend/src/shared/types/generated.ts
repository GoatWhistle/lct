// Сгенерировано `make types` из backend/app/domain/events.py.
// Руками не править: правка уедет при следующей генерации.
// Контракт: docs/arch/CONTRACT.md


export interface AICoaching {
  status: "ready" | "unavailable" | "disabled" | "not_needed";
  model?: string | null;
  recommendations?: Array<AIRecommendation>;
}

export interface AIRecommendation {
  metric_key: string;
  text: string;
}

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

export type CallEndReason = "hangup" | "dropped" | "instructor" | "complete";

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

/** Курсант решил, что карточка здесь не заводится. Не «завершить звонок», а именно «этот вызов закрывается иначе»: справкой или передачей в другой регион. Решение оценивается наравне с выбором признаков — это та же классификация, только на шаг раньше. */
export interface CallResolve {
  type: "call.resolve";
  outcome: Outcome;
  comment?: string;
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
  source?: "local_llm" | "scenario";
}

/** Останавливает норматив `dds_ack` (≤ 30 с). */
export interface CardAck {
  type: "card.ack";
  comment: string;
}

/** Возврат на уточнение: неполнота КИО становится сорванным выездом. */
export interface CardBounce {
  type: "card.bounce";
  missing_fields: Array<string>;
  comment?: string;
}

/** Текстовая вводная упражнения 112; эталонные значения сюда не входят. */
export interface CardBriefing {
  type: "card.briefing";
  scenario_id: string;
  mode: SessionMode;
  text: string;
  required_fields: Array<string>;
  card?: KIO | null;
  handoff_to_dds?: boolean;
}

export interface CardNext {
  type: "card.next";
  card_id: string;
}

/** Открыть одну из уже выданных карточек, не меняя её таймер. */
export interface CardOpen {
  type: "card.open";
  card_id: string;
}

/** Снимок КИО. После передачи не меняется — оператор не дописывает задним числом. */
export interface CardReceived {
  type: "card.received";
  card: KIO;
  from_operator: string;
  at: string;
  card_index?: number;
  card_total?: number;
}

export interface CardReply {
  type: "card.reply";
  card_id: string;
  text: string;
}

/** Статус карточки целиком — считается из статусов служб, руками не ставится. */
export type CardStatus = "registered" | "processed" | "checked" | "not_notified" | "refused" | "not_completed" | "completed";

export interface CardSubmit {
  type: "card.submit";
}

/** Durable confirmation for a station command, safe to replay by ID. */
export interface CommandAck {
  type: "command.ack";
  command_id: string;
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

export interface CrewSelect {
  type: "crew.select";
  crew: string;
}

/** Дежурно-диспетчерская служба, в которую уходит карточка. */
export type DDSCode = "01" | "02" | "03" | "04" | "gkh";

export interface DdsCardReport {
  card_id: string;
  scenario_id: string;
  score_auto: number;
  score_reviewed?: number | null;
  reply_text: string;
  metrics: Array<Metric>;
  findings: Array<Finding>;
  actions?: Array<Record<string, unknown>>;
  duration_ms?: number;
  title?: string | null;
  address?: string | null;
  description?: string | null;
  incident_type?: string | null;
  victims_count?: number | null;
  received_at?: string | null;
  managed_service?: string | null;
  recipient_services?: Array<string>;
}

export interface DdsCardSummary {
  card_id: string;
  scenario_id: string;
  score_auto: number;
}

/** Передача в ДДС. При маршрутизации по ЕКП служба уже есть в notify. */
export interface DdsDispatch {
  type: "dds.dispatch";
  service?: DDSCode | null;
}

/** Строка общей очереди ДДС; таймер каждой карточки идёт независимо. */
export interface DdsQueueCard {
  card_id: string;
  scenario_id: string;
  title: string;
  address?: string | null;
  description?: string | null;
  incident_type?: string | null;
  victims_count?: number | null;
  received_at: string;
  managed_service?: string | null;
  service_status: ServiceStatus;
  card_status: CardStatus;
  elapsed_ms: number;
  limit_ms: number;
  timer_stopped: boolean;
  active: boolean;
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

/** E — ошибки приёма вызова, D — ошибки диспетчера ДДС. Две роли, две таксономии. D1–D6 взяты из памятки заказчика дословно: она перечисляет типовые нарушения с реальными примерами, и преподаватель узнаёт их формулировки (docs/spec/DATASET.md). */
export type ErrorCode = "E1" | "E2" | "E3" | "E4" | "E5" | "E6" | "D1" | "D2" | "D3" | "D4" | "D5" | "D6";

export interface ErrorEvent {
  type: "error";
  code: ErrorKind;
  message: string;
}

/** Коды канала `error`. Фронт разбирает код, а не текст сообщения: текст — для человека, код — для поведения интерфейса. */
export type ErrorKind = "session_not_found" | "call_not_started" | "hint_denied_in_exam" | "models_warming_up" | "directive_needs_network" | "scenario_invalid" | "unsupported_event" | "forbidden" | "internal";

export type Exercise = "call" | "dds" | "card";

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
  metric_key?: string | null;
  card?: number | null;
  service?: string | null;
  author?: string | null;
  client_id?: string | null;
  reviews?: Array<FindingReview>;
}

/** Решение преподавателя по отметке на разборе. */
export type FindingDecision = "confirmed" | "dismissed";

/** Кто, когда и почему подтвердил или снял отметку. Сама отметка остаётся. */
export interface FindingReview {
  decision: FindingDecision;
  reason: string;
  author: string;
  at: string;
}

/** Источник объяснимой отметки; `judge` оставлен для старых отчётов. */
export type FindingSource = "slots" | "dispatcher" | "ground_truth" | "timers" | "kio" | "chain" | "grammar" | "judge" | "instructor";

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
  phone_on_scene?: string | null;
  language?: string;
  okato?: string | null;
  address?: string | null;
  street?: string | null;
  building?: string | null;
  entrance?: string | null;
  floor?: string | null;
  intercom_code?: string | null;
  coords?: Coords | null;
  incident_group?: number | null;
  signs?: Array<string>;
  /** Код ЕКП по признакам */
  incident_code?: string | null;
  incident_type?: IncidentType | null;
  description?: string | null;
  victims_count?: number | null;
  is_emergency?: boolean;
  life_threat?: boolean;
  evacuation_needed?: boolean;
  /** Список оповещения */
  notify?: Array<string>;
  /** Дополнительно оповестить */
  notify_extra?: Array<string>;
  /** Устаревшее: одна служба. Заменено списком оповещения */
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

/** Настраиваемые преподавателем условия именно этого занятия. Нормативы ГОСТ для приёма вызова сюда не входят. Для занятия меняются учебные лимиты первичной реакции и полного цикла карточки ДДС, заполнения КИО, порог успешности и веса метрик; веса сценария остаются базовыми, настройки занятия могут их переопределить. */
export interface LessonCriteria {
  decision_time_limit_seconds?: number;
  card_fill_time_limit_seconds?: number;
  dds_card_work_time_limit_seconds?: number;
  dds_report_reaction_limit_seconds?: number;
  allowed_errors?: number;
  require_correct_grammar?: boolean;
  score_weights?: Record<string, number>;
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

/** Факт против норматива со ссылкой; `credit` задаёт частичный вклад времени. */
export interface Metric {
  key: string;
  title: string;
  fact: string;
  norm: string;
  ref?: string | null;
  passed: boolean;
  weight?: number;
  credit?: number | null;
  card?: number | null;
  service?: string | null;
  passed_auto?: boolean | null;
  credit_auto?: number | null;
}

export interface ModeSet {
  type: "mode.set";
  mode: SessionMode;
}

/** Состояние звонящего. Фон подачи, не факты. */
export type Mood = "calm" | "worried" | "panic" | "aggressive" | "confused";

/** Чем заканчивается вызов. Весь продукт по умолчанию считает, что звонок → карточка → выезд. В билетах заказчика это не всегда так: «поругался с продавцом „Мегафон“» — справка, а вызов из Волгоградской области передаётся в систему-112 другого субъекта (docs/spec/TICKETS.md). Курсант, заведший карточку на такой вызов, ошибся, и система обязана это увидеть. */
export type Outcome = "card" | "consultation" | "transfer_region";

/** `auto` — сервер распознал факт из речи, `operator` — эхо правки курсанта. */
export type PatchSource = "auto" | "operator";

/** Что диспетчер передал старшему группы при первом исходящем звонке. */
export interface PhoneBrief {
  type: "phone.brief";
  address: string;
  incident: string;
  request?: string;
}

export interface PhoneCallPending {
  service: string;
  crew: string;
  phase: string;
}

/** Запрос диспетчера об обстановке после предыдущего доклада. */
export interface PhoneCheck {
  type: "phone.check";
  text: string;
}

export interface PhoneDial {
  type: "phone.dial";
}

export interface PhoneHangup {
  type: "phone.hangup";
}

export interface PhoneLine {
  type: "phone.line";
  service: string;
  crew: string;
  speaker: "dispatcher" | "crew";
  text: string;
  at: string;
}

export interface PhoneLineRecord {
  service: string;
  crew: string;
  speaker: string;
  text: string;
  at: string;
}

export interface PhoneReport {
  type: "phone.report";
  service: string;
  crew: string;
  phase: "dispatched" | "arrived" | "working" | "completed";
  text: string;
  at: string;
}

export interface PhoneReportRecord {
  service: string;
  crew: string;
  phase: string;
  text: string;
  at: string;
}

/** Уточняющие поля 02 — полиция. */
export interface PoliceDetails {
  offence_kind?: string | null;
  suspects?: string | null;
  suspect_fled?: boolean | null;
  vehicle?: string | null;
}

/** Кто вошёл. В событиях не участвует, но фронт разводит по роли экраны, и список ролей должен приезжать из домена, а не переписываться руками (lct-23). Полная модель — в `app/api/auth.py`. */
export interface Principal {
  login: string;
  full_name: string;
  role: Role;
  service?: string | null;
  trainee_id?: string | null;
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

export type Role = "admin" | "instructor" | "trainee";

export interface ScenarioStart {
  type: "scenario.start";
  scenario_id: string;
  scenario_ids?: Array<string> | null;
  random_scenario_ids?: Array<string> | null;
  dds_arrival_interval_seconds?: number;
  dds_max_waiting?: number;
  trainee: string;
  trainee_id?: string | null;
  dds_service?: string | null;
  group_id?: string | null;
  mode: SessionMode;
  exercise?: Exercise;
  handoff_to_dds?: boolean;
  criteria?: LessonCriteria;
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

/** Статус реагирования одной службы из списка оповещения. */
export type ServiceStatus = "added" | "received" | "accepted" | "declined" | "responding" | "arrived" | "working" | "completed" | "refused";

/** Диспетчер ставит статус реагирования своей службе. Последовательность жёсткая, комментарий к отказу обязателен — это не валидация формы, а то, чему учит второй режим занятия (docs/spec/DATASET.md#статусы-реагирования). */
export interface ServiceStatusSet {
  type: "card.status";
  service: string;
  status: ServiceStatus;
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
  exercise?: Exercise;
  attempt?: number;
  criteria: LessonCriteria;
  failed_metrics: number;
  passed: boolean;
  transcript: Array<TranscriptEntry>;
  findings: Array<Finding>;
  metrics: Array<Metric>;
  ai_coaching?: AICoaching | null;
  card_results?: Array<DdsCardReport>;
  competencies: Array<CompetencyScore>;
  reference_questions: Array<HintShown>;
  missed_checklist: Array<string>;
  hints_used: Array<HintUsage>;
  self_assessment?: SelfAssessment | null;
  self_assessment_diff?: SelfAssessmentDiff | null;
  notes?: Array<InstructorNoteShown>;
  score_auto: number;
  score_reviewed?: number | null;
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
  exercise?: Exercise;
  criteria?: LessonCriteria;
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

export interface StationFinish {
  type: "station.finish";
}

/** Что видит диспетчер: службы, их статусы и что доступно дальше. Все поля обязательные, хотя списки и могут быть пустыми: снимок всегда собирается целиком, а необязательное поле уезжает в TypeScript как `| undefined` и заставляет фронт проверять то, чего не бывает. */
export interface StationSnapshot {
  services: Array<string>;
  recipient_services?: Array<string>;
  managed_service?: string | null;
  statuses: Record<string, ServiceStatus>;
  available: Record<string, Array<ServiceStatus>>;
  card: CardStatus;
  log: Array<StatusEntry>;
  crew_options?: Array<string>;
  crew_selected?: string | null;
  zone_decision?: boolean | null;
  phone_reports?: Array<PhoneReportRecord>;
  phone_lines?: Array<PhoneLineRecord>;
  phone_pending?: PhoneCallPending | null;
  card_id?: string | null;
  card_index?: number;
  card_total?: number;
  reply_text?: string;
  completed_cards?: Array<DdsCardSummary>;
  queue_cards?: Array<DdsQueueCard>;
  pending_cards_count?: number;
  next_arrival_in_seconds?: number | null;
  max_waiting_cards?: number;
}

/** Состояние АРМ ДДС после каждой отметки: что стоит и что доступно дальше. */
export interface StationState {
  type: "station.state";
  snapshot: StationSnapshot;
}

/** Одна отметка службы. Комментарий — часть отметки, а не примечание к ней. */
export interface StatusEntry {
  service: string;
  status: ServiceStatus;
  at: string;
  comment?: string;
  author?: string;
}

export interface SttFinal {
  type: "stt.final";
  text: string;
  at: string;
}

export interface SttPartial {
  type: "stt.partial";
  text: string;
}

export interface TextTurn {
  type: "text.turn";
  text: string;
}

export interface TextTurnAccepted {
  type: "text.turn.accepted";
  text: string;
  at: string;
}

/** Коды одинаковы на бэке, фронте и в отчёте. */
export type TimerCode = "answer" | "interview" | "dds_notify" | "dds_ack" | "dds_work" | "card_fill" | "zone_check" | "callback" | "close";

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

export type ServerToTrainee = CallIncoming | CardBriefing | TextTurnAccepted | CallStarted | SttPartial | SttFinal | CallerUtterance | TtsBegin | TtsEnd | TtsCancel | BgStart | BgStop | EraData | KioPatchOut | HintShown | TimerTick | CallEnded | ScoreReady | ErrorEvent;

export type TraineeToServer = CallAnswer | CardSubmit | TextTurn | KioPatchIn | HintRequest | SelfAssessmentSubmit | DdsDispatch | CallResolve | CallHangup | CallbackDial;

export type ServerToObserver = SessionSnapshot | TranscriptAppend | KioState | TimerTick | ModeSet | HintShown | BgStart | BgStop | CallerUtterance | SessionEnded | ScoreReady | InstructorNoteShown | ReferenceStarted | ErrorEvent;

export type InstructorToServer = ScenarioStart | DirectorInject | ReferencePlay | InstructorNoteAdd | ScoreOverride | SessionStop;

export type ServerToStation = CardReceived | StationState | PhoneLine | PhoneReport | TimerTick | SessionEnded | ScoreReady | CommandAck | ErrorEvent;

export type StationToServer = CardAck | CardBounce | ServiceStatusSet | CrewSelect | PhoneDial | PhoneBrief | PhoneCheck | PhoneHangup | StationFinish | CardReply | CardNext | CardOpen | ZoneDecision | CrewDispatched | CrewArrived;

export type EventName =
  "bg.start"
  | "bg.stop"
  | "call.answer"
  | "call.ended"
  | "call.hangup"
  | "call.incoming"
  | "call.resolve"
  | "call.started"
  | "callback.dial"
  | "caller.utterance"
  | "card.ack"
  | "card.bounce"
  | "card.briefing"
  | "card.next"
  | "card.open"
  | "card.received"
  | "card.reply"
  | "card.status"
  | "card.submit"
  | "command.ack"
  | "crew.arrived"
  | "crew.dispatched"
  | "crew.select"
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
  | "phone.brief"
  | "phone.check"
  | "phone.dial"
  | "phone.hangup"
  | "phone.line"
  | "phone.report"
  | "reference.play"
  | "reference.started"
  | "scenario.start"
  | "score.override"
  | "score.ready"
  | "self_assessment.submit"
  | "session.ended"
  | "session.snapshot"
  | "session.stop"
  | "station.finish"
  | "station.state"
  | "stt.final"
  | "stt.partial"
  | "text.turn"
  | "text.turn.accepted"
  | "timer.tick"
  | "transcript.append"
  | "tts.begin"
  | "tts.cancel"
  | "tts.end"
  | "zone.decision";
