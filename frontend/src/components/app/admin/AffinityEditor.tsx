"use client";

import { useEffect, useId, useState } from "react";

import { FailureNotice } from "@/components/app/FailureNotice";
import { NumberInput } from "@/components/app/admin/NumberInput";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { copy } from "@/i18n";
import { getAdminAffinities, setAdminAffinity, type AdminAffinity } from "@/lib/adminUsers";
import { listCharacters, type Character } from "@/lib/api";
import { toFailure, type Failure } from "@/lib/failure";

const t = copy.admin.users.affinity;

/**
 * 一位用户对某位角色的好感度，由管理者手动设定：先选角色，再填 0–120 的值。保存后立即生效、
 * 解除冷却；用户仍然只看得到档位，看不到数值。读的是后台接口，不走用户自己的好感度接口。
 */
export function AffinityEditor({ userId }: { userId: string }) {
  const [characters, setCharacters] = useState<Character[]>([]);
  const [affinities, setAffinities] = useState<Record<string, AdminAffinity>>({});
  const [selected, setSelected] = useState("");
  const [value, setValue] = useState<number | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const selectId = useId();

  useEffect(() => {
    let live = true;
    Promise.all([listCharacters(), getAdminAffinities(userId)]).then(
      ([roster, listed]) => {
        if (!live) return;
        setCharacters(roster);
        const byCharacter = Object.fromEntries(listed.map((one) => [one.character_id, one]));
        setAffinities(byCharacter);
        const first = roster[0]?.id ?? "";
        setSelected(first);
        setValue(first ? (byCharacter[first]?.value ?? null) : null);
      },
      (e) => {
        if (live) setFailure(toFailure(e));
      },
    );
    return () => {
      live = false;
    };
  }, [userId]);

  function choose(characterId: string | null) {
    if (!characterId) return;
    setSelected(characterId);
    setValue(affinities[characterId]?.value ?? null);
    setSaved(false);
    setProblem(null);
  }

  async function save() {
    if (value === null || value < 0 || value > 120) {
      setProblem(t.invalid);
      return;
    }
    setSaving(true);
    setFailure(null);
    setProblem(null);
    setSaved(false);
    try {
      const updated = await setAdminAffinity(userId, selected, value);
      setAffinities((previous) => ({ ...previous, [updated.character_id]: updated }));
      setValue(updated.value);
      setSaved(true);
    } catch (e) {
      setFailure(toFailure(e));
    } finally {
      setSaving(false);
    }
  }

  const current = affinities[selected];
  const tier = current ? copy.conversation.affinity.tiers[current.tier] : "";

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.title}</CardTitle>
        <CardDescription>{t.hint}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {failure && <FailureNotice failure={failure} />}
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor={selectId} className="text-sm font-medium">
              {t.character}
            </label>
            <Select
              items={characters.map((character) => ({ value: character.id, label: character.name }))}
              value={selected}
              onValueChange={choose}
            >
              <SelectTrigger id={selectId} className="w-52">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {characters.map((character) => (
                  <SelectItem key={character.id} value={character.id}>
                    {character.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
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
              className="w-32"
            />
          </div>
          <Button onClick={save} disabled={saving || !selected || value === null}>
            {saving && <Spinner data-icon="inline-start" />}
            {t.save}
          </Button>
        </div>
        {problem && (
          <p role="alert" className="text-sm text-destructive">
            {problem}
          </p>
        )}
        {saved && !problem && (
          <p role="status" className="text-sm text-muted-foreground">
            {t.saved}
          </p>
        )}
        {current && (
          <p className="text-sm text-muted-foreground">
            {t.current(tier, String(current.value))}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
