// Типизированный сокет с переподключением.
//
// Три канала — три типа. Направление зашито в тип: у наблюдателя нельзя вызвать
// send, у преподавателя нет входящих. Разделение прав архитектурное, а не
// дисциплинарное (docs/arch/CONTRACT.md#каналы) — и на фронте тоже.

import type {
  InstructorToServer,
  ServerToObserver,
  ServerToStation,
  ServerToTrainee,
  StationToServer,
  TraineeToServer,
} from "@/shared/types/generated";

export type ChannelStatus = "connecting" | "open" | "reconnecting" | "closed";

interface ChannelOptions<In, Out> {
  onEvent?: (event: In) => void;
  /** Бинарный кадр: звук звонящего, без обёртки JSON. */
  onBinary?: (frame: ArrayBuffer) => void;
  onStatus?: (status: ChannelStatus) => void;
  /** Ключ безопасного идемпотентного события, которое можно повторить после
   *  краткого разрыва. null запрещает буферизацию команды. */
  queueKey?: (event: Out) => string | null;
}

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 5_000;
const BUFFER_MAX_AGE_MS = 35_000;
const BUFFER_MAX_ITEMS = 100;

interface Buffered<Out> { key: string; event: Out; at: number }

export class Channel<In extends { type: string }, Out extends { type: string }> {
  private socket: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closedByUs = false;
  private buffered: Buffered<Out>[] = [];

  constructor(
    private readonly path: string,
    private readonly options: ChannelOptions<In, Out> = {},
  ) {}

  connect(): this {
    this.closedByUs = false;
    this.open("connecting");
    return this;
  }

  private open(status: ChannelStatus): void {
    this.options.onStatus?.(status);
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}${this.path}`);
    socket.binaryType = "arraybuffer";

    socket.onopen = () => {
      this.attempt = 0;
      this.options.onStatus?.("open");
      const now = Date.now();
      const pending = this.buffered.filter((item) => now - item.at <= BUFFER_MAX_AGE_MS);
      this.buffered = [];
      for (const item of pending) socket.send(JSON.stringify(item.event));
    };
    socket.onmessage = (message) => {
      if (typeof message.data !== "string") {
        this.options.onBinary?.(message.data as ArrayBuffer);
        return;
      }
      this.options.onEvent?.(JSON.parse(message.data) as In);
    };
    socket.onclose = () => {
      this.socket = null;
      if (this.closedByUs) {
        this.options.onStatus?.("closed");
        return;
      }
      // Экспоненциальная пауза: при падении сервера вкладки всего класса
      // не должны долбить его переподключениями каждую миллисекунду.
      const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * 2 ** this.attempt++);
      this.options.onStatus?.("reconnecting");
      this.timer = setTimeout(() => this.open("reconnecting"), delay);
    };
    this.socket = socket;
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  send(event: Out): boolean {
    if (this.isOpen) {
      this.socket!.send(JSON.stringify(event));
      return true;
    }
    const key = this.options.queueKey?.(event) ?? null;
    if (!key) return false;
    const existing = this.buffered.find((item) => item.key === key);
    if (existing) {
      // Частые kio.patch объединяем: последнее значение каждого поля важнее,
      // но правки разных полей не должны затереть друг друга.
      if (existing.event.type === "kio.patch" && event.type === "kio.patch") {
        const previousPatch = existing.event as unknown as { fields: Record<string, unknown> };
        const nextPatch = event as unknown as { fields: Record<string, unknown> };
        existing.event = {
          ...existing.event,
          fields: { ...previousPatch.fields, ...nextPatch.fields },
        } as Out;
      } else {
        existing.event = event;
      }
      existing.at = Date.now();
    } else {
      this.buffered.push({ key, event, at: Date.now() });
      if (this.buffered.length > BUFFER_MAX_ITEMS) this.buffered.shift();
    }
    return true;
  }

  sendBinary(frame: ArrayBuffer): boolean {
    if (!this.isOpen) return false;
    this.socket!.send(frame);
    return true;
  }

  close(): void {
    this.closedByUs = true;
    if (this.timer) clearTimeout(this.timer);
    this.socket?.close();
    this.buffered = [];
  }
}

export const callChannel = (
  sessionId: string,
  options?: ChannelOptions<ServerToTrainee, TraineeToServer>,
) => new Channel<ServerToTrainee, TraineeToServer>(`/ws/call/${sessionId}`, {
  ...options,
  queueKey: (event) => event.type === "kio.patch" ? "kio.patch" : null,
});

/** Наблюдатель: отправлять нечего и нельзя — `Out` равен `never`. */
export const observeChannel = (sessionId: string, options?: ChannelOptions<ServerToObserver, never>) =>
  new Channel<ServerToObserver, never>(`/ws/observe/${sessionId}`, options);

/** Станция ДДС: только JSON, аудио здесь нет вообще. */
export const stationChannel = (
  sessionId: string,
  role: string,
  options?: ChannelOptions<ServerToStation, StationToServer>,
) => new Channel<ServerToStation, StationToServer>(`/ws/station/${sessionId}?role=${role}`, {
  ...options,
  queueKey: (event) => {
    if (event.type === "card.reply") return `card.reply:${event.card_id}`;
    if (event.type === "crew.select") return "crew.select";
    if (event.type === "zone.decision") return "zone.decision";
    return null;
  },
});

/** Преподаватель: только передача, входящих в этом канале нет. */
export const controlChannel = (sessionId: string, options?: { onStatus?: (status: ChannelStatus) => void }) =>
  new Channel<never, InstructorToServer>(`/ws/control/${sessionId}`, options);
