// Воспроизведение голоса звонящего.
//
// Очередь `AudioBufferSourceNode`, а не `<audio>`: перебивание требует оборвать
// звук мгновенно и выбросить всё, что уже пришло, а `<audio>` так не умеет
// (docs/arch/CONTRACT.md).

export const CALLER_RATE = 24_000; // выход Silero

/** То, что нужно от AudioContext. Отдельным типом — чтобы очередь проверялась без браузера. */
export interface AudioSink {
  readonly currentTime: number;
  readonly destination: AudioNode;
  createBuffer(channels: number, length: number, sampleRate: number): AudioBuffer;
  createBufferSource(): AudioBufferSourceNode;
}

//: Небольшое упреждение: чанки планируются чуть вперёд, иначе первый из них
//: обрывается на старте, а между соседними слышен щелчок.
const LEAD_S = 0.05;

export class Playback {
  private sources = new Set<AudioBufferSourceNode>();
  private nextStart = 0;

  constructor(private readonly context: AudioSink) {}

  /** Сколько секунд звука ещё не доиграло. */
  get remaining(): number {
    return Math.max(0, this.nextStart - this.context.currentTime);
  }

  get speaking(): boolean {
    return this.remaining > 0;
  }

  /** Чанк PCM16 24 кГц в очередь. */
  enqueue(pcm: ArrayBuffer): void {
    const samples = new Int16Array(pcm);
    if (samples.length === 0) return;

    const buffer = this.context.createBuffer(1, samples.length, CALLER_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;

    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);

    const startAt = Math.max(this.context.currentTime + LEAD_S, this.nextStart);
    source.start(startAt);
    this.nextStart = startAt + buffer.duration;

    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
  }

  /** Оборвать звук и выбросить очередь — реакция на перебивание. */
  flush(): void {
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // уже остановлен — ничего не делаем
      }
      source.disconnect();
    }
    this.sources.clear();
    this.nextStart = 0;
  }
}
