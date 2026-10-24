import { api, json } from "./api";

/** 群聊：几个人一个对话（`.scratch/group-chat/spec.md`）。单聊仍走 `/api/conversations`。 */

/** 服务端说不行的时候，把状态码带进错误里，页面上好分辨。 */
async function ensureOk(res: Response): Promise<Response> {
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status}${body ? `: ${body}` : ""}`);
  }
  return res;
}

export type GroupSegment = { ja: string; zh: string };

export type GroupMessage = {
  id: number;
  role: "user" | "character" | "system";
  speaker_id: string | null;
  segments: GroupSegment[];
  created_at: string;
};

export type Group = {
  id: string;
  title: string | null;
  participants: string[];
  created_at: string;
  last_message: GroupMessage | null;
  auto_chat?: boolean;
};

export async function listGroups(): Promise<Group[]> {
  const res = await api("/api/groups");
  const body = await json<{ groups: Group[] }>(res);
  return body.groups;
}

export async function createGroup(characterIds: string[]): Promise<Group> {
  const res = await api("/api/groups", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ character_ids: characterIds }),
  });
  return json<Group>(res);
}

export async function getGroup(groupId: string): Promise<{ group: Group; messages: GroupMessage[] }> {
  const res = await api(`/api/groups/${groupId}/messages`);
  return json<{ group: Group; messages: GroupMessage[] }>(res);
}

export type GroupTurnEvent =
  | { type: "user"; segments: GroupSegment[] }
  | { type: "thinking"; speaker_id: string }
  | { type: "message"; message: GroupMessage }
  | { type: "done" };

/** 发一句话，一条条读她们的回话（服务端是 SSE，谁说完谁就出现）。 */
export async function sendGroupTurn(
  groupId: string,
  text: string,
  onEvent: (event: GroupTurnEvent) => void,
  targetId?: string | null,
  signal?: AbortSignal,
): Promise<void> {
  const res = await api(`/api/groups/${groupId}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, target_id: targetId ?? null }),
    signal,
  });
  await ensureOk(res);
  await ensureOk(res);
  await ensureOk(res);
  if (!res.body) throw new Error("没有收到她们的回话");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        onEvent(JSON.parse(line.slice(6)) as GroupTurnEvent);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function deleteGroup(groupId: string): Promise<void> {
  const res = await api(`/api/groups/${groupId}`, { method: "DELETE" });
  await json<{ deleted: string }>(res);
}

export async function renameGroup(groupId: string, title: string): Promise<Group> {
  const res = await api(`/api/groups/${groupId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return json<Group>(res);
}

/** 让她们自己聊两三句（不用用户先说话）。 */
export async function startChatter(
  groupId: string,
  onEvent: (event: GroupTurnEvent) => void,
): Promise<void> {
  const res = await api(`/api/groups/${groupId}/chatter`, { method: "POST" });
  await ensureOk(res);
  await ensureOk(res);
  await ensureOk(res);
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)) as GroupTurnEvent);
    }
  }
}

export async function inviteMember(
  groupId: string,
  characterId: string,
): Promise<Group & { notice?: GroupMessage }> {
  const res = await api(`/api/groups/${groupId}/members`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ character_id: characterId }),
  });
  return json<Group & { notice?: GroupMessage }>(res);
}

/** 刚进群的人打个招呼，另一个人接一句。 */
export async function startWelcome(
  groupId: string,
  characterId: string,
  onEvent: (event: GroupTurnEvent) => void,
): Promise<void> {
  const res = await api(`/api/groups/${groupId}/welcome`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ character_id: characterId }),
  });
  await ensureOk(res);
  await ensureOk(res);
  await ensureOk(res);
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)) as GroupTurnEvent);
    }
  }
}

/** 开关「让她们自己聊」。开关在服务器上，关掉网页也继续，到点自己停。 */
export async function setAutoChat(
  groupId: string,
  enabled: boolean,
  minutes = 20,
): Promise<{ auto_chat: boolean; until: string | null }> {
  const res = await api(`/api/groups/${groupId}/auto`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, minutes }),
  });
  return json<{ auto_chat: boolean; until: string | null }>(res);
}

/** 群里某条回复的语音（后端合成一次就存着，再点是放缓存）。 */
export function groupSpeechUrl(groupId: string, messageId: number): string {
  return `/api/groups/${groupId}/messages/${messageId}/speech`;
}
