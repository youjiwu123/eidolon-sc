"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { MarkedAvatar } from "@/components/app/MarkedAvatar";
import type { Character } from "@/lib/api";
import { listGroups, type Group } from "@/lib/groups";
import { copy } from "@/i18n";
import { cn } from "@/lib/utils";

function nameOf(characters: Character[], id: string): string {
  const found = characters.find((c) => c.id === id);
  return found ? (found.name_zh ?? found.name) : id;
}

function avatarOf(characters: Character[], id: string): string | null {
  return characters.find((c) => c.id === id)?.avatar_url ?? null;
}

/** 对话列表顶上的群聊；没有群的时候什么也不出现。 */
export function GroupRows({ characters }: { characters: Character[] }) {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const pathname = usePathname();

  useEffect(() => {
    let live = true;
    listGroups()
      .then((next) => live && setGroups(next))
      .catch(() => live && setGroups([]));
    return () => {
      live = false;
    };
  }, [pathname]);

  if (!groups || groups.length === 0) return null;

  return (
    <div className="pb-1">
      <p className="px-4 pt-2 pb-1 text-meta text-muted-foreground">{copy.groups.title}</p>
      <ul aria-label={copy.groups.title}>
        {groups.map((group) => {
          const href = `/groups/${group.id}`;
          const active = pathname === href;
          const preview = group.last_message?.segments?.[0];
          return (
            <li key={group.id}>
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex w-full items-center gap-3 px-4 py-2.5 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:ring-inset",
                  active ? "bg-muted" : "hover:bg-muted/60",
                )}
              >
                <span className="flex -space-x-2">
                  {group.participants.slice(0, 3).map((id) => (
                    <MarkedAvatar
                      key={id}
                      name={nameOf(characters, id)}
                      src={avatarOf(characters, id)}
                      size="list"
                    />
                  ))}
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="min-w-0 truncate text-row-name">
                    {group.title ?? group.participants.map((id) => nameOf(characters, id)).join("、")}
                  </span>
                  {preview && (
                    <span className="min-w-0 truncate text-preview text-muted-foreground">
                      {preview.zh || preview.ja}
                    </span>
                  )}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
