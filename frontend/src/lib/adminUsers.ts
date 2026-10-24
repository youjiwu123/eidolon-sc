// The admin console's users page (ADR-0013): listing and searching users, a user's details,
// banning or unbanning them, setting their own limits, and deleting them. The backend never sends what anyone said,
// what was remembered of them or their producer profile, only who they are and how they
// use it.

import { copy } from "@/i18n";
import { api, expectNoContent, json } from "@/lib/api";
import type { Tier } from "@/lib/affinity";
import { formatCount } from "@/lib/dashboard";
import type { Provider } from "@/lib/signIn";

/** An identity a user signs in with; `subject` is what `EIDOLON_ADMINS` names it by. */
export type AdminSignInMethod = {
  provider: Provider;
  subject: string;
  display_name: string;
  email: string | null;
};

/** A user as the list shows them. `last_active_at` is null for a user signed in nowhere. Whether
 * they brought their own keys the backend never knows: their browsers keep them (ADR-0017). */
export type AdminUser = {
  id: string;
  name: string;
  sign_in_methods: AdminSignInMethod[];
  created_at: string;
  last_active_at: string | null;
  banned: boolean;
  is_admin: boolean;
  limits: AdminUserLimits | null;
  /** The turns and calls taken in the last 24 hours, rejected ones not counted. */
  usage_24h: { turns: number; calls: number };
};

/** How many, on the deployer's keys and on the user's own. */
export type KeyedCount = { deployer: number; own_keys: number };

/** What a user did in one range: the turns (those in calls too) and calls they took, and
 * the turns and calls rejected, which are counted only as rejected. */
export type RangeUsage = { turns: KeyedCount; calls: KeyedCount; rejected: KeyedCount };

export const USAGE_RANGES = ["24h", "7d", "30d"] as const;
export type UsageRange = (typeof USAGE_RANGES)[number];

/** A user's recent usage, from the activity records, for each range. */
export type AdminUserUsage = Record<UsageRange, RangeUsage>;

/** How the list is sorted: newest first, or by the last 24 hours' turns or calls, most first. */
export const USER_SORTS = ["newest", "turns", "calls"] as const;
export type UserSort = (typeof USER_SORTS)[number];

/** A user's own limit an hour: a number (0: on their own keys only), unlimited, or null,
 * the deployment's. */
export type UserLimit = number | "unlimited" | null;

/** The limits an admin set for the user, each apart, with a note for the admins: turns, calls and
 * the photos she sends them (GLOSSARY.md「照片」). */
export type AdminUserLimits = {
  turns_per_hour: UserLimit;
  calls_per_hour: UserLimit;
  photos_per_hour: UserLimit;
  note: string | null;
  updated_at: string;
};

/** What the editor saves: the limits. */
export type UserLimitsInput = Pick<AdminUserLimits, "turns_per_hour" | "calls_per_hour" | "photos_per_hour">;

/** One limit as the editor holds it: which kind, and the number typed for "number". */
/** A limit as the editor holds it: follow the deployment, a number of times (1 and up),
 * none, or only on their own keys (sent as 0). */
export type LimitDraft = { choice: "default" | "number" | "unlimited" | "own_keys"; count: number | null };

/** A user's own page adds when they were banned, the note only admins see, and their
 * recent usage. */
export type AdminUserDetails = AdminUser & {
  banned_at: string | null;
  ban_note: string | null;
  usage: AdminUserUsage;
};

export type AdminUserPage = { users: AdminUser[]; total: number };

export const USERS_PAGE_SIZE = 50;

/** What an admin can do about the user from the users page: ban them, lift their ban, or
 * nothing, for an admin, who cannot be banned in the admin console. */
export function banAction(user: AdminUser): "ban" | "unban" | null {
  if (user.is_admin) return null;
  return user.banned ? "unban" : "ban";
}

/** What a user's own limit is, in words; a number of `photos` is counted in 张. */
export function describeLimit(limit: UserLimit, photos = false): string {
  const t = copy.admin.users.limits;
  if (limit === null) return t.default;
  if (limit === "unlimited") return t.unlimited;
  if (limit === 0) return t.ownKeysOnly;
  return photos ? t.photosPerHour(limit) : t.perHour(limit);
}

/** The user's own limits in a line: turns and calls, and the photos if set apart. */
export function describeLimits(limits: AdminUserLimits): string {
  const t = copy.admin.users.limits;
  const line = t.summary(describeLimit(limits.turns_per_hour), describeLimit(limits.calls_per_hour));
  const photos = limits.photos_per_hour ?? null;
  return photos === null ? line : t.withPhotos(line, describeLimit(photos, true));
}

/** The editor's starting point for a limit the user has. */
export function limitDraft(limit: UserLimit): LimitDraft {
  if (limit === null) return { choice: "default", count: null };
  if (limit === "unlimited") return { choice: "unlimited", count: null };
  if (limit === 0) return { choice: "own_keys", count: null };
  return { choice: "number", count: limit };
}

/** What keeps the limits from being saved: a number chosen but none from 1 up given
 * (`count`), or all following the deployment, which is no limit of the user's own
 * (`needsOne`, put on the turns). */
export type LimitProblem = { field: "turns" | "calls" | "photos"; reason: "count" | "needsOne" };

