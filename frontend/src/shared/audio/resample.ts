// Нарезка звука микрофона в кадры PCM16 16 кГц по 20 мс — формат из контракта
// (docs/arch/CONTRACT.md#аудио-по-websocket). Ресемплинг делает фронт, а не сервер:
// так с микрофона не летит 48 кГц лишнего трафика, и сервер получает ровно то,
// что ждёт GigaAM.
//
// Работает потоком: AudioWorklet отдаёт звук кусками по 128 сэмплов, и граница
// кадра почти никогда не совпадает с границей куска.

export const TARGET_RATE = 16_000;
export const FRAME_SAMPLES = 320; // 20 мс при 16 кГц = 640 байт PCM16

export class Pcm16Framer {
  private readonly ratio: number;
  private sum = 0;
  private count = 0;
  private consumed = 0;
  private nextBoundary: number;
  private frame: Int16Array;
  private filled = 0;

  constructor(
    inputRate: number,
    outputRate = TARGET_RATE,
    private readonly frameSamples = FRAME_SAMPLES,
  ) {
    if (inputRate < outputRate) {
      throw new Error(`частота микрофона ${inputRate} ниже целевой ${outputRate}`);
    }
    this.ratio = inputRate / outputRate;
    this.nextBoundary = this.ratio;
    this.frame = new Int16Array(frameSamples);
  }

  // Усреднение входных сэмплов на интервале одного выходного. Это грубый
  // фильтр нижних частот, но без него всё, что выше 8 кГц, наложилось бы
  // на речь при прореживании 48 → 16 кГц и испортило распознавание.
  push(input: Float32Array): Int16Array[] {
    const frames: Int16Array[] = [];
    for (let i = 0; i < input.length; i++) {
      this.sum += input[i];
      this.count += 1;
      this.consumed += 1;
      if (this.consumed < this.nextBoundary) continue;

      const sample = Math.max(-1, Math.min(1, this.sum / this.count));
      this.frame[this.filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.sum = 0;
      this.count = 0;
      this.nextBoundary += this.ratio;

      if (this.filled === this.frameSamples) {
        frames.push(this.frame);
        this.frame = new Int16Array(this.frameSamples);
        this.filled = 0;
      }
    }
    return frames;
  }
}
