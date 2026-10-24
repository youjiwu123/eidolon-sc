import { api, json } from "./api";

/** 群聊：几个人一个对话（`.scratch/group-chat/spec.md`）。单聊仍走 `/api/conversations`。 */

export type GroupSegment = { ja: string; zh: string };

export type GroupMessage = {
  id: number;
  role: "user" | "character";
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
  if (!res.body) throw new Error("没有收到她们的回话");
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
      if (!line) continue;
      onEvent(JSON.parse(line.slice(6)) as GroupTurnEvent);
    }
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
