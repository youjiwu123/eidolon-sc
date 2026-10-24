"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { MarkedAvatar } from "@/components/app/MarkedAvatar";
import { SpeakIcon } from "@/theme/icons";
import { speechState, subscribeSpeech, toggleSpeech } from "@/lib/messageSpeech";
import { useSyncExternalStore } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import type { Character } from "@/lib/api";
import {
  getGroup,
  inviteMember,
  renameGroup,
  startChatter,
  sendGroupTurn,
  groupSpeechUrl,
  startWelcome,
  type Group,
  type GroupMessage,
} from "@/lib/groups";
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
  const [autoChat, setAutoChatState] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const speech = useSyncExternalStore(subscribeSpeech, speechState, speechState);
  const busy = useRef(false);
  const sendingRef = useRef(false);
  const abort = useRef<AbortController | null>(null);
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
    setAutoChatState(window.localStorage.getItem(`group-auto-${groupId}`) === "1");
  }, [load, groupId]);

  // 自动聊天跟着这个页面走：开着就每 20 秒让她们聊一轮，关掉网页、锁屏就停。
  // 依赖里只放开关和群——把 typing 放进来会让计时器不停重建，收到的消息被丢掉。
  useEffect(() => {
    if (!autoChat) return;
    let stopped = false;
    const run = async () => {
      if (stopped || busy.current || sendingRef.current) return;
      busy.current = true;
      try {
        await startChatter(groupId, (event) => {
          if (event.type === "thinking") setTyping(event.speaker_id);
          else if (event.type === "message") {
            setTyping(null);
            setMessages((current) => [...current, event.message]);
          } else if (event.type === "done") setTyping(null);
        });
      } catch (error) {
        if (String(error).includes("429")) {
          setAutoChatState(false);
          setFailure(copy.groups.quotaOut);
        }
      } finally {
        busy.current = false;
      }
    };
    const timer = setInterval(() => void run(), 20000);
    // 开满 20 分钟自己关掉，免得忘了它还开着。
    const stop = setTimeout(() => {
      setAutoChatState(false);
      setFailure(copy.groups.autoStopped);
    }, 20 * 60 * 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
      clearTimeout(stop);
      setTyping(null);
    };
  }, [autoChat, groupId]);

  // 每 15 秒对一次账：别的窗口、别的设备、或者漏掉的事件，都靠它补上。
  useEffect(() => {
    const pull = async () => {
      if (busy.current || sendingRef.current) return;
      try {
        const data = await getGroup(groupId);
        setGroup(data.group);
        setMessages((current) => (data.messages.length > current.length ? data.messages : current));
      } catch {
        /* 下一轮再试 */
      }
    };
    const timer = setInterval(() => void pull(), 15000);
    return () => clearInterval(timer);
  }, [groupId]);

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
    sendingRef.current = true;
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
    abort.current = new AbortController();
    try {
      await sendGroupTurn(groupId, text, (event) => {
        if (event.type === "thinking") setTyping(event.speaker_id);
        else if (event.type === "message") {
          setTyping(null);
          setMessages((current) => [...current, event.message]);
        } else if (event.type === "done") setTyping(null);
      }, parsed.targetId, abort.current.signal);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setTyping(null);
      setSending(false);
      sendingRef.current = false;
    }
  };

  // 兜底：真卡住了也不要一直禁用发送键。
  useEffect(() => {
    if (!sending) return;
    const guard = setTimeout(() => {
      setSending(false);
      sendingRef.current = false;
      setTyping(null);
    }, 90000);
    return () => clearTimeout(guard);
  }, [sending]);

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
        <Dialog open={inviteOpen} onOpenChange={setInviteOpen}>
          <DialogTrigger render={<Button size="sm" variant="ghost" />}>{copy.groups.invite}</DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{copy.groups.invite}</DialogTitle>
              <DialogDescription>{copy.groups.inviteHint}</DialogDescription>
            </DialogHeader>
            <div className="max-h-80 space-y-1 overflow-y-auto">
              {(characters ?? [])
                .filter((c) => !members.includes(c.id))
                .map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    className="flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-muted"
                    onClick={async () => {
                      try {
                        const updated = await inviteMember(groupId, c.id);
                        setGroup((current) =>
                          current ? { ...current, participants: updated.participants } : current,
                        );
                        setInviteOpen(false);
                        if (updated.notice) {
                          setMessages((current) => [...current, updated.notice as GroupMessage]);
                        }
                        setSending(true);
                        await startWelcome(groupId, c.id, (event) => {
                          if (event.type === "thinking") setTyping(event.speaker_id);
                          else if (event.type === "message") {
                            setTyping(null);
                            setMessages((current) => [...current, event.message]);
                          } else if (event.type === "done") setTyping(null);
                        });
                      } catch (error) {
                        setFailure(error instanceof Error ? error.message : String(error));
                      } finally {
                        setTyping(null);
                        setSending(false);
                      }
                    }}
                  >
                    <MarkedAvatar name={c.name} src={c.avatar_url} size="sm" />
                    <span className="text-base">{c.name_zh ?? c.name}</span>
                  </button>
                ))}
            </div>
          </DialogContent>
        </Dialog>
        <label className="flex shrink-0 items-center gap-2 text-meta text-muted-foreground">
          <Switch
            checked={autoChat}
            onCheckedChange={(next) => {
              setAutoChatState(next);
              window.localStorage.setItem(`group-auto-${groupId}`, next ? "1" : "0");
            }}
          />
          {copy.groups.chatter}
        </label>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto px-3 py-4">
        {messages.map((message) =>
          message.role === "system" ? (
            <p key={message.id} className="py-1 text-center text-meta text-muted-foreground">
              {message.segments[0]?.zh || message.segments[0]?.ja}
            </p>
          ) : message.role === "user" ? (
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
                <button
                  type="button"
                  aria-label={copy.groups.speak}
                  className="flex items-center gap-1 text-meta text-muted-foreground hover:text-foreground"
                  onClick={() => toggleSpeech(message.id, groupSpeechUrl(groupId, message.id))}
                >
                  <SpeakIcon aria-hidden className="size-3.5" />
                  {speech?.messageId === message.id
                    ? speech.phase === "loading"
                      ? copy.groups.speakingLoading
                      : copy.groups.speaking
                    : copy.groups.speak}
                </button>
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
        <Button
          variant={sending ? "destructive" : "outline"}
          disabled={!sending && !draft.trim()}
          className="disabled:bg-background disabled:text-muted-foreground disabled:opacity-60"
          onClick={() => {
            if (sending) {
              abort.current?.abort();
              setSending(false);
              sendingRef.current = false;
              setTyping(null);
              return;
            }
            void send();
          }}
        >
          {sending ? copy.groups.stop : copy.groups.send}
        </Button>
      </div>
    </div>
  );
}
