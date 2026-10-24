"use client";

import { useEffect, useState, type FormEvent } from "react";

import { FailureNotice } from "@/components/app/FailureNotice";
import { NumberInput } from "@/components/app/admin/NumberInput";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { copy } from "@/i18n";
import { getAdminAffinities, setAdminAffinity } from "@/lib/adminUsers";
import type { AffinityStatus, Tier } from "@/lib/affinity";
import type { Character } from "@/lib/api";
import { toFailure, type Failure } from "@/lib/failure";
import { herVars } from "@/theme/herVars";

const t = copy.admin.users.affinity;

/**
 * 管理者在对话页直接调整这位角色的好感度：更多菜单里打开，只对管理者显示。值 0–120，保存后
 * 立即生效并解除冷却；普通用户看不到这个入口，也调不动后台接口。对话框渲染到页首外面，
 * 在内容上再注入一次她的颜色（DESIGN.md「The Scoped Colour Rule」）。
 */
export function AdminAffinityDialog({
  userId,
  character,
  affinity,
  isOpen,
  onOpenChange,
  onSaved,
}: {
  userId: string;
  character: Character;
  /** 页首现在的好感度；读不到（好感度关着）时没有入口，也不会打开。 */
  affinity: AffinityStatus | null;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [value, setValue] = useState<number | null>(null);
  const [tier, setTier] = useState<Tier | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    let live = true;
    setLoading(true);
    setFailure(null);
    setProblem(null);
    setSaved(false);
    getAdminAffinities(userId).then(
      (listed) => {
        if (!live) return;
        const found = listed.find((one) => one.character_id === character.id);
        setValue(found?.value ?? null);
        setTier(found?.tier ?? affinity?.tier ?? null);
      },
      (e) => {
        if (live) setFailure(toFailure(e));
      },
    ).finally(() => {
      if (live) setLoading(false);
    });
    return () => {
      live = false;
    };
  }, [isOpen, userId, character.id, affinity?.tier]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (value === null || value < 0 || value > 120) {
      setProblem(t.invalid);
      return;
    }
    setSaving(true);
    setFailure(null);
    setProblem(null);
    setSaved(false);
    try {
      const updated = await setAdminAffinity(userId, character.id, value);
      setValue(updated.value);
      setTier(updated.tier);
      setSaved(true);
      onSaved();
    } catch (e) {
      setFailure(toFailure(e));
    } finally {
      setSaving(false);
    }
  }

  const shownTier = tier ? copy.conversation.affinity.tiers[tier] : "";
  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent style={herVars(character.color)} className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{`${character.name} · ${t.title}`}</DialogTitle>
          <DialogDescription>{t.hint}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium">{t.value}</span>
            <NumberInput
              label={t.value}
              value={value}
              onChange={(next) => {
                setValue(next);
                setSaved(false);
                setProblem(null);
              }}
              min={0}
              max={120}
              disabled={loading}
              className="w-32"
            />
          </div>
          {tier && (
            <p className="text-sm text-muted-foreground">
              {t.current(shownTier, String(value ?? ""))}
            </p>
          )}
          {failure && <FailureNotice failure={failure} />}
          {problem && (
            <p role="alert" className="text-sm text-destructive">
              {problem}
            </p>
          )}
          {saved && (
            <p role="status" className="text-sm text-muted-foreground">
              {t.saved}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={saving || loading || value === null}>
              {saving && <Spinner data-icon="inline-start" />}
              {t.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
