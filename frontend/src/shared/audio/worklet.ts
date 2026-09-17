// Процессор захвата: звук микрофона → кадры PCM16 16 кГц по 20 мс.
// Работает в отдельном аудиопотоке браузера — главный поток с React не тормозит звук.

import { FRAME_SAMPLES, Pcm16Framer, TARGET_RATE } from "./resample";

class PcmCaptureProcessor extends AudioWorkletProcessor {
  private readonly framer = new Pcm16Framer(sampleRate, TARGET_RATE, FRAME_SAMPLES);

  process(inputs: Float32Array[][]): boolean {
    const channel = inputs[0]?.[0];
    if (channel) {
      for (const frame of this.framer.push(channel)) {
        // Буфер передаётся, а не копируется: 50 кадров в секунду без лишних аллокаций.
        this.port.postMessage(frame.buffer, [frame.buffer]);
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
