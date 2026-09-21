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

interface ChannelOptions<In> {
  onEvent?: (event: In) => void;
  /** Бинарный кадр: звук звонящего, без обёртки JSON. */
  onBinary?: (frame: ArrayBuffer) => void;
  onStatus?: (status: ChannelStatus) => void;
}

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 5_000;

export class Channel<In extends { type: string }, Out extends { type: string }> {
  private socket: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closedByUs = false;

  constructor(
    private readonly path: string,
    private readonly options: ChannelOptions<In> = {},
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

  // Пока соединения нет, события не копятся: истина — на сервере, и после
  // переподключения интерфейс получит актуальное состояние, а не очередь старых правок.
  send(event: Out): boolean {
    if (!this.isOpen) return false;
    this.socket!.send(JSON.stringify(event));
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
  }
}

export const callChannel = (sessionId: string, options?: ChannelOptions<ServerToTrainee>) =>
  new Channel<ServerToTrainee, TraineeToServer>(`/ws/call/${sessionId}`, options);

/** Наблюдатель: отправлять нечего и нельзя — `Out` равен `never`. */
export const observeChannel = (sessionId: string, options?: ChannelOptions<ServerToObserver>) =>
  new Channel<ServerToObserver, never>(`/ws/observe/${sessionId}`, options);

/** Станция ДДС: только JSON, аудио здесь нет вообще. */
export const stationChannel = (
  sessionId: string,
  role: string,
  options?: ChannelOptions<ServerToStation>,
) => new Channel<ServerToStation, StationToServer>(`/ws/station/${sessionId}?role=${role}`, options);

/** Преподаватель: только передача, входящих в этом канале нет. */
export const controlChannel = (sessionId: string, options?: { onStatus?: (status: ChannelStatus) => void }) =>
  new Channel<never, InstructorToServer>(`/ws/control/${sessionId}`, options);
