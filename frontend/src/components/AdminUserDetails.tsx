"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { AppLink } from "@/components/app/AppLink";
import { FailureNotice } from "@/components/app/FailureNotice";
import { AffinityEditor } from "@/components/app/admin/AffinityEditor";
import { AdminPage } from "@/components/app/admin/AdminPage";
import { AdminTrail } from "@/components/app/admin/AdminTrail";
import { MetadataList, MetadataListItem } from "@/components/app/admin/MetadataList";
import { Timestamp } from "@/components/app/admin/Timestamp";
import { AdminBadge, BanStatus, UserLimits } from "@/components/AdminUserList";
import { BanUserDialog } from "@/components/BanUserDialog";
import { UserLimitsDialog } from "@/components/UserLimitsDialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { copy } from "@/i18n";
import {
  USAGE_RANGES,
  banAction,
  describeCount,
  deleteUser,
  getAdminUser,
  takeOffUserLimits,
  unbanUser,
  usageRows,
  type AdminUserDetails as Details,
  type AdminUserUsage,
  type KeyedCount,
  type UsageRow,
} from "@/lib/adminUsers";
import { toFailure, type Failure } from "@/lib/failure";
import { PROVIDER_LABELS } from "@/lib/signIn";
import { BackIcon } from "@/theme/icons";

const t = copy.admin.users;

/**
 * 一位用户的详情：登录方式（含邮箱和填进管理者名单用的标识）、注册和最后活跃的时间、
 * 单独限额（设定、修改、取消）、封禁的时间和备注、近期用量，以及封禁 / 解除封禁
 * 和删除（二次确认，删完回到列表）。不含任何对话、记忆和制作人档案。
 * 资料是一张 Card 里的两列定义列表，单独限额的按钮在 CardFooter；近期用量一张表；删除单独一张 Card
 * （DESIGN.md「Layout · 后台 · 用户详情」）。打开确认框的入口用 outline，执行的那一下用 destructive。
 */
