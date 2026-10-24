/**
 * 她的回复按需朗读：点气泡旁的小喇叭才合成，一次只放一条。
 *
 * 语音由后端合成并缓存在这条消息上（`/api/messages/<id>/speech`），再点一次是播放
 * 已存下的那份，不重复花钱。状态放模块里，好让同一条消息的几个气泡共用一个播放器：
 * 换一条就停掉上一条，不叠着响。
 */

export type SpeechState = { messageId: number; phase: "loading" | "playing" } | null;

let state: SpeechState = null;
let audio: HTMLAudioElement | null = null;
const listeners = new Set<() => void>();

function publish(next: SpeechState) {
  state = next;
  for (const listener of listeners) listener();
}

export function subscribeSpeech(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function speechState(): SpeechState {
  return state;
}

export function stopSpeech() {
  if (audio) {
    audio.pause();
    audio.src = "";
    audio = null;
  }
  if (state !== null) publish(null);
}

/** 播放这条回复的语音；正在放的就是它的，则停下。 */
export function toggleSpeech(messageId: number, url?: string) {
  if (state?.messageId === messageId) {
    stopSpeech();
    return;
  }
  stopSpeech();
  publish({ messageId, phase: "loading" });
  const element = new Audio(url ?? `/api/messages/${messageId}/speech`);
  audio = element;
  element.addEventListener("playing", () => publish({ messageId, phase: "playing" }));
  element.addEventListener("ended", stopSpeech);
  element.addEventListener("error", stopSpeech);
  void element.play().catch(stopSpeech);
}
