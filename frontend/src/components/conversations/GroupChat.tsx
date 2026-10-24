"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { MarkedAvatar } from "@/components/app/MarkedAvatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import type { Character } from "@/lib/api";
import { getGroup, renameGroup, sendGroupTurn, startChatter, type Group, type GroupMessage } from "@/lib/groups";
import { copy } from "@/i18n";

function nameOf(characters: Character[] | null, id: string): string {
  const found = characters?.find((c) => c.id === id);
  return found ? (found.name_zh ?? found.name) : id;
}

function avatarOf(characters: Character[] | null, id: string): string | null {
  return characters?.find((c) => c.id === id)?.avatar_url ?? null;
}

/** 一个群的对话：谁说的、说了什么；发一句话，她们接着回。 */
export function GroupChat({ groupId, characters }: { groupId: string; characters: Character[] | null }) {
  const [group, setGroup] = useState<Group | null>(null);
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [typing, setTyping] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [autoChat, setAutoChat] = useState(false);
  const auto = useRef(false);
  const [nameDraft, setNameDraft] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const data = await getGroup(groupId);
      setGroup(data.group);
      setMessages(data.messages);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    }
  }, [groupId]);

  useEffect(() => {
    void load();
    setAutoChat(window.localStorage.getItem(`group-auto-${groupId}`) === "1");
  }, [load, groupId]);

  // 自动聊天：开着就隔一会儿让她们自己聊一轮，直到关掉或有人在打字。
  useEffect(() => {
    auto.current = autoChat;
    if (!autoChat) return;
    let cancelled = false;
    const run = async () => {
      if (sending || typing) return;
      try {
        await startChatter(groupId, (event) => {
          if (cancelled) return;
          if (event.type === "thinking") setTyping(event.speaker_id);
          else if (event.type === "message") {
            setTyping(null);
            setMessages((current) => [...current, event.message]);
          } else if (event.type === "done") setTyping(null);
        });
      } catch {
        /* 下一轮再试 */
      }
    };
    const timer = setInterval(() => void run(), 20000);
    return () => {
      cancelled = true;
      clearInterval(timer);
      setTyping(null);
    };
  }, [autoChat, groupId, sending, typing]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);

  /** 解析开头的 `/名字`：给谁说的。名字按中文名、日文名或 id 前缀认。 */
  const parseTarget = (raw: string): { text: string; targetId: string | null } => {
    const match = raw.match(/^\/([^\s/]+)\s+([\s\S]+)$/);
    if (!match) return { text: raw, targetId: null };
    const wanted = match[1];
    const found = (characters ?? []).find(
      (c) =>
        c.id === wanted ||
        c.name === wanted ||
        c.name_zh === wanted ||
        c.name.startsWith(wanted) ||
        (c.name_zh?.startsWith(wanted) ?? false),
    );
    if (!found || !members.includes(found.id)) return { text: raw, targetId: null };
    return { text: match[2].trim(), targetId: found.id };
  };

  const send = async () => {
    const parsed = parseTarget(draft.trim());
    const text = parsed.text.trim();
    if (!text || sending) return;
    setSending(true);
    setFailure(null);
    setTyping(null);
    const optimistic: GroupMessage = {
      id: -Date.now(),
      role: "user",
      speaker_id: null,
      segments: [{ ja: "", zh: text }],
      created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, optimistic]);
    setDraft("");
    try {
      await sendGroupTurn(groupId, text, (event) => {
        if (event.type === "thinking") setTyping(event.speaker_id);
        else if (event.type === "message") {
          setTyping(null);
          setMessages((current) => [...current, event.message]);
        } else if (event.type === "done") setTyping(null);
      }, parsed.targetId);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setTyping(null);
      setSending(false);
    }
  };

  const members = group?.participants ?? [];

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b px-3 py-2">
        <Link href="/groups" className="text-meta text-muted-foreground hover:underline">
          {copy.groups.back}
        </Link>
        <div className="flex -space-x-2">
          {members.slice(0, 3).map((id) => (
            <MarkedAvatar key={id} name={nameOf(characters, id)} src={avatarOf(characters, id)} size="sm" />
          ))}
        </div>
        {renaming ? (
          <div className="flex flex-1 items-center gap-2">
            <Input
              autoFocus
              value={nameDraft}
              onChange={(event) => setNameDraft(event.target.value)}
              maxLength={40}
              className="h-8 flex-1"
            />
            <Button
              size="sm"
              onClick={async () => {
                try {
                  const updated = await renameGroup(groupId, nameDraft.trim());
                  setGroup((current) => (current ? { ...current, title: updated.title } : current));
                } catch (error) {
                  setFailure(error instanceof Error ? error.message : String(error));
                } finally {
                  setRenaming(false);
                }
              }}
            >
              {copy.groups.saveName}
            </Button>
          </div>
        ) : (
          <button
            type="button"
            className="min-w-0 flex-1 truncate text-left text-base hover:underline"
            onClick={() => {
              setNameDraft(group?.title ?? members.map((id) => nameOf(characters, id)).join("、"));
              setRenaming(true);
            }}
          >
            {group?.title ?? members.map((id) => nameOf(characters, id)).join("、")}
          </button>
        )}
        <label className="flex shrink-0 items-center gap-2 text-meta text-muted-foreground">
          <Switch
            checked={autoChat}
            onCheckedChange={(next) => {
              setAutoChat(next);
              window.localStorage.setItem(`group-auto-${groupId}`, next ? "1" : "0");
            }}
          />
          {copy.groups.chatter}
        </label>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto px-3 py-4">
        {messages.map((message) =>
          message.role === "user" ? (
            <div key={message.id} className="flex justify-end">
              <div className="max-w-[80%] rounded-2xl bg-primary px-3 py-2 text-primary-foreground">
                {(message.segments[0]?.zh || message.segments[0]?.ja) ?? ""}
              </div>
            </div>
          ) : (
            <div key={message.id} className="flex gap-2">
              <MarkedAvatar
                name={nameOf(characters, message.speaker_id ?? "")}
                src={message.speaker_id ? avatarOf(characters, message.speaker_id) : null}
                size="bubble"
              />
              <div className="max-w-[80%] space-y-1">
                <p className="text-meta text-muted-foreground">
                  {message.speaker_id ? nameOf(characters, message.speaker_id) : ""}
                </p>
                <div className="space-y-1 rounded-2xl bg-muted px-3 py-2">
                  {message.segments.map((segment, index) => (
                    <div key={index}>
                      {segment.ja && <p className="text-base">{segment.ja}</p>}
                      {segment.zh && <p className="text-meta text-muted-foreground">{segment.zh}</p>}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ),
        )}
        {typing && (
          <div className="flex items-center gap-2 text-meta text-muted-foreground">
            <Spinner className="size-4" />
            {copy.groups.typing(nameOf(characters, typing))}
          </div>
        )}
        {failure && <p className="text-meta text-destructive">{failure}</p>}
        <div ref={bottom} />
      </div>

      <div className="flex items-end gap-2 border-t p-3">
        <Textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}
          placeholder={copy.groups.placeholderHint}
          className="min-h-10 flex-1 resize-none"
          rows={1}
        />
        <Button disabled={!draft.trim() || sending} onClick={() => void send()}>
          {copy.groups.send}
        </Button>
      </div>
    </div>
  );
}