export function AdminUserDetails({ id }: { id: string }) {
  const [user, setUser] = useState<Details | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [banning, setBanning] = useState(false);
  const [unbanning, setUnbanning] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [editingLimits, setEditingLimits] = useState(false);
  const [takingOffLimits, setTakingOffLimits] = useState(false);
  const router = useRouter();

  useEffect(() => {
    getAdminUser(id).then(setUser, (e) => setFailure(toFailure(e)));
  }, [id]);

  async function unban() {
    setUnbanning(true);
    setFailure(null);
    try {
      setUser(await unbanUser(id));
    } catch (e) {
      setFailure(toFailure(e));
    } finally {
      setUnbanning(false);
    }
  }

  async function takeOffLimits() {
    setTakingOffLimits(true);
    setFailure(null);
    try {
      setUser(await takeOffUserLimits(id));
    } catch (e) {
      setFailure(toFailure(e));
    } finally {
      setTakingOffLimits(false);
    }
  }

  async function remove() {
    setDeleting(true);
    setFailure(null);
    try {
      await deleteUser(id);
      router.replace("/admin/users");
    } catch (e) {
      setFailure(toFailure(e));
      setConfirmingDelete(false);
      setDeleting(false);
    }
  }

  const action = user && banAction(user);
  const back = (
    <AppLink href="/admin/users" isStandalone className="inline-flex w-fit items-center gap-1">
      <BackIcon aria-hidden className="size-4" />
      {t.backToList}
    </AppLink>
  );

  if (!user) {
    // 还没拿到这位用户：没有名字可当标题，只放返回链接和转圈（或失败的原因）。限宽同 AdminPage 的 narrow。
    return (
      <div className="mx-auto flex w-full max-w-240 flex-col gap-6 p-4 md:p-6">
        {back}
        {failure && <FailureNotice failure={failure} />}
        {!failure && (
          <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner />
            {t.loading}
          </p>
        )}
      </div>
    );
  }

  return (
    <AdminPage
      width="narrow"
      above={back}
      title={user.name}
      meta={
        <div className="flex flex-wrap items-center gap-2">
          <BanStatus banned={user.banned} />
          {user.is_admin && <AdminBadge />}
        </div>
      }
      end={
        action === "ban" ? (
          <Button variant="outline" onClick={() => setBanning(true)}>
            {t.ban}
          </Button>
        ) : action === "unban" ? (
          <Button variant="outline" onClick={unban} disabled={unbanning}>
            {unbanning && <Spinner data-icon="inline-start" />}
            {t.unban}
          </Button>
        ) : (
          <p className="text-sm text-muted-foreground">{t.adminCannotBeBanned}</p>
        )
      }
    >
      {failure && <FailureNotice failure={failure} />}

      <Card>
        <CardHeader>
          <CardTitle>{t.details.title}</CardTitle>
        </CardHeader>
        <CardContent>
          <MetadataList>
            <MetadataListItem label={t.details.signInMethods}>
              {user.sign_in_methods.length === 0 ? (
                <span className="text-muted-foreground">{t.noSignInMethods}</span>
              ) : (
                <span className="flex flex-col gap-2">
                  {user.sign_in_methods.map((method) => (
                    <span key={method.provider} className="flex flex-col">
                      <span>
                        {PROVIDER_LABELS[method.provider]} {method.display_name}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {t.details.email}：{method.email ?? t.details.noEmail}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {t.details.identifier}：{method.provider}:{method.subject}
                      </span>
                    </span>
                  ))}
                </span>
              )}
            </MetadataListItem>
            <MetadataListItem label={t.details.id}>
              <code className="font-mono text-xs">{user.id}</code>
            </MetadataListItem>
            <MetadataListItem label={t.details.createdAt}>
              <Timestamp value={user.created_at} />
            </MetadataListItem>
            <MetadataListItem label={t.details.lastActive}>
              {user.last_active_at ? (
                <Timestamp value={user.last_active_at} />
              ) : (
                <span className="text-muted-foreground">{t.never}</span>
              )}
            </MetadataListItem>
            <MetadataListItem label={t.limits.title}>
              <span className="flex flex-col gap-0.5">
                <UserLimits user={user} />
                {user.limits?.note && <span className="text-xs text-muted-foreground">{user.limits.note}</span>}
                {user.limits && (
                  <span className="text-xs text-muted-foreground">
                    {t.limits.updatedAt} <Timestamp value={user.limits.updated_at} />
                  </span>
                )}
              </span>
            </MetadataListItem>
            {user.banned_at && (
              <MetadataListItem label={t.details.bannedAt}>
                <Timestamp value={user.banned_at} />
              </MetadataListItem>
            )}
            {user.banned && (
              <MetadataListItem label={t.details.banNote}>
                {user.ban_note ? (
                  <span>{user.ban_note}</span>
                ) : (
                  <span className="text-muted-foreground">{t.details.noNote}</span>
                )}
              </MetadataListItem>
            )}
          </MetadataList>
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => setEditingLimits(true)}>
            {user.limits ? t.limits.edit : t.limits.set}
          </Button>
          {user.limits && (
            <Button variant="ghost" size="sm" onClick={takeOffLimits} disabled={takingOffLimits}>
              {takingOffLimits && <Spinner data-icon="inline-start" />}
              {t.limits.takeOff}
            </Button>
          )}
        </CardFooter>
      </Card>

      <AffinityEditor userId={user.id} />

      <AdminTrail value={user.name} />
      <RecentUsage usage={user.usage} />

      {action !== null && (
        <Card>
          <CardHeader>
            <CardTitle>{t.delete}</CardTitle>
            <CardDescription>{t.deleteSection.hint}</CardDescription>
            <CardAction>
              <Button variant="outline" onClick={() => setConfirmingDelete(true)}>
                {t.delete}
              </Button>
            </CardAction>
          </CardHeader>
        </Card>
      )}

      <AlertDialog open={confirmingDelete} onOpenChange={(open) => !deleting && setConfirmingDelete(open)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t.deleteDialog.title(user.name)}</AlertDialogTitle>
            <AlertDialogDescription>
              {t.deleteDialog.hint}
              {user.banned ? t.deleteDialog.banned : t.deleteDialog.notBanned}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>{copy.common.cancel}</AlertDialogCancel>
            <AlertDialogAction variant="destructive" disabled={deleting} onClick={remove}>
              {deleting && <Spinner data-icon="inline-start" />}
              {t.deleteDialog.confirm}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <UserLimitsDialog
        user={editingLimits ? user : null}
        onClose={() => setEditingLimits(false)}
        onSaved={(saved) => {
          setEditingLimits(false);
          setUser(saved);
        }}
      />

      <BanUserDialog
        user={banning ? user : null}
        onClose={() => setBanning(false)}
        onBanned={(banned) => {
          setBanning(false);
          setUser(banned);
        }}
      />
    </AdminPage>
  );
}

/**
 * 近期用量：回合、通话、被拒绝各一行，最近 24 小时、7 天、30 天各一列；每格是总数，下面分出部署者的 key
 * 和自带密钥（通话分出计不计入限额）。≥ 1024px 是表格，更窄时换成 divide-y 的行（DESIGN.md「Layout · 后台 · 表格」）。
 */
function RecentUsage({ usage }: { usage: AdminUserUsage }) {
  const u = t.usage;
  const rows = usageRows(usage);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{u.title}</CardTitle>
        <CardDescription>{u.hint}</CardDescription>
      </CardHeader>
      <CardContent>
        <Table className="hidden lg:table">
          <TableHeader>
            <TableRow>
              <TableHead>
                <span className="sr-only">{u.title}</span>
              </TableHead>
              {USAGE_RANGES.map((range) => (
                <TableHead key={range} className="text-right">
                  {u.ranges[range]}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.what}>
                <TableCell className="align-top font-medium">{row.label}</TableCell>
                {USAGE_RANGES.map((range) => (
                  <TableCell key={range} className="text-right align-top">
                    <Count count={row[range]} what={row.what} />
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>

        <ul className="divide-y lg:hidden">
          {rows.map((row) => (
            <li key={row.what} className="flex flex-col gap-0.5 py-3 first:pt-0 last:pb-0">
              <span className="font-medium">{row.label}</span>
              <span className="text-xs text-muted-foreground tabular-nums">
                {USAGE_RANGES.map((range) => `${u.ranges[range]} ${describeCount(row[range], row.what).total}`).join(" · ")}
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function Count({ count, what }: { count: KeyedCount; what: UsageRow["what"] }) {
  const { total, split } = describeCount(count, what);
  return (
    <>
      <span className="block tabular-nums">{total}</span>
      {split && <span className="block text-xs text-muted-foreground tabular-nums">{split}</span>}
    </>
  );
}
