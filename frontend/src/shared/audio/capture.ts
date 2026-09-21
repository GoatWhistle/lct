// Захват микрофона: getUserMedia → AudioWorklet → кадры PCM16 16 кГц по 20 мс.

import workletUrl from "./worklet.ts?worker&url";

export interface Capture {
  readonly sampleRate: number;
  stop(): Promise<void>;
}

export async function startCapture(onFrame: (frame: ArrayBuffer) => void): Promise<Capture> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      // Оператор работает в гарнитуре: микрофон не слышит динамик, и эхоподавление
      // не нужно. А шумоподавление и автоусиление браузера портят речь для
      // распознавания сильнее, чем помогают (docs/arch/STACK.md).
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });

  const context = new AudioContext();
  await context.audioWorklet.addModule(workletUrl);

  const source = context.createMediaStreamSource(stream);
  const processor = new AudioWorkletNode(context, "pcm-capture");
  processor.port.onmessage = (message: MessageEvent<ArrayBuffer>) => onFrame(message.data);

  // Узел должен быть частью графа, иначе браузер может не вызывать его обработку.
  // Нулевое усиление — чтобы оператор не слышал в гарнитуре сам себя.
  const silence = context.createGain();
  silence.gain.value = 0;
  source.connect(processor).connect(silence).connect(context.destination);

  return {
    sampleRate: context.sampleRate,
    async stop() {
      processor.port.onmessage = null;
      source.disconnect();
      processor.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      await context.close();
    },
  };
}
