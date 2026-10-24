/**
 * 发语音：录一段，交给后端的语音识别听成文字，再当普通消息发出去。
 *
 * 聊天页只能在安全来源（HTTPS 或 localhost）下拿到麦克风，所以这个按钮在
 * `http://192.168.x.x` 那种地址上会失败——这是浏览器的限制，不是部署的问题。
 */

const TARGET_RATE = 16000;

export type Recording = {
  /** 停止录音，拿回 16 kHz 16 位单声道 PCM；什么都没录到就是 null。 */
  stop: () => Promise<Uint8Array | null>;
};

/** 开始录音。没有麦克风权限时抛出，调用方据此提示用户。 */
export async function startRecording(): Promise<Recording> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  processor.onaudioprocess = (event) => {
    chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  };
  source.connect(processor);
  processor.connect(context.destination);
  const rate = context.sampleRate;

  return {
    async stop() {
      processor.onaudioprocess = null;
      processor.disconnect();
      source.disconnect();
      for (const track of stream.getTracks()) track.stop();
      await context.close();
      const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      if (total === 0) return null;
      const flat = new Float32Array(total);
      let offset = 0;
      for (const chunk of chunks) {
        flat.set(chunk, offset);
        offset += chunk.length;
      }
      return encodePcm(resample(flat, rate, TARGET_RATE));
    },
  };
}

/** 把这段 PCM 交给后端听成文字。 */
export async function hearVoice(pcm: Uint8Array): Promise<string> {
  const response = await fetch("/api/voice", {
    method: "POST",
    headers: { "content-type": "application/octet-stream" },
    body: new Blob([pcm.buffer as ArrayBuffer]),
  });
  if (!response.ok) {
    const said = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(said?.detail ?? `语音识别失败（${response.status}）`);
  }
  const { text } = (await response.json()) as { text: string };
  return text;
}

/** 线性插值重采样，够用且没有依赖。 */
function resample(samples: Float32Array, from: number, to: number): Float32Array {
  if (from === to) return samples;
  const length = Math.round((samples.length * to) / from);
  const out = new Float32Array(length);
  const step = from / to;
  for (let i = 0; i < length; i += 1) {
    const at = i * step;
    const low = Math.floor(at);
    const high = Math.min(low + 1, samples.length - 1);
    const weight = at - low;
    out[i] = samples[low] * (1 - weight) + samples[high] * weight;
  }
  return out;
}

/** 浮点采样转 16 位小端。 */
function encodePcm(samples: Float32Array): Uint8Array {
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    pcm[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
  }
  return new Uint8Array(pcm.buffer);
}
