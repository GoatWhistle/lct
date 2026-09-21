// Аудио-фон происшествия: треск огня, детский плач, шум улицы.
//
// Файл лежит в сборке и крутится локально — трафика фон не создаёт
// (docs/arch/CONTRACT.md). Гейн заметно ниже голоса: фон даёт ощущение места,
// но не мешает разбирать речь.

export class Ambience {
  private source: AudioBufferSourceNode | null = null;
  private gain: GainNode | null = null;

  constructor(private readonly context: AudioContext) {}

  async start(loop: string, gainDb: number): Promise<void> {
    this.stop();
    let buffer: AudioBuffer;
    try {
      const response = await fetch(`/ambience/${loop}`);
      if (!response.ok) throw new Error(`${response.status}`);
      buffer = await this.context.decodeAudioData(await response.arrayBuffer());
    } catch (error) {
      // Записи фона — контент, его добавляет методист. Нет файла — занятие идёт
      // без фона, а не падает посреди звонка.
      console.warn(`фон ${loop} не загрузился, звонок идёт без него:`, error);
      return;
    }

    const gain = this.context.createGain();
    gain.gain.value = 10 ** (gainDb / 20);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.loop = true;
    source.connect(gain).connect(this.context.destination);
    source.start();
    this.source = source;
    this.gain = gain;
  }

  stop(): void {
    this.source?.stop();
    this.source?.disconnect();
    this.gain?.disconnect();
    this.source = null;
    this.gain = null;
  }
}
