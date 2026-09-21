// Энергетический гейт: локальный триггер перебивания.
//
// Перебивание двухслойное. Фронт гасит звук мгновенно по громкости микрофона,
// сервер подтверждает по VAD и присылает `tts.cancel` (docs/arch/STACK.md).
// Сервер — авторитет, фронт — скорость: на сервере распознавание речи занимает
// около 90 мс, здесь — два кадра, то есть 40.

/** Порог речи в долях полной шкалы. −40 dBFS: дыхание и шум класса ниже. */
const SPEECH_RMS = 0.01;

/** Сколько кадров подряд должны быть громкими. Один кадр — это щелчок или стук. */
const ATTACK_FRAMES = 2;

/** Сколько тихих кадров считаем концом реплики — чтобы поймать следующее начало. */
const RELEASE_FRAMES = 10;

export class EnergyGate {
  private loud = 0;
  private quiet = 0;
  private speaking = false;

  constructor(
    private readonly threshold = SPEECH_RMS,
    private readonly attack = ATTACK_FRAMES,
    private readonly release = RELEASE_FRAMES,
  ) {}

  /** `true` ровно один раз — в кадре, где речь началась. */
  push(frame: ArrayBuffer): boolean {
    const samples = new Int16Array(frame);
    let sum = 0;
    for (let i = 0; i < samples.length; i++) {
      const value = samples[i] / 32768;
      sum += value * value;
    }
    const rms = Math.sqrt(sum / Math.max(1, samples.length));

    if (rms >= this.threshold) {
      this.quiet = 0;
      this.loud += 1;
      if (!this.speaking && this.loud >= this.attack) {
        this.speaking = true;
        return true;
      }
      return false;
    }

    this.loud = 0;
    if (this.speaking && ++this.quiet >= this.release) {
      this.speaking = false;
      this.quiet = 0;
    }
    return false;
  }
}
