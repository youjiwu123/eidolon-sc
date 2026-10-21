// 好感度（GLOSSARY.md「好感度」「冷却」）：她对制作人的态度，用户只看到档位，看不到数值。
// 这里把读到的状态换成页首标识要显示的东西：哪种心形、写什么、读屏读什么；冷却中换成解除时刻。
// 回合或送礼改变了好感度时，她那条回复、那条送礼记录带着这次变化，这里写成胶囊上的那句话。

import { api, json } from "@/lib/api";
import { copy } from "@/i18n";

export const TIERS = ["cold", "distant", "trust", "close", "intimate", "love"] as const;
/** 冷淡、疏远、信任、亲近、亲密、爱意，从低到高。 */
export type Tier = (typeof TIERS)[number];

/** 读状态接口给的：档位，冷却中的解除时刻（ISO，不在冷却是 null）。 */
export type AffinityStatus = { tier: Tier; cooldown_until: string | null };

/** 一次变化：升还是降、变化后的档位、有没有跨档、是不是因此进了冷却，或者（只有送礼会）解除了冷却。
 * `level`/`step` 是判断模型对这一轮的打分和实际变化量，用来在她那条回复下写清原因；礼物没有。 */
export type AffinityChange = {
  up: boolean;
  tier_changed: boolean;
  tier: Tier;
  cooldown: boolean;
  thawed: boolean;
  level?: number | null;
  step?: number | null;
  out_of_line?: boolean;
};

/** 判断模型的分级，从「伤到她」到「让她很高兴」；和她说的话一致（backend `_FEELINGS`）。 */
export function feelingText(change: AffinityChange): string {
  const said = copy.conversation.affinity.feelings;
  if (change.out_of_line) return said.outOfLine;
  const level = change.level;
  if (level === null || level === undefined) return change.up ? said.happier : said.unhappier;
  return said.byLevel[Math.min(Math.max(level, 0), 4)] ?? (change.up ? said.happier : said.unhappier);
}

/** 她那条回复下面那行：「好感 ▲ +1 · 有点开心」这种。没有变化量就只说感受。 */
export function changeNote(change: AffinityChange): string {
  const t = copy.conversation.affinity.note;
  const feeling = feelingText(change);
  const step = change.step;
  if (step === null || step === undefined || step === 0) return feeling;
  const rounded = Math.round(step * 10) / 10;
  return t(rounded > 0 ? `+${rounded}` : `${rounded}`, feeling);
}

/** 胶囊上的那句话：进了或解除了冷却最先说，其次跨档写出新档名，同档只说开心或不高兴。 */
export function cueText(change: AffinityChange, name: string): string {
  const t = copy.conversation.affinity.cues;
  const tier = copy.conversation.affinity.tiers[change.tier];
  if (change.cooldown) return t.cooldown(name);
  if (change.thawed) return t.thawed(name);
  if (change.tier_changed) return change.up ? t.closer(name, tier) : t.further(name, tier);
  return change.up ? t.happier(name) : t.unhappier(name);
}

/** 五档的心形：冷淡 HeartOff、疏远 HeartCrack、信任空心、亲近半心（空心叠一层只露下半的实心）、亲密实心。 */
export type Heart = "off" | "crack" | "outline" | "half" | "filled" | "double";

const HEARTS: Record<Tier, Heart> = {
  cold: "off",
  distant: "crack",
  trust: "outline",
  close: "half",
  intimate: "filled",
  love: "double",
};

export function heartOf(tier: Tier): Heart {
  return HEARTS[tier];
}

/** 页首标识：一档的心形加档名，或者冷却中的沙漏加解除时刻；`label` 是读屏读的名字。 */
export type Badge =
  | { kind: "tier"; tier: Tier; heart: Heart; text: string; label: string }
  | { kind: "cooldown"; text: string; label: string };

export function badgeOf(status: AffinityStatus, name: string, now: Date, timeZone: string): Badge {
  const t = copy.conversation.affinity;
  if (status.cooldown_until !== null) {
    const until = untilText(new Date(status.cooldown_until), now, timeZone);
    return { kind: "cooldown", text: until, label: t.cooling(name) };
  }
  const text = t.tiers[status.tier];
  return { kind: "tier", tier: status.tier, heart: heartOf(status.tier), text, label: t.label(text) };
}

/** 解除时刻按制作人的时区写成「今天 HH:MM」或「明天 HH:MM」（冷却最长 24 小时，不会更远）。 */
export function untilText(until: Date, now: Date, timeZone: string): string {
  const t = copy.conversation.affinity;
  const day = (at: Date) => new Intl.DateTimeFormat("en-CA", { timeZone }).format(at);
  const time = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  }).format(until);
  return `${day(until) === day(now) ? t.today : t.tomorrow} ${time}`;
}

/** 她现在对这位用户的好感度；好感度不可用（没配判断模型或开关关着）时是 null。 */
export async function getAffinity(characterId: string): Promise<AffinityStatus | null> {
  return json(await api(`/api/characters/${encodeURIComponent(characterId)}/affinity`));
}
