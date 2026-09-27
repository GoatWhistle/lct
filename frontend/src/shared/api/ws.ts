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
  /** Подтверждение, что серверный durable snapshot уже содержит команду. */
  isAcknowledged?: (incoming: In, pending: Out) => boolean;
}

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 5_000;
const BUFFER_MAX_AGE_MS = 35_000;
const BUFFER_MAX_ITEMS = 100;

interface Buffered<Out> { key: string; commandId: string; event: Out; at: number }

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
      this.buffered = pending;
      for (const item of pending) socket.send(this.encode(item));
    };
    socket.onmessage = (message) => {
      if (typeof message.data !== "string") {
        this.options.onBinary?.(message.data as ArrayBuffer);
        return;
      }
      const event = JSON.parse(message.data) as In;
      if (event.type === "command.ack" && typeof (event as { command_id?: unknown }).command_id === "string") {
        const commandId = (event as unknown as { command_id: string }).command_id;
        this.buffered = this.buffered.filter((item) => item.commandId !== commandId);
      }
      if (this.options.isAcknowledged) {
        this.buffered = this.buffered.filter(
          (item) => !this.options.isAcknowledged!(event, item.event),
        );
      }
      this.options.onEvent?.(event);
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
    const key = this.options.queueKey?.(event) ?? null;
    if (!key) {
      if (!this.isOpen) return false;
      this.socket!.send(JSON.stringify(event));
      return true;
    }
    const existing = this.buffered.find((item) => item.key === key);
    let pending: Buffered<Out>;
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
      pending = existing;
    } else {
      pending = { key, commandId: crypto.randomUUID(), event, at: Date.now() };
      this.buffered.push(pending);
      if (this.buffered.length > BUFFER_MAX_ITEMS) this.buffered.shift();
    }
    // Keep a replay-safe command (for at most BUFFER_MAX_AGE_MS) until an event
    // emitted after the server's durable checkpoint proves it was applied. A
    // send() on an open socket is not itself an acknowledgement: the connection
    // may die before the server receives it or the response reaches this browser.
    if (this.isOpen) this.socket!.send(this.encode(pending));
    return true;
  }

  private encode(item: Buffered<Out>): string {
    return JSON.stringify({ ...item.event, _command_id: item.commandId });
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
  isAcknowledged: (incoming, pending) => {
    if (pending.type !== "kio.patch" || incoming.type !== "kio.patch") return false;
    // `auto` по всем полям команды с другим значением — сервер её переписал
    // (так он отвечает и на отклонённый пакет), а merge.ts уже снял правку из
    // pending. Повтор затёр бы серверную истину, а слияние с новыми правками
    // отклоняло бы и их. Совпадающий `auto` ничего не доказывает: команда
    // могла не дойти, повтор безвреден.
    if (incoming.source === "auto") {
      const keys = Object.keys(pending.fields);
      return keys.every((key) => key in incoming.fields)
        && keys.some((key) => !sameJsonValue(pending.fields[key], incoming.fields[key]));
    }
    return Object.entries(pending.fields).every(([key, value]) =>
      sameJsonValue(value, incoming.fields[key]),
    );
  },
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
    // Non-replacement station actions are each their own replayable command.
    // The server deduplicates by this ID inside its persisted session snapshot.
    return `command:${crypto.randomUUID()}`;
  },
  isAcknowledged: (incoming, pending) => incoming.type === "station.state"
    && pending.type === "card.reply"
    && incoming.snapshot.card_id === pending.card_id
    && incoming.snapshot.reply_text === pending.text,
});

/** Преподаватель: только передача, входящих в этом канале нет. */
export const controlChannel = (sessionId: string, options?: { onStatus?: (status: ChannelStatus) => void }) =>
  new Channel<never, InstructorToServer>(`/ws/control/${sessionId}`, options);

function sameJsonValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => sameJsonValue(item, right[index]));
  }
  if (left && right && typeof left === "object" && typeof right === "object") {
    const leftRecord = left as Record<string, unknown>;
    const rightRecord = right as Record<string, unknown>;
    const keys = Object.keys(leftRecord);
    return keys.length === Object.keys(rightRecord).length
      && keys.every((key) => key in rightRecord
        && sameJsonValue(leftRecord[key], rightRecord[key]));
  }
  return false;
}
