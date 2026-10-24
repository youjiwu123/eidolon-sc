"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { MarkedAvatar } from "@/components/app/MarkedAvatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import type { Character } from "@/lib/api";
import { createGroup, listGroups, type Group } from "@/lib/groups";
import { copy } from "@/i18n";

const MIN = 2;
const MAX = 4;

function nameOf(characters: Character[] | null, id: string): string {
  const found = characters?.find((c) => c.id === id);
  return found ? (found.name_zh ?? found.name) : id;
}

function avatarOf(characters: Character[] | null, id: string): string | null {
  return characters?.find((c) => c.id === id)?.avatar_url ?? null;
}

/** 群聊列表：已有的群 + 新建群聊。 */
export function GroupDirectory({ characters }: { characters: Character[] | null }) {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setGroups(await listGroups());
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
      setGroups([]);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const toggle = (id: string) => {
    setPicked((current) =>
      current.includes(id) ? current.filter((c) => c !== id) : current.length >= MAX ? current : [...current, id],
    );
  };

  const submit = async () => {
    if (picked.length < MIN || busy) return;
    setBusy(true);
    try {
      const group = await createGroup(picked);
      setOpen(false);
      setPicked([]);
      setGroups((current) => [group, ...(current ?? [])]);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-header font-medium">{copy.groups.title}</h2>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger render={<Button size="sm" variant="outline" />}>
            {copy.groups.create}
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{copy.groups.create}</DialogTitle>
              <DialogDescription>{copy.groups.pick(MIN, MAX)}</DialogDescription>
            </DialogHeader>
            <div className="max-h-80 space-y-1 overflow-y-auto">
              {(characters ?? []).map((character) => (
                <label
                  key={character.id}
                  className="flex cursor-pointer items-center gap-3 rounded-lg px-2 py-1.5 hover:bg-muted"
                >
                  <Checkbox
                    checked={picked.includes(character.id)}
                    onCheckedChange={() => toggle(character.id)}
                  />
                  <MarkedAvatar name={character.name} src={character.avatar_url} size="sm" />
                  <span className="text-base">{character.name_zh ?? character.name}</span>
                </label>
              ))}
            </div>
            <DialogFooter>
              <Button disabled={picked.length < MIN || busy} onClick={() => void submit()}>
                {busy ? <Spinner className="size-4" /> : copy.groups.createConfirm(picked.length)}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      {failure && <p className="text-meta text-destructive">{failure}</p>}
      {groups === null ? (
        <div className="flex justify-center p-6">
          <Spinner className="size-5 text-muted-foreground" />
        </div>
      ) : groups.length === 0 ? (
        <p className="text-meta text-muted-foreground">{copy.groups.empty}</p>
      ) : (
        <ul className="space-y-1">
          {groups.map((group) => {
            const last = group.last_message;
            const line = last?.segments?.[0]?.zh ?? last?.segments?.[0]?.ja ?? "";
            return (
              <li key={group.id}>
                <Link
                  href={`/groups/${group.id}`}
                  className="flex items-center gap-3 rounded-xl px-2 py-2 hover:bg-muted"
                >
                  <div className="flex -space-x-2">
                    {group.participants.slice(0, 3).map((id) => (
                      <MarkedAvatar
                        key={id}
                        name={nameOf(characters, id)}
                        src={avatarOf(characters, id)}
                        size="sm"
                      />
                    ))}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-base">
                      {group.participants.map((id) => nameOf(characters, id)).join("、")}
                    </p>
                    {line && <p className="truncate text-meta text-muted-foreground">{line}</p>}
                  </div>
                  <Badge variant="secondary">{group.participants.length}</Badge>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