/** The limits to save from the editor, or every problem that keeps them from being saved. */
export function limitsInput(
  turns: LimitDraft,
  calls: LimitDraft,
  photos: LimitDraft,
): { input: UserLimitsInput } | { problems: LimitProblem[] } {
  const limit = (draft: LimitDraft): UserLimit | undefined => {
    if (draft.choice === "default") return null;
    if (draft.choice === "unlimited") return "unlimited";
    if (draft.choice === "own_keys") return 0;
    return draft.count !== null && draft.count >= 1 ? draft.count : undefined;
  };
  const [turnsPerHour, callsPerHour, photosPerHour] = [limit(turns), limit(calls), limit(photos)];
  const problems: LimitProblem[] = [];
  if (turnsPerHour === null && callsPerHour === null && photosPerHour === null) {
    problems.push({ field: "turns", reason: "needsOne" });
  }
  if (turnsPerHour === undefined) problems.push({ field: "turns", reason: "count" });
  if (callsPerHour === undefined) problems.push({ field: "calls", reason: "count" });
  if (photosPerHour === undefined) problems.push({ field: "photos", reason: "count" });
  if (problems.length > 0 || turnsPerHour === undefined || callsPerHour === undefined || photosPerHour === undefined) {
    return { problems };
  }
  return { input: { turns_per_hour: turnsPerHour, calls_per_hour: callsPerHour, photos_per_hour: photosPerHour } };
}

/** The list's sort once a usage column's header is clicked: by that column, most first (the
 * backend sorts only that way); clicking the header of the column already sorted by takes
 * the sort off, back to newest. */
export function headerSort(sort: UserSort, column: "turns" | "calls"): UserSort {
  return sort === column ? "newest" : column;
}

/** A count of `what` as the usage section shows it: the total, and below it on whose keys, for
 * any at all. Calls are split by whether they counted towards the limit instead: a call on own
 * keys is one that brought every exempting key (GLOSSARY.md「运行记录」), not any own key. */
export function describeCount(count: KeyedCount, what: keyof RangeUsage): { total: string; split: string | null } {
  const total = count.deployer + count.own_keys;
  const usage = copy.admin.users.usage;
  const split = what === "calls" ? usage.splitCalls : usage.split;
  return {
    total: formatCount(total),
    split: total === 0 ? null : split(formatCount(count.deployer), formatCount(count.own_keys)),
  };
}

export type UsageRow = { what: keyof RangeUsage; label: string } & Record<UsageRange, KeyedCount>;

/** The usage section's rows: turns, calls and rejections, each across the ranges. */
export function usageRows(usage: AdminUserUsage): UsageRow[] {
  const labels = copy.admin.users.usage.rows;
  return (["turns", "calls", "rejected"] as const).map((what) => ({
    what,
    label: labels[what],
    "24h": usage["24h"][what],
    "7d": usage["7d"][what],
    "30d": usage["30d"][what],
  }));
}

/** How many users the pages before `page` (counted from 1) hold. */
export function pageOffset(page: number): number {
  return (page - 1) * USERS_PAGE_SIZE;
}

/** A page of the users, or of those `query` finds by name, email or user id, sorted by
 * `sort` before they are cut into pages. */
export async function listAdminUsers(query: string, page: number, sort: UserSort): Promise<AdminUserPage> {
  const params = new URLSearchParams({
    q: query,
    offset: String(pageOffset(page)),
    limit: String(USERS_PAGE_SIZE),
    sort,
  });
  return json(await api(`/api/admin/users?${params}`));
}

export async function getAdminUser(id: string): Promise<AdminUserDetails> {
  return json(await api(`/api/admin/users/${encodeURIComponent(id)}`));
}

/** Ban the user, with a note for the admins; they are signed out everywhere at once. */
export async function banUser(id: string, note: string): Promise<AdminUserDetails> {
  return json(
    await api(`/api/admin/users/${encodeURIComponent(id)}/ban`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ note: note.trim() || null }),
    }),
  );
}

export async function unbanUser(id: string): Promise<AdminUserDetails> {
  return json(await api(`/api/admin/users/${encodeURIComponent(id)}/ban`, { method: "DELETE" }));
}

/** Set the user's own limits, replacing those they had, from their next turn or call on. */
export async function setUserLimits(
  id: string,
  limits: UserLimitsInput,
  note: string,
): Promise<AdminUserDetails> {
  return json(
    await api(`/api/admin/users/${encodeURIComponent(id)}/limits`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...limits, note: note.trim() || null }),
    }),
  );
}

/** Take off the user's own limits; the deployment's hold for them again. */
export async function takeOffUserLimits(id: string): Promise<AdminUserDetails> {
  return json(await api(`/api/admin/users/${encodeURIComponent(id)}/limits`, { method: "DELETE" }));
}

/** Delete the user and all their data for good, as they would themselves; a banned user's
 * identities can never sign in or be linked again. */
export async function deleteUser(id: string): Promise<void> {
  await expectNoContent(await api(`/api/admin/users/${encodeURIComponent(id)}`, { method: "DELETE" }));
}

/** A user's affinity with one character, as the admin console shows and sets it: by value
 * and tier. The user never sees the value. */
export type AdminAffinity = { character_id: string; value: number; tier: Tier };

/** The user's affinity with every character, by value and tier. */
export async function getAdminAffinities(id: string): Promise<AdminAffinity[]> {
  const body = await json<{ affinities: AdminAffinity[] }>(
    await api(`/api/admin/users/${encodeURIComponent(id)}/affinity`),
  );
  return body.affinities;
}

/** Set the user's affinity with the character by hand, out of any cooldown; 0–120. */
export async function setAdminAffinity(
  id: string,
  characterId: string,
  value: number,
): Promise<AdminAffinity> {
  return json<AdminAffinity>(
    await api(`/api/admin/users/${encodeURIComponent(id)}/affinity`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ character_id: characterId, value }),
    }),
  );
}
