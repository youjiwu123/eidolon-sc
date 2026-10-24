"use client";

import { useEffect, useRef } from "react";

import type { LatestConversation } from "./api";

/**
 * 她主动搭话时用系统通知提醒（手机和电脑都支持）。
 *
 * 授权只能由用户手势触发，所以在第一次点按/按键时问一次；没授权或浏览器不支持就什么都不做。
 * 同一段搭话只提醒一次：列表里那一行的「没看过的搭话」标记消失后再出现，才算新的一次。
 */
export function useReachOutNotifications(
  characters: { id: string; name_zh: string | null; name: string }[] | null,
  latest: Record<string, LatestConversation>,
): void {
  const notified = useRef(new Set<string>());

  useEffect(() => {
    const ask = () => {
      const api = typeof window === "undefined" ? undefined : window.Notification;
      if (api && api.permission === "default") void api.requestPermission().catch(() => undefined);
      window.removeEventListener("pointerdown", ask);
      window.removeEventListener("keydown", ask);
    };
    window.addEventListener("pointerdown", ask, { once: true });
    window.addEventListener("keydown", ask, { once: true });
    return () => {
      window.removeEventListener("pointerdown", ask);
      window.removeEventListener("keydown", ask);
    };
  }, []);

  useEffect(() => {
    if (!characters) return;
    const api = typeof window === "undefined" ? undefined : window.Notification;
    if (!api || api.permission !== "granted") return;
    for (const character of characters) {
      const hers = latest[character.id];
      if (!hers?.unseen_reach_out) {
        notified.current.delete(character.id);
        continue;
      }
      if (notified.current.has(character.id)) continue;
      notified.current.add(character.id);
      const message = hers.last_message;
      const body = message && "text" in message ? (message.zh || message.text) : "";
      new api(character.name_zh || character.name, {
        body: body || undefined,
        tag: `reach-out-${character.id}`,
      });
    }
  }, [characters, latest]);
}
